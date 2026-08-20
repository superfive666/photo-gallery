"""剪辑检索 SQL 对真实 Postgres 的回归测试。

需要 DATABASE_URL 指向一个已跑过迁移的库 —— CI 的 python job 满足
（pgvector service container + 先 migrate 后 pytest）；本地没有库时自动跳过。

覆盖的生产事故：`:min_ms IS NULL OR ... >= :min_ms` 在 asyncpg 的预处理语句下
推断不出参数类型，min_ms/max_ms 不限时长（project_flow 的默认路径）在 prepare
阶段直接抛 AmbiguousParameterError → 剪辑 job 整个失败。空库即可复现：炸点在
prepare，不在数据。与 test_search_sql.py 覆盖的 album 参数是同一类问题。
"""

from __future__ import annotations

import math
import os
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.app.services.edit_search import search_scenes
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


async def test_search_scenes_without_duration_bounds() -> None:
    """min_ms/max_ms 均为 None —— 这正是生产上剪辑 job 炸掉的路径。"""
    engine = create_async_engine(DATABASE_URL)
    try:
        async with async_sessionmaker(engine)() as session:
            hits = await search_scenes(
                session,
                _unit_vector(),
                Settings(),
                album="2026-08-15",
                excluded=[],
            )
        assert hits == []
    finally:
        await engine.dispose()


async def test_search_scenes_with_duration_bounds() -> None:
    """带时长上下限与排除名单的路径同样要能 prepare（CAST 改动不能弄坏正常分支）。"""
    engine = create_async_engine(DATABASE_URL)
    try:
        async with async_sessionmaker(engine)() as session:
            hits = await search_scenes(
                session,
                _unit_vector(),
                Settings(),
                album="2026-08-15",
                excluded=[uuid.uuid4()],
                kind="video",
                min_ms=1000,
                max_ms=8000,
            )
        assert hits == []
    finally:
        await engine.dispose()
