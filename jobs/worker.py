"""常驻 worker：认领 job_run 队列里的剪辑域任务并执行。

api 只入队；下载、拆条、向量化、LLM 解析、渲染这些重活都在这里跑 ——
api 进程保持轻快，用户掉线不影响任务（断点恢复的基础）。

崩溃语义：任务在自己的事务里执行，失败标 failed 并把项目置 failed + 事件；
worker 被杀掉时任务停在 running，job_run 表本身就是排查现场。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from sqlalchemy import select

from gallery_core.clip_client import ClipClient
from gallery_core.config import Settings, get_settings
from gallery_core.db import session_scope
from gallery_core.jobs_queue import claim_next_job, enqueue_job, finish_job
from gallery_core.logging import get_logger
from gallery_core.models import EditProject, JobRun

log = get_logger(__name__)


async def _fail_project(project_id: uuid.UUID, error: str) -> None:
    from api.app.services.edit_flow import append_event

    async with session_scope() as session:
        project = await session.get(EditProject, project_id)
        if project is None:
            return
        project.status = "failed"
        project.error = error[:500]
        await append_event(session, project, "system", "project_failed", {"error": error[:500]})


async def _run_media_ingest(settings: Settings, job: JobRun) -> dict[str, Any]:
    from api.app.services.edit_flow import append_event
    from jobs.media_ingest import ingest_album_media
    from jobs.sources import build_adapter

    album = job.album or ""
    adapter = build_adapter()
    try:
        # 同 CLI：长活不占连接，写库短事务由 ingest_album_media 内部管理
        async with ClipClient() as clip:
            stats = await ingest_album_media(settings, clip, album, adapter)
    finally:
        aclose = getattr(adapter, "aclose", None)
        if aclose is not None:
            await aclose()

    # 建库完成 → 推进所有等这个相册的项目
    async with session_scope() as session:
        waiting = (
            (
                await session.execute(
                    select(EditProject).where(
                        EditProject.album == album, EditProject.status == "ingesting"
                    )
                )
            )
            .scalars()
            .all()
        )
        for project in waiting:
            project.status = "parsing"
            await append_event(
                session,
                project,
                "system",
                "ingest_done",
                {"album": album, "scenes": stats.scenes_added, "downloaded": stats.downloaded},
            )
            await enqueue_job(session, "project_flow", params={"project_id": str(project.id)})
    return stats.as_dict()


async def _run_project_flow(settings: Settings, job: JobRun) -> dict[str, Any]:
    from api.app.services.edit_flow import run_project_flow

    project_id = uuid.UUID(str(job.params["project_id"]))
    try:
        async with session_scope() as session:
            await run_project_flow(session, settings, project_id)
    except Exception as exc:
        await _fail_project(project_id, f"{type(exc).__name__}: {exc}")
        raise
    return {"project_id": str(project_id)}


async def _run_render(settings: Settings, job: JobRun) -> dict[str, Any]:
    from api.app.services.edit_flow import append_event
    from jobs.render import render_project
    from jobs.sources import build_adapter

    project_id = uuid.UUID(str(job.params["project_id"]))
    # 照片不落盘（006 迁移）：渲染时按 source_url 现下载，需要源站 adapter
    adapter = build_adapter()
    try:
        # 渲染不占连接（ffmpeg 以分钟计，攥着连接会被掐），读写都在 render_project
        # 内部的短事务里完成；这里只在结束后用一个短事务置状态、记事件。
        result = await render_project(settings, project_id, adapter)
        async with session_scope() as session:
            project = await session.get(EditProject, project_id)
            assert project is not None
            project.status = "done"
            await append_event(
                session,
                project,
                "assistant",
                "render_done",
                {"shots": result["shots"], "zip": str(result["zip"]).rsplit("/", 1)[-1]},
            )
    except Exception as exc:
        await _fail_project(project_id, f"{type(exc).__name__}: {exc}")
        raise
    finally:
        aclose = getattr(adapter, "aclose", None)
        if aclose is not None:
            await aclose()
    return {k: v for k, v in result.items() if k != "bytes"} | {"bytes": result["bytes"]}


async def _run_filters_import(settings: Settings, _job: JobRun) -> dict[str, Any]:
    from pathlib import Path

    from jobs.filters import import_filters

    async with session_scope() as session:
        stats = await import_filters(session, Path(settings.luts_dir()))
    return stats.as_dict()


_HANDLERS = {
    "media_ingest": _run_media_ingest,
    "project_flow": _run_project_flow,
    "render": _run_render,
    "filters_import": _run_filters_import,
}


async def run_one(settings: Settings) -> bool:
    """认领并执行一条任务。返回是否有任务被执行。"""
    async with session_scope() as session:
        job = await claim_next_job(session)
        if job is None:
            return False
        job_id, kind = job.id, job.kind
        params, album = dict(job.params), job.album

    log.info("job_claimed", job_id=str(job_id), kind=kind, album=album)
    # 用独立对象承载参数，执行阶段不复用已关闭 session 上的 ORM 实例
    detached = JobRun(id=job_id, kind=kind, album=album, params=params)

    handler = _HANDLERS.get(kind)
    try:
        if handler is None:
            raise ValueError(f"worker 不认识的任务种类: {kind}")
        stats = await handler(settings, detached)
        async with session_scope() as session:
            await finish_job(session, job_id, ok=True, stats=stats)
        log.info("job_done", job_id=str(job_id), kind=kind)
    except Exception as exc:
        async with session_scope() as session:
            await finish_job(session, job_id, ok=False, error=f"{type(exc).__name__}: {exc}")
        log.exception("job_failed", job_id=str(job_id), kind=kind)
    return True


async def run_forever() -> None:
    settings = get_settings()
    log.info("worker_started", poll_seconds=settings.worker_poll_seconds)
    while True:
        busy = await run_one(settings)
        if not busy:
            await asyncio.sleep(settings.worker_poll_seconds)
