"""Chinese-CLIP 图文双塔的 ONNX 封装。

这是全项目**唯一**做图文向量化的地方（约束 3 延伸到剪辑域）：`api` 的查询文本与
`jobs` 的关键帧都通过 HTTP 调本模块的端点。预处理（resize/归一化/分词）只在这里
实现一份 —— 在别处重复实现会让离线库和在线查询落在不同的向量空间里。

## 模型文件

`CLIP_MODEL_DIR`（默认 /opt/clip）下需要三个文件：

    image.onnx   图像塔（ViT-B/16，输入 1×3×224×224，输出 512 维）
    text.onnx    文本塔（RoBERTa-wwm-base，输入 token ids [+ attention mask]）
    vocab.txt    BERT 词表（分词用）

导出方法见 docs/media-edit.md「CLIP 模型导出」。文件缺失时服务照常启动，
/healthz 报 clip_loaded=false，CLIP 端点返回 503 —— 人脸功能不受影响。

## 预处理约定（与 Chinese-CLIP 官方一致，改动会让全库向量作废）

图像：RGB → 双三次缩放到 224×224 → [0,1] → 按 CLIP 均值方差归一化。
文本：BERT WordPiece，[CLS] ... [SEP]，最长 52 token，PAD 补零。
出口统一 L2 归一化（约束 4）。
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageOps

from gallery_core.logging import get_logger
from gallery_core.vector import l2_normalize

log = get_logger(__name__)

_IMAGE_SIZE = 224
_TEXT_MAX_LEN = 52
# CLIP 系模型的标准归一化参数
_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
_STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)


@dataclass(frozen=True, slots=True)
class ClipImageOutcome:
    embedding: list[float] | None
    error: str | None = None


class ClipEncoder:
    """常驻内存的图文双塔。进程启动时加载一次；文件缺失时优雅降级为未加载。"""

    def __init__(
        self,
        model_dir: str,
        model_name: str,
        model_version: str,
        num_threads: int = 4,
        use_gpu: bool = False,
    ) -> None:
        self.model_name = model_name
        self.model_version = model_version
        self._model_dir = Path(model_dir)
        self._num_threads = num_threads
        self._use_gpu = use_gpu
        self._image_session: Any = None
        self._text_session: Any = None
        self._tokenizer: Any = None

    # ------------------------------------------------------------------ 加载

    def load(self) -> None:
        """尽力加载。任何文件缺失都不抛异常 —— CLIP 是剪辑域的能力，
        不能因为它没配好而拖垮人脸检索服务。"""
        image_path = self._model_dir / "image.onnx"
        text_path = self._model_dir / "text.onnx"
        vocab_path = self._model_dir / "vocab.txt"
        missing = [p.name for p in (image_path, text_path, vocab_path) if not p.exists()]
        if missing:
            log.warning(
                "clip_model_missing",
                dir=str(self._model_dir),
                missing=missing,
                detail="CLIP 端点将返回 503；人脸功能不受影响",
            )
            return

        import onnxruntime as ort
        from tokenizers import BertWordPieceTokenizer

        providers = (
            ["CUDAExecutionProvider", "CPUExecutionProvider"]
            if self._use_gpu
            else ["CPUExecutionProvider"]
        )
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = self._num_threads

        self._image_session = ort.InferenceSession(
            str(image_path), sess_options=opts, providers=providers
        )
        self._text_session = ort.InferenceSession(
            str(text_path), sess_options=opts, providers=providers
        )
        self._tokenizer = BertWordPieceTokenizer(str(vocab_path), lowercase=True)
        log.info(
            "clip_loaded",
            model=self.model_name,
            version=self.model_version,
            gpu=self._use_gpu,
        )

    @property
    def loaded(self) -> bool:
        return self._image_session is not None and self._text_session is not None

    # ------------------------------------------------------------------ 文本

    def encode_texts(self, texts: list[str]) -> list[list[float]]:
        """同步、CPU/GPU 密集，调用方必须丢进线程池。出口已 L2 归一化。"""
        if self._text_session is None or self._tokenizer is None:
            raise RuntimeError("CLIP 文本塔尚未加载")

        ids_batch = np.zeros((len(texts), _TEXT_MAX_LEN), dtype=np.int64)
        mask_batch = np.zeros((len(texts), _TEXT_MAX_LEN), dtype=np.int64)
        for row, txt in enumerate(texts):
            encoded = self._tokenizer.encode(txt)
            ids = encoded.ids[:_TEXT_MAX_LEN]
            ids_batch[row, : len(ids)] = ids
            mask_batch[row, : len(ids)] = 1

        feats = self._run_text(ids_batch, mask_batch)
        return [l2_normalize(row).tolist() for row in np.atleast_2d(feats)]

    def _run_text(self, ids: NDArray[np.int64], mask: NDArray[np.int64]) -> NDArray[np.float32]:
        """按导出图的输入个数喂参：1 个输入 = 只要 ids；2 个 = ids + attention mask。

        不假设导出方式 —— 不同工具链导出的 Chinese-CLIP 文本塔输入签名不一致，
        按名字硬编码会在换一份导出文件时静默出错。
        """
        assert self._text_session is not None
        inputs = self._text_session.get_inputs()
        feed: dict[str, NDArray[np.int64]] = {inputs[0].name: ids}
        if len(inputs) >= 2:
            feed[inputs[1].name] = mask
        out = self._text_session.run(None, feed)[0]
        return np.asarray(out, dtype=np.float32)

    # ------------------------------------------------------------------ 图像

    def encode_images(self, images: list[bytes]) -> list[ClipImageOutcome]:
        """一批图片。返回值与入参一一对应；单张解码失败不影响同批其他图片。"""
        if self._image_session is None:
            raise RuntimeError("CLIP 图像塔尚未加载")

        tensors: list[NDArray[np.float32] | None] = []
        errors: list[str | None] = []
        for payload in images:
            try:
                tensors.append(self._preprocess(payload))
                errors.append(None)
            except Exception as exc:
                tensors.append(None)
                errors.append(f"decode_failed:{type(exc).__name__}")

        valid = [t for t in tensors if t is not None]
        feats_iter = iter(self._run_image(valid)) if valid else iter([])

        outcomes: list[ClipImageOutcome] = []
        for tensor, error in zip(tensors, errors, strict=True):
            if tensor is None:
                outcomes.append(ClipImageOutcome(embedding=None, error=error))
            else:
                outcomes.append(ClipImageOutcome(embedding=next(feats_iter), error=None))
        return outcomes

    def _run_image(self, tensors: list[NDArray[np.float32]]) -> list[list[float]]:
        assert self._image_session is not None
        batch = np.stack(tensors, axis=0)
        input_name = self._image_session.get_inputs()[0].name
        try:
            out = self._image_session.run(None, {input_name: batch})[0]
        except Exception:
            # 图像塔可能导出成固定 batch=1 —— 退化为逐张前向，慢但正确
            rows = [self._image_session.run(None, {input_name: t[None, ...]})[0] for t in tensors]
            out = np.concatenate(rows, axis=0)
        feats = np.atleast_2d(np.asarray(out, dtype=np.float32))
        return [l2_normalize(row).tolist() for row in feats]

    @staticmethod
    def _preprocess(image_bytes: bytes) -> NDArray[np.float32]:
        with Image.open(io.BytesIO(image_bytes)) as src:
            oriented: Image.Image = ImageOps.exif_transpose(src) or src
            resized = oriented.convert("RGB").resize(
                (_IMAGE_SIZE, _IMAGE_SIZE), Image.Resampling.BICUBIC
            )
            arr = np.asarray(resized, dtype=np.float32) / 255.0
        arr = (arr - _MEAN) / _STD
        # HWC → CHW
        return np.ascontiguousarray(arr.transpose(2, 0, 1))
