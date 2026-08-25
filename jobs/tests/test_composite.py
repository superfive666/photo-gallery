"""CompositeAdapter（auto 模式）的路由守卫。

两条纪律，破坏任何一条都是「能跑但结果全错」级别的事故：
  · 取字节按 URL scheme 分发 —— 混存时 face-thumbs 回填/渲染重下载不会拿错源；
  · list_assets 的目录兜底只认一级子目录名，slug 形态异常一律按远端处理。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from jobs.sources.base import SourceAsset
from jobs.sources.composite import CompositeAdapter
from jobs.sources.local_dir import LocalDirAdapter


class StubRemote:
    """最小可用的远端替身，记录被调用的 URL。"""

    def __init__(self, albums: list[str] | None = None, fail_listing: bool = False) -> None:
        self.albums = albums or []
        self.fail_listing = fail_listing
        self.opened: list[str] = []
        self.closed = False

    async def list_albums(self) -> list[str]:
        if self.fail_listing:
            raise RuntimeError("remote down")
        return self.albums

    async def list_assets(self, album: str) -> AsyncIterator[SourceAsset]:
        yield SourceAsset(
            album=album, filename="r.jpg", photo_url=f"https://photos.zrc.sg/album/{album}/r.jpg"
        )

    async def open_asset(self, asset: SourceAsset) -> AsyncIterator[bytes]:
        self.opened.append(asset.photo_url)
        yield b"remote-bytes"

    async def fetch_thumbnail(self, asset: SourceAsset) -> bytes | None:
        return b"remote-thumb"

    async def aclose(self) -> None:
        self.closed = True


def _composite(root: Path, remote: StubRemote | None = None) -> tuple[CompositeAdapter, StubRemote]:
    stub = remote or StubRemote()
    return CompositeAdapter(LocalDirAdapter(root), stub), stub


async def test_list_assets_prefers_local_dir(tmp_path: Path) -> None:
    (tmp_path / "2026-08-10").mkdir()
    (tmp_path / "2026-08-10" / "a.jpg").write_bytes(b"x")
    adapter, _ = _composite(tmp_path)

    assets = [a async for a in adapter.list_assets("2026-08-10")]
    assert [a.photo_url for a in assets] == ["local://album/2026-08-10/a.jpg"]


async def test_list_assets_falls_back_to_remote(tmp_path: Path) -> None:
    adapter, _ = _composite(tmp_path)
    assets = [a async for a in adapter.list_assets("no-such-dir")]
    assert [a.photo_url for a in assets] == ["https://photos.zrc.sg/album/no-such-dir/r.jpg"]


async def test_open_asset_dispatches_by_scheme(tmp_path: Path) -> None:
    (tmp_path / "alb").mkdir()
    (tmp_path / "alb" / "l.jpg").write_bytes(b"local-bytes")
    adapter, stub = _composite(tmp_path)

    local_asset = SourceAsset(album="alb", filename="l.jpg", photo_url="local://album/alb/l.jpg")
    remote_asset = SourceAsset(
        album="other", filename="r.jpg", photo_url="https://photos.zrc.sg/album/other/r.jpg"
    )
    local_bytes = b"".join([c async for c in adapter.open_asset(local_asset)])
    remote_bytes = b"".join([c async for c in adapter.open_asset(remote_asset)])

    assert local_bytes == b"local-bytes"
    assert remote_bytes == b"remote-bytes"
    # 本地资产绝不能打到远端（反向拿错源就是静默的错误数据）
    assert stub.opened == ["https://photos.zrc.sg/album/other/r.jpg"]


async def test_fetch_thumbnail_dispatches_by_scheme(tmp_path: Path) -> None:
    adapter, _ = _composite(tmp_path)
    local_asset = SourceAsset(album="alb", filename="l.jpg", photo_url="local://album/alb/l.jpg")
    remote_asset = SourceAsset(
        album="o", filename="r.jpg", photo_url="https://photos.zrc.sg/album/o/r.jpg"
    )
    # 本地没有现成缩略图（由 jobs 生成）；远端取源站字节
    assert await adapter.fetch_thumbnail(local_asset) is None
    assert await adapter.fetch_thumbnail(remote_asset) == b"remote-thumb"


async def test_list_albums_unions_and_degrades(tmp_path: Path) -> None:
    (tmp_path / "local-album").mkdir()
    adapter, _ = _composite(tmp_path, StubRemote(albums=["remote-album", "local-album"]))
    assert await adapter.list_albums() == ["local-album", "remote-album"]

    # 远端故障 → 降级为仅本地，不抛异常（全量建库不该被源站抖动整个打断）
    degraded, _ = _composite(tmp_path, StubRemote(fail_listing=True))
    assert await degraded.list_albums() == ["local-album"]


@pytest.mark.parametrize("album", ["", ".", "..", "a/b", "../x", "a\\b"])
def test_album_dir_exists_rejects_non_slugs(tmp_path: Path, album: str) -> None:
    adapter, _ = _composite(tmp_path)
    assert adapter.album_dir_exists(album) is False


async def test_for_source_and_aclose(tmp_path: Path) -> None:
    adapter, stub = _composite(tmp_path)
    assert isinstance(adapter.for_source("local"), LocalDirAdapter)
    assert adapter.for_source("remote") is stub
    await adapter.aclose()
    assert stub.closed is True
