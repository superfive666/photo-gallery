"""检索接口。

⚠️ 这里处理的是用户人脸。上传的字节只存在于本请求的内存中，函数返回即失去引用。
不写盘、不写库、不写日志。见 CLAUDE.md 约束 #1。
"""

from __future__ import annotations

import time

from fastapi import APIRouter, File, Form, UploadFile
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.app.auth import audit_hash
from api.app.deps import ClientIpDep, DbDep, EmbeddingDep, LimitersDep, SessionDep, SettingsDep
from api.app.services.search import search_by_embedding
from gallery_core.config import Settings
from gallery_core.embedding_client import EmbeddingServiceError
from gallery_core.logging import get_logger
from gallery_core.models import ALBUM_MAX_LEN, SearchAudit
from gallery_core.vector import mean_embedding

log = get_logger(__name__)
router = APIRouter(prefix="/search", tags=["search"])


class MatchOut(BaseModel):
    photo_id: str
    album: str
    score: float
    thumb_url: str | None
    original_url: str


class SearchOut(BaseModel):
    matches: list[MatchOut]
    faces_detected: int
    # 前端据此展示对应的空状态：没检测到脸 vs 检测到了但没匹配上
    status: str  # ok | no_face | no_match
    message: str | None = None
    latency_ms: int


@router.post("", response_model=SearchOut)
async def search(
    db: DbDep,
    embedding: EmbeddingDep,
    settings: SettingsDep,
    session: SessionDep,
    limiters: LimitersDep,
    ip: ClientIpDep,
    selfies: list[UploadFile] = File(...),  # noqa: B008
    # 只在某个相册里找。带上它会被分区裁剪成单分区检索 —— 更快也更精确。
    album: str | None = Form(default=None),
) -> SearchOut:
    from api.app.uploads import read_selfie, validate_count

    started = time.perf_counter()

    validate_count(selfies, settings)
    album_filter = _normalize_album(album)

    session_limiter, ip_limiter = limiters
    session_limiter.check(f"search:{session}")
    ip_limiter.check(f"search:{ip}")

    # 收集全部自拍中检测到的人脸向量。
    # 多张自拍取均值 —— 等价于一个角度更中性、更稳的查询点，是最便宜的召回率提升手段。
    vectors: list[list[float]] = []
    faces_detected = 0
    for upload in selfies:
        payload = await read_selfie(upload, settings)
        try:
            result = await embedding.extract(payload, filename="selfie")
        except EmbeddingServiceError:
            log.exception("embedding_unavailable")
            raise
        finally:
            # 显式丢弃引用。CPython 会立即回收，缩短人脸字节在内存中的存活时间。
            del payload

        faces_detected += len(result.faces)
        if result.faces:
            # 一张自拍里若有多张脸（例如背景里的人），取面积最大的那张 —— 那是拍摄者本人
            largest = max(result.faces, key=lambda f: f.bbox[2] * f.bbox[3])
            vectors.append(largest.embedding)

    latency_ms = int((time.perf_counter() - started) * 1000)

    if not vectors:
        await _audit(db, session, ip, settings, album_filter, faces_detected, 0, 0, latency_ms)
        return SearchOut(
            matches=[],
            faces_detected=0,
            status="no_face",
            message="没有在照片里检测到人脸。请用光线充足、正面清晰的自拍再试一次。",
            latency_ms=latency_ms,
        )

    query_vec = mean_embedding(vectors).tolist()
    outcome = await search_by_embedding(db, query_vec, settings, album=album_filter)
    latency_ms = int((time.perf_counter() - started) * 1000)

    await _audit(
        db,
        session,
        ip,
        settings,
        album_filter,
        faces_detected,
        outcome.person_candidates,
        len(outcome.matches),
        latency_ms,
    )

    matches = [
        MatchOut(
            photo_id=str(m.photo_id),
            album=m.album,
            score=round(m.score, 4),
            thumb_url=f"/api/photos/{m.photo_id}/thumb" if m.has_thumbnail else None,
            original_url=f"/api/photos/{m.photo_id}/original",
        )
        for m in outcome.matches
    ]

    # 只记计数与耗时
    log.info(
        "search_done",
        album=album_filter,
        faces_detected=faces_detected,
        person_candidates=outcome.person_candidates,
        results=len(matches),
        latency_ms=latency_ms,
    )

    return SearchOut(
        matches=matches,
        faces_detected=faces_detected,
        status="ok" if matches else "no_match",
        message=None
        if matches
        else "没有找到匹配的照片。可能是这些活动里没有你的照片，或者你出现在照片的远景中。",
        latency_ms=latency_ms,
    )


def _normalize_album(album: str | None) -> str | None:
    """空字符串按「不筛选」处理 —— 表单里没选相册时浏览器会送空串而不是省略字段。"""
    if album is None:
        return None
    trimmed = album.strip()
    if not trimmed:
        return None
    return trimmed[:ALBUM_MAX_LEN]


async def _audit(
    db: AsyncSession,
    session: str,
    ip: str,
    settings: Settings,
    album: str | None,
    faces_detected: int,
    candidates: int,
    results: int,
    latency_ms: int,
) -> None:
    """留痕。只有 hash 后的标识与计数 —— 无图片、无向量。"""
    db.add(
        SearchAudit(
            session_hash=audit_hash(session, settings),
            ip_hash=audit_hash(ip, settings),
            album_filter=album,
            faces_detected=faces_detected,
            candidate_count=candidates,
            result_count=results,
            latency_ms=latency_ms,
        )
    )
    await db.commit()
