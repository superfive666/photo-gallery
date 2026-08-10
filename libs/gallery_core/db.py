"""异步引擎与 session 工厂。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from gallery_core.config import get_settings


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    s = get_settings()
    return create_async_engine(
        s.database_url,
        pool_size=s.db_pool_size,
        max_overflow=s.db_max_overflow,
        pool_pre_ping=True,
        echo=False,
    )


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """一个事务边界。异常回滚，正常提交。"""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def apply_search_tuning(session: AsyncSession) -> None:
    """为当前事务调高 HNSW 的查询召回参数。

    `hnsw.ef_search` 默认 40，在本项目的数据量级下偏低 —— 会漏掉本该命中的邻居。
    调高到 100 左右仍是毫秒级延迟，值得为召回率买单。具体取值由评估集决定。

    必须用 SET LOCAL（事务级），不能用 SET —— 后者会污染整个连接池里的这条连接，
    影响之后复用它的所有请求。
    """
    ef = get_settings().hnsw_ef_search
    await session.execute(text(f"SET LOCAL hnsw.ef_search = {int(ef)}"))


async def ping(session: AsyncSession) -> bool:
    await session.execute(text("SELECT 1"))
    return True
