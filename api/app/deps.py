"""FastAPI 依赖。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.app.auth import require_session
from api.app.rate_limit import SlidingWindowLimiter
from gallery_core.config import Settings, get_settings
from gallery_core.db import get_sessionmaker
from gallery_core.embedding_client import EmbeddingClient


def settings_dep() -> Settings:
    return get_settings()


SettingsDep = Annotated[Settings, Depends(settings_dep)]


async def db_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


DbDep = Annotated[AsyncSession, Depends(db_session)]


def embedding_client(request: Request) -> EmbeddingClient:
    """复用 app.state 上的单例客户端，避免每请求新建连接池。"""
    client: EmbeddingClient = request.app.state.embedding_client
    return client


EmbeddingDep = Annotated[EmbeddingClient, Depends(embedding_client)]


def session_id(request: Request, settings: SettingsDep) -> str:
    return require_session(request, settings)


SessionDep = Annotated[str, Depends(session_id)]


def search_limiters(request: Request) -> tuple[SlidingWindowLimiter, SlidingWindowLimiter]:
    state = request.app.state
    return state.session_limiter, state.ip_limiter


LimitersDep = Annotated[tuple[SlidingWindowLimiter, SlidingWindowLimiter], Depends(search_limiters)]


def client_ip(request: Request) -> str:
    """取真实来源 IP。

    生产环境前面有反向代理，所以优先读 X-Forwarded-For 的第一段。
    注意：这个头是可伪造的，只有在确定前置代理会覆写它时才可信 ——
    部署时必须让反代设置（而非追加）该头。
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


ClientIpDep = Annotated[str, Depends(client_ip)]
