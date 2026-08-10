"""离线建库：拉取 → 提取人脸 → 落库。

设计要求（任一条被破坏都会造成实际故障）：
  · **幂等**：任意时刻被杀掉再重跑，不产生重复、不丢数据。靠 source_asset_id 唯一约束
    + ON CONFLICT DO UPDATE。
  · **增量**：checksum 未变则跳过，不重新下载、不重新推理。
  · **单张失败不中断整批**：错误记进 photo.processing_error，最后汇总。
  · **磁盘纪律**：原图流式下载到临时文件，提取完立即删除。不做原图缓存，否则磁盘必被打满。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from sqlalchemy import CursorResult, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from gallery_core.config import Settings
from gallery_core.embedding_client import EmbeddingClient, EmbeddingServiceError
from gallery_core.logging import get_logger
from gallery_core.models import Album, Face, JobRun, Photo, SourceSyncState
from jobs.sources.base import SourceAdapter, SourceAlbum, SourceAsset
from jobs.thumbnails import make_thumbnail

log = get_logger(__name__)


@dataclass
class IngestStats:
    processed: int = 0
    skipped_unchanged: int = 0
    skipped_video: int = 0
    failed: int = 0
    # 源站已不存在、被软删除的照片数
    soft_deleted: int = 0
    faces_added: int = 0
    # 通过检测但被质量门控丢弃的人脸数。「后排的人搜不到」的量化依据 ——
    # 如果这个数很大，该做的是调 MIN_FACE_PX / det_size，而不是调相似度阈值。
    faces_discarded: int = 0
    bytes_downloaded: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "processed": self.processed,
            "skipped_unchanged": self.skipped_unchanged,
            "skipped_video": self.skipped_video,
            "failed": self.failed,
            "soft_deleted": self.soft_deleted,
            "faces_added": self.faces_added,
            "faces_discarded": self.faces_discarded,
            "bytes_downloaded": self.bytes_downloaded,
            # 只留前若干条，避免 stats 无限膨胀
            "sample_errors": self.errors[:20],
        }


async def ingest(
    session: AsyncSession,
    adapter: SourceAdapter,
    embedding: EmbeddingClient,
    settings: Settings,
    album_filter: str | None = None,
    full: bool = False,
) -> IngestStats:
    stats = IngestStats()
    # 临时目录只建一次。pathlib 是阻塞调用，丢到线程里以免占用 event loop。
    await asyncio.to_thread(Path(settings.ingest_tmp_dir).mkdir, parents=True, exist_ok=True)

    run = JobRun(kind="ingest", stats={})
    session.add(run)
    await session.commit()

    try:
        albums = await adapter.list_albums()
        if album_filter:
            albums = [a for a in albums if a.id == album_filter]
            if not albums:
                raise ValueError(f"找不到相册 {album_filter}")

        for source_album in albums:
            if source_album.visibility != "public":
                # 非 public 一律不进检索库。'unknown' 也跳过 —— 拿不准就不收，
                # 绝不把不确定可见性的相册当作公开。
                log.info(
                    "album_skipped_visibility",
                    album=source_album.id,
                    visibility=source_album.visibility,
                )
                continue
            await _ingest_album(
                session, adapter, embedding, settings, source_album, stats, full=full
            )

        run.status = "succeeded"
    except Exception as exc:
        run.status = "failed"
        run.error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        run.stats = stats.as_dict()
        run.finished_at = dt.datetime.now(tz=dt.UTC)
        await session.commit()

    return stats


async def _ingest_album(
    session: AsyncSession,
    adapter: SourceAdapter,
    embedding: EmbeddingClient,
    settings: Settings,
    source_album: SourceAlbum,
    stats: IngestStats,
    *,
    full: bool,
) -> None:
    album = await _upsert_album(session, source_album)

    since: dt.datetime | None = None
    if not full:
        state = await session.get(SourceSyncState, album.id)
        since = state.last_synced_at if state else None

    started_at = dt.datetime.now(tz=dt.UTC)
    # 只有枚举了整个相册（全量，或首次同步）时，"没见到" 才等于 "源站已删除"。
    # 增量同步只会看到变更过的资产，据此推断删除会把大量存量照片误标为已删。
    enumerated_everything = full or since is None
    seen_asset_ids: set[str] = set()

    async for asset in adapter.list_assets(source_album.id, since=since):
        seen_asset_ids.add(asset.id)
        if asset.kind == "video":
            # 第一期不处理视频：抽帧会让计算量和存储上一个量级。
            # 仍然登记，便于统计视频占比来决定第二期优先级。
            await _register_skipped(session, album.id, asset, reason="video")
            stats.skipped_video += 1
            continue

        try:
            changed = await _needs_processing(session, asset, full=full)
            if not changed:
                stats.skipped_unchanged += 1
                continue
            await _process_asset(session, adapter, embedding, settings, album, asset, stats)
        except Exception as exc:
            stats.failed += 1
            message = f"{asset.filename}: {type(exc).__name__}: {exc}"
            stats.errors.append(message)
            log.warning("asset_failed", asset_id=asset.id, error_type=type(exc).__name__)
            await _mark_failed(session, album.id, asset, message)

    if enumerated_everything:
        stats.soft_deleted += await mark_missing_as_deleted(session, album.id, seen_asset_ids)

    await _upsert_sync_state(session, album.id, started_at, full=full)
    await session.commit()


async def _process_asset(
    session: AsyncSession,
    adapter: SourceAdapter,
    embedding: EmbeddingClient,
    settings: Settings,
    album: Album,
    asset: SourceAsset,
    stats: IngestStats,
) -> None:
    # 流式下载到临时文件。目录由 ingest() 在开始时创建一次（compose 里挂的是限容 tmpfs），
    # 且用 with 保证无论成功失败都会被删除 —— 不做原图缓存。
    tmp_dir = Path(settings.ingest_tmp_dir)

    with tempfile.NamedTemporaryFile(dir=tmp_dir, suffix=Path(asset.filename).suffix) as tmp:
        size = 0
        async for chunk in adapter.open_asset(asset):
            tmp.write(chunk)
            size += len(chunk)
        tmp.flush()
        stats.bytes_downloaded += size

        # 读回一次用于推理与缩略图。这一份字节只在本函数栈上存在。
        tmp.seek(0)
        payload = tmp.read()

    try:
        result = await embedding.extract(payload, filename=asset.filename)
    except EmbeddingServiceError:
        # embedding 服务挂了是全局故障，不该被当成单张照片失败静默吞掉
        raise

    thumb_bytes: bytes | None = None
    thumb_w = thumb_h = None
    try:
        thumb_bytes, thumb_w, thumb_h = make_thumbnail(
            payload, settings.thumb_max_edge, settings.thumb_quality
        )
    except Exception as exc:
        log.warning("thumbnail_failed", asset_id=asset.id, error_type=type(exc).__name__)

    del payload  # 尽早释放；大相册下这能显著压低内存峰值

    photo_id = await _upsert_photo(
        session,
        album_id=album.id,
        asset=asset,
        result_width=result.image_width,
        result_height=result.image_height,
        face_count=len(result.faces),
        faces_discarded=result.discarded,
        thumb=(thumb_bytes, thumb_w, thumb_h),
        original_url=adapter.build_original_url(asset, settings.signed_url_ttl_seconds),
    )

    # 重跑时先清掉这张照片的旧人脸，再写新的 —— 保证幂等，不会累积重复向量。
    await session.execute(text("DELETE FROM face WHERE photo_id = :pid"), {"pid": str(photo_id)})

    for f in result.faces:
        session.add(
            Face(
                photo_id=photo_id,
                bbox_x=f.bbox[0],
                bbox_y=f.bbox[1],
                bbox_w=f.bbox[2],
                bbox_h=f.bbox[3],
                face_px=f.face_px,
                det_score=f.det_score,
                landmarks=f.landmarks,
                embedding=f.embedding,
                model_name=f.model_name,
                model_version=f.model_version,
                dim=f.dim,
            )
        )

    stats.processed += 1
    stats.faces_added += len(result.faces)
    stats.faces_discarded += result.discarded
    await session.commit()


async def _needs_processing(session: AsyncSession, asset: SourceAsset, *, full: bool) -> bool:
    if full:
        return True
    row = (
        await session.execute(
            select(Photo.source_checksum, Photo.processing_status).where(
                Photo.source_asset_id == asset.id
            )
        )
    ).first()
    if row is None:
        return True
    # 之前失败过的要重试
    if row.processing_status != "embedded":
        return True
    # 源站给不出 checksum 时只能每次重新处理 —— 见 docs/data-source.md 问题 6
    if asset.checksum is None or row.source_checksum is None:
        return True
    return bool(row.source_checksum != asset.checksum)


async def _upsert_album(session: AsyncSession, source_album: SourceAlbum) -> Album:
    stmt = (
        pg_insert(Album)
        .values(
            source_album_id=source_album.id,
            name=source_album.name,
            source_url=source_album.url,
            visibility=source_album.visibility,
            event_date=source_album.event_date,
        )
        .on_conflict_do_update(
            index_elements=[Album.source_album_id],
            set_={
                "name": source_album.name,
                "source_url": source_album.url,
                # 可见性必须跟着源站更新：相册转私密后要能从检索中排除
                "visibility": source_album.visibility,
                "event_date": source_album.event_date,
                "updated_at": dt.datetime.now(tz=dt.UTC),
            },
        )
        .returning(Album.id)
    )
    album_id = (await session.execute(stmt)).scalar_one()
    await session.commit()
    album = await session.get(Album, album_id)
    assert album is not None
    return album


async def _upsert_photo(
    session: AsyncSession,
    *,
    album_id: uuid.UUID,
    asset: SourceAsset,
    result_width: int,
    result_height: int,
    face_count: int,
    faces_discarded: int,
    thumb: tuple[bytes | None, int | None, int | None],
    original_url: str,
) -> uuid.UUID:
    thumb_bytes, thumb_w, thumb_h = thumb
    now = dt.datetime.now(tz=dt.UTC)
    values = {
        "album_id": album_id,
        "source_asset_id": asset.id,
        "kind": asset.kind,
        "filename": asset.filename,
        "source_url": original_url,
        "source_checksum": asset.checksum,
        "size_bytes": asset.size_bytes,
        "width": asset.width or result_width or None,
        "height": asset.height or result_height or None,
        "taken_at": asset.taken_at,
        "thumb_webp": thumb_bytes,
        "thumb_width": thumb_w,
        "thumb_height": thumb_h,
        "processing_status": "embedded",
        "processing_error": None,
        "face_count": face_count,
        "faces_discarded": faces_discarded,
        "embedded_at": now,
        # 源站上重新出现的资产要撤销软删除
        "deleted_at": None,
    }
    stmt = (
        pg_insert(Photo)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[Photo.source_asset_id],
            set_={**values, "updated_at": now},
        )
        .returning(Photo.id)
    )
    return (await session.execute(stmt)).scalar_one()


async def _register_skipped(
    session: AsyncSession, album_id: uuid.UUID, asset: SourceAsset, reason: str
) -> None:
    now = dt.datetime.now(tz=dt.UTC)
    values = {
        "album_id": album_id,
        "source_asset_id": asset.id,
        "kind": asset.kind,
        "filename": asset.filename,
        "source_url": asset.url,
        "source_checksum": asset.checksum,
        "size_bytes": asset.size_bytes,
        "taken_at": asset.taken_at,
        "processing_status": "skipped",
        "processing_error": reason,
    }
    await session.execute(
        pg_insert(Photo)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[Photo.source_asset_id],
            set_={"processing_status": "skipped", "processing_error": reason, "updated_at": now},
        )
    )


async def _mark_failed(
    session: AsyncSession, album_id: uuid.UUID, asset: SourceAsset, message: str
) -> None:
    """记录失败，但不让它阻塞后续照片。下次运行会自动重试。"""
    await session.rollback()
    now = dt.datetime.now(tz=dt.UTC)
    values = {
        "album_id": album_id,
        "source_asset_id": asset.id,
        "kind": asset.kind,
        "filename": asset.filename,
        "source_url": asset.url,
        "processing_status": "failed",
        "processing_error": message[:2000],
    }
    await session.execute(
        pg_insert(Photo)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[Photo.source_asset_id],
            set_={
                "processing_status": "failed",
                "processing_error": message[:2000],
                "updated_at": now,
            },
        )
    )
    await session.commit()


async def _upsert_sync_state(
    session: AsyncSession, album_id: uuid.UUID, started_at: dt.datetime, *, full: bool
) -> None:
    now = dt.datetime.now(tz=dt.UTC)
    values: dict[str, object] = {
        "album_id": album_id,
        # 记的是本轮**开始**的时间，不是结束时间 ——
        # 否则运行期间新增的资产会落进「已同步」的时间窗里被永久跳过。
        "last_synced_at": started_at,
        "updated_at": now,
    }
    if full:
        values["last_full_sync_at"] = now
    await session.execute(
        pg_insert(SourceSyncState)
        .values(**values)
        .on_conflict_do_update(index_elements=[SourceSyncState.album_id], set_=values)
    )


async def mark_missing_as_deleted(
    session: AsyncSession, album_id: uuid.UUID, seen_asset_ids: set[str]
) -> int:
    """把源站已不存在的资产软删除。

    软删除而非物理删除：便于排查「照片怎么突然搜不到了」，也给误删留了恢复窗口。
    """
    if not seen_asset_ids:
        return 0
    result = await session.execute(
        text(
            """
            UPDATE photo SET deleted_at = now(), updated_at = now()
            WHERE album_id = :album_id
              AND deleted_at IS NULL
              AND source_asset_id <> ALL(CAST(:seen AS text[]))
            """
        ),
        {"album_id": str(album_id), "seen": list(seen_asset_ids)},
    )
    await session.commit()
    # UPDATE 返回的是 CursorResult，只有它才有 rowcount；Result 的静态类型上没有这个属性。
    return cast("CursorResult[Any]", result).rowcount or 0
