"""人脸检索：查询向量 → 相似人脸 → 所属照片。

单段式：直接对 `face.embedding` 做 KNN，命中的人脸映射回照片，每张照片取其上所有命中
人脸的最高相似度作为得分。库里不存 person / 不做聚类。

## 为什么写成「先取候选、再过滤」

pgvector 的 HNSW 索引只在 `ORDER BY embedding <=> q LIMIT n` 这个形式下会被用到。
如果直接写 `WHERE 1 - (embedding <=> q) >= 阈值`，Postgres 没法用索引，会退化成全表
顺序扫描（十万行也就几十毫秒，但没必要）。

所以分两步：内层用 `ORDER BY ... LIMIT :candidates` 走索引取出最近的一批候选，
外层再按阈值、软删除、屏蔽名单、相册过滤。

**`:candidates` 是召回上限**：一个人在库里有 300 张照片、每张一张脸，而候选只取 500，
那是够的；但如果候选数设得太小，或者叠加了相册过滤（过滤发生在取候选之后），
就可能少返结果。`SEARCH_CANDIDATES` 可配，带相册过滤时会自动放大。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from gallery_core.config import Settings
from gallery_core.db import apply_search_tuning

# 带相册过滤时把候选数放大的倍数。过滤在取候选之后发生，不放大会让窄相册少返结果。
_ALBUM_FILTER_CANDIDATE_MULTIPLIER = 4


@dataclass(frozen=True, slots=True)
class PhotoMatch:
    photo_id: uuid.UUID
    album: str
    photo_url: str
    score: float
    has_thumbnail: bool


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    matches: list[PhotoMatch]
    # 实际从索引里取出的候选人脸数，用于诊断「是不是候选数不够导致少返」
    candidates_scanned: int


def _to_pgvector(vec: list[float]) -> str:
    """pgvector 的文本输入格式。"""
    return "[" + ",".join(f"{v:.7f}" for v in vec) + "]"


# 屏蔽名单在 SQL 层过滤，不在应用层结果集里过滤 ——
# 后者容易在新增查询路径时被漏掉，导致 opt-out 静默失效。
#
# 屏蔽有两种粒度：屏蔽某张脸（那个人不希望被检索到）、屏蔽整张照片。
_SEARCH_SQL = text(
    """
    WITH candidate AS (
        -- 这一层必须保持 ORDER BY + LIMIT 的形式，否则用不上 HNSW 索引
        SELECT f.id   AS face_id,
               f.photo_id,
               1 - (f.embedding <=> CAST(:q AS vector)) AS sim
        FROM face f
        ORDER BY f.embedding <=> CAST(:q AS vector)
        LIMIT :candidates
    )
    SELECT ph.id                        AS photo_id,
           ph.album                     AS album,
           ph.photo_url                 AS photo_url,
           (ph.thumbnail IS NOT NULL)   AS has_thumbnail,
           MAX(c.sim)                   AS score
    FROM candidate c
    JOIN photo ph ON ph.id = c.photo_id
    WHERE c.sim >= :threshold
      AND ph.deleted_at IS NULL
      -- CAST 不能省：asyncpg 走预处理语句，纯 `:album IS NULL OR ...` 推断不出
      -- 参数类型，album=None（不筛相册，最常见路径）会在 prepare 阶段直接 500
      AND (CAST(:album AS text) IS NULL OR ph.album = CAST(:album AS text))
      AND NOT EXISTS (SELECT 1 FROM block_list b WHERE b.face_id  = c.face_id)
      AND NOT EXISTS (SELECT 1 FROM block_list b WHERE b.photo_id = ph.id)
    GROUP BY ph.id, ph.album, ph.photo_url, ph.thumbnail IS NOT NULL
    ORDER BY score DESC, ph.album DESC
    LIMIT :limit
    """
)


async def search_by_embedding(
    session: AsyncSession,
    query_vec: list[float],
    settings: Settings,
    album: str | None = None,
    limit: int | None = None,
) -> SearchOutcome:
    """`query_vec` 必须是已 L2 归一化的单位向量。"""
    # 调高 HNSW 召回参数。必须是事务级 SET LOCAL，否则会污染连接池里的这条连接。
    await apply_search_tuning(session)

    candidates = settings.search_candidates
    if album is not None:
        # 相册过滤发生在取候选之后，不放大候选数会让窄相册少返结果
        candidates *= _ALBUM_FILTER_CANDIDATE_MULTIPLIER

    rows = (
        await session.execute(
            _SEARCH_SQL,
            {
                "q": _to_pgvector(query_vec),
                "candidates": candidates,
                "threshold": settings.face_match_threshold,
                "album": album,
                "limit": min(limit or settings.max_results, settings.max_results),
            },
        )
    ).all()

    matches = [
        PhotoMatch(
            photo_id=r.photo_id,
            album=r.album,
            photo_url=r.photo_url,
            score=float(r.score),
            has_thumbnail=bool(r.has_thumbnail),
        )
        for r in rows
    ]

    return SearchOutcome(matches=matches, candidates_scanned=candidates)


_BLOCK_CANDIDATES_SQL = text(
    """
    SELECT f.id AS face_id, 1 - (f.embedding <=> CAST(:q AS vector)) AS sim
    FROM face f
    ORDER BY f.embedding <=> CAST(:q AS vector)
    LIMIT :candidates
    """
)


async def find_faces_for_blocking(
    session: AsyncSession,
    query_vec: list[float],
    settings: Settings,
    threshold: float | None = None,
) -> list[uuid.UUID]:
    """找出与查询向量匹配的全部 face id，用于 opt-out。

    与检索共用同一套阈值与候选逻辑，但返回的是 face id 而不是照片 ——
    因为没有 person 表，「屏蔽某个人」就是屏蔽他的那一批 face。

    这里刻意**不**排除已屏蔽的 face：重复运行应该是幂等的，不该因为上次屏蔽过一部分
    就在这次漏掉剩下的。
    """
    await apply_search_tuning(session)

    rows = (
        await session.execute(
            _BLOCK_CANDIDATES_SQL,
            {
                "q": _to_pgvector(query_vec),
                # opt-out 要尽量收全，宁可多取候选
                "candidates": settings.search_candidates * _ALBUM_FILTER_CANDIDATE_MULTIPLIER,
            },
        )
    ).all()

    cutoff = threshold if threshold is not None else settings.face_match_threshold
    return [r.face_id for r in rows if float(r.sim) >= cutoff]
