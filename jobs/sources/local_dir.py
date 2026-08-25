"""本地目录 adapter。

两个用途：
  · 开发与评估集（`SOURCE_ADAPTER=local_dir` 单源模式，根目录 = SOURCE_LOCAL_DIR）；
  · 本地相册（auto 模式经 CompositeAdapter 路由，根目录 = {media_root}/media，
    与剪辑域原片目录合一，见 plans/0010）。

目录约定（一级子目录名 = album slug）：

    <root>/
      2026-08-10/
        IMG_0001.jpg
      2026-07-20/
        DSC_0002.jpg
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

# URL↔路径换算收敛在 gallery_core（api 的本地原图分发也要用，且 api 不 import jobs）
from gallery_core.local_source import photo_url_for, resolve_local_path
from jobs.sources.base import SourceAsset

__all__ = ["LocalDirAdapter", "photo_url_for"]

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
_VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".webm"}
_CHUNK = 256 * 1024


class LocalDirAdapter:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    async def list_albums(self) -> list[str]:
        if not self._root.is_dir():
            return []
        return sorted(p.name for p in self._root.iterdir() if p.is_dir())

    # 这里用 async def（函数体有 yield，是 async generator）；
    # SourceAdapter 协议那边声明为普通 def —— 两者是匹配的，理由见 base.py 的注释。
    async def list_assets(self, album: str) -> AsyncIterator[SourceAsset]:
        album_dir = self._root / album
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

            relative = path.relative_to(self._root).as_posix()
            yield SourceAsset(
                album=album,
                filename=path.name,
                photo_url=photo_url_for(relative),
                kind=kind,  # type: ignore[arg-type]
                size_bytes=path.stat().st_size,
            )

    async def open_asset(self, asset: SourceAsset) -> AsyncIterator[bytes]:
        path = self._path_of(asset)
        with path.open("rb") as fh:
            while chunk := fh.read(_CHUNK):
                yield chunk

    async def fetch_thumbnail(self, asset: SourceAsset) -> bytes | None:
        # 本地目录不提供现成缩略图，一律本地生成
        return None

    def _path_of(self, asset: SourceAsset) -> Path:
        path = resolve_local_path(self._root, asset.photo_url)
        if path is None:
            # 不是 local://album/ 形态，或解析后越出根目录 —— 都不该发生在
            # 建库流程产出的 URL 上，遇到即说明喂错了 adapter 或数据被篡改
            raise ValueError(f"不是本地相册素材的合法 photo_url: {asset.photo_url}")
        return path
