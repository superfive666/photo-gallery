from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.app.rate_limit import SlidingWindowLimiter
from api.app.routers import albums, health, photos, search, session
from gallery_core.config import get_settings
from gallery_core.embedding_client import EmbeddingClient
from gallery_core.logging import configure_logging, get_logger

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)

    # 单例：复用连接池，避免每请求新建
    app.state.embedding_client = EmbeddingClient()
    app.state.session_limiter = SlidingWindowLimiter(settings.rate_limit_searches_per_hour)
    # IP 维度放宽一些：同一栋楼/同一运营商 NAT 出口下会有多个成员共用 IP
    app.state.ip_limiter = SlidingWindowLimiter(settings.rate_limit_searches_per_hour * 3)
    # 设备维度最紧（默认 3/小时）。清 cookie 可绕过，所以上面两层才是硬边界。
    app.state.device_limiter = SlidingWindowLimiter(
        settings.rate_limit_searches_per_device_per_hour
    )

    if not settings.invite_code_hash:
        # 不 fail-open，但要吵得足够响，避免部署时静默地谁都进不来
        log.warning("invite_code_hash_missing", detail="未配置邀请码，所有登录都会被拒绝")

    insecure = settings.insecure_secrets()
    if insecure:
        # 只警告不中止：本地开发就是用占位值跑的。
        # 但生产必须替换 —— 默认 jwt_secret 意味着任何人都能伪造 session。
        log.warning(
            "placeholder_secrets_in_use",
            fields=insecure,
            detail="这些密钥仍是占位值，生产环境必须替换",
        )

    log.info("api_started")
    try:
        yield
    finally:
        await app.state.embedding_client.aclose()


app = FastAPI(
    title="zrc face search",
    version="0.1.0",
    lifespan=lifespan,
    # 生产不暴露交互式文档：减少暴露面，也避免被误当作公开 API
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# 全部业务路由挂在 /api 下，与 nginx 的反代前缀一致
app.include_router(health.router)
app.include_router(session.router, prefix="/api")
app.include_router(albums.router, prefix="/api")
app.include_router(search.router, prefix="/api")
app.include_router(photos.router, prefix="/api")
