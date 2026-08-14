"""photos.zrc.sg —— 公开的静态照片墙 adapter。

## 页面结构（2026-08-14 以真实相册页确认，此前的通用猜测式解析已删除）

相册页 `{SOURCE_BASE_URL}/album/{slug}` 返回服务端渲染的 HTML，每个媒体项是
一个带 `data-lightbox` 属性的 `<div>`（站点自己的 ZephyrLightbox 组件），
全部信息都在 data-* 属性里：

    <div class="group relative ..." data-lightbox
         data-id="20260812215627863"
         data-src="/album/2026-08-12/20260812215627863.mp4"
         data-original-src="/album/2026-08-12/20260812215627863.mp4"
         data-thumb="/album/thumb/2026-08-12/20260812215627863.mp4"
         data-is-video="true"
         data-uploader="Stone" ...>

映射关系：
    photo_url     ← data-original-src（缺失时退回 data-src）
    thumbnail_url ← data-thumb
    kind          ← data-is-video（"true" → video；另用扩展名兜底）
视频照常产出（kind="video"）—— pipeline 对视频只登记不提取，这是既有语义。

页面上找不到任何 data-lightbox 节点视为**页面结构变更**，打 error 日志并返回空，
让 ingest 报告的 0 计数把问题暴露出来，而不是退回猜测式解析给出错误结果。

抓取纪律：限速、并发上限、指数退避、可识别 UA、流式下载、单张失败不中断整批。
"""

from __future__ import annotations

import asyncio
import posixpath
import time
from collections.abc import AsyncIterator
from urllib.parse import unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from bs4.element import Tag

from gallery_core.logging import get_logger
from jobs.sources.base import SourceAsset

log = get_logger(__name__)

_CHUNK = 256 * 1024

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".avif"}
_VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".avi"}


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


def _suffix_of(url: str) -> str:
    path = unquote(urlparse(url).path)
    return posixpath.splitext(path)[1].lower()


def _kind_of(url: str) -> str | None:
    suffix = _suffix_of(url)
    if suffix in _IMAGE_SUFFIXES:
        return "image"
    if suffix in _VIDEO_SUFFIXES:
        return "video"
    return None


class StaticGalleryAdapter:
    def __init__(
        self,
        base_url: str,
        user_agent: str = "zrc-face-search/0.1",
        concurrency: int = 4,
        rate_limit_per_second: float = 5.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"User-Agent": user_agent},
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
        )
        self._semaphore = asyncio.Semaphore(concurrency)
        self._limiter = RateLimiter(rate_limit_per_second)

    async def aclose(self) -> None:
        await self._client.aclose()

    def album_url(self, album: str) -> str:
        return f"{self._base_url}/album/{album}"

    # ------------------------------------------------------------------ HTTP

    async def _get(self, url: str, *, attempts: int = 4) -> httpx.Response:
        """带限速与指数退避的 GET。5xx/超时重试，4xx 不重试。"""
        delay = 1.0
        last_exc: Exception | None = None
        for attempt in range(attempts):
            await self._limiter.acquire()
            async with self._semaphore:
                try:
                    resp = await self._client.get(url)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_exc = exc
                else:
                    if resp.status_code < 500:
                        return resp
                    last_exc = httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}", request=resp.request, response=resp
                    )
            if attempt < attempts - 1:
                log.warning("source_retry", url=url, attempt=attempt + 1, delay=delay)
                await asyncio.sleep(delay)
                delay *= 2
        assert last_exc is not None
        raise last_exc

    # ---------------------------------------------------------------- 相册列表

    async def list_albums(self) -> list[str]:
        """尽力发现相册列表。

        源站是否有索引页尚未确认，所以失败不算错误 —— 返回空列表，
        由调用方用 `--album` 显式指定。
        """
        for path in ("/album/", "/albums", "/"):
            try:
                resp = await self._get(path)
            except httpx.HTTPError:
                continue
            if resp.status_code != 200:
                continue

            slugs: list[str] = []
            seen: set[str] = set()
            soup = BeautifulSoup(resp.text, "lxml")
            for anchor in soup.find_all("a", href=True):
                if not isinstance(anchor, Tag):
                    continue
                slug = self._slug_from_href(str(anchor["href"]))
                if slug and slug not in seen:
                    seen.add(slug)
                    slugs.append(slug)
            if slugs:
                log.info("albums_discovered", path=path, count=len(slugs))
                return sorted(slugs)

        log.warning(
            "album_discovery_failed",
            detail="没找到相册索引页，请用 --album 指定。见 docs/data-source.md。",
        )
        return []

    @staticmethod
    def _slug_from_href(href: str) -> str | None:
        parts = [p for p in unquote(urlparse(href).path).split("/") if p]
        if "album" not in parts:
            return None
        index = parts.index("album")
        if index + 1 >= len(parts):
            return None
        slug = parts[index + 1]
        # 排除把文件名当 slug 的情况
        if posixpath.splitext(slug)[1]:
            return None
        return slug if len(slug) <= 200 else None

    # ---------------------------------------------------------------- 资产列表

    async def list_assets(self, album: str) -> AsyncIterator[SourceAsset]:
        assets = await self._collect_assets(album)
        seen: set[str] = set()
        for asset in assets:
            if asset.photo_url in seen:
                continue
            seen.add(asset.photo_url)
            yield asset

    async def _collect_assets(self, album: str) -> list[SourceAsset]:
        page_url = self.album_url(album)
        resp = await self._get(page_url)
        resp.raise_for_status()
        assets = self._parse_album_page(album, page_url, resp.text)
        log.info("album_parsed", album=album, count=len(assets))
        return assets

    def _parse_album_page(self, album: str, page_url: str, body: str) -> list[SourceAsset]:
        """从相册 HTML 里抽资产。结构契约见模块 docstring。"""
        soup = BeautifulSoup(body, "lxml")
        nodes = soup.find_all(attrs={"data-lightbox": True})

        assets: list[SourceAsset] = []
        for node in nodes:
            if not isinstance(node, Tag):
                continue
            raw = _attr(node, "data-original-src") or _attr(node, "data-src")
            if raw is None:
                log.warning("lightbox_item_without_src", album=album, id=_attr(node, "data-id"))
                continue
            url = urljoin(page_url, raw)

            # data-is-video 是权威来源；扩展名兜底（属性缺失或将来改名时仍能分对）
            is_video = (_attr(node, "data-is-video") or "").strip().lower() == "true"
            kind = "video" if is_video or _kind_of(url) == "video" else "image"

            raw_thumb = _attr(node, "data-thumb")
            assets.append(
                SourceAsset(
                    album=album,
                    filename=posixpath.basename(unquote(urlparse(url).path)) or "unnamed",
                    photo_url=url,
                    kind=kind,  # type: ignore[arg-type]
                    thumbnail_url=urljoin(page_url, raw_thumb) if raw_thumb else None,
                )
            )

        if not assets:
            # 一个都没解析到 = 页面结构变了（或 slug 不存在）。宁可 0 计数报警，
            # 也不退回猜测式解析给出「能跑但结果全错」。
            log.error(
                "album_page_no_lightbox_items",
                album=album,
                url=page_url,
                hint="页面里没有 data-lightbox 节点。源站结构变更？用 jobs probe 核对。",
            )
        return assets

    # ------------------------------------------------------------------ 下载

    async def open_asset(self, asset: SourceAsset) -> AsyncIterator[bytes]:
        """流式下载原图。"""
        await self._limiter.acquire()
        async with self._semaphore, self._client.stream("GET", asset.photo_url) as resp:
            resp.raise_for_status()
            async for chunk in resp.aiter_bytes(_CHUNK):
                yield chunk

    async def fetch_thumbnail(self, asset: SourceAsset) -> bytes | None:
        """源站已提供缩略图就直接取其字节，省掉一次本地重编码。

        取不到不算错误 —— 调用方会从原图生成。
        """
        if not asset.thumbnail_url:
            return None
        try:
            resp = await self._get(asset.thumbnail_url)
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        return resp.content


def _attr(node: Tag, name: str) -> str | None:
    value = node.get(name)
    return value if isinstance(value, str) and value else None
