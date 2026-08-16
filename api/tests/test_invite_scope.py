"""邀请码-相册绑定的边界：JWT scope、CSRF 双提交、检索侧的强制校验。

这些都是安全边界 —— 测的是「越权必须被挡下」，不是「正常路径能跑通」。
"""

from __future__ import annotations

from http.cookies import SimpleCookie

import pytest
from fastapi import HTTPException, Response
from starlette.requests import Request

from api.app.auth import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    SessionInfo,
    generate_invite_code,
    issue_session,
    require_csrf,
    require_session,
    split_invite_code,
    verify_code_hash,
)
from api.app.routers.search import _enforce_scope
from gallery_core.config import Settings


def _request(
    cookies: dict[str, str] | None = None, headers: dict[str, str] | None = None
) -> Request:
    raw: list[tuple[bytes, bytes]] = []
    if cookies:
        raw.append((b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()))
    for key, value in (headers or {}).items():
        raw.append((key.lower().encode(), value.encode()))
    return Request({"type": "http", "method": "GET", "path": "/", "headers": raw})


def _cookies_from(response: Response) -> dict[str, str]:
    jar: SimpleCookie = SimpleCookie()
    for header in response.headers.getlist("set-cookie"):
        jar.load(header)
    return {name: morsel.value for name, morsel in jar.items()}


# ------------------------------------------------------------------- 码的形态


def test_generated_code_roundtrips() -> None:
    full, prefix, code_hash = generate_invite_code()
    parts = split_invite_code(full)
    assert parts is not None
    got_prefix, secret = parts
    assert got_prefix == prefix
    assert verify_code_hash(code_hash, secret) is True
    assert verify_code_hash(code_hash, secret + "x") is False


@pytest.mark.parametrize(
    "code",
    [
        "no-dot-here",  # 旧全局码形态
        "shrt.secret",  # prefix 不是 8 位 hex
        "GHIJKLMN.secret",  # 非 hex 字符
        "0123abcd.",  # secret 为空
        ".secret",  # prefix 为空
    ],
)
def test_non_scoped_forms_fall_back_to_legacy(code: str) -> None:
    assert split_invite_code(code) is None


# ------------------------------------------------------------- session 的 scope


def test_session_carries_album_scope() -> None:
    settings = Settings(jwt_secret="unit-test-secret-32-bytes-long!!")
    response = Response()
    issue_session(response, settings, album="2026-08-10")
    cookies = _cookies_from(response)
    info = require_session(_request(cookies={SESSION_COOKIE: cookies[SESSION_COOKIE]}), settings)
    assert info.album == "2026-08-10"


def test_session_without_album_is_unscoped() -> None:
    settings = Settings(jwt_secret="unit-test-secret-32-bytes-long!!")
    response = Response()
    issue_session(response, settings, album=None)
    cookies = _cookies_from(response)
    info = require_session(_request(cookies={SESSION_COOKIE: cookies[SESSION_COOKIE]}), settings)
    assert info.album is None


def test_legacy_token_without_alb_claim_rejected() -> None:
    """权限升级前签发的旧 token 没有 alb claim —— 必须判无效而不是当成全相册。"""
    import datetime as dt

    import jwt

    settings = Settings(jwt_secret="unit-test-secret-32-bytes-long!!")
    now = dt.datetime.now(tz=dt.UTC)
    old_token = jwt.encode(
        {
            "sid": "legacy",
            "iat": int(now.timestamp()),
            "exp": int((now + dt.timedelta(hours=1)).timestamp()),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )
    with pytest.raises(HTTPException) as exc_info:
        require_session(_request(cookies={SESSION_COOKIE: old_token}), settings)
    assert exc_info.value.status_code == 401


def test_session_binds_device_id_into_jwt() -> None:
    """设备 id 进 JWT：检索限流的键取自 token 而不是请求 cookie ——
    脚本不带 cookie / 清 cookie 都换不来新设备身份，重登要过 captcha。"""
    settings = Settings(jwt_secret="unit-test-secret-32-bytes-long!!")
    response = Response()
    issue_session(response, settings, device="dev-abc123")
    cookies = _cookies_from(response)
    info = require_session(_request(cookies={SESSION_COOKIE: cookies[SESSION_COOKIE]}), settings)
    assert info.dev == "dev-abc123"


def test_token_without_dev_claim_rejected() -> None:
    """0007 之前的 token 没有 dev claim —— 判无效，不宽容地补默认值。"""
    import datetime as dt

    import jwt

    settings = Settings(jwt_secret="unit-test-secret-32-bytes-long!!")
    now = dt.datetime.now(tz=dt.UTC)
    old_token = jwt.encode(
        {
            "sid": "legacy",
            "alb": None,
            "iat": int(now.timestamp()),
            "exp": int((now + dt.timedelta(hours=1)).timestamp()),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )
    with pytest.raises(HTTPException) as exc_info:
        require_session(_request(cookies={SESSION_COOKIE: old_token}), settings)
    assert exc_info.value.status_code == 401


# ------------------------------------------------------------- 检索的强制校验


def test_scoped_session_forces_album_when_unset() -> None:
    session = SessionInfo(sid="s", album="2026-08-10", dev="d")
    assert _enforce_scope(None, session) == "2026-08-10"


def test_scoped_session_rejects_other_album_with_403() -> None:
    session = SessionInfo(sid="s", album="2026-08-10", dev="d")
    with pytest.raises(HTTPException) as exc_info:
        _enforce_scope("2026-01-01", session)
    assert exc_info.value.status_code == 403


def test_scoped_session_allows_matching_album() -> None:
    session = SessionInfo(sid="s", album="2026-08-10", dev="d")
    assert _enforce_scope("2026-08-10", session) == "2026-08-10"


def test_unscoped_session_passes_through() -> None:
    session = SessionInfo(sid="s", album=None, dev="d")
    assert _enforce_scope("2026-01-01", session) == "2026-01-01"
    assert _enforce_scope(None, session) is None


# ------------------------------------------------------------------------ CSRF


def test_csrf_matching_header_passes() -> None:
    require_csrf(_request(cookies={CSRF_COOKIE: "tok123"}, headers={"X-CSRF-Token": "tok123"}))


@pytest.mark.parametrize(
    ("cookies", "headers"),
    [
        ({}, {}),  # 都没有
        ({CSRF_COOKIE: "tok123"}, {}),  # 只有 cookie（典型的跨站伪造请求）
        ({}, {"X-CSRF-Token": "tok123"}),  # 只有头
        ({CSRF_COOKIE: "tok123"}, {"X-CSRF-Token": "other"}),  # 不匹配
    ],
)
def test_csrf_rejects_missing_or_mismatched(
    cookies: dict[str, str], headers: dict[str, str]
) -> None:
    with pytest.raises(HTTPException) as exc_info:
        require_csrf(_request(cookies=cookies, headers=headers))
    assert exc_info.value.status_code == 403
