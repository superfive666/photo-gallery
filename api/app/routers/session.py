from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from api.app import captcha
from api.app.auth import (
    CSRF_COOKIE,
    ROLE_EDIT,
    ROLE_SEARCH,
    SESSION_COOKIE,
    SessionInfo,
    hash_invite_code,
    issue_session,
    require_edit_session,
    require_session,
    split_invite_code,
    verify_code_hash,
    verify_invite_code,
)
from api.app.deps import ClientIpDep, CsrfDep, DbDep, DeviceDep, LimitersDep, SettingsDep
from gallery_core.models import InviteCode

router = APIRouter(prefix="/session", tags=["session"])

# 进程启动时生成一次的假 hash：prefix 未命中时也烧一次等价的 argon2 时间，
# 否则响应耗时会泄露「这个 prefix 存不存在」。
_DUMMY_HASH = hash_invite_code("dummy-for-timing-equalization")


class CaptchaOut(BaseModel):
    token: str
    # SVG 文本。前端转成 data URI 塞进 <img>，符合 CSP 的 img-src data: 白名单
    svg: str


class LoginIn(BaseModel):
    invite_code: str = Field(min_length=1, max_length=128)
    captcha_token: str = Field(min_length=1, max_length=512)
    captcha_answer: str = Field(min_length=1, max_length=16)
    # 隐私告知的同意勾选。没有它就没有合法的处理基础。
    consent: bool = False


class LoginOut(BaseModel):
    ok: bool
    ttl_hours: int
    # 本 session 绑定的相册；null = 全相册
    album: str | None
    # search（查照片）| edit（剪辑聊天窗）。前端据此分流 UI。
    role: str = ROLE_SEARCH


@router.get("/captcha", response_model=CaptchaOut)
async def get_captcha(settings: SettingsDep) -> CaptchaOut:
    """签发一张登录验证码。答案只进 SVG 渲染，绝不进响应体的结构化字段。"""
    answer, token = captcha.issue(settings.jwt_secret, settings.captcha_ttl_seconds)
    return CaptchaOut(token=token, svg=captcha.render_svg(answer))


@router.post("/login", response_model=LoginOut)
async def login(
    body: LoginIn,
    response: Response,
    settings: SettingsDep,
    db: DbDep,
    limiters: LimitersDep,
    ip: ClientIpDep,
    device: DeviceDep,
) -> LoginOut:
    if not body.consent:
        raise HTTPException(status_code=400, detail="需要先同意隐私说明")

    # 邀请码也要限流，否则可以被暴力枚举。设备 cookie 在这里顺带首发。
    limiters.ip.check(f"login:{ip}")

    # captcha 先于邀请码校验：让脚本在便宜的一关就被挡下，argon2 是贵的
    if not captcha.verify(body.captcha_token, body.captcha_answer, settings.jwt_secret):
        raise HTTPException(status_code=400, detail="验证码不正确或已过期，请重试")

    album, role, workspace_id = await _resolve_invite(body.invite_code, db, settings)

    # 设备 id 绑进 JWT：检索限流用 JWT 里的值，脚本清 cookie 换不来新身份
    issue_session(
        response, settings, album=album, device=device, role=role, workspace_id=workspace_id
    )
    return LoginOut(ok=True, ttl_hours=settings.session_ttl_hours, album=album, role=role)


async def _resolve_invite(
    code: str, db: DbDep, settings: SettingsDep
) -> tuple[str | None, str, str | None]:
    """验证邀请码，返回 (相册, 角色, workspace_id)。失败抛 401。

    三条路径：
      `<prefix>.<secret>` 且 role='search' → 查找，绑定单相册；
      `<prefix>.<secret>` 且 role='edit'   → 剪辑聊天窗，workspace_id = 行 id；
      其他形态                             → .env 的全局码，全相册查找。
    """
    parts = split_invite_code(code)
    if parts is None:
        if not verify_invite_code(code, settings):
            raise HTTPException(status_code=401, detail="邀请码不正确")
        return None, ROLE_SEARCH, None

    prefix, secret = parts
    row = (
        await db.execute(
            select(InviteCode).where(
                InviteCode.prefix == prefix,
                InviteCode.disabled_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        # 找不到行时也要烧掉一次与 argon2 等价的时间，
        # 否则响应耗时会泄露「这个 prefix 存不存在」
        await asyncio.to_thread(verify_code_hash, _DUMMY_HASH, secret)
        raise HTTPException(status_code=401, detail="邀请码不正确")

    # argon2 要 ~100ms 的 CPU，丢线程池，别卡事件循环
    if not await asyncio.to_thread(verify_code_hash, row.code_hash, secret):
        raise HTTPException(status_code=401, detail="邀请码不正确")
    if row.role == ROLE_EDIT:
        # 一码一相册是剪辑域的硬语义：发码时就该带 --album，缺了是运维配置错误
        if not row.album:
            raise HTTPException(status_code=500, detail="服务端鉴权配置错误，请联系管理员")
        return row.album, ROLE_EDIT, str(row.id)
    return row.album, ROLE_SEARCH, None


@router.post("/logout")
async def logout(response: Response, _csrf: CsrfDep) -> dict[str, bool]:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
async def me(request: Request, settings: SettingsDep) -> dict[str, bool | str | None]:
    """前端用它判断是否已登录、本 session 的相册边界、走哪套 UI（查找/剪辑）。"""
    try:
        edit = require_edit_session(request, settings)
        return {"authenticated": True, "album": edit.album, "role": ROLE_EDIT}
    except HTTPException:
        pass
    try:
        info: SessionInfo = require_session(request, settings)
    except HTTPException:
        return {"authenticated": False, "album": None}
    return {"authenticated": True, "album": info.album, "role": ROLE_SEARCH}
