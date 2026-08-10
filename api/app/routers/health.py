from __future__ import annotations

from fastapi import APIRouter, Response, status

from api.app.deps import DbDep, EmbeddingDep
from gallery_core.db import ping

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """存活探针。不查依赖 —— 依赖故障不应导致容器被反复重启。"""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(db: DbDep, embedding: EmbeddingDep, response: Response) -> dict[str, object]:
    """就绪探针。DB 与 embedding 服务任一不可用即 503，部署流程靠它判断是否回滚。"""
    checks: dict[str, bool] = {}
    try:
        checks["db"] = await ping(db)
    except Exception:
        checks["db"] = False
    checks["embedding"] = await embedding.healthy()

    ok = all(checks.values())
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if ok else "degraded", "checks": checks}
