"""InsightFace buffalo_l 的封装，支持单张与批量两种模式。

这是全项目**唯一**做人脸检测与 embedding 的地方。`api` 与 `jobs` 都通过 HTTP 调用本服务。

## 批量为什么这么切分

一次推理有两段：检测（SCRFD det_10g）和识别（ArcFace w600k_r50）。

- **识别做批量**：ArcFace 对每一张脸各跑一次前向。跑团合影动辄十几个人，
  识别的总计算量因此远超检测。把一整批照片里所有对齐后的人脸拼成一个 batch
  丢进一次前向，是 GPU 利用率提升最大的一处，也是本模块的核心。
- **检测不做批量**：SCRFD 的前后处理（尺度归一、anchor 解码、NMS）都在
  insightface 内部按单图实现。要批量就得把这段重写一遍 —— 那是在服务里复制一份
  预处理逻辑，跨 insightface 版本极易出错，收益又小于识别侧。
  改为多线程并发跑检测：ORT 的 session.run 会释放 GIL，能吃到多核/GPU 队列。

模型许可提醒：InsightFace 的预训练权重仅授权非商业研究用途。见 README「模型许可」。
"""

from __future__ import annotations

import io
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import numpy as np
from insightface.app import FaceAnalysis
from insightface.utils import face_align
from numpy.typing import NDArray
from PIL import Image, ImageOps

from gallery_core.logging import get_logger
from gallery_core.vector import l2_normalize

log = get_logger(__name__)

# 解压炸弹防护：一张几十 KB 的 PNG 可以解出几十 GB 的像素。
# 4 亿像素足够覆盖任何真实相机（含 100MP 手机），远低于打爆内存的量级。
Image.MAX_IMAGE_PIXELS = 400_000_000

# 误检的典型特征是极端长宽比的框
_MAX_ASPECT_RATIO = 2.5

# ArcFace 的输入尺寸，对齐裁剪后固定 112×112
_REC_IMAGE_SIZE = 112


@dataclass(frozen=True, slots=True)
class Face:
    bbox: tuple[int, int, int, int]
    det_score: float
    face_px: int
    embedding: list[float]
    landmarks: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class ExtractOutcome:
    faces: list[Face]
    discarded: int
    image_width: int
    image_height: int
    # 解码或检测失败时给出原因，调用方据此把该张标成 failed 而不是「没有脸」
    error: str | None = None


@dataclass(slots=True)
class _Detection:
    """一张待识别的人脸：已通过质量门控、已对齐，等着进批量识别。"""

    image_index: int
    bbox: tuple[int, int, int, int]
    det_score: float
    face_px: int
    kps: NDArray[np.float32] | None
    crop: NDArray[np.uint8]


def _most_prominent(detections: list[_Detection]) -> _Detection:
    """挑出「最明显」的那张脸。

    判据是人脸框面积，det_score 作为并列时的次序 —— 自拍里本人的脸几乎总是画面中
    最大的那张，面积是最稳也最好解释的信号。刻意不引入更复杂的打分（清晰度、
    居中程度）：这个选择直接决定查询结果，规则越简单越容易在出问题时说清原因。
    """
    return max(detections, key=lambda d: (d.bbox[2] * d.bbox[3], d.det_score))


class FaceExtractor:
    """常驻内存的模型。进程启动时加载一次，绝不按请求加载。"""

    def __init__(
        self,
        model_name: str,
        model_version: str,
        min_det_score: float,
        min_face_px: int,
        num_threads: int,
        use_gpu: bool = False,
        rec_batch_size: int = 64,
    ) -> None:
        self.model_name = model_name
        self.model_version = model_version
        self.min_det_score = min_det_score
        self.min_face_px = min_face_px
        self.rec_batch_size = rec_batch_size
        self._num_threads = num_threads
        self._use_gpu = use_gpu
        self._app: FaceAnalysis | None = None
        self._rec_model: Any = None
        # 识别模型的 ONNX 图是否接受可变 batch。启动时探测，不假设。
        self._rec_batch_ok = False
        self._pool: ThreadPoolExecutor | None = None

    # ------------------------------------------------------------------ 加载

    def load(self) -> None:
        providers = (
            ["CUDAExecutionProvider", "CPUExecutionProvider"]
            if self._use_gpu
            else ["CPUExecutionProvider"]
        )
        app = FaceAnalysis(
            name=self.model_name,
            # ⚠️ insightface 不认任何环境变量，模型目录只能靠这个参数传。
            # 必须与 Dockerfile.embedding 下载权重时用的 root 完全一致，否则
            # 运行时（appuser 身份）会去 ~/.insightface 找 → 找不到 → 联网重下，
            # 恰好破坏「权重打进镜像、冷启动不依赖外网」的设计。
            root=os.environ.get("INSIGHTFACE_HOME", "~/.insightface"),
            # 只要检测 + 识别；不加载年龄/性别等无关模型，省内存和加载时间
            allowed_modules=["detection", "recognition"],
            providers=providers,
        )
        # ctx_id >= 0 表示用 GPU，-1 表示 CPU。
        # det_size 是检测输入分辨率；调大能提升小脸检出率但成本上升 ——
        # 若评估显示漏检主要来自小脸，这是第一个该调的参数（而不是相似度阈值）。
        app.prepare(ctx_id=0 if self._use_gpu else -1, det_size=(640, 640))

        actual_providers = list(getattr(app.det_model.session, "get_providers", list)())
        if self._use_gpu and "CUDAExecutionProvider" not in actual_providers:
            # 宁可启动失败也不静默降级。onnxruntime 在 CUDA provider 加载失败时
            # 只打一条日志就退回 CPU 继续跑，容器照样 healthy —— 那种「一切正常、
            # 只是慢十倍」的状态比崩溃难发现得多（首次上线时 GPU 机就这样跑了
            # 十小时 CPU 推理）。这里抛错让健康检查失败，部署会自动回滚并留下明确记录。
            raise RuntimeError(
                f"EMBEDDING_USE_GPU=true 但 CUDA provider 未生效（实际: {actual_providers}）。"
                "排查顺序：①容器启动日志里 onnxruntime 的报错，缺库会写明少哪个 .so；"
                "②镜像是否以 EMBEDDING_GPU=true 构建；"
                "③nvidia-smi 驱动版本是否满足 CUDA 13（≥580）；"
                "④compose 是否叠加了 gpu.yml（容器要挂到卡）。"
            )

        self._app = app
        self._rec_model = app.models.get("recognition")
        self._rec_batch_ok = self._probe_rec_batch()
        self._pool = ThreadPoolExecutor(max_workers=self._num_threads, thread_name_prefix="detect")

        log.info(
            "model_loaded",
            model=self.model_name,
            version=self.model_version,
            gpu=self._use_gpu,
            actual_providers=actual_providers,
            rec_batch=self._rec_batch_ok,
        )

    def _probe_rec_batch(self) -> bool:
        """探测识别模型是否支持可变 batch。

        buffalo_l 的 w600k_r50.onnx 通常导出为动态 batch，但这不是保证 ——
        如果导出时被固定成 1，批量前向会直接报形状错误。宁可探测一次，
        也不要在跑到第 3000 张照片时才炸。
        """
        session = getattr(self._rec_model, "session", None)
        if session is None:
            return False
        try:
            batch_dim = session.get_inputs()[0].shape[0]
        except (AttributeError, IndexError):
            return False
        # 动态维度在 ORT 里表现为字符串（如 'batch'）、None 或 -1
        dynamic = isinstance(batch_dim, str) or batch_dim is None or batch_dim in (-1, 0)
        if not dynamic:
            log.warning(
                "rec_batch_unsupported",
                batch_dim=str(batch_dim),
                detail="识别模型 batch 维固定，退化为逐张前向",
            )
        return dynamic

    @property
    def loaded(self) -> bool:
        return self._app is not None

    @property
    def batch_supported(self) -> bool:
        return self._rec_batch_ok

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False)
            self._pool = None

    # ------------------------------------------------------------------ 推理

    def extract(self, image_bytes: bytes, primary_only: bool = False) -> ExtractOutcome:
        """单张。同步、CPU/GPU 密集，调用方必须丢到 threadpool。"""
        return self.extract_batch([image_bytes], primary_only=primary_only)[0]

    def extract_batch(
        self, images: list[bytes], primary_only: bool = False
    ) -> list[ExtractOutcome]:
        """一批图片。返回值与入参一一对应、顺序一致。

        `primary_only=True` 时每张图只保留**最明显的一张脸**，用于查询自拍：
        用户要找的是自己，背景里的路人不该参与匹配。筛选发生在识别前向之前，
        所以其余人脸根本不会被向量化 —— 既省算力，也让离开本服务的人脸数据最少化。

        单张失败（解码错误、损坏文件）不会影响同批其他图片 —— 该位置返回带 error
        的空结果，调用方据此把那一张标成 failed。
        """
        if self._app is None or self._pool is None:
            raise RuntimeError("模型尚未加载")
        if not images:
            return []

        # ① 解码 + 检测，多线程并发。ORT 的 session.run 会释放 GIL。
        # map 保证返回顺序与入参一致，image_index 才能这样赋值。
        per_image = list(self._pool.map(self._decode_and_detect, images))

        # ② 把整批的人脸裁剪拼成一个大 batch，做识别前向
        detections: list[_Detection] = []
        for index, (_, dets) in enumerate(per_image):
            kept = [_most_prominent(dets)] if primary_only and dets else dets
            for det in kept:
                det.image_index = index
                detections.append(det)
        embeddings = self._embed_crops([d.crop for d in detections])

        # ③ 按 image_index 散回各自的图片
        faces_by_image: dict[int, list[Face]] = {}
        for det, vec in zip(detections, embeddings, strict=True):
            faces_by_image.setdefault(det.image_index, []).append(
                Face(
                    bbox=det.bbox,
                    det_score=det.det_score,
                    face_px=det.face_px,
                    embedding=vec,
                    landmarks=None
                    if det.kps is None
                    else {"kps": [[float(x), float(y)] for x, y in det.kps.tolist()]},
                )
            )

        outcomes: list[ExtractOutcome] = []
        for index, (meta, _) in enumerate(per_image):
            outcomes.append(
                ExtractOutcome(
                    faces=faces_by_image.get(index, []),
                    discarded=meta.discarded,
                    image_width=meta.width,
                    image_height=meta.height,
                    error=meta.error,
                )
            )
        return outcomes

    # ------------------------------------------------------------- 内部实现

    @dataclass(slots=True)
    class _ImageMeta:
        width: int
        height: int
        discarded: int
        error: str | None

    def _decode_and_detect(
        self, image_bytes: bytes
    ) -> tuple[FaceExtractor._ImageMeta, list[_Detection]]:
        # 这个方法在线程池里跑，所以不能抛异常出去 —— 否则整批一起失败
        try:
            rgb, width, height = self._decode(image_bytes)
        except Exception as exc:
            return self._ImageMeta(0, 0, 0, f"decode_failed:{type(exc).__name__}"), []

        try:
            bgr = rgb[:, :, ::-1]
            assert self._app is not None
            bboxes, kpss = self._app.det_model.detect(bgr, max_num=0, metric="default")
        except Exception as exc:
            return (
                self._ImageMeta(width, height, 0, f"detect_failed:{type(exc).__name__}"),
                [],
            )

        detections: list[_Detection] = []
        discarded = 0
        # image_index 在返回后由 extract_batch 覆写；这里先占位
        for i in range(bboxes.shape[0] if bboxes is not None else 0):
            x1, y1, x2, y2 = (int(v) for v in bboxes[i][:4])
            det_score = float(bboxes[i][4])
            w, h = max(0, x2 - x1), max(0, y2 - y1)
            face_px = min(w, h)

            if not self._passes_quality_gate(det_score, w, h, face_px):
                discarded += 1
                continue

            kps = None if kpss is None else np.asarray(kpss[i], dtype=np.float32)
            if kps is None:
                # 没有关键点就无法做仿射对齐。未对齐的裁剪送进 ArcFace 会得到
                # 明显偏移的向量，宁可丢掉也不要污染检索库。
                discarded += 1
                continue

            detections.append(
                _Detection(
                    image_index=-1,
                    bbox=(x1, y1, w, h),
                    det_score=det_score,
                    face_px=face_px,
                    kps=kps,
                    crop=face_align.norm_crop(bgr, landmark=kps, image_size=_REC_IMAGE_SIZE),
                )
            )

        return self._ImageMeta(width, height, discarded, None), detections

    def _embed_crops(self, crops: list[NDArray[np.uint8]]) -> list[list[float]]:
        """对齐后的人脸批量过 ArcFace。这是整个批量化的收益所在。"""
        if not crops:
            return []

        chunk = self.rec_batch_size if self._rec_batch_ok else 1
        vectors: list[list[float]] = []
        for start in range(0, len(crops), chunk):
            batch = crops[start : start + chunk]
            # get_feat 接受列表并用 blobFromImages 拼 batch，内部只跑一次 session.run
            feats = self._rec_model.get_feat(batch)
            feats = np.atleast_2d(np.asarray(feats, dtype=np.float32))
            # L2 归一化是全库不变量，在这里统一做一次，下游不再动。
            vectors.extend(l2_normalize(row).tolist() for row in feats)
        return vectors

    def _passes_quality_gate(self, det_score: float, w: int, h: int, face_px: int) -> bool:
        """入库前就丢掉不可靠的人脸，比在检索阶段调阈值有效得多。

        小于 min_face_px 的人脸 embedding 噪声大，既召回不了本人、又是误报的主要来源。
        """
        if det_score < self.min_det_score:
            return False
        if face_px < self.min_face_px:
            return False
        if h == 0 or w == 0:
            return False
        return max(w / h, h / w) <= _MAX_ASPECT_RATIO

    @staticmethod
    def _decode(image_bytes: bytes) -> tuple[NDArray[np.uint8], int, int]:
        with Image.open(io.BytesIO(image_bytes)) as src:
            # EXIF 方向必须先矫正 —— 手机自拍常带 Orientation 标记，
            # 不处理会导致人脸横躺着，检测直接失败。
            # exif_transpose 在没有方向标记时返回 None，此时用原图。
            oriented: Image.Image = ImageOps.exif_transpose(src) or src
            # convert 会丢掉全部 EXIF（含 GPS）。自拍的元数据不该进入后续任何环节。
            arr = np.asarray(oriented.convert("RGB"), dtype=np.uint8)
        return arr, arr.shape[1], arr.shape[0]
