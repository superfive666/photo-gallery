"""本地目录 adapter。

用途：开发与评估集。用本地样例照片把「离线建库 → 聚类 → 检索」整条链路跑通，
不依赖源站可用性。

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

from jobs.sources.base import SourceAsset

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
_VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".webm"}
_CHUNK = 256 * 1024

# 与真站保持同样的 URL 形态，这样 photo_url 在两种 adapter 之间语义一致
_LOCAL_SCHEME = "local://album"


def photo_url_for(relative_path: str) -> str:
    """相对根目录的路径（形如 `2026-08-10/IMG_0001.jpg`）→ photo_url。

    评估集要把 labels.csv 里的相对路径对上库里的行，靠的就是这个函数。
    URL 规则只在这里定义一次 —— 两处各写一遍的话，评估会把每张照片都判成
    not_ingested，指标全为 0，而且看起来像「检索坏了」，极容易误诊。
    """
    return f"{_LOCAL_SCHEME}/{relative_path.lstrip('/')}"


class LocalDirAdapter:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

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
        relative = asset.photo_url.removeprefix(f"{_LOCAL_SCHEME}/")
        return self._root / relative
