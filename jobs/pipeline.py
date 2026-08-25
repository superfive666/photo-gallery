"""离线建库：拉取 → 批量提取人脸 → 落库。

设计要求（任一条被破坏都会造成实际故障）：

  · **批量**：一批照片一次性送进 embedding 服务，整批的人脸拼成一次识别前向。
    这是 GPU 利用率的主要来源。批大小同时受「张数」和「字节数」两个上限约束 ——
    只限张数的话，遇到一批高像素原图会直接把内存顶穿。
  · **幂等**：任意时刻被杀掉再重跑，不产生重复、不丢数据。靠 photo_url 唯一约束
    + ON CONFLICT DO UPDATE，以及写新人脸前先按 photo_id 删旧人脸。
  · **单张失败不中断整批**：错误记进 photo.processing_error，最后汇总。
  · **原图不落盘**：批次的字节只存在于内存中，处理完即释放。不做原图缓存。
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from gallery_core.config import Settings
from gallery_core.embedding_client import EmbeddingClient, EmbeddingServiceError, ExtractResult
from gallery_core.logging import get_logger
from gallery_core.models import Face, JobRun, Photo
from jobs.sources.base import SourceAdapter, SourceAsset
from jobs.thumbnails import crop_face, make_thumbnail

log = get_logger(__name__)


@dataclass
class IngestStats:
    albums: int = 0
    processed: int = 0
    skipped_existing: int = 0
    # 因超长/超大而跳过的视频（正常尺寸的视频自 plans/0008 起会被处理）
    skipped_video: int = 0
    videos_processed: int = 0
    video_frames: int = 0
    video_segments: int = 0
    failed: int = 0
    soft_deleted: int = 0
    faces_added: int = 0
    # 通过检测但被质量门控丢弃的人脸数。「后排的人搜不到」的量化依据 ——
    # 如果这个数很大，该做的是调 MIN_FACE_PX / det_size，而不是调相似度阈值。
    faces_discarded: int = 0
    thumbs_from_source: int = 0
    thumbs_generated: int = 0
    bytes_downloaded: int = 0
    batches: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "albums": self.albums,
            "processed": self.processed,
            "skipped_existing": self.skipped_existing,
            "skipped_video": self.skipped_video,
            "videos_processed": self.videos_processed,
            "video_frames": self.video_frames,
            "video_segments": self.video_segments,
            "failed": self.failed,
            "soft_deleted": self.soft_deleted,
            "faces_added": self.faces_added,
            "faces_discarded": self.faces_discarded,
            "thumbs_from_source": self.thumbs_from_source,
            "thumbs_generated": self.thumbs_generated,
            "bytes_downloaded": self.bytes_downloaded,
            "batches": self.batches,
            # 只留前若干条，避免 stats 无限膨胀
            "sample_errors": self.errors[:20],
        }


@dataclass(slots=True)
class _Loaded:
    """已下载到内存、等待送进批量推理的一张照片。"""

    asset: SourceAsset
    payload: bytes
    source_thumbnail: bytes | None


async def ingest(
    session: AsyncSession,
    adapter: SourceAdapter,
    embedding: EmbeddingClient,
    settings: Settings,
    album_filter: str | None = None,
    full: bool = False,
) -> IngestStats:
    stats = IngestStats()
    run = JobRun(kind="ingest", album=album_filter, stats={})
    session.add(run)
    await session.commit()

    try:
        if album_filter:
            albums = [album_filter]
        else:
            albums = await adapter.list_albums()
            if not albums:
                raise ValueError(
                    "源站没有返回相册列表，请用 --album 指定。见 docs/data-source.md。"
                )

        # 批大小不硬编码两处：以服务端 /healthz 报告的上限为准
        server_limit = await embedding.max_batch_images()
        batch_images = max(1, min(settings.ingest_batch_images, server_limit))
        log.info("ingest_batch_size", images=batch_images, server_limit=server_limit)

        for album in albums:
            stats.albums += 1
            await _ingest_album(
                session, adapter, embedding, settings, album, stats, batch_images, full=full
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
    album: str,
    stats: IngestStats,
    batch_images: int,
    *,
    full: bool,
) -> None:
    started_at = dt.datetime.now(tz=dt.UTC)

    # auto 模式（CompositeAdapter）：先按 plans/0010 的路由规则判定相册来源，
    # 再用对应的子 adapter 列资产。不能直接用 composite 的目录兜底路由 ——
    # 远端相册跑过剪辑建库后 media/<album>/ 里有视频副本，会被误判成本地相册。
    # 取字节仍走传入的 adapter（按 URL scheme 分发，混存安全）。
    lister = adapter
    selector = getattr(adapter, "for_source", None)
    if selector is not None:
        from jobs.sources import resolve_album_source

        source = await resolve_album_source(session, album, settings)
        lister = selector(source)
        log.info("album_source_resolved", album=album, source=source)

    # 静态相册页一次就给出全部条目，所以我们总是掌握完整清单，
    # 「没见到」即等于「源站已删除」。
    assets = [a async for a in lister.list_assets(album)]
    seen_urls = {a.photo_url for a in assets}

    images: list[SourceAsset] = []
    videos: list[SourceAsset] = []
    for asset in assets:
        if asset.kind == "video":
            videos.append(asset)
        else:
            images.append(asset)

    if not full:
        already = await _already_embedded(session, album, [a.photo_url for a in images])
        before = len(images)
        images = [a for a in images if a.photo_url not in already]
        stats.skipped_existing += before - len(images)

        # 视频的终态多一种：因超长/超大被跳过的不再反复下载重试
        video_done = await _videos_to_skip(session, album, [a.photo_url for a in videos])
        before = len(videos)
        videos = [a for a in videos if a.photo_url not in video_done]
        stats.skipped_existing += before - len(videos)

    for batch in _chunk_by_size(images, batch_images, settings.ingest_batch_max_bytes):
        await _process_batch(session, adapter, embedding, settings, album, batch, stats)
        stats.batches += 1

    # 视频逐个处理（单个就是一批帧的量），帧在内部按 batch_images 分块送 GPU
    for asset in videos:
        try:
            await _process_video(
                session, adapter, embedding, settings, album, asset, stats, batch_images
            )
        except EmbeddingServiceError:
            raise  # 服务级故障不该被当成单个视频失败吞掉
        except Exception as exc:
            stats.failed += 1
            message = f"{asset.filename}: video {type(exc).__name__}: {exc}"
            stats.errors.append(message)
            log.warning("video_failed", album=album, error_type=type(exc).__name__)
            await _mark_failed(session, asset, message)

    stats.soft_deleted += await _mark_missing_deleted(session, album, seen_urls)
    await _upsert_sync_state(session, album, started_at, len(seen_urls), full=full)


def _chunk_by_size(
    assets: list[SourceAsset], max_images: int, max_bytes: int
) -> list[list[SourceAsset]]:
    """按张数和字节数双上限切批。

    只按张数切的话，一批 32 张 8MB 的原图会在内存里堆出 256MB，再加上 embedding
    服务端解码后的像素数组，很容易 OOM。size_bytes 未知时按保守估计计入。
    """
    assumed = 4 * 1024 * 1024
    batches: list[list[SourceAsset]] = []
    current: list[SourceAsset] = []
    current_bytes = 0

    for asset in assets:
        size = asset.size_bytes or assumed
        too_many = len(current) >= max_images
        too_big = current and current_bytes + size > max_bytes
        if too_many or too_big:
            batches.append(current)
            current, current_bytes = [], 0
        current.append(asset)
        current_bytes += size

    if current:
        batches.append(current)
    return batches


async def _process_batch(
    session: AsyncSession,
    adapter: SourceAdapter,
    embedding: EmbeddingClient,
    settings: Settings,
    album: str,
    batch: list[SourceAsset],
    stats: IngestStats,
) -> None:
    loaded: list[_Loaded] = []
    for asset in batch:
        try:
            item = await _load_asset(adapter, asset)
        except Exception as exc:
            stats.failed += 1
            message = f"{asset.filename}: download {type(exc).__name__}: {exc}"
            stats.errors.append(message)
            log.warning("asset_download_failed", album=album, error_type=type(exc).__name__)
            await _mark_failed(session, asset, message)
            continue
        stats.bytes_downloaded += len(item.payload)
        loaded.append(item)

    if not loaded:
        return

    try:
        results = await embedding.extract_batch(
            [(item.asset.filename, item.payload) for item in loaded]
        )
    except EmbeddingServiceError:
        # embedding 服务挂了是全局故障，不该被当成单张照片失败静默吞掉
        raise

    photo_rows: list[dict[str, Any]] = []
    face_specs: list[tuple[str, ExtractResult, list[bytes | None]]] = []

    for item, result in zip(loaded, results, strict=True):
        if result.error is not None:
            stats.failed += 1
            message = f"{item.asset.filename}: embedding {result.error}"
            stats.errors.append(message)
            await _mark_failed(session, item.asset, message)
            continue

        thumbnail, thumb_w, thumb_h = _resolve_thumbnail(item, settings, stats)
        photo_rows.append(
            {
                "album": album,
                "photo_url": item.asset.photo_url,
                "thumbnail": thumbnail,
                "thumbnail_width": thumb_w,
                "thumbnail_height": thumb_h,
                "kind": item.asset.kind,
                "processing_status": "embedded",
                "processing_error": None,
                "face_count": len(result.faces),
                "faces_discarded": result.discarded,
                "embedded_at": dt.datetime.now(tz=dt.UTC),
                "deleted_at": None,
            }
        )
        # 人脸小图必须在这里裁 —— 原图字节马上会被 loaded.clear() 释放。
        # 单张裁剪失败不拖垮整张照片（thumb 可空，前端有占位）。
        face_thumbs: list[bytes | None] = []
        for f in result.faces:
            try:
                face_thumbs.append(crop_face(item.payload, f.bbox))
            except Exception as exc:
                log.warning("face_thumb_failed", error_type=type(exc).__name__)
                face_thumbs.append(None)
        face_specs.append((item.asset.photo_url, result, face_thumbs))

    # 尽早释放这一批的原图字节。大相册下这能显著压低内存峰值。
    loaded.clear()

    if not photo_rows:
        return

    url_to_id = await _upsert_photos(session, photo_rows)
    await _replace_faces(session, url_to_id, face_specs, stats)
    await session.commit()


async def _load_asset(adapter: SourceAdapter, asset: SourceAsset) -> _Loaded:
    """把原图读进内存。

    不落盘 —— 批量推理需要把字节 POST 给 embedding 服务，写临时文件只是多一次
    往返，还会给「不缓存原图」这条纪律留缺口。
    """
    chunks: list[bytes] = []
    async for chunk in adapter.open_asset(asset):
        chunks.append(chunk)
    payload = b"".join(chunks)

    # 源站已提供缩略图就直接用它的字节，省掉一次本地重编码
    source_thumb = await adapter.fetch_thumbnail(asset)
    return _Loaded(asset=asset, payload=payload, source_thumbnail=source_thumb)


def _resolve_thumbnail(
    item: _Loaded, settings: Settings, stats: IngestStats
) -> tuple[bytes | None, int | None, int | None]:
    """缩略图：源站给了就用源站的，没给才本地生成。"""
    if item.source_thumbnail:
        stats.thumbs_from_source += 1
        # 源站缩略图的尺寸未知且不重要（前端按 CSS 显示），不额外解码去量
        return item.source_thumbnail, None, None

    try:
        data, width, height = make_thumbnail(
            item.payload, settings.thumb_max_edge, settings.thumb_quality
        )
    except Exception as exc:
        log.warning("thumbnail_failed", error_type=type(exc).__name__)
        return None, None, None

    stats.thumbs_generated += 1
    return data, width, height


async def _upsert_photos(session: AsyncSession, rows: list[dict[str, Any]]) -> dict[str, uuid.UUID]:
    """一次 upsert 整批照片，返回 photo_url → id。

    批量 RETURNING 而不是逐张 RETURNING：一批 32 张就省掉 31 次往返。
    """
    now = dt.datetime.now(tz=dt.UTC)
    insert_stmt = pg_insert(Photo).values(rows)
    upsert = insert_stmt.on_conflict_do_update(
        index_elements=[Photo.photo_url],
        set_={
            "album": insert_stmt.excluded.album,
            "thumbnail": insert_stmt.excluded.thumbnail,
            "thumbnail_width": insert_stmt.excluded.thumbnail_width,
            "thumbnail_height": insert_stmt.excluded.thumbnail_height,
            "kind": insert_stmt.excluded.kind,
            "processing_status": insert_stmt.excluded.processing_status,
            "processing_error": insert_stmt.excluded.processing_error,
            "face_count": insert_stmt.excluded.face_count,
            "faces_discarded": insert_stmt.excluded.faces_discarded,
            "embedded_at": insert_stmt.excluded.embedded_at,
            # 源站上重新出现的照片要撤销软删除
            "deleted_at": None,
            "updated_at": now,
        },
    ).returning(Photo.id, Photo.photo_url)

    result = (await session.execute(upsert)).all()
    return {row.photo_url: row.id for row in result}


async def _replace_faces(
    session: AsyncSession,
    url_to_id: dict[str, uuid.UUID],
    specs: list[tuple[str, ExtractResult, list[bytes | None]]],
    stats: IngestStats,
) -> None:
    photo_ids = [url_to_id[url] for url, _, _ in specs if url in url_to_id]
    if not photo_ids:
        return

    # 重跑时先清掉这些照片的旧人脸再写新的 —— 保证幂等，不累积重复向量。
    await session.execute(
        text("DELETE FROM face WHERE photo_id = ANY(CAST(:ids AS uuid[]))"),
        {"ids": [str(pid) for pid in photo_ids]},
    )

    face_rows: list[dict[str, Any]] = []
    for url, result, face_thumbs in specs:
        photo_id = url_to_id.get(url)
        if photo_id is None:
            continue
        for f, thumb in zip(result.faces, face_thumbs, strict=True):
            face_rows.append(
                {
                    "photo_id": photo_id,
                    "embedding": f.embedding,
                    "bbox_x": f.bbox[0],
                    "bbox_y": f.bbox[1],
                    "bbox_w": f.bbox[2],
                    "bbox_h": f.bbox[3],
                    "face_px": f.face_px,
                    "det_score": f.det_score,
                    "model_name": f.model_name,
                    "model_version": f.model_version,
                    "dim": f.dim,
                    "thumb": thumb,
                }
            )
        stats.faces_discarded += result.discarded

    if face_rows:
        # executemany 一次写入整批人脸
        await session.execute(pg_insert(Face), face_rows)
        stats.faces_added += len(face_rows)

    stats.processed += len(photo_ids)


async def _process_video(
    session: AsyncSession,
    adapter: SourceAdapter,
    embedding: EmbeddingClient,
    settings: Settings,
    album: str,
    asset: SourceAsset,
    stats: IngestStats,
    batch_images: int,
) -> None:
    """单个视频：下载到临时文件 → 抽帧 → 批量提取 → tracklet 聚合 → 落库。

    帧不是「原图缓存」：临时文件在 finally 里删除，帧 JPEG 随处理释放
    （只保留各段最清晰帧，出人脸小图用）。见 plans/0008。
    """
    import asyncio
    import tempfile
    from pathlib import Path

    from jobs.video import TrackletBuilder, probe_duration, sample_frames

    # 源站页面若标了体积，超限的连下载都省了
    if asset.size_bytes and asset.size_bytes > settings.video_max_bytes:
        await _register_skipped(session, asset, reason="video_too_big")
        stats.skipped_video += 1
        return

    tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115 - 路径要跨 with 块存活
        suffix=Path(asset.filename).suffix or ".mp4", delete=False
    )
    tmp_path = Path(tmp.name)
    tmp.close()
    try:
        size = 0
        # 本地磁盘的分块写是微秒级的页缓存操作；jobs 是批处理 CLI 不是服务，
        # 为它引入 aiofiles 不值得
        with tmp_path.open("wb") as fh:  # noqa: ASYNC230
            async for chunk in adapter.open_asset(asset):
                size += len(chunk)
                if size > settings.video_max_bytes:
                    await _register_skipped(session, asset, reason="video_too_big")
                    stats.skipped_video += 1
                    return
                fh.write(chunk)
        stats.bytes_downloaded += size

        info = await asyncio.to_thread(probe_duration, tmp_path)
        if info.duration_ms > settings.video_max_duration_s * 1000:
            await _register_skipped(session, asset, reason="video_too_long")
            stats.skipped_video += 1
            return

        builder = TrackletBuilder(
            gap_ms=int(settings.video_track_gap_s * 1000),
            sim_threshold=settings.video_track_sim,
            iou_threshold=settings.video_track_iou,
        )
        # 各帧的 JPEG 暂存于此；每批后按 builder.referenced_frames() 收缩，
        # 上限 = tracklet 数 + 1（poster），不随视频长度增长
        frame_jpegs: dict[int, bytes] = {}
        poster_jpeg: bytes | None = None
        discarded = 0
        frames_total = 0

        # 抽帧是 CPU 解码活，放线程池；主协程消费并分块送 GPU
        frame_iter = await asyncio.to_thread(
            lambda: list(
                sample_frames(tmp_path, settings.video_sample_fps, settings.video_max_frames)
            )
        )
        for i in range(0, len(frame_iter), batch_images):
            chunk_frames = frame_iter[i : i + batch_images]
            results = await embedding.extract_batch(
                [(f"{asset.filename}@{fr.t_ms}", fr.jpeg) for fr in chunk_frames]
            )
            for fr, result in zip(chunk_frames, results, strict=True):
                frames_total += 1
                if poster_jpeg is None:
                    poster_jpeg = fr.jpeg
                if result.error is not None:
                    # 单帧解码/检测失败不拖垮整个视频，也不值得标 failed 重试
                    log.warning("video_frame_error", t_ms=fr.t_ms)
                    continue
                discarded += result.discarded
                if result.faces:
                    frame_jpegs[fr.t_ms] = fr.jpeg
                    builder.feed(fr.t_ms, result.faces)
            # 释放不再被引用的帧
            keep = builder.referenced_frames()
            frame_jpegs = {t: j for t, j in frame_jpegs.items() if t in keep}
        frame_iter.clear()

        tracklets = builder.finalize()
        stats.video_frames += frames_total

        # poster：源站给了缩略图就用，没给用第一帧
        source_thumb = await adapter.fetch_thumbnail(asset)
        if source_thumb:
            thumbnail, thumb_w, thumb_h = source_thumb, None, None
            stats.thumbs_from_source += 1
        elif poster_jpeg is not None:
            data, width, height = make_thumbnail(
                poster_jpeg, settings.thumb_max_edge, settings.thumb_quality
            )
            thumbnail, thumb_w, thumb_h = data, width, height
            stats.thumbs_generated += 1
        else:
            thumbnail, thumb_w, thumb_h = None, None, None

        photo_rows = [
            {
                "album": album,
                "photo_url": asset.photo_url,
                "thumbnail": thumbnail,
                "thumbnail_width": thumb_w,
                "thumbnail_height": thumb_h,
                "kind": "video",
                "duration_ms": info.duration_ms or None,
                "processing_status": "embedded",
                "processing_error": None,
                "face_count": len(tracklets),
                "faces_discarded": discarded,
                "embedded_at": dt.datetime.now(tz=dt.UTC),
                "deleted_at": None,
            }
        ]
        url_to_id = await _upsert_photos(session, photo_rows)
        photo_id = url_to_id[asset.photo_url]

        await session.execute(
            text("DELETE FROM face WHERE photo_id = :pid"), {"pid": str(photo_id)}
        )
        face_rows: list[dict[str, Any]] = []
        for tr in tracklets:
            thumb: bytes | None = None
            best_jpeg = frame_jpegs.get(tr.best_t_ms)
            if best_jpeg is not None:
                try:
                    thumb = crop_face(best_jpeg, tr.best.bbox)
                except Exception as exc:
                    log.warning("face_thumb_failed", error_type=type(exc).__name__)
            face_rows.append(
                {
                    "photo_id": photo_id,
                    "embedding": tr.mean_embedding(),
                    "bbox_x": tr.best.bbox[0],
                    "bbox_y": tr.best.bbox[1],
                    "bbox_w": tr.best.bbox[2],
                    "bbox_h": tr.best.bbox[3],
                    "face_px": tr.best.face_px,
                    "det_score": tr.best.det_score,
                    "model_name": tr.best.model_name,
                    "model_version": tr.best.model_version,
                    "dim": tr.best.dim,
                    "thumb": thumb,
                    "t_start_ms": tr.t_start_ms,
                    "t_end_ms": tr.t_end_ms,
                }
            )
        if face_rows:
            await session.execute(pg_insert(Face), face_rows)
        await session.commit()

        stats.videos_processed += 1
        stats.video_segments += len(face_rows)
        stats.faces_discarded += discarded
        log.info(
            "video_ingested",
            album=album,
            frames=frames_total,
            segments=len(face_rows),
            duration_ms=info.duration_ms,
        )
    finally:
        tmp_path.unlink(missing_ok=True)  # noqa: ASYNC240 —— 同上，本地删文件微秒级


async def _videos_to_skip(session: AsyncSession, album: str, urls: list[str]) -> set[str]:
    """增量模式下不再碰的视频：已入库的，以及因超长/超大被判为终态跳过的。

    与照片不同，视频的「跳过」也算终态 —— 否则每晚定时任务都会白下载一遍
    超限的大视频。想重新纳入就调大上限后跑 `--full`。
    """
    if not urls:
        return set()
    rows = (
        await session.execute(
            text(
                """
                SELECT photo_url FROM photo
                WHERE album = :album
                  AND deleted_at IS NULL
                  AND photo_url = ANY(CAST(:urls AS text[]))
                  AND (
                        processing_status = 'embedded'
                     OR (processing_status = 'skipped'
                         AND processing_error IN ('video_too_long', 'video_too_big'))
                  )
                """
            ),
            {"album": album, "urls": urls},
        )
    ).all()
    return {row.photo_url for row in rows}


async def _already_embedded(session: AsyncSession, album: str, urls: list[str]) -> set[str]:
    """已成功入库的照片，增量模式下跳过。

    注意：源站是追加式的照片墙，同一个 URL 的内容基本不会变，所以这里只看
    「有没有处理成功过」，不做内容校验。真需要重算时用 `--full`。
    之前失败过的（status != 'embedded'）不在返回集里，会被自动重试。
    """
    if not urls:
        return set()
    rows = (
        await session.execute(
            text(
                """
                SELECT photo_url FROM photo
                WHERE album = :album
                  AND processing_status = 'embedded'
                  AND deleted_at IS NULL
                  AND photo_url = ANY(CAST(:urls AS text[]))
                """
            ),
            {"album": album, "urls": urls},
        )
    ).all()
    return {row.photo_url for row in rows}


async def _register_skipped(session: AsyncSession, asset: SourceAsset, reason: str) -> None:
    now = dt.datetime.now(tz=dt.UTC)
    values = {
        "album": asset.album,
        "photo_url": asset.photo_url,
        "kind": asset.kind,
        "processing_status": "skipped",
        "processing_error": reason,
    }
    stmt = pg_insert(Photo).values(**values)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[Photo.photo_url],
            set_={"processing_status": "skipped", "processing_error": reason, "updated_at": now},
        )
    )
    await session.commit()


async def _mark_failed(session: AsyncSession, asset: SourceAsset, message: str) -> None:
    """记录失败但不阻塞后续照片。下次运行会自动重试。"""
    await session.rollback()
    now = dt.datetime.now(tz=dt.UTC)
    values = {
        "album": asset.album,
        "photo_url": asset.photo_url,
        "kind": asset.kind,
        "processing_status": "failed",
        "processing_error": message[:2000],
    }
    stmt = pg_insert(Photo).values(**values)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[Photo.photo_url],
            set_={
                "processing_status": "failed",
                "processing_error": message[:2000],
                "updated_at": now,
            },
        )
    )
    await session.commit()


async def _mark_missing_deleted(session: AsyncSession, album: str, seen_urls: set[str]) -> int:
    """把源站已不存在的照片软删除。

    软删除而非物理删除：便于排查「照片怎么突然搜不到了」，也给误删留恢复窗口。
    """
    if not seen_urls:
        return 0
    result = await session.execute(
        text(
            """
            UPDATE photo SET deleted_at = now(), updated_at = now()
            WHERE album = :album
              AND deleted_at IS NULL
              AND photo_url <> ALL(CAST(:seen AS text[]))
            """
        ),
        {"album": album, "seen": list(seen_urls)},
    )
    await session.commit()
    return cast("CursorResult[Any]", result).rowcount or 0


async def _upsert_sync_state(
    session: AsyncSession, album: str, started_at: dt.datetime, photo_count: int, *, full: bool
) -> None:
    now = dt.datetime.now(tz=dt.UTC)
    params = {
        "album": album,
        # 记的是本轮开始的时间而不是结束时间 —— 否则运行期间新增的照片会落进
        # 「已同步」的时间窗里被永久跳过。
        "started_at": started_at,
        "photo_count": photo_count,
        "full_at": now if full else None,
        "now": now,
    }
    await session.execute(
        text(
            """
            INSERT INTO album_sync_state (album, last_synced_at, last_full_sync_at,
                                          photo_count, updated_at)
            VALUES (:album, :started_at, :full_at, :photo_count, :now)
            ON CONFLICT (album) DO UPDATE SET
                last_synced_at    = EXCLUDED.last_synced_at,
                last_full_sync_at = COALESCE(EXCLUDED.last_full_sync_at,
                                             album_sync_state.last_full_sync_at),
                photo_count       = EXCLUDED.photo_count,
                updated_at        = EXCLUDED.updated_at
            """
        ),
        params,
    )
    await session.commit()
