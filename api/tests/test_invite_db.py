"""邀请码登录对真实 Postgres 的回归测试（覆盖 002_invite_code 迁移）。

需要 DATABASE_URL 指向一个已跑过迁移的库 —— CI 的 python job 满足；本地没有库时自动跳过。
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from api.app.auth import ROLE_EDIT, ROLE_SEARCH, generate_invite_code
from api.app.routers.session import _resolve_invite
from gallery_core.config import Settings
from gallery_core.models import InviteCode

DATABASE_URL = os.environ.get("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="需要真实 Postgres（CI 的 python job 提供；本地可用 DATABASE_URL 指向测试库）",
)

SETTINGS = Settings(jwt_secret="unit-test-secret-32-bytes-long!!")


async def _with_session(fn: Callable[[AsyncSession], Awaitable[None]]) -> None:
    engine = create_async_engine(DATABASE_URL)
    try:
        async with async_sessionmaker(engine)() as session:
            await fn(session)
    finally:
        await engine.dispose()


async def _cleanup(session: AsyncSession, prefix: str) -> None:
    await session.execute(text("DELETE FROM invite_code WHERE prefix = :p"), {"p": prefix})
    await session.commit()


async def test_scoped_code_resolves_to_bound_album() -> None:
    full, prefix, code_hash = generate_invite_code()

    async def run(session: AsyncSession) -> None:
        session.add(InviteCode(prefix=prefix, code_hash=code_hash, album="2026-08-10"))
        await session.commit()
        try:
            resolved = await _resolve_invite(full, session, SETTINGS)
            assert resolved == ("2026-08-10", ROLE_SEARCH, None)
            # secret 错 → 401
            with pytest.raises(HTTPException) as exc_info:
                await _resolve_invite(f"{prefix}.wrong-secret", session, SETTINGS)
            assert exc_info.value.status_code == 401
        finally:
            await _cleanup(session, prefix)

    await _with_session(run)


async def test_disabled_code_rejected() -> None:
    full, prefix, code_hash = generate_invite_code()

    async def run(session: AsyncSession) -> None:
        session.add(InviteCode(prefix=prefix, code_hash=code_hash, album="2026-08-10"))
        await session.commit()
        await session.execute(
            text("UPDATE invite_code SET disabled_at = now() WHERE prefix = :p"), {"p": prefix}
        )
        await session.commit()
        try:
            with pytest.raises(HTTPException) as exc_info:
                await _resolve_invite(full, session, SETTINGS)
            assert exc_info.value.status_code == 401
        finally:
            await _cleanup(session, prefix)

    await _with_session(run)


async def test_unknown_prefix_rejected() -> None:
    full, prefix, _ = generate_invite_code()

    async def run(session: AsyncSession) -> None:
        # 不插行 —— prefix 未命中也必须是 401（且内部会做时间均衡，这里只验行为）
        with pytest.raises(HTTPException) as exc_info:
            await _resolve_invite(full, session, SETTINGS)
        assert exc_info.value.status_code == 401

    await _with_session(run)
    del prefix


async def test_edit_code_resolves_role_and_workspace() -> None:
    """剪辑码（005 迁移的 role 列）：返回 (相册, edit, workspace_id=行 id)。"""
    full, prefix, code_hash = generate_invite_code()

    async def run(session: AsyncSession) -> None:
        invite = InviteCode(prefix=prefix, code_hash=code_hash, album="2026-08-10", role="edit")
        session.add(invite)
        await session.commit()
        try:
            album, role, wid = await _resolve_invite(full, session, SETTINGS)
            assert (album, role) == ("2026-08-10", ROLE_EDIT)
            assert wid == str(invite.id)
        finally:
            await _cleanup(session, prefix)

    await _with_session(run)
