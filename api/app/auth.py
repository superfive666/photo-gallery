"""邀请码 → session cookie。

访问控制不是可选项：站点公开开放意味着任何人拿一张他人照片就能扒出该人的全部活动照片。
见 docs/privacy.md「滥用风险与对策」。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import uuid
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import HTTPException, Request, Response

from gallery_core.config import Settings
from gallery_core.logging import get_logger

log = get_logger(__name__)

SESSION_COOKIE = "zrc_face_session"
_hasher = PasswordHasher()


def hash_invite_code(code: str) -> str:
    """生成 INVITE_CODE_HASH。明文邀请码不进配置、不进 git。"""
    return _hasher.hash(code)


def verify_invite_code(code: str, settings: Settings) -> bool:
    if not settings.invite_code_hash:
        # 没配 hash 就直接拒绝。绝不 fail-open —— 配置缺失不能变成「无需鉴权」。
        return False
    try:
        return _hasher.verify(settings.invite_code_hash, code)
    except InvalidHashError as exc:
        # 存储的 hash 本身格式非法 —— 这是运维配置错误，不是用户输错邀请码。
        # 最常见的成因：argon2 hash 里全是 $，docker compose 读 .env 时把
        # $argon2id / $v / $m 当变量引用替换成了空串。修法：.env 里给值加单引号
        # （INVITE_CODE_HASH='$argon2id$...'），compose 与 pydantic-settings
        # 都会按字面处理。hash_invite 工具的输出已带好引号。
        # 这里抛 500 而不是静默返回 False：hash 坏了意味着任何人都永远登不进来，
        # 伪装成「邀请码不正确」(401) 只会让排查绕远路。
        log.error(
            "invite_code_hash_invalid",
            hint="INVITE_CODE_HASH 不是合法的 argon2 hash，"
            "检查 .env 里 $ 是否被 compose 插值吃掉（值要用单引号包住）",
        )
        raise HTTPException(status_code=500, detail="服务端鉴权配置错误，请联系管理员") from exc
    except (VerifyMismatchError, VerificationError):
        return False


def issue_session(response: Response, settings: Settings) -> str:
    """签发 session cookie，返回 session id。"""
    session_id = uuid.uuid4().hex
    now = dt.datetime.now(tz=dt.UTC)
    token = jwt.encode(
        {
            "sid": session_id,
            "iat": int(now.timestamp()),
            "exp": int((now + dt.timedelta(hours=settings.session_ttl_hours)).timestamp()),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        # 生产环境必须是 True。本地 http 调试时由 .env 覆盖。
        secure=True,
        samesite="lax",
        path="/",
    )
    return session_id


def require_session(request: Request, settings: Settings) -> str:
    """校验 session，返回 session id。失败抛 401。"""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="需要邀请码")
    try:
        payload: dict[str, Any] = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="会话已失效，请重新输入邀请码") from exc
    sid = payload.get("sid")
    if not isinstance(sid, str):
        raise HTTPException(status_code=401, detail="会话无效")
    return sid


def audit_hash(value: str, settings: Settings) -> str:
    """给 search_audit 用的带盐 hash。不存 session id / IP 原值。"""
    return hmac.new(settings.audit_hash_salt.encode(), value.encode(), hashlib.sha256).hexdigest()
