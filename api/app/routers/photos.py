from __future__ import annotations

import hashlib
import uuid

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.app.auth import SessionInfo
from api.app.deps import DbDep, SessionDep
from gallery_core.models import Face, Photo

router = APIRouter(prefix="/photos", tags=["photos"])


class PhotoItem(BaseModel):
    photo_id: str
    album: str
    thumb_url: str | None
    original_url: str
    face_count: int
    kind: str = "image"  # image | video
    duration_ms: int | None = None


class PhotoPage(BaseModel):
    items: list[PhotoItem]
    total: int
    page: int
    per_page: int


class FaceItem(BaseModel):
    face_id: str
    # 小图可能还没回填（003 之前的存量数据），前端做占位
    thumb_url: str | None


@router.get("", response_model=PhotoPage)
async def list_photos(
    db: DbDep,
    session: SessionDep,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=10, ge=1, le=50),
    album: str | None = Query(default=None, max_length=200),
) -> PhotoPage:
    """浏览模式的分页照片列表。

    scope 与检索同一套规则：绑定相册的 session 传别的相册 → 403，
    不传 → 强制绑定相册。只列已入库的图片（视频只登记不提取，浏览没意义）。
    """
    if session.album is not None:
        if album is not None and album.strip() and album.strip() != session.album:
            raise HTTPException(status_code=403, detail="邀请码与所选相册不符")
        album = session.album
    elif album is not None and not album.strip():
        album = None

    conditions = [
        Photo.deleted_at.is_(None),
        # 视频自 plans/0008 起也可浏览（tracklet 人脸小图 + 点脸检索同一条链路）
        Photo.kind.in_(("image", "video")),
        Photo.processing_status == "embedded",
    ]
    if album is not None:
        conditions.append(Photo.album == album)

    total = (await db.execute(select(func.count()).select_from(Photo).where(*conditions))).scalar()
    rows = (
        await db.execute(
            select(
                Photo.id,
                Photo.album,
                Photo.face_count,
                Photo.kind,
                Photo.duration_ms,
                Photo.thumbnail.isnot(None).label("has_thumb"),
            )
            .where(*conditions)
            # uuidv7 时间有序：按 id 排即按入库时间排，稳定且走主键索引
            .order_by(Photo.id.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
    ).all()

    return PhotoPage(
        items=[
            PhotoItem(
                photo_id=str(r.id),
                album=r.album,
                thumb_url=f"/api/photos/{r.id}/thumb" if r.has_thumb else None,
                original_url=f"/api/photos/{r.id}/original",
                face_count=r.face_count,
                kind=r.kind,
                duration_ms=r.duration_ms,
            )
            for r in rows
        ],
        total=total or 0,
        page=page,
        per_page=per_page,
    )


@router.get("/{photo_id}/faces", response_model=list[FaceItem])
async def list_faces(photo_id: uuid.UUID, db: DbDep, session: SessionDep) -> list[FaceItem]:
    """一张照片上检测到的人脸，最大的脸在前。点脸即可按此人检索（见 /search/by-face）。"""
    await _load_photo(db, photo_id, session)
    rows = (
        await db.execute(
            select(Face.id, Face.thumb.isnot(None).label("has_thumb"))
            .where(Face.photo_id == photo_id)
            .order_by((Face.bbox_w * Face.bbox_h).desc())
        )
    ).all()
    return [
        FaceItem(
            face_id=str(r.id),
            thumb_url=f"/api/photos/faces/{r.id}/thumb" if r.has_thumb else None,
        )
        for r in rows
    ]


@router.get("/faces/{face_id}/thumb")
async def face_thumbnail(face_id: uuid.UUID, db: DbDep, session: SessionDep) -> Response:
    """人脸小图。scope 越权与不存在统一 404。"""
    row = (
        await db.execute(
            select(Face.thumb, Photo.album)
            .join(Photo, Photo.id == Face.photo_id)
            .where(Face.id == face_id, Photo.deleted_at.is_(None))
        )
    ).one_or_none()
    if (
        row is None
        or row.thumb is None
        or (session.album is not None and row.album != session.album)
    ):
        raise HTTPException(status_code=404, detail="人脸小图不存在")

    etag = hashlib.sha256(row.thumb).hexdigest()[:32]
    return Response(
        content=row.thumb,
        media_type="image/webp",
        headers={"ETag": f'"{etag}"', "Cache-Control": "private, max-age=604800"},
    )


async def _load_photo(db: AsyncSession, photo_id: uuid.UUID, session: SessionInfo) -> Photo:
    row = (
        await db.execute(select(Photo).where(Photo.id == photo_id, Photo.deleted_at.is_(None)))
    ).scalar_one_or_none()
    # 相册边界照样管住图片分发：越权拿别的相册的图统一回 404 而不是 403 ——
    # 不向持码人确认「这个 id 存在，只是你没权限」。
    if row is None or (session.album is not None and row.album != session.album):
        raise HTTPException(status_code=404, detail="照片不存在")
    return row


@router.get("/{photo_id}/thumb")
async def thumbnail(photo_id: uuid.UUID, db: DbDep, session: SessionDep) -> Response:
    """缩略图。搜索响应只带 URL，图片本体由这里分发并被浏览器按 ETag 缓存。"""
    photo = await _load_photo(db, photo_id, session)
    if photo.thumbnail is None:
        raise HTTPException(status_code=404, detail="缩略图不存在")

    etag = hashlib.sha256(photo.thumbnail).hexdigest()[:32]
    return Response(
        content=photo.thumbnail,
        # 源站提供的缩略图可能是 JPEG，本地生成的是 WebP。两者浏览器都按实际字节解码，
        # 但 Content-Type 得说得过去 —— 用嗅探出来的类型，不硬写。
        media_type=_sniff_image_type(photo.thumbnail),
        headers={
            "ETag": f'"{etag}"',
            # 内容不变可以长期缓存；缩略图被替换时 ETag 会变
            "Cache-Control": "private, max-age=604800",
        },
    )


@router.get("/{photo_id}/original")
async def original(photo_id: uuid.UUID, db: DbDep, session: SessionDep) -> RedirectResponse:
    """302 到源站原图。

    photos.zrc.sg 是公开、无需鉴权的照片墙，所以这里不需要签名链接 ——
    签名保护的是源站的访问控制，而源站本来就没有访问控制可绕过。
    这一跳仍然保留，用途是：不把源站 URL 结构写进前端，以后源站改版只改这里。
    """
    photo = await _load_photo(db, photo_id, session)
    return RedirectResponse(url=photo.photo_url, status_code=302)


def _sniff_image_type(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"
