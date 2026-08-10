"""InsightFace buffalo_l 的封装。

这是全项目**唯一**做人脸检测与 embedding 的地方。`api` 与 `jobs` 都通过 HTTP 调用本服务。

模型许可提醒：InsightFace 的预训练权重仅授权非商业研究用途。见 README「模型许可」。
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from typing import Any

import numpy as np
from insightface.app import FaceAnalysis
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


class FaceExtractor:
    """常驻内存的模型。进程启动时加载一次，绝不按请求加载。"""

    def __init__(
        self,
        model_name: str,
        model_version: str,
        min_det_score: float,
        min_face_px: int,
        num_threads: int,
    ) -> None:
        self.model_name = model_name
        self.model_version = model_version
        self.min_det_score = min_det_score
        self.min_face_px = min_face_px
        self._app: FaceAnalysis | None = None
        self._num_threads = num_threads

    def load(self) -> None:
        # ONNXRuntime 的线程数必须在会话创建前通过环境变量设定
        os.environ.setdefault("OMP_NUM_THREADS", str(self._num_threads))

        app = FaceAnalysis(
            name=self.model_name,
            # 只需要检测 + 识别；不加载年龄/性别/关键点等无关模型，省内存和加载时间
            allowed_modules=["detection", "recognition"],
            providers=["CPUExecutionProvider"],
        )
        # det_size 是检测输入分辨率。640 是 buffalo_l 的标准值；调大能提升小脸检出率
        # 但成本上升 —— 若评估显示漏检主要来自小脸，这是第一个该调的参数（而非阈值）。
        app.prepare(ctx_id=-1, det_size=(640, 640))
        self._app = app
        log.info("model_loaded", model=self.model_name, version=self.model_version)

    @property
    def loaded(self) -> bool:
        return self._app is not None

    def extract(self, image_bytes: bytes) -> ExtractOutcome:
        """同步、CPU 密集。调用方必须把它丢到 threadpool，不能直接在 event loop 里 await。"""
        if self._app is None:
            raise RuntimeError("模型尚未加载")

        rgb, width, height = self._decode(image_bytes)
        # InsightFace 期望 BGR
        faces_raw = self._app.get(rgb[:, :, ::-1])

        kept: list[Face] = []
        discarded = 0
        for f in faces_raw:
            x1, y1, x2, y2 = (int(v) for v in f.bbox)
            w, h = max(0, x2 - x1), max(0, y2 - y1)
            face_px = min(w, h)
            det_score = float(f.det_score)

            if not self._passes_quality_gate(det_score, w, h, face_px):
                discarded += 1
                continue

            # InsightFace 已提供 normed_embedding，这里再显式归一化一次：
            # L2 归一化是全库的不变量，宁可多算一次点积也不要依赖上游实现细节。
            vec = l2_normalize(f.normed_embedding)

            kept.append(
                Face(
                    bbox=(x1, y1, w, h),
                    det_score=det_score,
                    face_px=face_px,
                    embedding=vec.tolist(),
                    landmarks=self._landmarks(f),
                )
            )

        return ExtractOutcome(
            faces=kept, discarded=discarded, image_width=width, image_height=height
        )

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
        aspect = max(w / h, h / w)
        return aspect <= _MAX_ASPECT_RATIO

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

    @staticmethod
    def _landmarks(face: Any) -> dict[str, Any] | None:
        kps = getattr(face, "kps", None)
        if kps is None:
            return None
        return {"kps": [[float(x), float(y)] for x, y in np.asarray(kps).tolist()]}
