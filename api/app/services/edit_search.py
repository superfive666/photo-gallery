"""剪辑域的融合检索：镜头描述 → CLIP 文本向量 → KNN + 画质融合重排。

两层结构（约束 7）与人脸检索完全一致：内层 ORDER BY + LIMIT 吃 HNSW，
外层做硬过滤与融合重排。要点：

  · **相似度硬门槛在先**：画质只在"已相关"的候选之间起排序作用，融合权重
    有上限（settings.edit_quality_weight ≤ 0.35），"高清但不相关"上不了位。
  · **相册过滤来自邀请码绑定**（一码一相册），值取自 JWT，用户无从越出。
  · **负反馈**：被用户否决过的 scene 通过 excluded 排除，换血而不是复读。
  · 多条 query 的结果用 RRF 融合后取 Top-K。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from gallery_core.config import Settings
from gallery_core.db import apply_search_tuning


@dataclass(frozen=True, slots=True)
class SceneHit:
    scene_id: uuid.UUID
    similarity: float
    quality: float
    final_score: float
    start_ms: int
    end_ms: int
    kind: str


def _to_pgvector(vec: list[float]) -> str:
    return "[" + ",".join(f"{v:.7f}" for v in vec) + "]"


_SCENE_SEARCH_SQL = text(
    """
    WITH candidate AS (
        -- 这一层必须保持 ORDER BY + LIMIT 的形式，否则用不上 HNSW 索引
        SELECT s.id AS scene_id,
               1 - (s.embedding <=> CAST(:q AS vector)) AS sim
        FROM scene s
        ORDER BY s.embedding <=> CAST(:q AS vector)
        LIMIT :candidates
    )
    SELECT s.id            AS scene_id,
           c.sim           AS similarity,
           s.quality_score AS quality,
           (1 - CAST(:qw AS float8)) * c.sim
               + CAST(:qw AS float8) * s.quality_score AS final_score,
           s.start_ms,
           s.end_ms,
           ma.kind
    FROM candidate c
    JOIN scene s        ON s.id = c.scene_id
    JOIN media_asset ma ON ma.id = s.asset_id
    WHERE c.sim >= :sim_floor
      AND s.album = :album
      AND s.stability >= :min_stability
      AND (:kind = 'any' OR ma.kind = :kind)
      AND NOT (s.id = ANY(CAST(:excluded AS uuid[])))
      AND (:min_ms IS NULL OR ma.kind = 'image' OR (s.end_ms - s.start_ms) >= :min_ms)
      AND (:max_ms IS NULL OR ma.kind = 'image' OR (s.end_ms - s.start_ms) <= :max_ms)
    ORDER BY final_score DESC
    LIMIT :limit
    """
)


async def apply_edit_search_tuning(session: AsyncSession) -> None:
    """ef_search 复用人脸检索的调优；再开 iterative_scan ——
    多相册共表时按 album 过滤后候选不足，索引要能继续往后扫（pgvector ≥ 0.8）。"""
    await apply_search_tuning(session)
    await session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))


async def search_scenes(
    session: AsyncSession,
    query_vec: list[float],
    settings: Settings,
    *,
    album: str,
    excluded: list[uuid.UUID],
    kind: str = "any",
    min_ms: int | None = None,
    max_ms: int | None = None,
    limit: int | None = None,
) -> list[SceneHit]:
    """单条 query 向量的融合检索。query_vec 必须是已 L2 归一化的单位向量。"""
    rows = (
        await session.execute(
            _SCENE_SEARCH_SQL,
            {
                "q": _to_pgvector(query_vec),
                "candidates": settings.edit_knn_candidates,
                "qw": settings.edit_quality_weight,
                "sim_floor": settings.edit_sim_floor,
                "album": album,
                "min_stability": settings.edit_min_stability,
                "kind": kind,
                "excluded": [str(e) for e in excluded],
                "min_ms": min_ms,
                "max_ms": max_ms,
                "limit": limit or settings.edit_top_k * 4,
            },
        )
    ).all()
    return [
        SceneHit(
            scene_id=row.scene_id,
            similarity=float(row.similarity),
            quality=float(row.quality),
            final_score=float(row.final_score),
            start_ms=int(row.start_ms),
            end_ms=int(row.end_ms),
            kind=str(row.kind),
        )
        for row in rows
    ]


def rrf_merge(result_lists: list[list[SceneHit]], top_k: int, k: int = 60) -> list[SceneHit]:
    """Reciprocal Rank Fusion。多条 query 各自的排名互补，比取分数最大值稳。

    纯函数，方便单测。同一 scene 在多个列表中出现时保留 final_score 最高的那份元数据。
    """
    scores: dict[uuid.UUID, float] = {}
    best: dict[uuid.UUID, SceneHit] = {}
    for hits in result_lists:
        for rank, hit in enumerate(hits):
            scores[hit.scene_id] = scores.get(hit.scene_id, 0.0) + 1.0 / (k + rank + 1)
            prev = best.get(hit.scene_id)
            if prev is None or hit.final_score > prev.final_score:
                best[hit.scene_id] = hit
    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return [best[scene_id] for scene_id, _ in ordered[:top_k]]
