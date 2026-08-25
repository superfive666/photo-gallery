"""剪辑素材建库：视频下载落盘 + 拆条；照片内存直通 —— 全部产出关键帧 + CLIP 向量 + 画质指标。

**照片不落盘**（006 迁移）：分析所需的一切（关键帧缩略图、画质、向量）在下载字节的
生命周期内完成，评审预览用库里的关键帧，渲染导出时按 source_url 现下载 ——
本地盘只需要放视频（拆条与 ffmpeg 剪裁必须随机访问文件）。

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
from gallery_core.db import session_scope
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
    image_keyframe_bytes,
    probe_video,
    sample_gray_frames,
)
from jobs.sources import resolve_album_source
from jobs.sources.base import SourceAdapter, SourceAsset

log = get_logger(__name__)

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
_VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm"}
_CLIP_BATCH = 16
# 快速指纹：整条视频算 sha256 太慢，头 1MB + 文件大小足以发现文件被替换
_FINGERPRINT_BYTES = 1024 * 1024


@dataclass
class MediaIngestStats:
    downloaded: int = 0
    images_remote: int = 0
    processed: int = 0
    skipped_existing: int = 0
    failed: int = 0
    scenes_added: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "downloaded": self.downloaded,
            "images_remote": self.images_remote,
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


async def _sync_remote_assets(
    adapter: SourceAdapter,
    album: str,
    media_dir: Path,
    stats: MediaIngestStats,
) -> tuple[dict[str, str], list[SourceAsset]]:
    """视频下载到 media/<album>/（缺什么下什么）；照片不落盘，收集起来走内存直通。

    返回 ({视频文件名: source_url}, 待处理的照片资产列表)。
    """
    url_by_name: dict[str, str] = {}
    remote_images: list[SourceAsset] = []
    async for asset in adapter.list_assets(album):
        if asset.kind == "image":
            remote_images.append(asset)
            continue
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
    return url_by_name, remote_images


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


def _analyze_image_bytes(data: bytes) -> tuple[int, int, _SceneDraft]:
    """照片字节：整张即一个 scene，稳定性恒为 1。返回 (原宽, 原高, draft)。

    只吃字节 —— 远端照片不落盘（006 迁移），本地预拷入的照片由调用方读出字节。
    """
    import io

    import numpy as np
    from PIL import Image

    payload, kw, kh, width, height = image_keyframe_bytes(data)
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


@dataclass(slots=True)
class _KnownAsset:
    """已入库素材的轻量快照。只用于跳过判断 —— 不持有 ORM 对象、不持有连接。"""

    processing_status: str
    checksum: str | None
    path: str | None


async def _load_known(album: str) -> dict[str, _KnownAsset]:
    """{source_url: 快照}。一次短查询，之后整个建库过程不再占用这条连接。"""
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(
                    MediaAsset.source_url,
                    MediaAsset.processing_status,
                    MediaAsset.checksum,
                    MediaAsset.path,
                ).where(MediaAsset.album == album)
            )
        ).all()
    return {
        r.source_url: _KnownAsset(
            processing_status=r.processing_status, checksum=r.checksum, path=r.path
        )
        for r in rows
    }


async def ingest_album_media(
    settings: Settings,
    clip: ClipClient,
    album: str,
    adapter: SourceAdapter | None = None,
) -> MediaIngestStats:
    """一个相册的完整建库。重复执行幂等：已入库且指纹未变的素材直接跳过。

    ⚠️ 连接纪律：本函数**不持有**数据库连接。下载/拆条/向量化动辄以小时计，
    攥着一条连接跨过这些长活，连接会被宿主机 Postgres/内核当空闲掐掉，
    到写库时才爆 "connection is closed"。所以长活全程无连接，
    每个素材写库时开一个即用即还的短 session（gallery_core.db.session_scope）。
    """
    stats = MediaIngestStats()

    if not await clip.available():
        raise ClipServiceError(
            "embedding 服务的 CLIP 双塔未加载（CLIP_MODEL_DIR 配好了吗？），中止建库。"
        )

    # auto 模式（CompositeAdapter）：相册来源按 plans/0010 规则判定。
    # 本地相册不走远端列举 —— 素材本来就在 media/<album>/，直接走下面的本地
    # 文件扫描；若还经 adapter 列一遍，同一张照片会以 local://album/... 与
    # local://<album>/... 两种幂等键重复入库。远端相册则拿远端子 adapter，
    # 避免 composite 的目录兜底路由被视频副本目录带偏。
    selector = getattr(adapter, "for_source", None)
    if selector is not None:
        async with session_scope() as session:
            source = await resolve_album_source(session, album, settings)
        log.info("album_source_resolved", album=album, source=source)
        adapter = None if source == "local" else selector("remote")

    media_dir = Path(settings.media_dir(album))
    await asyncio.to_thread(media_dir.mkdir, parents=True, exist_ok=True)

    known = await _load_known(album)

    url_by_name: dict[str, str] = {}
    if adapter is not None:
        url_by_name, remote_images = await _sync_remote_assets(adapter, album, media_dir, stats)
        for asset in remote_images:
            await _ingest_remote_image(settings, clip, adapter, asset, known, stats)

    files = sorted(
        p
        for p in await asyncio.to_thread(lambda: list(media_dir.iterdir()))
        if p.is_file() and _kind_of(p) is not None
    )
    by_path = {k.path: k for k in known.values() if k.path}

    for path in files:
        kind = _kind_of(path)
        assert kind is not None
        checksum = await asyncio.to_thread(_fingerprint, path)

        snap = by_path.get(str(path))
        if snap is not None and snap.processing_status == "embedded" and snap.checksum == checksum:
            stats.skipped_existing += 1
            continue

        # 幂等键：下载来的用真实 URL；手动预拷入的用 local:// 合成键
        source_url = url_by_name.get(path.name, f"local://{album}/{path.name}")

        try:
            await _ingest_local_file(settings, clip, album, source_url, path, kind, checksum, stats)
            stats.processed += 1
        except (ClipServiceError, VideoProbeError, OSError) as exc:
            stats.failed += 1
            stats.errors.append(f"{path.name}: {type(exc).__name__}")
            await _mark_failed(album, source_url, str(path), kind, f"{type(exc).__name__}: {exc}")
            log.warning("media_asset_failed", file=path.name, error_type=type(exc).__name__)

    return stats


async def _ingest_local_file(
    settings: Settings,
    clip: ClipClient,
    album: str,
    source_url: str,
    path: Path,
    kind: str,
    checksum: str,
    stats: MediaIngestStats,
) -> None:
    """本地文件（视频，或手动预拷入的素材）：分析与向量化不占连接，最后短事务落库。"""
    meta: dict[str, Any] = {"checksum": checksum}
    if kind == "video":
        info, drafts = await asyncio.to_thread(_analyze_video, path, settings.scene_min_seconds)
        meta |= {
            "duration_ms": info.duration_ms,
            "width": info.width,
            "height": info.height,
            "fps": info.fps,
            "codec": info.codec,
        }
        tier = resolution_tier(info.height)
    else:
        payload = await asyncio.to_thread(path.read_bytes)
        width, height, draft = await asyncio.to_thread(_analyze_image_bytes, payload)
        del payload
        drafts = [draft]
        meta |= {"width": width, "height": height}
        tier = resolution_tier(height)
    meta["size_bytes"] = (await asyncio.to_thread(path.stat)).st_size
    meta["resolution_tier"] = tier

    encoded, model_name, model_version = await _encode_keyframes(clip, drafts, stats, path.name)
    scenes = await _persist_asset(
        settings, album, source_url, str(path), kind, meta, encoded, model_name, model_version
    )
    stats.scenes_added += scenes
    log.info("media_asset_embedded", file=path.name, kind=kind, scenes=scenes)


async def _ingest_remote_image(
    settings: Settings,
    clip: ClipClient,
    adapter: SourceAdapter,
    asset: SourceAsset,
    known: dict[str, _KnownAsset],
    stats: MediaIngestStats,
) -> None:
    """远端照片的内存直通：下载字节 → 分析 → 向量化 → 丢弃，不写盘。

    幂等：source_url 唯一，已 embedded 的直接跳过（源站照片 URL 稳定且内容不变，
    与 photo 表的幂等假设一致）—— 重跑不重新下载。
    """
    snap = known.get(asset.photo_url)
    if snap is not None and snap.processing_status == "embedded":
        stats.skipped_existing += 1
        return

    name = _safe_filename(asset.filename or asset.photo_url)
    try:
        chunks: list[bytes] = []
        async for chunk in adapter.open_asset(asset):
            chunks.append(chunk)
        payload = b"".join(chunks)
        del chunks

        width, height, draft = await asyncio.to_thread(_analyze_image_bytes, payload)
        meta: dict[str, Any] = {
            "width": width,
            "height": height,
            "size_bytes": len(payload),
            "checksum": hashlib.sha256(payload).hexdigest(),
            "resolution_tier": resolution_tier(height),
        }
        del payload

        encoded, model_name, model_version = await _encode_keyframes(clip, [draft], stats, name)
        scenes = await _persist_asset(
            settings,
            asset.album,
            asset.photo_url,
            None,
            "image",
            meta,
            encoded,
            model_name,
            model_version,
        )
        stats.scenes_added += scenes
        stats.images_remote += 1
        stats.processed += 1
        log.info("media_asset_embedded", file=name, kind="image_remote", scenes=scenes)
    except Exception as exc:  # 网络/解码/推理任一失败都只废这一张，不中断整批
        stats.failed += 1
        stats.errors.append(f"{name}: {type(exc).__name__}")
        await _mark_failed(
            asset.album, asset.photo_url, None, "image", f"{type(exc).__name__}: {exc}"
        )
        log.warning("media_asset_failed", file=name, error_type=type(exc).__name__)


async def _encode_keyframes(
    clip: ClipClient,
    drafts: list[_SceneDraft],
    stats: MediaIngestStats,
    label: str,
) -> tuple[list[tuple[_SceneDraft, list[float]]], str, str]:
    """关键帧 → CLIP 向量（分块批量）。HTTP 调用，不占数据库连接。"""
    usable = [d for d in drafts if d.keyframe is not None]
    encoded: list[tuple[_SceneDraft, list[float]]] = []
    model_name, model_version = "", ""
    for start in range(0, len(usable), _CLIP_BATCH):
        chunk = usable[start : start + _CLIP_BATCH]
        batch = await clip.encode_images(
            [(f"kf{start + i}.jpg", d.keyframe or b"") for i, d in enumerate(chunk)]
        )
        model_name, model_version = batch.model_name, batch.model_version
        for d, result in zip(chunk, batch.results, strict=True):
            if result.embedding is None:
                stats.errors.append(f"{label}@{d.start_ms}: {result.error}")
            else:
                encoded.append((d, result.embedding))
    return encoded, model_name, model_version


async def _upsert_asset_row(
    session: AsyncSession, album: str, source_url: str, path: str | None, kind: str
) -> MediaAsset:
    row = (
        await session.execute(select(MediaAsset).where(MediaAsset.source_url == source_url))
    ).scalar_one_or_none()
    if row is None:
        row = MediaAsset(album=album, source_url=source_url, path=path, kind=kind)
        session.add(row)
        await session.flush()
    elif path is not None:
        # 文件换了位置就跟着更新；远端照片（path=None）不覆盖已有的本地路径
        row.path = path
    return row


async def _persist_asset(
    settings: Settings,
    album: str,
    source_url: str,
    path: str | None,
    kind: str,
    meta: dict[str, Any],
    encoded: list[tuple[_SceneDraft, list[float]]],
    model_name: str,
    model_version: str,
) -> int:
    """一个素材的全部写库：即开即还的短事务，前后都不持有连接。"""
    async with session_scope() as session:
        row = await _upsert_asset_row(session, album, source_url, path, kind)
        for key, value in meta.items():
            setattr(row, key, value)

        # 重跑安全：先删旧 scene 再写新的
        await session.execute(delete(Scene).where(Scene.asset_id == row.id))
        for draft, vec in encoded:
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
                        meta.get("resolution_tier", 0.0),
                        draft.stability,
                        draft.sharpness,
                        draft.exposure,
                    ),
                )
            )
        row.scene_count = len(encoded)
        row.processing_status = "embedded"
        row.processing_error = None
        row.updated_at = dt.datetime.now(tz=dt.UTC)
        return len(encoded)


async def _mark_failed(
    album: str, source_url: str, path: str | None, kind: str, error: str
) -> None:
    """失败留痕也用独立短事务 —— 处理失败绝不能因为连接问题而丢掉失败原因。"""
    try:
        async with session_scope() as session:
            row = await _upsert_asset_row(session, album, source_url, path, kind)
            row.processing_status = "failed"
            row.processing_error = error[:500]
    except Exception:
        log.exception("mark_failed_write_failed", source_url=source_url)


async def album_is_ingested(session: AsyncSession, album: str) -> bool:
    """该相册是否已有可检索的 scene（决定新项目要不要先走建库）。"""
    row = (await session.execute(select(Scene.id).where(Scene.album == album).limit(1))).first()
    return row is not None
