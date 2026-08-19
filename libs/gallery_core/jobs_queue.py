"""剪辑域的任务队列：job_run 表复用为队列（status='queued' 即待执行）。

api 只入队（enqueue_job），常驻的 `jobs worker` 用 FOR UPDATE SKIP LOCKED 认领执行 ——
不引入 Redis/Celery：单机部署、任务量是「每个剪辑项目几条」的量级，Postgres 够用，
且 job_run 本来就是离线任务的第一现场。
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from gallery_core.models import JobRun

# 剪辑域的队列任务种类（001 的 ingest/recompute 不走队列，保持原样）
QUEUE_KINDS = ("media_ingest", "project_flow", "render", "filters_import")


async def enqueue_job(
    session: AsyncSession,
    kind: str,
    *,
    album: str | None = None,
    params: dict[str, Any] | None = None,
    dedupe: bool = False,
) -> uuid.UUID | None:
    """入队。dedupe=True 时，同 kind+album+params 已有 queued/running 的任务则不重复入队
    （同一相册被多个项目并发引用时防止重复下载）。返回 job id；去重命中返回 None。"""
    if kind not in QUEUE_KINDS:
        raise ValueError(f"未知的队列任务种类: {kind}")

    if dedupe:
        existing = await session.execute(
            text(
                """
                SELECT id FROM job_run
                WHERE kind = :kind
                  AND status IN ('queued', 'running')
                  AND album IS NOT DISTINCT FROM :album
                  AND params = CAST(:params AS jsonb)
                LIMIT 1
                """
            ),
            {"kind": kind, "album": album, "params": _to_json(params)},
        )
        if existing.first() is not None:
            return None

    run = JobRun(kind=kind, album=album, status="queued", stats={}, params=params or {})
    session.add(run)
    await session.flush()
    return run.id


async def claim_next_job(session: AsyncSession) -> JobRun | None:
    """认领一条待执行任务（queued → running）。

    FOR UPDATE SKIP LOCKED：多个 worker 并存时不会抢到同一条；
    认领即在同一事务里置 running，被杀掉的 worker 的任务会停在 running ——
    排查手段是 job_run 表本身（它就是离线任务的第一现场）。
    """
    row = (
        await session.execute(
            text(
                """
                SELECT id FROM job_run
                WHERE status = 'queued'
                ORDER BY started_at
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """
            )
        )
    ).first()
    if row is None:
        return None

    run = await session.get(JobRun, row[0])
    assert run is not None
    run.status = "running"
    run.started_at = dt.datetime.now(tz=dt.UTC)
    await session.flush()
    return run


async def finish_job(
    session: AsyncSession,
    job_id: uuid.UUID,
    *,
    ok: bool,
    stats: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    run = await session.get(JobRun, job_id)
    if run is None:
        return
    run.status = "succeeded" if ok else "failed"
    run.finished_at = dt.datetime.now(tz=dt.UTC)
    if stats is not None:
        run.stats = stats
    run.error = error


def _to_json(params: dict[str, Any] | None) -> str:
    import json

    return json.dumps(params or {}, ensure_ascii=False, sort_keys=True)
