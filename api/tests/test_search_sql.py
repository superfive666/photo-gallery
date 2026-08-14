"""检索 SQL 对真实 Postgres 的回归测试。

需要 DATABASE_URL 指向一个已跑过迁移的库 —— CI 的 python job 满足
（pgvector service container + 先 migrate 后 pytest）；本地没有库时自动跳过。

覆盖的生产事故：`:album IS NULL OR ph.album = :album` 在 asyncpg 的预处理语句下
推断不出参数类型，album=None（不筛相册 —— 最常见的检索路径）在 prepare 阶段
直接抛 AmbiguousParameterError → 500。空库即可复现：炸点在 prepare，不在数据。
"""

from __future__ import annotations

import math
import os

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.app.services.search import search_by_embedding
from gallery_core.config import Settings

DATABASE_URL = os.environ.get("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="需要真实 Postgres（CI 的 python job 提供；本地可用 DATABASE_URL 指向测试库）",
)


def _unit_vector() -> list[float]:
    vec = [1.0] + [0.0] * 511
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec]


async def test_search_without_album_filter() -> None:
    """album=None 的全局检索不许 500 —— 这正是生产上炸掉的路径。"""
    engine = create_async_engine(DATABASE_URL)
    try:
        async with async_sessionmaker(engine)() as session:
            outcome = await search_by_embedding(session, _unit_vector(), Settings(), album=None)
        assert outcome.matches == []
    finally:
        await engine.dispose()


async def test_search_with_album_filter() -> None:
    """带相册筛选的路径同样要能 prepare（CAST 改动不能弄坏原本正常的分支）。"""
    engine = create_async_engine(DATABASE_URL)
    try:
        async with async_sessionmaker(engine)() as session:
            outcome = await search_by_embedding(
                session, _unit_vector(), Settings(), album="2026-08-12"
            )
        assert outcome.matches == []
    finally:
        await engine.dispose()
