"""源站抽象。

`pipeline.py` 只依赖这里的协议，不知道 photos.zrc.sg 长什么样。换源站或源站改版
只需替换 adapter 实现。源站细节不得泄漏到 `jobs/sources/` 之外。

photos.zrc.sg 的具体接入方式尚未确定，待答问题见 docs/data-source.md。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class SourceAlbum:
    id: str
    name: str
    url: str | None = None
    event_date: dt.date | None = None
    # 源站若有可见性概念，必须如实映射：非 public 的相册不进检索库。
    # 拿不到确切信息时用 'unknown'，pipeline 会跳过而不是当作公开。
    visibility: Literal["public", "private", "unknown"] = "unknown"


@dataclass(frozen=True, slots=True)
class SourceAsset:
    """源站的一个资产。`id` 必须在源站内稳定唯一 —— 它是幂等写入的依据。"""

    id: str
    album_id: str
    filename: str
    url: str
    kind: Literal["image", "video"] = "image"
    # etag / md5。源站没有则由 adapter 退化为 f"{size}:{mtime}" 弱校验。
    # 为 None 时 pipeline 无法判断是否变更，只能每次重新处理。
    checksum: str | None = None
    size_bytes: int | None = None
    taken_at: dt.datetime | None = None
    width: int | None = None
    height: int | None = None


@runtime_checkable
class SourceAdapter(Protocol):
    async def list_albums(self) -> list[SourceAlbum]: ...

    # 注意这两个方法声明为普通 `def` 而不是 `async def`：
    # 实现是 async generator，调用它直接返回 AsyncIterator，而不是返回一个
    # 「await 之后才拿到 AsyncIterator」的协程。写成 `async def` 会让类型变成
    # Coroutine[..., AsyncIterator[...]]，`async for` 就对不上了。
    def list_assets(
        self, album_id: str, since: dt.datetime | None = None
    ) -> AsyncIterator[SourceAsset]:
        """列出相册内的资产。`since` 用于增量；源站不支持时 adapter 可忽略它。"""
        ...

    def open_asset(self, asset: SourceAsset) -> AsyncIterator[bytes]:
        """流式读取原始字节。

        必须是流式的：整块读进内存会在处理大相册时把容器打爆。
        """
        ...

    def build_original_url(self, asset: SourceAsset, ttl_seconds: int) -> str:
        """给前端用的原图地址。若源站支持签名链接，在这里生成短效签名。"""
        ...
