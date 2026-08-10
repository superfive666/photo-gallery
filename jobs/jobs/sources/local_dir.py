"""本地目录 adapter。

用途：在 photos.zrc.sg 的接入方式确定之前，用本地样例照片把「离线建库 → 聚类 → 检索」
整条链路和评估流程完全跑通。源站的未知因此不阻塞任何其他工作。

目录约定（一级子目录 = 一个相册）：

    <root>/
      2024-annual-dinner/
        IMG_0001.jpg
      2025-orientation/
        DSC_0002.jpg
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

from jobs.sources.base import SourceAlbum, SourceAsset

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
_VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi"}
_CHUNK = 256 * 1024


class LocalDirAdapter:
    def __init__(self, root: str | Path, base_url: str = "file://") -> None:
        self._root = Path(root)
        self._base_url = base_url.rstrip("/")

    async def list_albums(self) -> list[SourceAlbum]:
        if not self._root.is_dir():
            return []
        albums: list[SourceAlbum] = []
        for child in sorted(p for p in self._root.iterdir() if p.is_dir()):
            albums.append(
                SourceAlbum(
                    id=child.name,
                    name=child.name,
                    url=f"{self._base_url}/{child.name}",
                    event_date=_date_from_name(child.name),
                    # 本地目录没有可见性概念，开发场景下按 public 处理
                    visibility="public",
                )
            )
        return albums

    # 这里用 async def（函数体有 yield，是 async generator）；
    # SourceAdapter 协议那边声明为普通 def —— 两者是匹配的，理由见 base.py 的注释。
    async def list_assets(
        self, album_id: str, since: dt.datetime | None = None
    ) -> AsyncIterator[SourceAsset]:
        album_dir = self._root / album_id
        if not album_dir.is_dir():
            return
        for path in sorted(album_dir.rglob("*")):
            if not path.is_file():
                continue
            suffix = path.suffix.lower()
            if suffix in _IMAGE_SUFFIXES:
                kind = "image"
            elif suffix in _VIDEO_SUFFIXES:
                kind = "video"
            else:
                continue

            stat = path.stat()
            mtime = dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.UTC)
            if since is not None and mtime <= since:
                continue

            rel = path.relative_to(self._root).as_posix()
            yield SourceAsset(
                # 用相对路径的 hash 而非路径本身：路径可能很长，且 hash 更适合做唯一键。
                # 注意这意味着相册被整理/改名后 id 会变 —— 真实源站 adapter 若有这个问题，
                # 应改用文件内容 hash。见 docs/data-source.md 问题 3。
                id=hashlib.sha256(rel.encode()).hexdigest(),
                album_id=album_id,
                filename=path.name,
                url=f"{self._base_url}/{rel}",
                kind=kind,  # type: ignore[arg-type]
                checksum=f"{stat.st_size}:{int(stat.st_mtime)}",
                size_bytes=stat.st_size,
                taken_at=mtime,
            )

    async def open_asset(self, asset: SourceAsset) -> AsyncIterator[bytes]:
        rel = asset.url.removeprefix(f"{self._base_url}/")
        path = self._root / rel
        with path.open("rb") as fh:
            while chunk := fh.read(_CHUNK):
                yield chunk

    def build_original_url(self, asset: SourceAsset, ttl_seconds: int) -> str:
        # 本地开发不做签名
        return asset.url


def _date_from_name(name: str) -> dt.date | None:
    """从 `2024-annual-dinner` 这类目录名里抽出日期前缀。"""
    parts = name.split("-")
    if len(parts) >= 3:
        try:
            return dt.date(int(parts[0]), int(parts[1]), int(parts[2]))
        except ValueError:
            pass
    if parts and len(parts[0]) == 4 and parts[0].isdigit():
        try:
            return dt.date(int(parts[0]), 1, 1)
        except ValueError:
            return None
    return None
