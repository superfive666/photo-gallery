"""两段式人脸检索。

朴素做法是「自拍向量 vs 每一条 face 向量取 top-k」。本项目不这么做：一个人的侧脸或背光
照片与其正脸自拍的 cosine 距离常常超过阈值，会漏掉大量本该命中的照片。

改为先认人、再取图：
  ① 对 person.centroid 做 KNN —— 簇心是同一人多张样本（正脸/侧脸/不同光照）的均值，
     比任何单张照片都更接近这个人的「平均长相」，对查询角度更鲁棒；
  ② 对 face.embedding 直接做 KNN 作为兜底 —— 覆盖聚类失败的噪声点和只出现过一次的人；
  ③ 合并两路，每张照片取其上所有命中脸的最高相似度作为得分。

② 不能省：没有它，孤立出现的人永远搜不到。

## 关于 album 过滤

face 按 album 做 LIST 分区，所以带 `album =` 条件的查询会被裁剪到单个分区，
只在那一个相册里做向量检索 —— 又快又精确。

不带 album 时（主流程：找所有活动的照片）无法裁剪，Postgres 需要对每个分区各做一次
HNSW 索引扫描再 MergeAppend。相册数量上到千级时开销会变得明显，
见 docs/schema/README.md「分区的代价」。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from gallery_core.config import Settings
from gallery_core.db import apply_search_tuning

# 候选簇上限。一个查询自拍不该匹配到很多人；取太多只会引入误报。
_MAX_PERSON_CANDIDATES = 10


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
    person_candidates: int


def _to_pgvector(vec: list[float]) -> str:
    """pgvector 的文本输入格式。"""
    return "[" + ",".join(f"{v:.7f}" for v in vec) + "]"


# 候选簇：屏蔽名单在 SQL 层过滤，不在应用层结果集里过滤 ——
# 后者容易在新增查询路径时被漏掉，导致 opt-out 静默失效。
#
# person 不分区（一个人跨多个相册），所以这一段不受 album 过滤影响；
# album 的裁剪发生在下面 _MATCH_SQL 对 face 的扫描上。
_PERSON_SQL = text(
    """
    SELECT p.id, 1 - (p.centroid <=> CAST(:q AS vector)) AS similarity
    FROM person p
    WHERE NOT EXISTS (SELECT 1 FROM block_list b WHERE b.person_id = p.id)
      AND 1 - (p.centroid <=> CAST(:q AS vector)) >= :person_threshold
    ORDER BY p.centroid <=> CAST(:q AS vector)
    LIMIT :person_limit
    """
)

# 合并两路命中并按最佳分打分。
#
# `:album IS NULL OR f.album = :album` 这个写法能被 Postgres 在执行时裁剪分区
# （runtime pruning）；写成 `f.album = COALESCE(:album, f.album)` 就不行了。
_MATCH_SQL = text(
    """
    WITH hit AS (
        -- ① 候选簇内的全部人脸
        SELECT f.photo_id, 1 - (f.embedding <=> CAST(:q AS vector)) AS sim
        FROM face f
        WHERE f.person_id = ANY(CAST(:person_ids AS uuid[]))
          AND (:album IS NULL OR f.album = :album)

        UNION ALL

        -- ② 直接命中的人脸（兜底：聚类失败或只出现一次的人）
        SELECT f.photo_id, 1 - (f.embedding <=> CAST(:q AS vector)) AS sim
        FROM face f
        WHERE 1 - (f.embedding <=> CAST(:q AS vector)) >= :face_threshold
          AND (:album IS NULL OR f.album = :album)
    )
    SELECT ph.id                        AS photo_id,
           ph.album                     AS album,
           ph.photo_url                 AS photo_url,
           (ph.thumbnail IS NOT NULL)   AS has_thumbnail,
           MAX(hit.sim)                 AS score
    FROM hit
    JOIN photo ph ON ph.id = hit.photo_id
    WHERE ph.deleted_at IS NULL
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
    """`query_vec` 必须是已 L2 归一化的单位向量。

    多张自拍的情况由调用方先用 `mean_embedding` 合成单一查询点后再进来。
    `album` 非空时只在该相册内检索（走分区裁剪）。
    """
    # 调高 HNSW 召回参数。必须是事务级 SET LOCAL，否则会污染连接池里的这条连接。
    await apply_search_tuning(session)

    q = _to_pgvector(query_vec)

    person_rows = (
        await session.execute(
            _PERSON_SQL,
            {
                "q": q,
                "person_threshold": settings.person_match_threshold,
                "person_limit": _MAX_PERSON_CANDIDATES,
            },
        )
    ).all()
    person_ids = [str(r.id) for r in person_rows]

    rows = (
        await session.execute(
            _MATCH_SQL,
            {
                "q": q,
                # 空数组也要传：= ANY('{}') 恒为 false，等价于只走兜底那一路
                "person_ids": person_ids,
                "face_threshold": settings.face_match_threshold,
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

    return SearchOutcome(matches=matches, person_candidates=len(person_ids))
