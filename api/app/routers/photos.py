from __future__ import annotations

import hashlib
import uuid

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.app.deps import DbDep, SessionDep
from gallery_core.models import Photo

router = APIRouter(prefix="/photos", tags=["photos"])


async def _load_photo(db: AsyncSession, photo_id: uuid.UUID) -> Photo:
    row = (
        await db.execute(select(Photo).where(Photo.id == photo_id, Photo.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="照片不存在")
    return row


@router.get("/{photo_id}/thumb")
async def thumbnail(photo_id: uuid.UUID, db: DbDep, _session: SessionDep) -> Response:
    """缩略图。搜索响应只带 URL，图片本体由这里分发并被浏览器按 ETag 缓存。"""
    photo = await _load_photo(db, photo_id)
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
async def original(photo_id: uuid.UUID, db: DbDep, _session: SessionDep) -> RedirectResponse:
    """302 到源站原图。

    photos.zrc.sg 是公开、无需鉴权的照片墙，所以这里不需要签名链接 ——
    签名保护的是源站的访问控制，而源站本来就没有访问控制可绕过。
    这一跳仍然保留，用途是：不把源站 URL 结构写进前端，以后源站改版只改这里。
    """
    photo = await _load_photo(db, photo_id)
    return RedirectResponse(url=photo.photo_url, status_code=302)


def _sniff_image_type(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"
