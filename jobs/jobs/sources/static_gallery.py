"""photos.zrc.sg（自建/静态相册）adapter —— 占位实现，待填充。

⚠️ 这个文件目前**不可用**。填充它需要 docs/data-source.md 里列出的答案，尤其是：
  · album 列表是 HTML 还是有 JSON 索引 / 目录 autoindex
  · 原图与相册页的 URL 模板
  · 什么可以作为稳定的 source_asset_id（路径？内容 hash？）
  · 是否需要鉴权，是否存在 private 相册
  · 有没有 ETag / Last-Modified 供增量同步使用

在这些答案到位之前，用 `local_dir` adapter 推进其余全部工作（SOURCE_ADAPTER=local_dir）。

已经确定的抓取纪律写在下面的骨架里，填充时不要绕过它们：
限速、并发上限、指数退避、可识别 UA、流式下载、单张失败不中断整批。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from collections.abc import AsyncIterator

import httpx

from gallery_core.logging import get_logger
from jobs.sources.base import SourceAlbum, SourceAsset

log = get_logger(__name__)
_CHUNK = 256 * 1024


class RateLimiter:
    """全局请求间隔控制。别把自己家的图库打挂。"""

    def __init__(self, per_second: float) -> None:
        self._min_interval = 1.0 / per_second if per_second > 0 else 0.0
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        async with self._lock:
            wait = self._last + self._min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()


class StaticGalleryAdapter:
    def __init__(
        self,
        base_url: str,
        token: str = "",
        user_agent: str = "zrc-face-search/0.1",
        concurrency: int = 4,
        rate_limit_per_second: float = 5.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        headers = {"User-Agent": user_agent}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
        )
        self._semaphore = asyncio.Semaphore(concurrency)
        self._limiter = RateLimiter(rate_limit_per_second)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, *, attempts: int = 4) -> httpx.Response:
        """带限速与指数退避的 GET。5xx/超时重试，4xx 不重试。"""
        delay = 1.0
        last_exc: Exception | None = None
        for attempt in range(attempts):
            await self._limiter.acquire()
            async with self._semaphore:
                try:
                    resp = await self._client.get(path)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_exc = exc
                else:
                    if resp.status_code < 500:
                        return resp
                    last_exc = httpx.HTTPStatusError(
                        f"{resp.status_code}", request=resp.request, response=resp
                    )
            if attempt < attempts - 1:
                log.warning("source_retry", path=path, attempt=attempt + 1, delay=delay)
                await asyncio.sleep(delay)
                delay *= 2
        assert last_exc is not None
        raise last_exc

    # ---------------------------------------------------------------------
    # 以下三个方法待填充
    # ---------------------------------------------------------------------

    async def list_albums(self) -> list[SourceAlbum]:
        raise NotImplementedError(
            "待填充：需要先确定 photos.zrc.sg 的相册列表获取方式。见 docs/data-source.md。"
            " 在此之前请用 SOURCE_ADAPTER=local_dir。"
        )

    def list_assets(
        self, album_id: str, since: dt.datetime | None = None
    ) -> AsyncIterator[SourceAsset]:
        raise NotImplementedError(
            "待填充：需要先确定相册内资产的列举方式与稳定唯一标识。见 docs/data-source.md。"
        )

    async def open_asset(self, asset: SourceAsset) -> AsyncIterator[bytes]:
        """流式下载。这一段的实现方式与源站关系不大，可以直接用。"""
        await self._limiter.acquire()
        async with self._semaphore, self._client.stream("GET", asset.url) as resp:
            resp.raise_for_status()
            async for chunk in resp.aiter_bytes(_CHUNK):
                yield chunk

    def build_original_url(self, asset: SourceAsset, ttl_seconds: int) -> str:
        # 待确认源站是否支持签名链接。若不支持，则由 api 侧的 /original 302
        # 承担访问控制，且必须确认源站本身不允许被直接遍历。
        return asset.url
