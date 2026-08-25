"""相册粒度的来源路由 adapter（`SOURCE_ADAPTER=auto`，plans/0010）。

同时持有 LocalDirAdapter（根目录 = {media_root}/media）与 StaticGalleryAdapter，
自身不做任何 IO 之外的决策状态：

  · `open_asset` / `fetch_thumbnail` **按 URL scheme 分发** —— 拿存量 URL 取字节的
    场景（face-thumbs 回填、渲染重下载）天然支持本地/远端混存。
  · `list_assets` 只实现「目录存在 → 本地」这一条（路由规则 2）——
    它是无 DB 依赖的兜底，给 probe 这类没有库可查的调用方用。
    **建库入口必须先走 `resolve_album_source`（规则 1：库里记录的 scheme 优先）**，
    再用 `for_source()` 拿对应的子 adapter —— 否则远端相册跑过剪辑建库后，
    media/<album>/ 里的视频副本会让它被误判成本地相册。
"""

from __future__ import annotations

from asyncio import to_thread
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal

from gallery_core.local_source import is_local_url
from gallery_core.logging import get_logger
from jobs.sources.base import SourceAdapter, SourceAsset
from jobs.sources.local_dir import LocalDirAdapter

log = get_logger(__name__)

AlbumSource = Literal["local", "remote"]


class CompositeAdapter:
    def __init__(self, local: LocalDirAdapter, remote: SourceAdapter) -> None:
        self._local = local
        self._remote = remote

    async def aclose(self) -> None:
        aclose = getattr(self._remote, "aclose", None)
        if aclose is not None:
            await aclose()

    def for_source(self, source: AlbumSource) -> SourceAdapter:
        """按 `resolve_album_source` 的判定结果取子 adapter（列资产用）。"""
        return self._local if source == "local" else self._remote

    def album_dir_exists(self, album: str) -> bool:
        """`{root}/<album>` 是否为已存在的一级子目录。

        slug 里出现路径分隔符/相对段一律按「不存在」处理 —— slug 的约定就是
        一级子目录名，其余形态既不该来自源站也不该来自运维。
        """
        if not album or "/" in album or "\\" in album or album in {".", ".."}:
            return False
        return (Path(self._local_root) / album).is_dir()

    @property
    def _local_root(self) -> Path:
        return self._local.root

    # ------------------------------------------------------------------ 列表

    async def list_albums(self) -> list[str]:
        """本地一级子目录 ∪ 远端相册列表。

        远端故障降级为仅本地：全量建库不该被源站的一次抖动整个打断，
        少列出的远端相册由下一次运行补上。
        """
        local = set(await self._local.list_albums())
        try:
            remote = set(await self._remote.list_albums())
        except Exception as exc:
            log.warning(
                "remote_album_listing_failed",
                error_type=type(exc).__name__,
                detail="降级为仅本地相册。远端相册请稍后重跑或用 --album 指定。",
            )
            remote = set()
        return sorted(local | remote)

    async def list_assets(self, album: str) -> AsyncIterator[SourceAsset]:
        """无 DB 依赖的兜底路由（规则 2）。建库入口请改用
        resolve_album_source + for_source，理由见模块 docstring。"""
        exists = await to_thread(self.album_dir_exists, album)
        sub = self._local if exists else self._remote
        async for asset in sub.list_assets(album):
            yield asset

    # ------------------------------------------------------------------ 字节

    def _by_url(self, url: str) -> SourceAdapter:
        return self._local if is_local_url(url) else self._remote

    async def open_asset(self, asset: SourceAsset) -> AsyncIterator[bytes]:
        async for chunk in self._by_url(asset.photo_url).open_asset(asset):
            yield chunk

    async def fetch_thumbnail(self, asset: SourceAsset) -> bytes | None:
        return await self._by_url(asset.photo_url).fetch_thumbnail(asset)
