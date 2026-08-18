"""视频抽帧与 tracklet 聚合。见 docs/plans/0008。

抽帧只做解码 —— 不 resize、不对齐、不归一化，帧以 JPEG 原样发给 embedding
服务（约束 #3：识别预处理只存在于 embedding 服务）。

tracklet = 单个视频内一段时间连续出现的一张脸。它**不是**「一个人」：
边界在单个视频内、有起止时间，语义等同「一张照片上的一张脸」，
不做跨视频/跨照片的身份关联（约束 #8）。
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from PIL import Image

from gallery_core.embedding_client import DetectedFace
from gallery_core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SampledFrame:
    t_ms: int
    jpeg: bytes


@dataclass(frozen=True, slots=True)
class VideoInfo:
    duration_ms: int
    width: int
    height: int


def probe_duration(path: Path) -> VideoInfo:
    """读视频时长与画幅。打不开/没有视频流会抛异常，调用方按单个资产失败处理。"""
    import av

    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        # container.duration 是 AV_TIME_BASE(µs)；流级 duration 是流 time_base。
        # 两处都可能缺失（少数封装器），都缺就在抽帧时以实际帧时间为准。
        duration_ms = 0
        if container.duration is not None:
            duration_ms = int(container.duration / 1000)
        elif stream.duration is not None and stream.time_base is not None:
            duration_ms = int(stream.duration * stream.time_base * 1000)
        return VideoInfo(
            duration_ms=duration_ms,
            width=stream.codec_context.width,
            height=stream.codec_context.height,
        )


def sample_frames(path: Path, fps: float, max_frames: int) -> Iterator[SampledFrame]:
    """按目标频率抽帧，产出 (毫秒时间戳, JPEG 字节)。

    顺序解码、按展示时间戳（PTS）取样 —— 比 seek 稳（活动视频常见的
    变帧率/长 GOP 下 seek 会落点不准）。解码是 CPU 活，调用方放线程池。
    """
    import av

    interval = Fraction(1000) / Fraction(fps)  # 毫秒
    next_due = Fraction(0)
    emitted = 0

    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        # 只解码不滤镜；多线程解码对长视频有实打实的加速
        stream.thread_type = "AUTO"
        for frame in container.decode(stream):
            if emitted >= max_frames:
                log.warning("video_frames_truncated", max_frames=max_frames)
                break
            if frame.pts is None or frame.time_base is None:
                continue
            t_ms = Fraction(frame.pts) * frame.time_base * 1000
            if t_ms < next_due:
                continue
            next_due = t_ms + interval

            img: Image.Image = frame.to_image()  # type: ignore[no-untyped-call]
            # 手机竖拍的旋转存在容器元数据里，解码帧本身不转正 —— 不处理的话
            # 竖拍视频的人脸全横躺，检测大量漏。frame.rotation 是逆时针度数，
            # PIL rotate 同为逆时针，二者语义一致（有编解码往返测试佐证）。
            if frame.rotation:
                img = img.rotate(frame.rotation, expand=True)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=88)
            yield SampledFrame(t_ms=int(t_ms), jpeg=buf.getvalue())
            emitted += 1


# ------------------------------------------------------------------ tracklet


@dataclass(slots=True)
class Tracklet:
    t_start_ms: int
    t_end_ms: int
    # 成员 embedding 逐元素累加，出口再取均值 + L2 归一化
    _sum: list[float]
    count: int
    # 质量最好的一帧成员：拿来出代表 bbox / det_score / face_px / 人脸小图
    best: DetectedFace
    best_t_ms: int
    last_bbox: tuple[int, int, int, int] = field(default=(0, 0, 0, 0))
    last_seen_ms: int = 0

    def mean_embedding(self) -> list[float]:
        mean = [v / self.count for v in self._sum]
        norm = sum(v * v for v in mean) ** 0.5
        if norm == 0:
            return mean
        return [v / norm for v in mean]


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _cosine(a: list[float], b: list[float]) -> float:
    # embedding 服务出口已 L2 归一化，点积即余弦相似度
    return sum(x * y for x, y in zip(a, b, strict=True))


class TrackletBuilder:
    """逐帧喂入检测、增量串段。

    贪心匹配：对每个检测，在活跃段里找「余弦相似度最高且达标」者并入；
    相似度不够但 IoU 达标也算同一段（侧脸/模糊帧 embedding 会漂，位置兜底）。
    1 fps 下人会移动，所以相似度是主判据、IoU 是辅助 —— 与追踪器的习惯相反，
    这是采样稀疏时的正确取舍。

    做成增量形式是为了让调用方能边喂边释放帧 JPEG：
    只有 `referenced_frames()` 里的帧还会被用到（各段最清晰帧，出人脸小图）。
    """

    def __init__(self, *, gap_ms: int, sim_threshold: float, iou_threshold: float) -> None:
        self._gap_ms = gap_ms
        self._sim = sim_threshold
        self._iou = iou_threshold
        self._active: list[Tracklet] = []
        self._done: list[Tracklet] = []

    def feed(self, t_ms: int, faces: list[DetectedFace]) -> None:
        # 先封掉超时的段，缩小匹配池
        still_active: list[Tracklet] = []
        for tr in self._active:
            if t_ms - tr.last_seen_ms > self._gap_ms:
                self._done.append(tr)
            else:
                still_active.append(tr)
        self._active = still_active

        claimed: set[int] = set()  # 一帧内一个段最多吃一个检测
        for face in faces:
            best_idx = -1
            best_sim = -1.0
            for idx, tr in enumerate(self._active):
                if idx in claimed:
                    continue
                sim = _cosine(face.embedding, tr.mean_embedding())
                matched = sim >= self._sim or _iou(face.bbox, tr.last_bbox) >= self._iou
                if matched and sim > best_sim:
                    best_sim = sim
                    best_idx = idx

            if best_idx >= 0:
                tr = self._active[best_idx]
                claimed.add(best_idx)
                tr.t_end_ms = t_ms
                tr.last_seen_ms = t_ms
                tr.last_bbox = face.bbox
                tr.count += 1
                for i, v in enumerate(face.embedding):
                    tr._sum[i] += v
                # 「最清晰」偏向大脸：face_px 优先，其次 det_score
                if (face.face_px, face.det_score) > (tr.best.face_px, tr.best.det_score):
                    tr.best = face
                    tr.best_t_ms = t_ms
            else:
                self._active.append(
                    Tracklet(
                        t_start_ms=t_ms,
                        t_end_ms=t_ms,
                        _sum=list(face.embedding),
                        count=1,
                        best=face,
                        best_t_ms=t_ms,
                        last_bbox=face.bbox,
                        last_seen_ms=t_ms,
                    )
                )

    def referenced_frames(self) -> set[int]:
        """当前仍会被用到的帧时间戳（各段最清晰帧）。其余帧的 JPEG 可以释放。"""
        return {tr.best_t_ms for tr in self._active} | {tr.best_t_ms for tr in self._done}

    def finalize(self) -> list[Tracklet]:
        self._done.extend(self._active)
        self._active = []
        self._done.sort(key=lambda tr: tr.t_start_ms)
        return self._done


def build_tracklets(
    frames: list[tuple[int, list[DetectedFace]]],
    *,
    gap_ms: int,
    sim_threshold: float,
    iou_threshold: float,
) -> list[Tracklet]:
    """一次性版本（测试与小场景用）。语义同 TrackletBuilder。"""
    builder = TrackletBuilder(
        gap_ms=gap_ms, sim_threshold=sim_threshold, iou_threshold=iou_threshold
    )
    for t_ms, faces in frames:
        builder.feed(t_ms, faces)
    return builder.finalize()
