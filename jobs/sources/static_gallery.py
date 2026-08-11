"""photos.zrc.sg —— 公开的静态照片墙 adapter。

## 已确认

- 站点公开，无需鉴权。
- 相册地址：`{SOURCE_BASE_URL}/album/{slug}`，例如 `https://photos.zrc.sg/album/2026-08-10`。
- 相册里同时有照片和视频。

## 仍未确认（决定 `_parse_album_page` 的最终形态）

相册页的具体标记结构。所以这里实现的是**按优先级依次尝试**的通用解析：

1. `/album/{slug}/index.json`、`/album/{slug}.json`、`?format=json` —— 若站点有结构化索引，
   优先用它，比解析 HTML 稳定得多。
2. HTML：抓 `<a href>` 里指向图片/视频后缀的链接作为原图，抓同一元素内的 `<img src>`
   作为缩略图；再兜底扫全页的 `<img src>`。

**先用 `jobs probe --album <slug>` 对着真站跑一次**，它会打印解析到的结构和样例条目。
拿到实际输出后再把这里收敛成精确的选择器 —— 通用解析只是让工作能立刻推进，
不是最终形态。

抓取纪律（无论最终解析怎么写都要遵守）：限速、并发上限、指数退避、可识别 UA、
流式下载、单张失败不中断整批。
"""

from __future__ import annotations

import asyncio
import posixpath
import time
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import httpx
from lxml import html as lxml_html

from gallery_core.logging import get_logger
from jobs.sources.base import SourceAsset

log = get_logger(__name__)

_CHUNK = 256 * 1024

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".avif"}
_VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".avi"}

# 常见的缩略图路径特征。命中这些的链接不会被当成原图。
_THUMB_HINTS = ("thumb", "thumbnail", "small", "preview", "_t.", "/tn/", "resized")

_JSON_CANDIDATES = ("index.json", "album.json")


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


def _looks_like_thumbnail(url: str) -> bool:
    lowered = url.lower()
    return any(hint in lowered for hint in _THUMB_HINTS)


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
            try:
                tree = lxml_html.fromstring(resp.text)
            # 这个候选路径返回的不是 HTML 就换下一个，不是错误。
            except Exception as exc:
                log.debug("album_index_unparsable", path=path, error_type=type(exc).__name__)
                continue
            for href in tree.xpath("//a/@href"):
                slug = self._slug_from_href(str(href))
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
        json_assets = await self._try_json_index(album)
        if json_assets is not None:
            log.info("album_parsed", album=album, via="json", count=len(json_assets))
            return json_assets

        page_url = self.album_url(album)
        resp = await self._get(page_url)
        resp.raise_for_status()
        assets = self._parse_album_page(album, page_url, resp.text)
        log.info("album_parsed", album=album, via="html", count=len(assets))
        return assets

    async def _try_json_index(self, album: str) -> list[SourceAsset] | None:
        """如果站点提供 JSON 索引就用它 —— 比解析 HTML 稳定得多。"""
        candidates = [f"/album/{album}/{name}" for name in _JSON_CANDIDATES]
        candidates.append(f"/album/{album}?format=json")

        for path in candidates:
            try:
                resp = await self._get(path)
            except httpx.HTTPError:
                continue
            if resp.status_code != 200:
                continue
            if "json" not in resp.headers.get("content-type", "").lower():
                continue
            try:
                payload = resp.json()
            except ValueError:
                continue

            assets = self._parse_json_payload(album, str(resp.url), payload)
            if assets:
                return assets
        return None

    def _parse_json_payload(self, album: str, base: str, payload: Any) -> list[SourceAsset]:
        """从 JSON 索引里抽资产。

        字段名未知，所以对常见命名做兼容。拿到真实结构后应收敛成精确读取。
        """
        items: Any = payload
        if isinstance(payload, dict):
            for key in ("items", "photos", "assets", "files", "data", "media"):
                if isinstance(payload.get(key), list):
                    items = payload[key]
                    break
        if not isinstance(items, list):
            return []

        assets: list[SourceAsset] = []
        for item in items:
            if isinstance(item, str):
                url = urljoin(base, item)
                thumb = None
            elif isinstance(item, dict):
                raw = _first_str(item, ("url", "src", "original", "full", "path", "file"))
                if raw is None:
                    continue
                url = urljoin(base, raw)
                raw_thumb = _first_str(item, ("thumbnail", "thumb", "preview", "small"))
                thumb = urljoin(base, raw_thumb) if raw_thumb else None
            else:
                continue

            kind = _kind_of(url)
            if kind is None:
                continue
            assets.append(
                SourceAsset(
                    album=album,
                    filename=posixpath.basename(unquote(urlparse(url).path)) or "unnamed",
                    photo_url=url,
                    kind=kind,  # type: ignore[arg-type]
                    thumbnail_url=thumb,
                )
            )
        return assets

    def _parse_album_page(self, album: str, page_url: str, body: str) -> list[SourceAsset]:
        """从相册 HTML 里抽资产。

        策略：先看 `<a href>`（静态相册通常用链接指向原图，`<img>` 才是缩略图），
        没有可用链接时退回扫 `<img src>`。命中缩略图特征的链接不会被当成原图。
        """
        tree = lxml_html.fromstring(body)
        assets: list[SourceAsset] = []

        for anchor in tree.xpath("//a[@href]"):
            href = urljoin(page_url, str(anchor.get("href")))
            kind = _kind_of(href)
            if kind is None or _looks_like_thumbnail(href):
                continue
            # 链接内部的 <img> 就是这张照片的缩略图
            inner = anchor.xpath(".//img/@src")
            thumb = urljoin(page_url, str(inner[0])) if inner else None
            assets.append(
                SourceAsset(
                    album=album,
                    filename=posixpath.basename(unquote(urlparse(href).path)) or "unnamed",
                    photo_url=href,
                    kind=kind,  # type: ignore[arg-type]
                    thumbnail_url=thumb,
                )
            )

        if assets:
            return assets

        # 兜底：页面直接把原图放在 <img src> 里
        for src in tree.xpath("//img/@src"):
            url = urljoin(page_url, str(src))
            if _kind_of(url) != "image":
                continue
            assets.append(
                SourceAsset(
                    album=album,
                    filename=posixpath.basename(unquote(urlparse(url).path)) or "unnamed",
                    photo_url=url,
                    kind="image",
                )
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


def _first_str(item: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return None
