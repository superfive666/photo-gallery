"""剪辑域接口。全部要求 edit 角色；一切数据按 JWT 里的 workspace_id / album 隔离。

聊天窗前端的数据来源：
  · GET /edit/projects            会话列表
  · GET /edit/projects/{id}/events?after_seq=N   时间线增量拉取（轮询）
  · 其余是评审动作与只读资源（候选缩略图、滤镜预览、成片下载）
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from api.app.auth import EditSession, require_edit_session
from api.app.deps import CsrfDep, DbDep, SettingsDep
from api.app.services import edit_flow
from api.app.services.edit_flow import FlowError
from gallery_core.models import (
    EditProject,
    FilterPreset,
    ProjectEvent,
    RenderOutput,
    Scene,
    Shot,
    ShotCandidate,
)

router = APIRouter(prefix="/edit", tags=["edit"])


def edit_session(request: Request, settings: SettingsDep) -> EditSession:
    return require_edit_session(request, settings)


EditDep = Annotated[EditSession, Depends(edit_session)]


def _raise(exc: FlowError) -> HTTPException:
    return HTTPException(status_code=exc.http_status, detail=str(exc))


# ------------------------------------------------------------------ 滤镜库


class FilterOut(BaseModel):
    slug: str
    display_name: str
    builtin: bool


@router.get("/filters", response_model=list[FilterOut])
async def list_filters(db: DbDep, _es: EditDep) -> list[FilterOut]:
    rows = (
        (
            await db.execute(
                select(FilterPreset)
                .where(FilterPreset.enabled.is_(True))
                .order_by(FilterPreset.builtin.desc(), FilterPreset.slug)
            )
        )
        .scalars()
        .all()
    )
    return [FilterOut(slug=r.slug, display_name=r.display_name, builtin=r.builtin) for r in rows]


@router.get("/filters/{slug}/preview")
async def filter_preview(slug: str, db: DbDep, _es: EditDep) -> Response:
    row = (
        await db.execute(select(FilterPreset).where(FilterPreset.slug == slug))
    ).scalar_one_or_none()
    if row is None or row.preview is None:
        raise HTTPException(status_code=404, detail="滤镜不存在")
    return Response(
        content=row.preview,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=86400", "ETag": row.checksum[:16]},
    )


# ------------------------------------------------------------------ 项目


class ProjectCreateIn(BaseModel):
    script: str = Field(min_length=1, max_length=50_000)


class ProjectSummary(BaseModel):
    id: str
    title: str
    album: str
    status: str
    current_round: int
    state_version: int
    created_at: str
    updated_at: str


def _summary(p: EditProject) -> ProjectSummary:
    return ProjectSummary(
        id=str(p.id),
        title=p.title,
        album=p.album,
        status=p.status,
        current_round=p.current_round,
        state_version=p.state_version,
        created_at=p.created_at.isoformat(),
        updated_at=p.updated_at.isoformat(),
    )


@router.get("/projects", response_model=list[ProjectSummary])
async def list_projects(db: DbDep, es: EditDep) -> list[ProjectSummary]:
    rows = (
        (
            await db.execute(
                select(EditProject)
                .where(EditProject.workspace_id == es.workspace_id)
                .order_by(EditProject.created_at.desc())
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    return [_summary(p) for p in rows]


@router.post("/projects", response_model=ProjectSummary)
async def create_project(
    body: ProjectCreateIn, db: DbDep, es: EditDep, settings: SettingsDep, _csrf: CsrfDep
) -> ProjectSummary:
    try:
        project = await edit_flow.create_project(
            db, settings, es.workspace_id, es.album, body.script
        )
        await db.commit()
    except FlowError as exc:
        raise _raise(exc) from exc
    return _summary(project)


class CandidateOut(BaseModel):
    id: str
    scene_id: str
    rank: int
    similarity: float
    quality: float
    final_score: float
    status: str
    kind: str
    start_ms: int
    end_ms: int
    in_ms: int | None
    out_ms: int | None


class ShotOut(BaseModel):
    id: str
    idx: int
    source_text: str
    description: str
    queries: list[str]
    media_kind: str
    filter_slug: str | None
    locked: bool
    locked_candidate_id: str | None
    feedback: str | None
    round_no: int
    candidates: list[CandidateOut]


class ProjectDetail(ProjectSummary):
    script: str
    default_filter_slug: str | None
    error: str | None
    shots: list[ShotOut]


async def _load_project(db: DbDep, es: EditSession, project_id: uuid.UUID) -> EditProject:
    project = (
        await db.execute(
            select(EditProject).where(
                EditProject.id == project_id, EditProject.workspace_id == es.workspace_id
            )
        )
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return project


@router.get("/projects/{project_id}", response_model=ProjectDetail)
async def project_detail(project_id: uuid.UUID, db: DbDep, es: EditDep) -> ProjectDetail:
    project = await _load_project(db, es, project_id)

    shots = (
        (await db.execute(select(Shot).where(Shot.project_id == project.id).order_by(Shot.idx)))
        .scalars()
        .all()
    )
    shot_ids = [s.id for s in shots]
    candidates: dict[uuid.UUID, list[tuple[ShotCandidate, Scene]]] = {}
    if shot_ids:
        rows = await db.execute(
            select(ShotCandidate, Scene)
            .join(Scene, Scene.id == ShotCandidate.scene_id)
            .where(
                ShotCandidate.shot_id.in_(shot_ids),
                ShotCandidate.round_no == project.current_round,
            )
            .order_by(ShotCandidate.rank)
        )
        for cand, scene in rows.tuples():
            candidates.setdefault(cand.shot_id, []).append((cand, scene))

    def _shot_out(s: Shot) -> ShotOut:
        return ShotOut(
            id=str(s.id),
            idx=s.idx,
            source_text=s.source_text,
            description=s.description,
            queries=[str(q) for q in s.queries],
            media_kind=s.media_kind,
            filter_slug=s.filter_slug,
            locked=s.locked,
            locked_candidate_id=str(s.locked_candidate_id) if s.locked_candidate_id else None,
            feedback=s.feedback,
            round_no=s.round_no,
            candidates=[
                CandidateOut(
                    id=str(c.id),
                    scene_id=str(sc.id),
                    rank=c.rank,
                    similarity=c.similarity,
                    quality=c.quality,
                    final_score=c.final_score,
                    status=c.status,
                    kind="video" if sc.end_ms > sc.start_ms else "image",
                    start_ms=sc.start_ms,
                    end_ms=sc.end_ms,
                    in_ms=c.in_ms,
                    out_ms=c.out_ms,
                )
                for c, sc in candidates.get(s.id, [])
            ],
        )

    base = _summary(project)
    return ProjectDetail(
        **base.model_dump(),
        script=project.script,
        default_filter_slug=project.default_filter_slug,
        error=project.error,
        shots=[_shot_out(s) for s in shots],
    )


class EventOut(BaseModel):
    seq: int
    actor: str
    kind: str
    payload: dict[str, Any]
    created_at: str


class EventsOut(BaseModel):
    events: list[EventOut]
    last_seq: int
    project_status: str
    state_version: int


@router.get("/projects/{project_id}/events", response_model=EventsOut)
async def project_events(
    project_id: uuid.UUID, db: DbDep, es: EditDep, after_seq: int = 0
) -> EventsOut:
    """时间线增量拉取。前端带上次的 last_seq 轮询，只传新事件。"""
    project = await _load_project(db, es, project_id)
    rows = (
        (
            await db.execute(
                select(ProjectEvent)
                .where(ProjectEvent.project_id == project.id, ProjectEvent.seq > after_seq)
                .order_by(ProjectEvent.seq)
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    last = rows[-1].seq if rows else after_seq
    return EventsOut(
        events=[
            EventOut(
                seq=e.seq,
                actor=e.actor,
                kind=e.kind,
                payload=e.payload,
                created_at=e.created_at.isoformat(),
            )
            for e in rows
        ],
        last_seq=last,
        project_status=project.status,
        state_version=project.state_version,
    )


# ------------------------------------------------------------------ 评审动作


class ApproveIn(BaseModel):
    candidate_id: uuid.UUID
    filter_slug: str | None = None
    in_ms: int | None = Field(default=None, ge=0)
    out_ms: int | None = Field(default=None, ge=0)
    state_version: int


class FeedbackIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    state_version: int


class RegenerateIn(BaseModel):
    note: str | None = Field(default=None, max_length=2000)
    state_version: int


class RenderIn(BaseModel):
    state_version: int


class ActionOut(BaseModel):
    ok: bool
    state_version: int
    project_status: str


async def _locked_project(
    db: DbDep, es: EditSession, project_id: uuid.UUID, state_version: int
) -> EditProject:
    try:
        project = await edit_flow.lock_project(db, project_id, es.workspace_id)
        edit_flow.check_state_version(project, state_version)
    except FlowError as exc:
        raise _raise(exc) from exc
    return project


def _action_out(project: EditProject) -> ActionOut:
    return ActionOut(ok=True, state_version=project.state_version, project_status=project.status)


@router.post("/projects/{project_id}/shots/{shot_id}/approve", response_model=ActionOut)
async def approve(
    project_id: uuid.UUID,
    shot_id: uuid.UUID,
    body: ApproveIn,
    db: DbDep,
    es: EditDep,
    _csrf: CsrfDep,
) -> ActionOut:
    project = await _locked_project(db, es, project_id, body.state_version)
    try:
        await edit_flow.approve_shot(
            db, project, shot_id, body.candidate_id, body.filter_slug, body.in_ms, body.out_ms
        )
        await db.commit()
    except FlowError as exc:
        raise _raise(exc) from exc
    return _action_out(project)


@router.post("/projects/{project_id}/shots/{shot_id}/feedback", response_model=ActionOut)
async def feedback(
    project_id: uuid.UUID,
    shot_id: uuid.UUID,
    body: FeedbackIn,
    db: DbDep,
    es: EditDep,
    _csrf: CsrfDep,
) -> ActionOut:
    project = await _locked_project(db, es, project_id, body.state_version)
    try:
        await edit_flow.feedback_shot(db, project, shot_id, body.text)
        await db.commit()
    except FlowError as exc:
        raise _raise(exc) from exc
    return _action_out(project)


@router.post("/projects/{project_id}/regenerate", response_model=ActionOut)
async def regenerate(
    project_id: uuid.UUID,
    body: RegenerateIn,
    db: DbDep,
    es: EditDep,
    settings: SettingsDep,
    _csrf: CsrfDep,
) -> ActionOut:
    project = await _locked_project(db, es, project_id, body.state_version)
    try:
        await edit_flow.regenerate(db, project, settings, body.note)
        await db.commit()
    except FlowError as exc:
        raise _raise(exc) from exc
    return _action_out(project)


@router.post("/projects/{project_id}/render", response_model=ActionOut)
async def render(project_id: uuid.UUID, body: RenderIn, db: DbDep, es: EditDep) -> ActionOut:
    project = await _locked_project(db, es, project_id, body.state_version)
    try:
        await edit_flow.start_render(db, project)
        await db.commit()
    except FlowError as exc:
        raise _raise(exc) from exc
    return _action_out(project)


# ------------------------------------------------------------------ 只读资源


@router.get("/scenes/{scene_id}/thumb")
async def scene_thumb(scene_id: uuid.UUID, db: DbDep, es: EditDep) -> Response:
    """候选关键帧缩略图。只允许取本码绑定相册的 scene（素材池共享，但按码的相册裁剪）。"""
    scene = (
        await db.execute(select(Scene).where(Scene.id == scene_id, Scene.album == es.album))
    ).scalar_one_or_none()
    if scene is None or scene.keyframe is None:
        raise HTTPException(status_code=404, detail="不存在")
    return Response(
        content=scene.keyframe,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.get("/projects/{project_id}/download")
async def download(
    project_id: uuid.UUID, db: DbDep, es: EditDep, settings: SettingsDep
) -> FileResponse:
    """成片打包下载。zip 由 render job 产出，api 只读分发。"""
    project = await _load_project(db, es, project_id)
    if project.status != "done":
        raise HTTPException(status_code=409, detail="渲染尚未完成")

    row = (
        await db.execute(
            select(RenderOutput)
            .where(RenderOutput.project_id == project.id)
            .order_by(RenderOutput.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="没有渲染产物")

    def _resolve_zip(path_str: str, output_dir: str) -> Path | None:
        # 磁盘操作打包成同步函数丢线程池，避免在 event loop 里做文件系统调用
        zip_path = Path(path_str).parent / f"{Path(path_str).parent.name}.zip"
        output_root = Path(output_dir).resolve()
        resolved = zip_path.resolve()
        # 防路径逃逸：zip 必须落在本 workspace 的输出目录下
        if not resolved.is_relative_to(output_root) or not resolved.is_file():
            return None
        return resolved

    resolved = await asyncio.to_thread(
        _resolve_zip, row.path, settings.output_dir(str(es.workspace_id))
    )
    if resolved is None:
        raise HTTPException(status_code=404, detail="打包文件不存在")
    return FileResponse(resolved, filename=resolved.name, media_type="application/zip")
