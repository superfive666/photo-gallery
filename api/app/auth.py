"""邀请码 → session cookie，以及 CSRF / 设备 cookie。

访问控制不是可选项：站点公开开放意味着任何人拿一张他人照片就能扒出该人的全部活动照片。
见 docs/privacy.md「滥用风险与对策」。

邀请码有两种（见 docs/plans/0006）：

  · **绑定相册的码**：形如 `<prefix>.<secret>`，存在 invite_code 表里。
    prefix 公开、唯一索引定位行 → 登录只做一次 argon2 验证。
    session 的 JWT 带 `alb` claim，检索被硬性限制在那一个相册内。
  · **全相册码（兼容旧行为）**：不含 `.` 的码走 `.env` 的 INVITE_CODE_HASH，
    `alb` 为 null —— 站主自用。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import re
import secrets
import uuid
from dataclasses import dataclass
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import HTTPException, Request, Response

from gallery_core.config import Settings
from gallery_core.logging import get_logger

log = get_logger(__name__)

SESSION_COOKIE = "zrc_face_session"
# CSRF 双提交 cookie：故意**非** httponly —— 前端要读它并回填到 X-CSRF-Token 头，
# 这正是双提交模式的工作方式。跨站攻击者发得出请求，但读不到别人域下的 cookie。
CSRF_COOKIE = "zrc_csrf"
# 设备标识：httponly、长期。用于设备维度限流，见 plans/0006「诚实说明」。
DEVICE_COOKIE = "zrc_device"
_DEVICE_COOKIE_MAX_AGE = 400 * 24 * 3600

_hasher = PasswordHasher()

_PREFIX_RE = re.compile(r"^[0-9a-f]{8}$")


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """一个已通过校验的 session。album 为 None 表示全相册权限。"""

    sid: str
    album: str | None


# ---------------------------------------------------------------------- 邀请码


def hash_invite_code(code: str) -> str:
    """生成邀请码的 argon2 hash。明文不进配置、不进库、不进 git。"""
    return _hasher.hash(code)


def generate_invite_code() -> tuple[str, str, str]:
    """造一张新码。返回 (完整码, prefix, secret 的 argon2 hash)。

    完整码只在此刻存在 —— 调用方打印给发码人后即丢弃，库里只存 prefix + hash。
    """
    prefix = secrets.token_hex(4)
    secret = secrets.token_urlsafe(18)
    return f"{prefix}.{secret}", prefix, _hasher.hash(secret)


def split_invite_code(code: str) -> tuple[str, str] | None:
    """拆出 (prefix, secret)。不符合绑定码形态则返回 None（走旧的全局码路径）。"""
    prefix, dot, secret = code.partition(".")
    if not dot or not secret or not _PREFIX_RE.match(prefix):
        return None
    return prefix, secret


def verify_code_hash(stored_hash: str, candidate: str) -> bool:
    """argon2 校验，容忍不匹配、拒绝格式非法的存储值。"""
    try:
        return _hasher.verify(stored_hash, candidate)
    except InvalidHashError as exc:
        # 存储的 hash 本身格式非法 —— 运维配置错误，不是用户输错。
        # 典型成因：argon2 hash 里全是 $，compose 读 .env 时把 $argon2id / $v
        # 当变量引用啃掉了（值要用单引号包住；hash_invite 工具输出已带引号）。
        # 抛 500 而不是伪装成 401：hash 坏了谁都永远登不进来，装成「码不对」
        # 只会让排查绕远路。
        log.error(
            "invite_code_hash_invalid",
            hint="存储的邀请码 hash 不是合法的 argon2 hash（.env 值要用单引号包住）",
        )
        raise HTTPException(status_code=500, detail="服务端鉴权配置错误，请联系管理员") from exc
    except (VerifyMismatchError, VerificationError):
        return False


def verify_invite_code(code: str, settings: Settings) -> bool:
    """旧路径：与 .env 的全局码比对（全相册权限）。"""
    if not settings.invite_code_hash:
        # 没配 hash 就直接拒绝。绝不 fail-open —— 配置缺失不能变成「无需鉴权」。
        return False
    return verify_code_hash(settings.invite_code_hash, code)


# ---------------------------------------------------------------------- session


def issue_session(response: Response, settings: Settings, album: str | None = None) -> str:
    """签发 session + CSRF 两个 cookie，返回 session id。

    album 进 JWT 的 `alb` claim：检索的相册边界由它决定，服务端强制执行。
    """
    session_id = uuid.uuid4().hex
    now = dt.datetime.now(tz=dt.UTC)
    token = jwt.encode(
        {
            "sid": session_id,
            "alb": album,
            "iat": int(now.timestamp()),
            "exp": int((now + dt.timedelta(hours=settings.session_ttl_hours)).timestamp()),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )
    max_age = settings.session_ttl_hours * 3600
    # HTTPS 环境必须是 True；内网 http 直测阶段用 SESSION_COOKIE_SECURE=false
    # 关掉，否则浏览器拒收 Secure cookie（登录 200 但 session 存不下来 → 全线 401）
    secure = settings.session_cookie_secure
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        secrets.token_hex(16),
        max_age=max_age,
        httponly=False,  # 双提交模式：前端要读它回填请求头
        secure=secure,
        samesite="lax",
        path="/",
    )
    return session_id


def require_session(request: Request, settings: Settings) -> SessionInfo:
    """校验 session。失败抛 401。

    没有 `alb` claim 的旧 token 一律判无效（重新登录一次），
    而不是宽容地当成全相册 —— 权限升级宁可多登录一次，不能多放行一次。
    """
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="需要邀请码")
    try:
        payload: dict[str, Any] = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="会话已失效，请重新输入邀请码") from exc
    sid = payload.get("sid")
    if not isinstance(sid, str) or "alb" not in payload:
        raise HTTPException(status_code=401, detail="会话已失效，请重新输入邀请码")
    album = payload.get("alb")
    if album is not None and not isinstance(album, str):
        raise HTTPException(status_code=401, detail="会话无效")
    return SessionInfo(sid=sid, album=album)


# ------------------------------------------------------------------ CSRF / 设备


def require_csrf(request: Request) -> None:
    """双提交校验：X-CSRF-Token 头必须与 zrc_csrf cookie 相等。"""
    cookie = request.cookies.get(CSRF_COOKIE)
    header = request.headers.get("x-csrf-token")
    if not cookie or not header or not hmac.compare_digest(cookie, header):
        raise HTTPException(status_code=403, detail="请求校验失败，请刷新页面后重试")


def ensure_device_id(request: Request, response: Response, settings: Settings) -> str:
    """取（或首次签发）设备 id cookie，返回 id 供限流使用。

    清 cookie 就能换身份 —— 这层只抬高普通滥用成本，硬边界是 IP/session 限流。
    """
    device = request.cookies.get(DEVICE_COOKIE)
    if device and re.fullmatch(r"[0-9a-f]{32}", device):
        return device
    device = secrets.token_hex(16)
    response.set_cookie(
        DEVICE_COOKIE,
        device,
        max_age=_DEVICE_COOKIE_MAX_AGE,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )
    return device


def audit_hash(value: str, settings: Settings) -> str:
    """给 search_audit 用的带盐 hash。不存 session id / IP 原值。"""
    return hmac.new(settings.audit_hash_salt.encode(), value.encode(), hashlib.sha256).hexdigest()
