"""embedding 服务。/extract（单张，在线检索用）与 /extract/batch（批量，离线建库用）。"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from embedding.app.clip import ClipEncoder
from embedding.app.model import ExtractOutcome, FaceExtractor
from gallery_core.logging import configure_logging, get_logger

log = get_logger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


MODEL_NAME = os.getenv("MODEL_NAME", "buffalo_l")
MODEL_VERSION = os.getenv("MODEL_VERSION", "1")
MIN_DET_SCORE = float(os.getenv("MIN_DET_SCORE", "0.5"))
MIN_FACE_PX = int(os.getenv("MIN_FACE_PX", "40"))
ORT_NUM_THREADS = int(os.getenv("ORT_NUM_THREADS", "4"))
USE_GPU = _env_bool("EMBEDDING_USE_GPU")
REC_BATCH_SIZE = int(os.getenv("REC_BATCH_SIZE", "64"))
MAX_IMAGE_BYTES = int(os.getenv("MAX_IMAGE_BYTES", str(40 * 1024 * 1024)))
# 一次 /extract/batch 最多几张图。上限存在的意义是给内存设一个天花板：
# 每张图解码后是 W×H×3 字节，32 张 4000×3000 的图就是 ~1.1GB。
MAX_BATCH_IMAGES = int(os.getenv("MAX_BATCH_IMAGES", "32"))

# CLIP 图文双塔（剪辑域）。模型文件缺失时优雅降级：/healthz 报 clip_loaded=false，
# CLIP 端点返回 503，人脸功能不受影响。
CLIP_MODEL_DIR = os.getenv("CLIP_MODEL_DIR", "/opt/clip")
CLIP_MODEL_NAME = os.getenv("CLIP_MODEL_NAME", "chinese-clip-vit-b-16")
CLIP_MODEL_VERSION = os.getenv("CLIP_MODEL_VERSION", "1")
CLIP_MAX_TEXTS = int(os.getenv("CLIP_MAX_TEXTS", "64"))

_extractor = FaceExtractor(
    model_name=MODEL_NAME,
    model_version=MODEL_VERSION,
    min_det_score=MIN_DET_SCORE,
    min_face_px=MIN_FACE_PX,
    num_threads=ORT_NUM_THREADS,
    use_gpu=USE_GPU,
    rec_batch_size=REC_BATCH_SIZE,
)

_clip = ClipEncoder(
    model_dir=CLIP_MODEL_DIR,
    model_name=CLIP_MODEL_NAME,
    model_version=CLIP_MODEL_VERSION,
    num_threads=ORT_NUM_THREADS,
    use_gpu=USE_GPU,
)

# 推理是同步的密集调用。同时在跑的推理请求数限制在线程数附近：
# 超出只会加剧争抢，让所有请求一起变慢，不如排队。
_inference_slots = asyncio.Semaphore(max(1, ORT_NUM_THREADS))


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    configure_logging(os.getenv("LOG_LEVEL", "INFO"))
    # 阻塞加载放到线程里，避免拖住 event loop 的启动
    await asyncio.to_thread(_extractor.load)
    await asyncio.to_thread(_clip.load)
    try:
        yield
    finally:
        _extractor.close()


app = FastAPI(title="face-embedding", version="0.1.0", lifespan=lifespan)


class FaceOut(BaseModel):
    bbox: list[int]
    det_score: float
    face_px: int
    embedding: list[float]
    landmarks: dict[str, object] | None = None


class ImageResult(BaseModel):
    faces: list[FaceOut]
    # 通过检测但被质量门控丢弃的数量。调用方会把它记进 photo.faces_discarded，
    # 用于「漏检归因」——判断召回不足是小脸被丢，还是相似度不够。
    discarded: int
    image_width: int
    image_height: int
    # 非 None 表示这一张失败了（解码/检测出错），而不是「没有检测到人脸」。
    # 两者对调用方意义完全不同：前者要标 failed 并重试，后者是正常结果。
    error: str | None = None


class ExtractOut(ImageResult):
    model_name: str
    model_version: str
    latency_ms: int

    model_config = {"protected_namespaces": ()}


class BatchExtractOut(BaseModel):
    # 与入参图片一一对应、顺序一致
    results: list[ImageResult]
    model_name: str
    model_version: str
    latency_ms: int
    faces_total: int
    # 本次识别前向是否真的走了批量。false 说明模型的 batch 维被固定成 1，
    # 退化成逐张前向 —— GPU 利用率上不去，值得排查。
    batched: bool

    model_config = {"protected_namespaces": ()}


@app.get("/healthz")
async def healthz() -> dict[str, object]:
    return {
        "status": "ok",
        "model_loaded": _extractor.loaded,
        "model_name": MODEL_NAME,
        "model_version": MODEL_VERSION,
        "gpu": USE_GPU,
        "batch_supported": _extractor.batch_supported,
        "max_batch_images": MAX_BATCH_IMAGES,
        "clip_loaded": _clip.loaded,
        "clip_model_name": CLIP_MODEL_NAME,
        "clip_model_version": CLIP_MODEL_VERSION,
    }


def _to_image_result(outcome: ExtractOutcome) -> ImageResult:
    return ImageResult(
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
        error=outcome.error,
    )


async def _read_upload(upload: UploadFile) -> bytes:
    payload = await upload.read()
    if not payload:
        raise HTTPException(status_code=400, detail="空文件")
    if len(payload) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="图片过大")
    return payload


@app.post("/extract", response_model=ExtractOut)
async def extract(
    image: UploadFile = File(...),  # noqa: B008
    # 只保留最明显的一张脸。查询自拍走这个：用户要找的是自己，
    # 背景里的路人不该参与匹配，也不该被向量化。
    primary_only: bool = Form(default=False),
) -> ExtractOut:
    """单张。在线检索走这个 —— 延迟优先。"""
    if not _extractor.loaded:
        raise HTTPException(status_code=503, detail="模型尚未加载完成")

    payload = await _read_upload(image)

    started = time.perf_counter()
    async with _inference_slots:
        # to_thread 是关键：推理是同步阻塞的，直接在 event loop 里跑
        # 会让整个服务在并发下卡死。
        outcome = await asyncio.to_thread(_extractor.extract, payload, primary_only)
    latency_ms = int((time.perf_counter() - started) * 1000)

    if outcome.error is not None:
        raise HTTPException(status_code=422, detail="图片无法解析")

    # 只记计数与耗时，绝不记向量或图片
    log.info(
        "extract_done",
        faces=len(outcome.faces),
        discarded=outcome.discarded,
        primary_only=primary_only,
        latency_ms=latency_ms,
    )

    base = _to_image_result(outcome)
    return ExtractOut(
        **base.model_dump(),
        model_name=MODEL_NAME,
        model_version=MODEL_VERSION,
        latency_ms=latency_ms,
    )


@app.post("/extract/batch", response_model=BatchExtractOut)
async def extract_batch(images: list[UploadFile] = File(...)) -> BatchExtractOut:  # noqa: B008
    """批量。离线建库走这个 —— 吞吐优先。

    整批照片里所有对齐后的人脸会拼成一个 batch 做一次识别前向，这是 GPU 利用率
    提升最大的一处。单张解码失败不影响同批其他图片，对应位置的 error 非空。
    """
    if not _extractor.loaded:
        raise HTTPException(status_code=503, detail="模型尚未加载完成")
    if not images:
        raise HTTPException(status_code=400, detail="没有收到图片")
    if len(images) > MAX_BATCH_IMAGES:
        raise HTTPException(
            status_code=413,
            detail=f"单次批量最多 {MAX_BATCH_IMAGES} 张，收到 {len(images)} 张",
        )

    payloads = [await _read_upload(image) for image in images]

    started = time.perf_counter()
    async with _inference_slots:
        outcomes = await asyncio.to_thread(_extractor.extract_batch, payloads)
    latency_ms = int((time.perf_counter() - started) * 1000)

    results = [_to_image_result(o) for o in outcomes]
    faces_total = sum(len(r.faces) for r in results)

    log.info(
        "extract_batch_done",
        images=len(results),
        faces=faces_total,
        discarded=sum(r.discarded for r in results),
        failed=sum(1 for r in results if r.error),
        latency_ms=latency_ms,
        per_image_ms=latency_ms // max(1, len(results)),
        batched=_extractor.batch_supported,
    )

    return BatchExtractOut(
        results=results,
        model_name=MODEL_NAME,
        model_version=MODEL_VERSION,
        latency_ms=latency_ms,
        faces_total=faces_total,
        batched=_extractor.batch_supported,
    )


# ---------------------------------------------------------------------------
# CLIP 图文端点（剪辑域）。见 embedding/app/clip.py 的预处理约定。
# ---------------------------------------------------------------------------


class ClipTextIn(BaseModel):
    texts: list[str]


class ClipTextOut(BaseModel):
    embeddings: list[list[float]]
    model_name: str
    model_version: str
    dim: int
    latency_ms: int

    model_config = {"protected_namespaces": ()}


class ClipImageResult(BaseModel):
    embedding: list[float] | None = None
    # 非 None 表示这一张失败（解码错误），不影响同批其他图片
    error: str | None = None


class ClipImageBatchOut(BaseModel):
    results: list[ClipImageResult]
    model_name: str
    model_version: str
    dim: int
    latency_ms: int

    model_config = {"protected_namespaces": ()}


@app.post("/clip/text", response_model=ClipTextOut)
async def clip_text(body: ClipTextIn) -> ClipTextOut:
    """文本批量向量化。在线检索的 query 走这里。"""
    if not _clip.loaded:
        raise HTTPException(status_code=503, detail="CLIP 模型未加载（模型文件缺失？）")
    if not body.texts:
        raise HTTPException(status_code=400, detail="texts 不能为空")
    if len(body.texts) > CLIP_MAX_TEXTS:
        raise HTTPException(status_code=413, detail=f"单次最多 {CLIP_MAX_TEXTS} 条文本")

    started = time.perf_counter()
    async with _inference_slots:
        embeddings = await asyncio.to_thread(_clip.encode_texts, body.texts)
    latency_ms = int((time.perf_counter() - started) * 1000)

    # 只记计数与耗时，绝不记文本内容或向量
    log.info("clip_text_done", texts=len(body.texts), latency_ms=latency_ms)
    return ClipTextOut(
        embeddings=embeddings,
        model_name=CLIP_MODEL_NAME,
        model_version=CLIP_MODEL_VERSION,
        dim=len(embeddings[0]) if embeddings else 0,
        latency_ms=latency_ms,
    )


@app.post("/clip/image/batch", response_model=ClipImageBatchOut)
async def clip_image_batch(images: list[UploadFile] = File(...)) -> ClipImageBatchOut:  # noqa: B008
    """图片批量向量化。离线建库的关键帧走这里，返回值与入参一一对应。"""
    if not _clip.loaded:
        raise HTTPException(status_code=503, detail="CLIP 模型未加载（模型文件缺失？）")
    if not images:
        raise HTTPException(status_code=400, detail="没有收到图片")
    if len(images) > MAX_BATCH_IMAGES:
        raise HTTPException(
            status_code=413,
            detail=f"单次批量最多 {MAX_BATCH_IMAGES} 张，收到 {len(images)} 张",
        )

    payloads = [await _read_upload(image) for image in images]

    started = time.perf_counter()
    async with _inference_slots:
        outcomes = await asyncio.to_thread(_clip.encode_images, payloads)
    latency_ms = int((time.perf_counter() - started) * 1000)

    dim = next((len(o.embedding) for o in outcomes if o.embedding), 0)
    log.info(
        "clip_image_batch_done",
        images=len(outcomes),
        failed=sum(1 for o in outcomes if o.error),
        latency_ms=latency_ms,
    )
    return ClipImageBatchOut(
        results=[ClipImageResult(embedding=o.embedding, error=o.error) for o in outcomes],
        model_name=CLIP_MODEL_NAME,
        model_version=CLIP_MODEL_VERSION,
        dim=dim,
        latency_ms=latency_ms,
    )
