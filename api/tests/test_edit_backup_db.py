"""评审备选（007）对真实 Postgres 的回归测试。

需要 DATABASE_URL 指向一个已跑过迁移的库 —— CI 的 python job 满足；本地没有库时自动跳过。

覆盖三条硬语义：
  · 主选 + 备选一同锁定，两条候选都标 approved；
  · 备选与主选不能是同一条；
  · 撤销锁定（写反馈）时主/备选一并清空，approved 复位 pending ——
    否则该候选躲过 regenerate 的负反馈标记，下一轮还会复读。
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from api.app.services import edit_flow
from api.app.services.edit_flow import FlowError

DATABASE_URL = os.environ.get("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="需要真实 Postgres（CI 的 python job 提供；本地可用 DATABASE_URL 指向测试库）",
)

_ALBUM = "test-backup-album"


async def _seed(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """一套最小可评审现场：项目(reviewing) + 1 镜头 + 2 候选。返回各 id。"""
    workspace_id = (
        await session.execute(
            text(
                "INSERT INTO invite_code (prefix, code_hash, album, role)"
                " VALUES (:p, 'x', :album, 'edit') RETURNING id"
            ),
            {"p": uuid.uuid4().hex[:8], "album": _ALBUM},
        )
    ).scalar_one()
    project_id = (
        await session.execute(
            text(
                "INSERT INTO edit_project (workspace_id, album, script, status)"
                " VALUES (:w, :album, 's', 'reviewing') RETURNING id"
            ),
            {"w": workspace_id, "album": _ALBUM},
        )
    ).scalar_one()
    asset_id = (
        await session.execute(
            text(
                "INSERT INTO media_asset (album, source_url, path, kind)"
                " VALUES (:album, :url, '/photo-gallery/media/x/a.mp4', 'video') RETURNING id"
            ),
            {"album": _ALBUM, "url": f"https://example.test/{uuid.uuid4()}.mp4"},
        )
    ).scalar_one()
    emb = "[" + ",".join(["1"] + ["0"] * 511) + "]"
    scene_ids = []
    for _ in range(2):
        scene_ids.append(
            (
                await session.execute(
                    text(
                        "INSERT INTO scene (asset_id, album, start_ms, end_ms, embedding,"
                        " model_name, model_version)"
                        " VALUES (:a, :album, 0, 3000, CAST(:emb AS vector), 'm', '1')"
                        " RETURNING id"
                    ),
                    {"a": asset_id, "album": _ALBUM, "emb": emb},
                )
            ).scalar_one()
        )
    shot_id = (
        await session.execute(
            text("INSERT INTO shot (project_id, idx) VALUES (:p, 1) RETURNING id"),
            {"p": project_id},
        )
    ).scalar_one()
    cand_ids = []
    for rank, scene_id in enumerate(scene_ids, start=1):
        cand_ids.append(
            (
                await session.execute(
                    text(
                        "INSERT INTO shot_candidate (shot_id, scene_id, rank)"
                        " VALUES (:s, :sc, :r) RETURNING id"
                    ),
                    {"s": shot_id, "sc": scene_id, "r": rank},
                )
            ).scalar_one()
        )
    return project_id, shot_id, cand_ids[0], cand_ids[1]


async def _cleanup(session: AsyncSession, project_id: uuid.UUID) -> None:
    workspace = (
        await session.execute(
            text("DELETE FROM edit_project WHERE id = :p RETURNING workspace_id"),
            {"p": project_id},
        )
    ).scalar_one_or_none()
    if workspace is not None:
        await session.execute(text("DELETE FROM invite_code WHERE id = :w"), {"w": workspace})
    await session.execute(text("DELETE FROM media_asset WHERE album = :a"), {"a": _ALBUM})
    await session.commit()


async def _candidate_statuses(session: AsyncSession, shot_id: uuid.UUID) -> dict[uuid.UUID, str]:
    rows = await session.execute(
        text("SELECT id, status FROM shot_candidate WHERE shot_id = :s"), {"s": shot_id}
    )
    return {row.id: row.status for row in rows}


async def test_approve_with_backup_then_feedback_resets() -> None:
    engine = create_async_engine(DATABASE_URL)
    try:
        async with async_sessionmaker(engine)() as session:
            project_id, shot_id, primary_id, backup_id = await _seed(session)
            try:
                project = await edit_flow.lock_project(session, project_id)
                await edit_flow.approve_shot(
                    session,
                    project,
                    shot_id,
                    primary_id,
                    None,
                    None,
                    None,
                    backup_candidate_id=backup_id,
                )
                row = (
                    await session.execute(
                        text(
                            "SELECT locked, locked_candidate_id, backup_candidate_id"
                            " FROM shot WHERE id = :s"
                        ),
                        {"s": shot_id},
                    )
                ).one()
                assert row.locked is True
                assert row.locked_candidate_id == primary_id
                assert row.backup_candidate_id == backup_id
                statuses = await _candidate_statuses(session, shot_id)
                assert statuses[primary_id] == "approved"
                assert statuses[backup_id] == "approved"

                # 撤销锁定：主/备选清空，approved 复位 pending（负反馈闭环依赖它）
                await edit_flow.feedback_shot(session, project, shot_id, "换个画面")
                row = (
                    await session.execute(
                        text(
                            "SELECT locked, locked_candidate_id, backup_candidate_id"
                            " FROM shot WHERE id = :s"
                        ),
                        {"s": shot_id},
                    )
                ).one()
                assert row.locked is False
                assert row.locked_candidate_id is None
                assert row.backup_candidate_id is None
                statuses = await _candidate_statuses(session, shot_id)
                assert set(statuses.values()) == {"pending"}
            finally:
                await session.rollback()
                await _cleanup(session, project_id)
    finally:
        await engine.dispose()


async def test_backup_must_differ_from_primary() -> None:
    engine = create_async_engine(DATABASE_URL)
    try:
        async with async_sessionmaker(engine)() as session:
            project_id, shot_id, primary_id, _ = await _seed(session)
            try:
                project = await edit_flow.lock_project(session, project_id)
                with pytest.raises(FlowError, match="同一条"):
                    await edit_flow.approve_shot(
                        session,
                        project,
                        shot_id,
                        primary_id,
                        None,
                        None,
                        None,
                        backup_candidate_id=primary_id,
                    )
            finally:
                await session.rollback()
                await _cleanup(session, project_id)
    finally:
        await engine.dispose()
