"""视频拆条与帧提取：ffprobe 元数据、PySceneDetect 内容感知切分、关键帧、采样帧。

这些函数只读原片文件，绝不改写（media/ 目录对本模块是只读的）。
同步、IO/CPU 密集 —— 调用方（media_ingest）负责丢进线程池。
"""

from __future__ import annotations

import io
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from numpy.typing import NDArray
from PIL import Image

from gallery_core.logging import get_logger

log = get_logger(__name__)

_KEYFRAME_MAX_EDGE = 512
_KEYFRAME_JPEG_QUALITY = 82
# 稳定性估计的采样帧数与缩放边长。帧多更准但更慢，8 帧已能区分手持抖与架机位。
_STABILITY_SAMPLES = 8
_STABILITY_EDGE = 240


class VideoProbeError(RuntimeError):
    """ffprobe 无法解析这个文件。"""


@dataclass(frozen=True, slots=True)
class VideoInfo:
    duration_ms: int
    width: int
    height: int
    fps: float
    codec: str


def probe_video(path: Path) -> VideoInfo:
    """ffprobe 读元数据。"""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,codec_name,avg_frame_rate:format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        # 入参是我们自己拼的固定命令 + 本地文件路径，无 shell 注入面
        out = subprocess.run(  # noqa: S603
            cmd, capture_output=True, timeout=120, check=True
        ).stdout
    except (subprocess.SubprocessError, OSError) as exc:
        raise VideoProbeError(f"ffprobe 失败: {type(exc).__name__}") from exc

    data = json.loads(out)
    streams = data.get("streams") or []
    if not streams:
        raise VideoProbeError("没有视频流")
    stream = streams[0]

    rate = str(stream.get("avg_frame_rate") or "0/1")
    num, _, den = rate.partition("/")
    try:
        fps = float(num) / float(den or "1") if float(den or "1") else 0.0
    except ValueError:
        fps = 0.0

    duration_s = float(data.get("format", {}).get("duration") or 0.0)
    return VideoInfo(
        duration_ms=int(duration_s * 1000),
        width=int(stream.get("width") or 0),
        height=int(stream.get("height") or 0),
        fps=fps,
        codec=str(stream.get("codec_name") or ""),
    )


def detect_scenes(path: Path, min_seconds: float) -> list[tuple[int, int]]:
    """内容感知拆镜头。返回 [(start_ms, end_ms)]；检不出切点时整条视频算一个镜头。"""
    from scenedetect import ContentDetector, SceneManager, open_video

    video = open_video(str(path))
    manager = SceneManager()
    fps = video.frame_rate or 25.0
    manager.add_detector(ContentDetector(min_scene_len=int(min_seconds * fps)))
    manager.detect_scenes(video)
    scene_list = manager.get_scene_list()

    if not scene_list:
        info = probe_video(path)
        return [(0, max(info.duration_ms, 0))]
    return [
        (int(start.get_seconds() * 1000), int(end.get_seconds() * 1000))
        for start, end in scene_list
    ]


def _read_frame_at(cap: cv2.VideoCapture, ms: int) -> NDArray[np.uint8] | None:
    cap.set(cv2.CAP_PROP_POS_MSEC, float(ms))
    ok, frame = cap.read()
    if not ok or frame is None:
        return None
    # OpenCV 是 BGR，统一转 RGB 再离开本模块
    rgb: NDArray[np.uint8] = np.ascontiguousarray(frame[:, :, ::-1])
    return rgb


def _encode_jpeg(rgb: NDArray[np.uint8], max_edge: int, quality: int) -> tuple[bytes, int, int]:
    img = Image.fromarray(rgb, mode="RGB")
    img.thumbnail((max_edge, max_edge))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue(), img.width, img.height


def extract_keyframe(path: Path, ms: int) -> tuple[bytes, int, int] | None:
    """取指定时刻的关键帧，缩到 512 边长的 JPEG。(bytes, w, h)，取不到返回 None。"""
    cap = cv2.VideoCapture(str(path))
    try:
        rgb = _read_frame_at(cap, ms)
    finally:
        cap.release()
    if rgb is None:
        return None
    return _encode_jpeg(rgb, _KEYFRAME_MAX_EDGE, _KEYFRAME_JPEG_QUALITY)


def sample_gray_frames(
    path: Path, start_ms: int, end_ms: int, samples: int = _STABILITY_SAMPLES
) -> list[NDArray[np.float32]]:
    """镜头内等间隔采样，缩小转灰度 —— 稳定性估计的输入。"""
    if end_ms <= start_ms:
        return []
    cap = cv2.VideoCapture(str(path))
    frames: list[NDArray[np.float32]] = []
    try:
        step = (end_ms - start_ms) / (samples + 1)
        for i in range(1, samples + 1):
            rgb = _read_frame_at(cap, int(start_ms + step * i))
            if rgb is None:
                continue
            h, w = rgb.shape[:2]
            scale = _STABILITY_EDGE / max(h, w, 1)
            small = cv2.resize(rgb, (max(int(w * scale), 8), max(int(h * scale), 8)))
            weights = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
            frames.append((small.astype(np.float32) @ weights).astype(np.float32))
    finally:
        cap.release()
    return frames


def image_keyframe_bytes(data: bytes) -> tuple[bytes, int, int, int, int]:
    """照片字节 → 关键帧缩略图。返回 (jpeg, thumb_w, thumb_h, 原宽, 原高)。

    接受字节而不是路径：远端照片不落盘，分析在内存中完成（见 006 迁移）。
    """
    with Image.open(io.BytesIO(data)) as src:
        from PIL import ImageOps

        oriented = ImageOps.exif_transpose(src) or src
        rgb = np.asarray(oriented.convert("RGB"), dtype=np.uint8)
    payload, w, h = _encode_jpeg(rgb, _KEYFRAME_MAX_EDGE, _KEYFRAME_JPEG_QUALITY)
    return payload, w, h, rgb.shape[1], rgb.shape[0]


def image_keyframe(path: Path) -> tuple[bytes, int, int, int, int]:
    """本地照片文件的关键帧（手动预拷入的素材走这条）。"""
    return image_keyframe_bytes(path.read_bytes())
