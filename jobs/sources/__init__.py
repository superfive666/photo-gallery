"""源站 adapter 的工厂与相册来源判定。pipeline / media_ingest / worker 都从这里拿实现。"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from gallery_core.config import Settings, get_settings
from gallery_core.logging import get_logger
from jobs.sources.base import SourceAdapter
from jobs.sources.composite import AlbumSource, CompositeAdapter
from jobs.sources.local_dir import LocalDirAdapter
from jobs.sources.static_gallery import StaticGalleryAdapter

log = get_logger(__name__)

__all__ = ["AlbumSource", "CompositeAdapter", "build_adapter", "resolve_album_source"]


def _build_remote() -> StaticGalleryAdapter:
    s = get_settings()
    return StaticGalleryAdapter(
        base_url=s.source_base_url,
        user_agent=s.source_user_agent,
        concurrency=s.source_concurrency,
        rate_limit_per_second=s.source_rate_limit_per_second,
    )


def build_adapter() -> SourceAdapter:
    """按 SOURCE_ADAPTER 选择实现。默认 auto = 相册粒度路由（plans/0010）。"""
    s = get_settings()
    if s.source_adapter == "local_dir":
        return LocalDirAdapter(s.source_local_dir)
    if s.source_adapter == "static_gallery":
        return _build_remote()
    return CompositeAdapter(LocalDirAdapter(s.local_albums_root()), _build_remote())


# 库里已有记录的相册，按 photo_url / source_url 的 scheme 判来源。
# 软删除的行也算证据 —— 相册「曾经从哪来」不随删除而改变。
_ALBUM_EVIDENCE_SQL = text(
    """
    SELECT
      EXISTS (SELECT 1 FROM photo
              WHERE album = :album AND photo_url NOT LIKE 'local://%')       AS photo_remote,
      EXISTS (SELECT 1 FROM photo
              WHERE album = :album AND photo_url LIKE 'local://%')           AS photo_local,
      EXISTS (SELECT 1 FROM media_asset
              WHERE album = :album AND source_url NOT LIKE 'local://%')      AS media_remote,
      EXISTS (SELECT 1 FROM media_asset
              WHERE album = :album AND source_url LIKE 'local://%')          AS media_local
    """
)


async def resolve_album_source(
    session: AsyncSession, album: str, settings: Settings
) -> AlbumSource:
    """判定一个相册的素材来源（plans/0010 路由规则）。

    1. **库里已有记录 → 记录的 scheme 说了算**（photo 与 media_asset 两张表都算证据，
       远端证据优先）。这一条挡住关键陷阱：远端相册跑过剪辑建库后
       media/<album>/ 里会有下载的视频副本，只看目录会把它误判成本地相册，
       同一相册在库里裂成两套 URL。
    2. 首次入库：media/<album>/ 目录存在 → local，否则 remote。
    3. 两种 scheme 同时在库（已经分叉）→ 沿用 remote 并告警，绝不静默改道。
    """
    row = (await session.execute(_ALBUM_EVIDENCE_SQL, {"album": album})).one()
    remote_evidence = bool(row.photo_remote or row.media_remote)
    local_evidence = bool(row.photo_local or row.media_local)

    if remote_evidence:
        # 只有 photo 表出现两套 scheme 才算真分叉。media_asset 里远端相册带
        # local:// 行是既有的正常形态（手动拷入的剪辑素材走合成键），不告警。
        if row.photo_local and row.photo_remote:
            log.warning(
                "album_source_diverged",
                album=album,
                detail="photo 表同时存在远端与 local:// 记录，按远端处理。需要人工核对数据。",
            )
        return "remote"
    if local_evidence:
        return "local"

    is_flat_slug = bool(album) and "/" not in album and "\\" not in album
    album_dir = Path(settings.local_albums_root()) / album
    if is_flat_slug and album not in {".", ".."} and album_dir.is_dir():
        log.info("album_source_local", album=album)
        return "local"
    return "remote"
