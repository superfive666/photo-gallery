"""相册列表。

前端用它渲染「只在某次活动里找」的下拉 —— 那条路径会被分区裁剪成单分区检索，
比全库检索又快又精确，值得在 UI 上给出来。
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from api.app.deps import DbDep, SessionDep

router = APIRouter(prefix="/albums", tags=["albums"])


class AlbumOut(BaseModel):
    album: str
    photo_count: int
    face_count: int


@router.get("", response_model=list[AlbumOut])
async def list_albums(db: DbDep, _session: SessionDep) -> list[AlbumOut]:
    # 只列出真的有可检索人脸的相册：一个只有风景照的相册出现在筛选器里
    # 只会让用户白选一次。
    rows = (
        await db.execute(
            text(
                """
                SELECT album,
                       count(*)                       AS photo_count,
                       coalesce(sum(face_count), 0)   AS face_count
                FROM photo
                WHERE deleted_at IS NULL
                  AND processing_status = 'embedded'
                GROUP BY album
                HAVING coalesce(sum(face_count), 0) > 0
                ORDER BY album DESC
                """
            )
        )
    ).all()
    return [
        AlbumOut(album=r.album, photo_count=r.photo_count, face_count=r.face_count) for r in rows
    ]
