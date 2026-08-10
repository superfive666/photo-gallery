from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from api.app.auth import SESSION_COOKIE, issue_session, verify_invite_code
from api.app.deps import ClientIpDep, LimitersDep, SettingsDep

router = APIRouter(prefix="/session", tags=["session"])


class LoginIn(BaseModel):
    invite_code: str = Field(min_length=1, max_length=128)
    # 隐私告知的同意勾选。没有它就没有合法的处理基础。
    consent: bool = False


class LoginOut(BaseModel):
    ok: bool
    ttl_hours: int


@router.post("/login", response_model=LoginOut)
async def login(
    body: LoginIn,
    response: Response,
    settings: SettingsDep,
    limiters: LimitersDep,
    ip: ClientIpDep,
) -> LoginOut:
    if not body.consent:
        raise HTTPException(status_code=400, detail="需要先同意隐私说明")

    # 邀请码也要限流，否则可以被暴力枚举
    _, ip_limiter = limiters
    ip_limiter.check(f"login:{ip}")

    if not verify_invite_code(body.invite_code, settings):
        raise HTTPException(status_code=401, detail="邀请码不正确")

    issue_session(response, settings)
    return LoginOut(ok=True, ttl_hours=settings.session_ttl_hours)


@router.post("/logout")
async def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
async def me(request: Request, settings: SettingsDep) -> dict[str, bool]:
    """前端用它判断是否已登录，避免每次刷新都弹邀请码框。"""
    from api.app.auth import require_session

    try:
        require_session(request, settings)
    except HTTPException:
        return {"authenticated": False}
    return {"authenticated": True}
