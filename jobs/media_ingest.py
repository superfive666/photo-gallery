"""剪辑素材建库：下载相册原片（首次）→ 拆条 → 关键帧 → CLIP 向量 + 画质指标 → 落库。

与 pipeline.py（人脸建库）是两条独立管线，但同一套纪律：
  · **幂等**：media_asset 以 source_url 唯一约束 upsert；重写 scene 前先按 asset 删旧。
  · **单个文件失败不中断整批**：错误记进 media_asset.processing_error。
  · **原片只读**：下载落盘之后绝不改写；分析产物（关键帧/向量）只进数据库。
  · **向量化只发生在 embedding 服务**（约束 3）：关键帧字节走 HTTP 调 /clip/image/batch。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import itertools
import posixpath
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from gallery_core.clip_client import ClipClient, ClipServiceError
from gallery_core.config import Settings
from gallery_core.logging import get_logger
from gallery_core.models import MediaAsset, Scene
from jobs.quality import (
    combined_quality,
    exposure_score,
    phase_shift,
    resolution_tier,
    sharpness_score,
    stability_score,
    to_gray,
)
from jobs.scenes import (
    VideoProbeError,
    detect_scenes,
    extract_keyframe,
    image_keyframe,
    probe_video,
    sample_gray_frames,
)
from jobs.sources.base import SourceAdapter

log = get_logger(__name__)

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
_VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm"}
_CLIP_BATCH = 16
# 快速指纹：整条视频算 sha256 太慢，头 1MB + 文件大小足以发现文件被替换
_FINGERPRINT_BYTES = 1024 * 1024


@dataclass
class MediaIngestStats:
    downloaded: int = 0
    processed: int = 0
    skipped_existing: int = 0
    failed: int = 0
    scenes_added: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "downloaded": self.downloaded,
            "processed": self.processed,
            "skipped_existing": self.skipped_existing,
            "failed": self.failed,
            "scenes_added": self.scenes_added,
            "sample_errors": self.errors[:20],
        }


def _kind_of(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in _IMAGE_SUFFIXES:
        return "image"
    if suffix in _VIDEO_SUFFIXES:
        return "video"
    return None


def _fingerprint(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        h.update(fh.read(_FINGERPRINT_BYTES))
    h.update(str(path.stat().st_size).encode())
    return h.hexdigest()


def _safe_filename(url_or_name: str) -> str:
    """从 URL/文件名提取安全的落盘名：去目录、去查询串。"""
    name = posixpath.basename(unquote(urlparse(url_or_name).path)) or "asset"
    return name.replace("/", "_").replace("\x00", "")[:200]


async def _download_missing(
    adapter: SourceAdapter,
    album: str,
    media_dir: Path,
    stats: MediaIngestStats,
) -> dict[str, str]:
    """把源站相册里本地还没有的原片下载到 media/<album>/。返回 {文件名: source_url}。"""
    url_by_name: dict[str, str] = {}
    async for asset in adapter.list_assets(album):
        name = _safe_filename(asset.filename or asset.photo_url)
        url_by_name[name] = asset.photo_url
        target = media_dir / name
        if await asyncio.to_thread(target.exists):
            continue
        tmp = target.with_suffix(target.suffix + ".part")
        try:
            with tmp.open("wb") as fh:
                async for chunk in adapter.open_asset(asset):
                    fh.write(chunk)
            await asyncio.to_thread(tmp.rename, target)
            stats.downloaded += 1
            log.info("media_downloaded", album=album, file=name)
        except Exception as exc:
            stats.errors.append(f"download {name}: {type(exc).__name__}")
            await asyncio.to_thread(tmp.unlink, True)
    return url_by_name


@dataclass(slots=True)
class _SceneDraft:
    start_ms: int
    end_ms: int
    keyframe: bytes | None
    keyframe_width: int | None
    keyframe_height: int | None
    stability: float
    sharpness: float
    exposure: float


def _analyze_video(path: Path, min_seconds: float) -> tuple[Any, list[_SceneDraft]]:
    """拆条 + 逐镜头关键帧与画质指标。同步重活，调用方丢线程池。"""
    info = probe_video(path)
    drafts: list[_SceneDraft] = []
    for start_ms, end_ms in detect_scenes(path, min_seconds):
        mid = (start_ms + end_ms) // 2
        key = extract_keyframe(path, mid)
        sharp = 0.0
        expo = 0.0
        if key is not None:
            import io

            import numpy as np
            from PIL import Image

            with Image.open(io.BytesIO(key[0])) as img:
                gray = to_gray(np.asarray(img.convert("RGB"), dtype=np.uint8))
            sharp = sharpness_score(gray)
            expo = exposure_score(gray)

        frames = sample_gray_frames(path, start_ms, end_ms)
        shifts = [phase_shift(a, b) for a, b in itertools.pairwise(frames)]
        stab = stability_score(shifts, frames[0].shape[0] if frames else 0)

        drafts.append(
            _SceneDraft(
                start_ms=start_ms,
                end_ms=end_ms,
                keyframe=key[0] if key else None,
                keyframe_width=key[1] if key else None,
                keyframe_height=key[2] if key else None,
                stability=stab,
                sharpness=sharp,
                exposure=expo,
            )
        )
    return info, drafts


def _analyze_image(path: Path) -> tuple[int, int, _SceneDraft]:
    """照片：整张即一个 scene，稳定性恒为 1。返回 (原宽, 原高, draft)。"""
    import io

    import numpy as np
    from PIL import Image

    payload, kw, kh, width, height = image_keyframe(path)
    with Image.open(io.BytesIO(payload)) as img:
        gray = to_gray(np.asarray(img.convert("RGB"), dtype=np.uint8))
    return (
        width,
        height,
        _SceneDraft(
            start_ms=0,
            end_ms=0,
            keyframe=payload,
            keyframe_width=kw,
            keyframe_height=kh,
            stability=1.0,
            sharpness=sharpness_score(gray),
            exposure=exposure_score(gray),
        ),
    )


async def ingest_album_media(
    session: AsyncSession,
    settings: Settings,
    clip: ClipClient,
    album: str,
    adapter: SourceAdapter | None = None,
) -> MediaIngestStats:
    """一个相册的完整建库。重复执行幂等：已入库且指纹未变的文件直接跳过。"""
    stats = MediaIngestStats()

    if not await clip.available():
        raise ClipServiceError(
            "embedding 服务的 CLIP 双塔未加载（CLIP_MODEL_DIR 配好了吗？），中止建库。"
        )

    media_dir = Path(settings.media_dir(album))
    await asyncio.to_thread(media_dir.mkdir, parents=True, exist_ok=True)

    url_by_name: dict[str, str] = {}
    if adapter is not None:
        url_by_name = await _download_missing(adapter, album, media_dir, stats)

    files = sorted(
        p
        for p in await asyncio.to_thread(lambda: list(media_dir.iterdir()))
        if p.is_file() and _kind_of(p) is not None
    )

    existing_rows = (
        (await session.execute(select(MediaAsset).where(MediaAsset.album == album))).scalars().all()
    )
    by_path = {row.path: row for row in existing_rows}

    for path in files:
        kind = _kind_of(path)
        assert kind is not None
        checksum = await asyncio.to_thread(_fingerprint, path)

        row = by_path.get(str(path))
        if row is not None and row.processing_status == "embedded" and row.checksum == checksum:
            stats.skipped_existing += 1
            continue

        # 幂等键：下载来的用真实 URL；手动预拷入的用 local:// 合成键
        source_url = url_by_name.get(path.name, f"local://{album}/{path.name}")

        if row is None:
            row = MediaAsset(album=album, source_url=source_url, path=str(path), kind=kind)
            session.add(row)
            try:
                await session.flush()
            except Exception:
                # source_url 撞唯一约束：同一文件换了路径。回读那一行继续处理。
                await session.rollback()
                row = (
                    await session.execute(
                        select(MediaAsset).where(MediaAsset.source_url == source_url)
                    )
                ).scalar_one()
                row.path = str(path)

        try:
            await _process_asset(session, settings, clip, row, path, kind, checksum, stats)
            stats.processed += 1
        except (ClipServiceError, VideoProbeError, OSError) as exc:
            stats.failed += 1
            stats.errors.append(f"{path.name}: {type(exc).__name__}")
            row.processing_status = "failed"
            row.processing_error = f"{type(exc).__name__}: {exc}"
            log.warning("media_asset_failed", file=path.name, error_type=type(exc).__name__)
        await session.commit()

    return stats


async def _process_asset(
    session: AsyncSession,
    settings: Settings,
    clip: ClipClient,
    row: MediaAsset,
    path: Path,
    kind: str,
    checksum: str,
    stats: MediaIngestStats,
) -> None:
    if kind == "video":
        info, drafts = await asyncio.to_thread(_analyze_video, path, settings.scene_min_seconds)
        row.duration_ms = info.duration_ms
        row.width, row.height = info.width, info.height
        row.fps, row.codec = info.fps, info.codec
        tier = resolution_tier(info.height)
    else:
        width, height, draft = await asyncio.to_thread(_analyze_image, path)
        drafts = [draft]
        row.width, row.height = width, height
        tier = resolution_tier(height)

    row.checksum = checksum
    row.size_bytes = (await asyncio.to_thread(path.stat)).st_size
    row.resolution_tier = tier

    # 关键帧 → CLIP 向量（分块批量）。取不到关键帧的镜头没有可检索的内容，直接丢弃。
    usable = [d for d in drafts if d.keyframe is not None]
    embeddings: list[list[float]] = []
    model_name, model_version = "", ""
    for start in range(0, len(usable), _CLIP_BATCH):
        chunk = usable[start : start + _CLIP_BATCH]
        batch = await clip.encode_images(
            [(f"kf{start + i}.jpg", d.keyframe or b"") for i, d in enumerate(chunk)]
        )
        model_name, model_version = batch.model_name, batch.model_version
        for d, result in zip(chunk, batch.results, strict=True):
            if result.embedding is None:
                stats.errors.append(f"{path.name}@{d.start_ms}: {result.error}")
                embeddings.append([])
            else:
                embeddings.append(result.embedding)

    # 重跑安全：先删旧 scene 再写新的
    await session.execute(delete(Scene).where(Scene.asset_id == row.id))

    added = 0
    for draft, vec in zip(usable, embeddings, strict=True):
        if not vec:
            continue
        session.add(
            Scene(
                asset_id=row.id,
                album=row.album,
                start_ms=draft.start_ms,
                end_ms=draft.end_ms,
                keyframe=draft.keyframe,
                keyframe_width=draft.keyframe_width,
                keyframe_height=draft.keyframe_height,
                embedding=vec,
                model_name=model_name or settings.clip_model_name,
                model_version=model_version or settings.clip_model_version,
                dim=len(vec),
                stability=draft.stability,
                sharpness=draft.sharpness,
                exposure=draft.exposure,
                quality_score=combined_quality(
                    tier, draft.stability, draft.sharpness, draft.exposure
                ),
            )
        )
        added += 1

    row.scene_count = added
    row.processing_status = "embedded"
    row.processing_error = None
    row.updated_at = dt.datetime.now(tz=dt.UTC)
    stats.scenes_added += added
    log.info("media_asset_embedded", file=path.name, kind=kind, scenes=added)


async def album_is_ingested(session: AsyncSession, album: str) -> bool:
    """该相册是否已有可检索的 scene（决定新项目要不要先走建库）。"""
    row = (await session.execute(select(Scene.id).where(Scene.album == album).limit(1))).first()
    return row is not None
