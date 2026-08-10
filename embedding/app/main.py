"""embedding 服务。只有 /extract 与健康检查。"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel

from embedding.app.model import FaceExtractor
from gallery_core.logging import configure_logging, get_logger

log = get_logger(__name__)

MODEL_NAME = os.getenv("MODEL_NAME", "buffalo_l")
MODEL_VERSION = os.getenv("MODEL_VERSION", "1")
MIN_DET_SCORE = float(os.getenv("MIN_DET_SCORE", "0.5"))
MIN_FACE_PX = int(os.getenv("MIN_FACE_PX", "40"))
ORT_NUM_THREADS = int(os.getenv("ORT_NUM_THREADS", "4"))
MAX_IMAGE_BYTES = int(os.getenv("MAX_IMAGE_BYTES", str(40 * 1024 * 1024)))

_extractor = FaceExtractor(
    model_name=MODEL_NAME,
    model_version=MODEL_VERSION,
    min_det_score=MIN_DET_SCORE,
    min_face_px=MIN_FACE_PX,
    num_threads=ORT_NUM_THREADS,
)

# 推理是同步 CPU 密集调用。并发度限制在线程数附近：
# 超出只会加剧 CPU 争抢，让所有请求一起变慢，不如排队。
_inference_slots = asyncio.Semaphore(ORT_NUM_THREADS)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    configure_logging(os.getenv("LOG_LEVEL", "INFO"))
    # 阻塞加载放到线程里，避免拖住 event loop 的启动
    await asyncio.to_thread(_extractor.load)
    yield


app = FastAPI(title="face-embedding", version="0.1.0", lifespan=lifespan)


class FaceOut(BaseModel):
    bbox: list[int]
    det_score: float
    face_px: int
    embedding: list[float]
    landmarks: dict[str, object] | None = None


class ExtractOut(BaseModel):
    faces: list[FaceOut]
    # 通过检测但被质量门控丢弃的数量。调用方会把它记进 photo.faces_discarded，
    # 用于「漏检归因」——判断召回不足是小脸被丢，还是相似度不够。
    discarded: int
    image_width: int
    image_height: int
    model_name: str
    model_version: str
    latency_ms: int

    model_config = {"protected_namespaces": ()}


@app.get("/healthz")
async def healthz() -> dict[str, object]:
    return {
        "status": "ok",
        "model_loaded": _extractor.loaded,
        "model_name": MODEL_NAME,
        "model_version": MODEL_VERSION,
    }


@app.post("/extract", response_model=ExtractOut)
async def extract(image: UploadFile = File(...)) -> ExtractOut:  # noqa: B008
    if not _extractor.loaded:
        raise HTTPException(status_code=503, detail="模型尚未加载完成")

    payload = await image.read()
    if not payload:
        raise HTTPException(status_code=400, detail="空文件")
    if len(payload) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="图片过大")

    started = time.perf_counter()
    async with _inference_slots:
        try:
            # to_thread 是关键：ONNXRuntime 推理是同步阻塞的，直接在 event loop 里跑
            # 会让整个服务在并发下卡死。
            outcome = await asyncio.to_thread(_extractor.extract, payload)
        except Exception as exc:
            log.warning("extract_failed", error_type=type(exc).__name__)
            raise HTTPException(status_code=422, detail="图片无法解析") from exc
    latency_ms = int((time.perf_counter() - started) * 1000)

    # 只记计数与耗时，绝不记向量或图片
    log.info(
        "extract_done",
        faces=len(outcome.faces),
        discarded=outcome.discarded,
        latency_ms=latency_ms,
    )

    return ExtractOut(
        faces=[
            FaceOut(
                bbox=list(f.bbox),
                det_score=f.det_score,
                face_px=f.face_px,
                embedding=f.embedding,
                landmarks=f.landmarks,
            )
            for f in outcome.faces
        ],
        discarded=outcome.discarded,
        image_width=outcome.image_width,
        image_height=outcome.image_height,
        model_name=MODEL_NAME,
        model_version=MODEL_VERSION,
        latency_ms=latency_ms,
    )
