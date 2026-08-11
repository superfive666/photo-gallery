"""源站抽象。

`pipeline.py` 只依赖这里的协议，不知道 photos.zrc.sg 的页面长什么样。源站改版只需
替换 adapter 实现。源站细节不得泄漏到 `jobs/sources/` 之外。

已知：photos.zrc.sg 是公开、无需鉴权的静态照片墙，相册地址形如
`https://photos.zrc.sg/album/2026-08-10`，album slug 即数据库里的 `album` 字段。
仍未确认的是相册页的具体标记结构，见 docs/data-source.md。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Literal, Protocol, runtime_checkable

from pydantic.dataclasses import dataclass

# album 就是 URL 里那一段 slug，本身即业务标识，不需要额外的 SourceAlbum 元数据对象。
# 长度上限与 DDL 的 VARCHAR(200) 一致。
ALBUM_MAX_LEN = 200


@dataclass(frozen=True)
class SourceAsset:
    """源站的一个资产。`photo_url` 即幂等写入的唯一键。"""

    album: str
    filename: str
    # 原图（original quality）的完整地址
    photo_url: str
    kind: Literal["image", "video"] = "image"
    # 源站若已提供缩略图，记下它的地址 —— 直接下载其字节入库，省一次本地重编码。
    # 为 None 时由 jobs/thumbnails.py 从原图生成。
    thumbnail_url: str | None = None
    size_bytes: int | None = None


@runtime_checkable
class SourceAdapter(Protocol):
    async def list_albums(self) -> list[str]:
        """列出可抓取的 album slug。源站没有索引页时可以返回空列表，
        此时由调用方通过 `--album` 显式指定。"""
        ...

    # 注意这两个方法声明为普通 `def` 而不是 `async def`：
    # 实现是 async generator，调用它直接返回 AsyncIterator，而不是返回一个
    # 「await 之后才拿到 AsyncIterator」的协程。写成 `async def` 会让类型变成
    # Coroutine[..., AsyncIterator[...]]，`async for` 就对不上了。
    def list_assets(self, album: str) -> AsyncIterator[SourceAsset]:
        """列出相册内的资产。"""
        ...

    def open_asset(self, asset: SourceAsset) -> AsyncIterator[bytes]:
        """流式读取原图字节。

        必须是流式的：整块读进内存会在处理大相册时把容器打爆。
        """
        ...

    async def fetch_thumbnail(self, asset: SourceAsset) -> bytes | None:
        """取源站提供的缩略图字节。没有则返回 None，由本地生成。"""
        ...
