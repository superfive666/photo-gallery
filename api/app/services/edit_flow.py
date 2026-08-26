"""剪辑项目的状态机与反馈闭环。

状态：ingesting → parsing → matching → reviewing ⇄ refining → rendering → done/failed。
api 只做轻操作（建项目、评审动作、入队）；重活（建库/解析/检索/渲染）由常驻的
`jobs worker` 认领执行 —— worker 直接复用本模块（jobs 依赖 api 是既有模式，见 eval.py）。

并发纪律：
  · 每个写操作先 `SELECT ... FOR UPDATE` 锁项目行 —— 事件 seq 分配、state_version
    检查都在这把锁下串行化，双设备并发写不会错乱。
  · 事件只追加；payload 只存小快照与 id 引用，**绝不出现向量或图片字节**（约束 2）。

反馈闭环的硬约定（见 docs/plans/0005）：
  · 锁定镜头绝不重写、不重检；
  · 被否决的候选（上一轮 Top-K）标 rejected，新一轮检索排除这些 scene；
  · 每一轮的用户输入与 shot list 快照留痕于 edit_round，LLM 上下文按轮次全量组装。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.app.agent.runner import dumps_shot_list, parse_script, refine_shots
from api.app.services.edit_search import rrf_merge, search_scenes
from gallery_core.clip_client import ClipClient
from gallery_core.config import Settings
from gallery_core.jobs_queue import enqueue_job
from gallery_core.logging import get_logger
from gallery_core.models import (
    EditProject,
    EditRound,
    FilterPreset,
    ProjectEvent,
    Scene,
    Shot,
    ShotCandidate,
)

log = get_logger(__name__)


class FlowError(RuntimeError):
    """业务错误。http_status 供路由层映射。"""

    def __init__(self, message: str, http_status: int = 400) -> None:
        super().__init__(message)
        self.http_status = http_status


# ---------------------------------------------------------------------------
# 基础操作
# ---------------------------------------------------------------------------


async def lock_project(
    session: AsyncSession,
    project_id: uuid.UUID,
    workspace_id: uuid.UUID | None = None,
) -> EditProject:
    """FOR UPDATE 锁项目行。workspace_id 不匹配按不存在处理（不泄露他人项目的存在）。"""
    project = (
        await session.execute(
            select(EditProject).where(EditProject.id == project_id).with_for_update()
        )
    ).scalar_one_or_none()
    if project is None or (workspace_id is not None and project.workspace_id != workspace_id):
        raise FlowError("项目不存在", http_status=404)
    return project


async def append_event(
    session: AsyncSession,
    project: EditProject,
    actor: str,
    kind: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """追加一条时间线事件。调用方必须已持有项目行锁（seq 分配靠它串行化）。"""
    row = await session.execute(
        select(ProjectEvent.seq)
        .where(ProjectEvent.project_id == project.id)
        .order_by(ProjectEvent.seq.desc())
        .limit(1)
    )
    last = row.scalar_one_or_none() or 0
    session.add(
        ProjectEvent(
            project_id=project.id, seq=last + 1, actor=actor, kind=kind, payload=payload or {}
        )
    )


def check_state_version(project: EditProject, state_version: int) -> None:
    """乐观并发控制：另一台设备已经改过 → 409，前端重拉时间线后重试。"""
    if project.state_version != state_version:
        raise FlowError("项目状态已被其他设备更新，请刷新后重试", http_status=409)
    project.state_version += 1


async def _album_ingested(session: AsyncSession, album: str) -> bool:
    row = (await session.execute(select(Scene.id).where(Scene.album == album).limit(1))).first()
    return row is not None


# ---------------------------------------------------------------------------
# 创建项目（api 在线路径）
# ---------------------------------------------------------------------------


async def create_project(
    session: AsyncSession,
    settings: Settings,
    workspace_id: uuid.UUID,
    album: str,
    script: str,
) -> EditProject:
    """提交剧本即隐式创建项目。相册来自剪辑码绑定，用户无从选择。"""
    script = script.strip()
    if not script:
        raise FlowError("剧本不能为空")

    first_line = next((line.strip() for line in script.splitlines() if line.strip()), "未命名剪辑")
    project = EditProject(
        workspace_id=workspace_id,
        album=album,
        title=first_line[:40],
        script=script,
        status="ingesting",
    )
    session.add(project)
    await session.flush()

    await append_event(
        session, project, "user", "script_submitted", {"chars": len(script), "title": project.title}
    )

    if await _album_ingested(session, album):
        project.status = "parsing"
        await enqueue_job(session, "project_flow", params={"project_id": str(project.id)})
    else:
        # 首次引用该相册：先建库。同相册并发引用靠 dedupe 防重复下载。
        await append_event(
            session,
            project,
            "system",
            "ingest_queued",
            {"album": album, "detail": "该相册首次被引用，正在下载原片并建库"},
        )
        await enqueue_job(session, "media_ingest", album=album, dedupe=True)

    log.info("project_created", project_id=str(project.id), status=project.status)
    return project


# ---------------------------------------------------------------------------
# worker 执行的重活：解析 + 检索（首轮），或反馈闭环重生成（后续轮）
# ---------------------------------------------------------------------------


async def run_project_flow(
    session: AsyncSession, settings: Settings, project_id: uuid.UUID
) -> None:
    project = await lock_project(session, project_id)
    if project.status not in ("parsing", "ingesting", "refining", "matching"):
        # 重复入队/迟到的任务是无害的（比如项目已进入 reviewing/rendering/done）。
        # 静默跳过而不是报错 —— 报错会让 worker 把一个健康的项目误标成 failed。
        log.info("project_flow_skipped", project_id=str(project_id), status=project.status)
        return

    if project.current_round <= 1 and not await _has_shots(session, project.id):
        await _first_round(session, settings, project)
    else:
        await _refine_round(session, settings, project)

    project.status = "reviewing"
    await append_event(
        session, project, "assistant", "review_ready", {"round": project.current_round}
    )


async def _has_shots(session: AsyncSession, project_id: uuid.UUID) -> bool:
    return (
        await session.execute(select(Shot.id).where(Shot.project_id == project_id).limit(1))
    ).first() is not None


async def _enabled_filter_slugs(session: AsyncSession) -> list[str]:
    rows = await session.execute(
        select(FilterPreset.slug).where(FilterPreset.enabled.is_(True)).order_by(FilterPreset.slug)
    )
    return [row[0] for row in rows]


def duration_bounds(
    min_seconds: float | None, max_seconds: float | None
) -> tuple[int | None, int | None]:
    """LLM 给的镜头时长收敛成素材筛选条件。

    语义：min = 素材至少要多长（成片会再剪裁），max 只有严格大于 min 时才是
    真实的上限。剧本常写「这个镜头 X 秒」，模型会填 min == max == X ——
    那是成片目标时长，不是素材区间；照单全收会要求 scene **恰好等长**，
    检索必然 0 候选（真实事故：zephyr-hoa 项目全部镜头空手而归）。
    prompt 里已写明规则，这里是防御层 —— 换模型后 prompt 遵从度不可假设。
    """
    min_ms = int(min_seconds * 1000) if min_seconds else None
    max_ms = int(max_seconds * 1000) if max_seconds else None
    if min_ms is not None and max_ms is not None and max_ms <= min_ms:
        max_ms = None
    return min_ms, max_ms


async def _first_round(session: AsyncSession, settings: Settings, project: EditProject) -> None:
    project.status = "parsing"
    slugs = await _enabled_filter_slugs(session)
    result, llm_model, fingerprint = await parse_script(settings, project.script, slugs)

    if result.title:
        project.title = result.title[:40]
    if result.default_filter_slug in slugs:
        project.default_filter_slug = result.default_filter_slug

    shots = []
    for draft in result.shots:
        min_ms, max_ms = duration_bounds(draft.min_seconds, draft.max_seconds)
        shots.append(
            Shot(
                project_id=project.id,
                idx=draft.idx,
                source_text=draft.source_text,
                description=draft.description,
                queries=list(draft.queries),
                media_kind=draft.media_kind,
                min_ms=min_ms,
                max_ms=max_ms,
                round_no=1,
            )
        )
    session.add_all(shots)
    session.add(
        EditRound(
            project_id=project.id,
            round_no=1,
            shot_list=dumps_shot_list(result),
            llm_model=llm_model,
            prompt_fingerprint=fingerprint,
        )
    )
    await session.flush()

    await append_event(
        session,
        project,
        "assistant",
        "polish_done",
        {
            "round": 1,
            "title": project.title,
            "default_filter": project.default_filter_slug,
            "llm": llm_model or "fallback",
            "shots": [{"idx": s.idx, "description": s.description[:120]} for s in shots],
        },
    )

    project.status = "matching"
    await _retrieve_for_shots(session, settings, project, shots)


async def _refine_round(session: AsyncSession, settings: Settings, project: EditProject) -> None:
    """反馈闭环：只重写未锁定镜头，历史轮次全量组装进上下文。"""
    project.status = "refining"
    shots = (
        (
            await session.execute(
                select(Shot).where(Shot.project_id == project.id).order_by(Shot.idx)
            )
        )
        .scalars()
        .all()
    )
    unlocked = [s for s in shots if not s.locked]
    if not unlocked:
        return

    rounds = (
        (
            await session.execute(
                select(EditRound)
                .where(EditRound.project_id == project.id)
                .order_by(EditRound.round_no)
            )
        )
        .scalars()
        .all()
    )
    history_parts: list[str] = []
    notes_parts: list[str] = []
    for r in rounds:
        feedback = ", ".join(f"镜头{k}: {v}" for k, v in (r.shot_feedback or {}).items())
        history_parts.append(
            f"第 {r.round_no} 轮：{len(r.shot_list)} 个镜头。反馈：{feedback or '（无）'}"
        )
        if r.user_note:
            notes_parts.append(f"第 {r.round_no} 轮：{r.user_note}")

    targets = [(s.idx, s.description, s.feedback or "换一个不同的画面") for s in unlocked]
    result, llm_model, fingerprint = await refine_shots(
        settings,
        project.script,
        "\n".join(history_parts),
        "\n".join(notes_parts),
        targets,
    )

    by_idx = {s.idx: s for s in unlocked}
    refined = 0
    for item in result.shots:
        shot = by_idx.get(item.idx)
        if shot is None or shot.locked:
            continue  # LLM 越界改锁定镜头 → 丢弃（硬约定：锁定绝不动）
        shot.description = item.description
        shot.queries = list(item.queries)
        shot.round_no = project.current_round
        refined += 1

    current = next((r for r in rounds if r.round_no == project.current_round), None)
    if current is not None:
        current.llm_model = llm_model
        current.prompt_fingerprint = fingerprint
        current.shot_list = [
            {"idx": s.idx, "description": s.description, "queries": s.queries} for s in shots
        ]

    await append_event(
        session,
        project,
        "assistant",
        "refine_done",
        {
            "round": project.current_round,
            "refined": refined,
            "llm": llm_model or "fallback",
            "shots": [{"idx": s.idx, "description": s.description[:120]} for s in unlocked],
        },
    )

    project.status = "matching"
    await _retrieve_for_shots(session, settings, project, unlocked)


async def _rejected_scene_ids(session: AsyncSession, shot_id: uuid.UUID) -> list[uuid.UUID]:
    rows = await session.execute(
        select(ShotCandidate.scene_id).where(
            ShotCandidate.shot_id == shot_id, ShotCandidate.status == "rejected"
        )
    )
    return [row[0] for row in rows]


async def _retrieve_for_shots(
    session: AsyncSession,
    settings: Settings,
    project: EditProject,
    shots: list[Shot],
) -> None:
    """逐镜头融合检索并写候选。query 向量化走 embedding 服务（约束 3）。"""
    from api.app.services.edit_search import apply_edit_search_tuning

    all_queries: list[str] = []
    spans: list[tuple[int, int]] = []
    for shot in shots:
        queries = [str(q) for q in shot.queries][:3] or [shot.description]
        spans.append((len(all_queries), len(queries)))
        all_queries.extend(queries)

    async with ClipClient() as clip:
        vectors, _, _ = await clip.encode_texts(all_queries)

    await apply_edit_search_tuning(session)

    counts: list[dict[str, Any]] = []
    for shot, (offset, n) in zip(shots, spans, strict=True):
        excluded = await _rejected_scene_ids(session, shot.id)
        result_lists = [
            await search_scenes(
                session,
                vectors[offset + i],
                settings,
                album=project.album,
                excluded=excluded,
                kind=shot.media_kind,
                min_ms=shot.min_ms,
                max_ms=shot.max_ms,
            )
            for i in range(n)
        ]
        merged = rrf_merge(result_lists, settings.edit_top_k)

        # 时长过滤把候选清零时降级重试：宁可给出偏短、可评审的素材，也不给
        # 「零候选」让整轮无从进行。如实标进事件，前端可以据此向用户说明。
        duration_relaxed = False
        if not merged and (shot.min_ms is not None or shot.max_ms is not None):
            result_lists = [
                await search_scenes(
                    session,
                    vectors[offset + i],
                    settings,
                    album=project.album,
                    excluded=excluded,
                    kind=shot.media_kind,
                )
                for i in range(n)
            ]
            merged = rrf_merge(result_lists, settings.edit_top_k)
            duration_relaxed = bool(merged)

        for rank, hit in enumerate(merged, start=1):
            session.add(
                ShotCandidate(
                    shot_id=shot.id,
                    scene_id=hit.scene_id,
                    round_no=project.current_round,
                    rank=rank,
                    similarity=hit.similarity,
                    quality=hit.quality,
                    final_score=hit.final_score,
                )
            )
        entry: dict[str, Any] = {"idx": shot.idx, "candidates": len(merged)}
        if duration_relaxed:
            entry["duration_relaxed"] = True
        counts.append(entry)

    await append_event(
        session,
        project,
        "assistant",
        "candidates_ready",
        {"round": project.current_round, "per_shot": counts},
    )


# ---------------------------------------------------------------------------
# 评审动作（api 在线路径，每个动作一个事务）
# ---------------------------------------------------------------------------


async def _get_shot(session: AsyncSession, project: EditProject, shot_id: uuid.UUID) -> Shot:
    shot = (
        await session.execute(select(Shot).where(Shot.id == shot_id, Shot.project_id == project.id))
    ).scalar_one_or_none()
    if shot is None:
        raise FlowError("镜头不存在", http_status=404)
    return shot


async def _get_candidate(
    session: AsyncSession, shot: Shot, candidate_id: uuid.UUID, label: str
) -> ShotCandidate:
    candidate = (
        await session.execute(
            select(ShotCandidate).where(
                ShotCandidate.id == candidate_id, ShotCandidate.shot_id == shot.id
            )
        )
    ).scalar_one_or_none()
    if candidate is None:
        raise FlowError(f"{label}不存在", http_status=404)
    if candidate.status == "rejected":
        raise FlowError(f"该{label}已被否决过，不能再选定", http_status=409)
    return candidate


async def approve_shot(
    session: AsyncSession,
    project: EditProject,
    shot_id: uuid.UUID,
    candidate_id: uuid.UUID,
    filter_slug: str | None,
    in_ms: int | None,
    out_ms: int | None,
    backup_candidate_id: uuid.UUID | None = None,
) -> None:
    """满意：从候选中选定一条（可再带一条备选）并锁定该镜头。"""
    if project.status != "reviewing":
        raise FlowError(f"当前状态 {project.status} 不能评审", http_status=409)
    shot = await _get_shot(session, project, shot_id)
    candidate = await _get_candidate(session, shot, candidate_id, "候选")

    backup: ShotCandidate | None = None
    if backup_candidate_id is not None:
        if backup_candidate_id == candidate_id:
            raise FlowError("备选不能与主选是同一条")
        backup = await _get_candidate(session, shot, backup_candidate_id, "备选候选")

    if in_ms is not None or out_ms is not None:
        if in_ms is not None and out_ms is not None and out_ms <= in_ms:
            raise FlowError("out 点必须晚于 in 点")
        candidate.in_ms = in_ms
        candidate.out_ms = out_ms

    # 重复 approve（换主意重锁）时先把上次的选中状态复位，
    # 否则被放弃的旧选中会以 approved 状态躲过 regenerate 的负反馈标记
    picked_ids = [candidate.id] + ([backup.id] if backup is not None else [])
    await session.execute(
        update(ShotCandidate)
        .where(
            ShotCandidate.shot_id == shot.id,
            ShotCandidate.status == "approved",
            ShotCandidate.id.not_in(picked_ids),
        )
        .values(status="pending")
    )

    candidate.status = "approved"
    if backup is not None:
        backup.status = "approved"
    shot.locked = True
    shot.locked_candidate_id = candidate.id
    shot.backup_candidate_id = backup.id if backup is not None else None
    shot.feedback = None
    if filter_slug is not None:
        shot.filter_slug = filter_slug

    await append_event(
        session,
        project,
        "user",
        "shot_locked",
        {
            "idx": shot.idx,
            "candidate_id": str(candidate.id),
            "backup_candidate_id": str(backup.id) if backup is not None else None,
            "filter": shot.filter_slug,
        },
    )


async def feedback_shot(
    session: AsyncSession, project: EditProject, shot_id: uuid.UUID, feedback: str
) -> None:
    """不满意：记录补充 idea/提示词。重生成在 regenerate 时统一触发。"""
    if project.status != "reviewing":
        raise FlowError(f"当前状态 {project.status} 不能评审", http_status=409)
    feedback = feedback.strip()
    if not feedback:
        raise FlowError("反馈不能为空")
    shot = await _get_shot(session, project, shot_id)

    shot.feedback = feedback
    shot.locked = False
    shot.locked_candidate_id = None
    shot.backup_candidate_id = None
    # 撤销锁定时把 approved 复位成 pending：这些候选重新回到「看过但没选中」，
    # regenerate 时才会被标 rejected 参与负反馈，否则下一轮还会复读
    await session.execute(
        update(ShotCandidate)
        .where(ShotCandidate.shot_id == shot.id, ShotCandidate.status == "approved")
        .values(status="pending")
    )
    await append_event(
        session, project, "user", "shot_feedback", {"idx": shot.idx, "text": feedback}
    )


async def regenerate(
    session: AsyncSession, project: EditProject, settings: Settings, note: str | None
) -> None:
    """带上下文重新生成：轮次 +1，否决未锁定镜头的当前候选，入队 refine。"""
    if project.status != "reviewing":
        raise FlowError(f"当前状态 {project.status} 不能重新生成", http_status=409)

    shots = (
        (await session.execute(select(Shot).where(Shot.project_id == project.id))).scalars().all()
    )
    unlocked = [s for s in shots if not s.locked]
    if not unlocked:
        raise FlowError("所有镜头都已锁定，直接渲染即可", http_status=409)

    # 负反馈：用户看过且没选中的候选标 rejected，新一轮检索排除这些 scene
    await session.execute(
        update(ShotCandidate)
        .where(
            ShotCandidate.shot_id.in_([s.id for s in unlocked]),
            ShotCandidate.status == "pending",
        )
        .values(status="rejected")
    )

    project.current_round += 1
    session.add(
        EditRound(
            project_id=project.id,
            round_no=project.current_round,
            user_note=(note or "").strip() or None,
            shot_feedback={str(s.idx): s.feedback for s in unlocked if s.feedback},
        )
    )
    project.status = "refining"

    await append_event(
        session,
        project,
        "user",
        "regenerate_requested",
        {"round": project.current_round, "unlocked": len(unlocked), "note": (note or "")[:500]},
    )
    await enqueue_job(session, "project_flow", params={"project_id": str(project.id)})


async def start_render(session: AsyncSession, project: EditProject) -> None:
    """全部镜头锁定后才可渲染。渲染由 worker 执行。"""
    if project.status != "reviewing":
        raise FlowError(f"当前状态 {project.status} 不能渲染", http_status=409)
    shots = (
        (await session.execute(select(Shot).where(Shot.project_id == project.id))).scalars().all()
    )
    if not shots:
        raise FlowError("项目还没有镜头", http_status=409)
    unlocked = [s.idx for s in shots if not s.locked]
    if unlocked:
        raise FlowError(f"还有未锁定的镜头: {unlocked}", http_status=409)

    project.status = "rendering"
    await append_event(session, project, "user", "render_requested", {"shots": len(shots)})
    await enqueue_job(session, "render", params={"project_id": str(project.id)})
