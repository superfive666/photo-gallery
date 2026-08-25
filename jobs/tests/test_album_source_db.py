"""resolve_album_source 对真实 Postgres 的回归测试（plans/0010 路由规则）。

需要 DATABASE_URL 指向一个已跑过迁移的库 —— CI 的 python job 满足；本地没有库时自动跳过。

守卫的核心事故：远端相册跑过剪辑建库后 media/<album>/ 里有视频副本，
若来源判定只看目录，下一次 face ingest 会把它误判成本地相册，
同一相册在库里裂成两套 URL —— 「能跑但结果全错」。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Sequence
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from gallery_core.config import Settings
from jobs.sources import resolve_album_source

DATABASE_URL = os.environ.get("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="需要真实 Postgres（CI 的 python job 提供；本地可用 DATABASE_URL 指向测试库）",
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(media_root=str(tmp_path))


def _album() -> str:
    return f"test-src-{uuid.uuid4().hex[:12]}"


async def _insert_photo(session: AsyncSession, album: str, photo_url: str) -> None:
    await session.execute(
        text("INSERT INTO photo (album, photo_url) VALUES (:album, :url)"),
        {"album": album, "url": photo_url},
    )


async def _insert_media_asset(session: AsyncSession, album: str, source_url: str) -> None:
    await session.execute(
        text(
            "INSERT INTO media_asset (album, source_url, path, kind)"
            " VALUES (:album, :url, NULL, 'video')"
        ),
        {"album": album, "url": source_url},
    )


async def _cleanup(session: AsyncSession, album: str) -> None:
    await session.execute(text("DELETE FROM photo WHERE album = :album"), {"album": album})
    await session.execute(text("DELETE FROM media_asset WHERE album = :album"), {"album": album})
    await session.commit()


async def _resolve_with(
    tmp_path: Path,
    album: str,
    *,
    photo_urls: Sequence[str] = (),
    media_urls: Sequence[str] = (),
    make_dir: bool = False,
) -> str:
    if make_dir:
        (tmp_path / "media" / album).mkdir(parents=True)
    engine = create_async_engine(DATABASE_URL)
    try:
        async with async_sessionmaker(engine)() as session:
            for url in photo_urls:
                await _insert_photo(session, album, url)
            for url in media_urls:
                await _insert_media_asset(session, album, url)
            await session.commit()
            try:
                return await resolve_album_source(session, album, _settings(tmp_path))
            finally:
                await _cleanup(session, album)
    finally:
        await engine.dispose()


async def test_remote_photo_rows_win_over_existing_dir(tmp_path: Path) -> None:
    """规则 1 的核心：远端相册即使目录存在（剪辑建库的视频副本），仍按远端处理。"""
    album = _album()
    source = await _resolve_with(
        tmp_path,
        album,
        photo_urls=[f"https://photos.zrc.sg/album/{album}/a.jpg"],
        make_dir=True,
    )
    assert source == "remote"


async def test_remote_media_asset_rows_win_over_existing_dir(tmp_path: Path) -> None:
    """只跑过剪辑建库（media_asset 有 https 行、photo 表为空）的相册同样是远端。"""
    album = _album()
    source = await _resolve_with(
        tmp_path,
        album,
        media_urls=[f"https://photos.zrc.sg/album/{album}/v.mp4"],
        make_dir=True,
    )
    assert source == "remote"


async def test_local_photo_rows_stay_local_without_dir(tmp_path: Path) -> None:
    """本地相册一旦入库，即使目录被临时卸载也不改道去打源站。"""
    album = _album()
    source = await _resolve_with(tmp_path, album, photo_urls=[f"local://album/{album}/a.jpg"])
    assert source == "local"


async def test_first_ingest_uses_dir_existence(tmp_path: Path) -> None:
    """规则 2：全新相册看目录 —— 这是「指定本地路径素材」的唯一运维动作。"""
    album = _album()
    assert await _resolve_with(tmp_path, album, make_dir=True) == "local"
    assert await _resolve_with(tmp_path, f"{album}-absent") == "remote"


async def test_diverged_album_resolves_remote(tmp_path: Path) -> None:
    """规则 3：photo 表两套 scheme（已分叉）→ 按远端处理，绝不静默改道。"""
    album = _album()
    source = await _resolve_with(
        tmp_path,
        album,
        photo_urls=[
            f"https://photos.zrc.sg/album/{album}/a.jpg",
            f"local://album/{album}/b.jpg",
        ],
        make_dir=True,
    )
    assert source == "remote"
