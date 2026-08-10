from __future__ import annotations

import hashlib
import uuid

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.app.deps import DbDep, SessionDep, SettingsDep
from api.app.signed_url import sign
from gallery_core.models import Album, Photo

router = APIRouter(prefix="/photos", tags=["photos"])


async def _load_visible_photo(db: AsyncSession, photo_id: uuid.UUID) -> Photo:
    """只返回可见的照片：未软删除，且所属相册为 public。

    这个过滤必须在每一个对外暴露照片内容的接口上生效，否则 private 相册会从这里泄露。
    """
    row = (
        await db.execute(
            select(Photo)
            .join(Album, Album.id == Photo.album_id)
            .where(
                Photo.id == photo_id,
                Photo.deleted_at.is_(None),
                Album.visibility == "public",
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="照片不存在")
    return row


@router.get("/{photo_id}/thumb")
async def thumbnail(photo_id: uuid.UUID, db: DbDep, _session: SessionDep) -> Response:
    """缩略图。搜索响应只带 URL，图片本体由这里分发并被浏览器按 ETag 缓存。"""
    photo = await _load_visible_photo(db, photo_id)
    if photo.thumb_webp is None:
        raise HTTPException(status_code=404, detail="缩略图不存在")

    etag = hashlib.sha256(photo.thumb_webp).hexdigest()[:32]
    return Response(
        content=photo.thumb_webp,
        media_type="image/webp",
        headers={
            "ETag": f'"{etag}"',
            # 缩略图内容不变，可以长期缓存；照片被替换时 photo_id 不变但 ETag 会变
            "Cache-Control": "private, max-age=604800",
        },
    )


@router.get("/{photo_id}/original")
async def original(
    photo_id: uuid.UUID, db: DbDep, settings: SettingsDep, _session: SessionDep
) -> RedirectResponse:
    """302 到源站的短效签名地址，不暴露裸地址。"""
    photo = await _load_visible_photo(db, photo_id)
    signature, expires_at = sign(str(photo.id), settings)
    separator = "&" if "?" in photo.source_url else "?"
    target = f"{photo.source_url}{separator}exp={expires_at}&sig={signature}"
    return RedirectResponse(url=target, status_code=302)
