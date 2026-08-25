"""本地相册原图分发对真实 Postgres 的回归测试（plans/0010）。

需要 DATABASE_URL 指向一个已跑过迁移的库 —— CI 的 python job 满足；本地没有库时自动跳过。

覆盖三条边界：远端 302 行为无回归；本地相册流式分发；库里 photo_url 被构造成
越界路径时 api 必须 404 且不读根外文件（防御纵深 —— 即使建库流程绝不产出这种行）。
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from api.app.auth import SessionInfo
from api.app.routers.photos import original
from gallery_core.config import Settings

DATABASE_URL = os.environ.get("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="需要真实 Postgres（CI 的 python job 提供；本地可用 DATABASE_URL 指向测试库）",
)


def _session_info(album: str | None) -> SessionInfo:
    return SessionInfo(sid="test", album=album, dev="test-dev")


async def _insert_photo(session: AsyncSession, album: str, photo_url: str) -> uuid.UUID:
    row = (
        await session.execute(
            text("INSERT INTO photo (album, photo_url) VALUES (:album, :url) RETURNING id"),
            {"album": album, "url": photo_url},
        )
    ).one()
    await session.commit()
    return uuid.UUID(str(row.id))


async def _run(
    tmp_path: Path, album: str, photo_url: str, *, viewer_album: str | None = None
) -> object:
    settings = Settings(media_root=str(tmp_path))
    engine = create_async_engine(DATABASE_URL)
    try:
        async with async_sessionmaker(engine)() as session:
            photo_id = await _insert_photo(session, album, photo_url)
            try:
                return await original(
                    photo_id, session, _session_info(viewer_album or album), settings
                )
            finally:
                await session.execute(
                    text("DELETE FROM photo WHERE id = :id"), {"id": str(photo_id)}
                )
                await session.commit()
    finally:
        await engine.dispose()


def _album() -> str:
    return f"test-orig-{uuid.uuid4().hex[:12]}"


async def test_remote_photo_still_redirects(tmp_path: Path) -> None:
    album = _album()
    url = f"https://photos.zrc.sg/album/{album}/a.jpg"
    response = await _run(tmp_path, album, url)
    assert isinstance(response, RedirectResponse)
    assert response.status_code == 302
    assert response.headers["location"] == url


async def test_local_photo_is_served_from_media_dir(tmp_path: Path) -> None:
    album = _album()
    target = tmp_path / "media" / album / "IMG_0001.jpg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"\xff\xd8\xffjpeg-bytes")

    response = await _run(tmp_path, album, f"local://album/{album}/IMG_0001.jpg")
    assert isinstance(response, FileResponse)
    assert Path(response.path) == target.resolve()
    assert response.headers["cache-control"] == "private, max-age=604800"


async def test_local_photo_missing_file_is_404(tmp_path: Path) -> None:
    album = _album()
    with pytest.raises(HTTPException) as exc:
        await _run(tmp_path, album, f"local://album/{album}/gone.jpg")
    assert exc.value.status_code == 404


async def test_traversal_photo_url_is_404(tmp_path: Path) -> None:
    """库里的行被构造成 ../ 逃逸也读不到根外文件 —— 统一 404，不区分原因。"""
    album = _album()
    secret = tmp_path / "secret.txt"
    secret.write_text("outside media root")
    with pytest.raises(HTTPException) as exc:
        await _run(tmp_path, album, "local://album/../secret.txt")
    assert exc.value.status_code == 404


async def test_album_scope_still_enforced_for_local(tmp_path: Path) -> None:
    """A 相册的码拿不到 B（本地）相册的原图 —— 越权仍统一 404。"""
    album = _album()
    target = tmp_path / "media" / album / "x.jpg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")
    with pytest.raises(HTTPException) as exc:
        await _run(tmp_path, album, f"local://album/{album}/x.jpg", viewer_album="other-album")
    assert exc.value.status_code == 404
