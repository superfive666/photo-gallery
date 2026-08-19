"""双角色互不越权：查找码进不了剪辑接口，剪辑码进不了查找接口。

一码一相册的权限语义靠这里守住 —— 角色在 invite_code.role 上，登录时写进 JWT
的 role/wid claim；接口只认 token，不认请求参数。
"""

from __future__ import annotations

import datetime as dt
import uuid

import jwt as pyjwt
import pytest
from fastapi import HTTPException, Response
from starlette.requests import Request

from api.app.auth import (
    ROLE_EDIT,
    SESSION_COOKIE,
    issue_session,
    require_edit_session,
    require_session,
)
from gallery_core.config import Settings

_SETTINGS = Settings(  # type: ignore[call-arg]
    jwt_secret="test-secret-0123456789abcdef-0123456789",
    _env_file=None,
)

_WID = str(uuid.uuid4())


def _issue(**kwargs: str | None) -> str:
    """签发一枚 token，返回 cookie 值。"""
    response = Response()
    issue_session(response, _SETTINGS, **kwargs)  # type: ignore[arg-type]
    set_cookie = response.headers["set-cookie"]
    assert SESSION_COOKIE in set_cookie
    return set_cookie.split(f"{SESSION_COOKIE}=", 1)[1].split(";", 1)[0]


def _request_with_cookie(token: str | None) -> Request:
    headers = [] if token is None else [(b"cookie", f"{SESSION_COOKIE}={token}".encode())]
    return Request({"type": "http", "headers": headers, "method": "GET", "path": "/"})


def test_search_token_ok_for_search() -> None:
    token = _issue(album="2026-08-10")
    info = require_session(_request_with_cookie(token), _SETTINGS)
    assert info.album == "2026-08-10"


def test_legacy_token_without_role_is_search() -> None:
    """存量 token 无 role claim → 按 search 处理，老会话不失效。"""
    now = dt.datetime.now(tz=dt.UTC)
    token = pyjwt.encode(
        {
            "sid": "legacy",
            "alb": None,
            "dev": "d" * 32,
            "iat": int(now.timestamp()),
            "exp": int(now.timestamp()) + 600,
        },
        _SETTINGS.jwt_secret,
        algorithm="HS256",
    )
    assert require_session(_request_with_cookie(token), _SETTINGS).sid == "legacy"
    with pytest.raises(HTTPException) as exc:
        require_edit_session(_request_with_cookie(token), _SETTINGS)
    assert exc.value.status_code == 403


def test_edit_token_rejected_on_search_endpoints() -> None:
    token = _issue(album="2026-08-10", role=ROLE_EDIT, workspace_id=_WID)
    with pytest.raises(HTTPException) as exc:
        require_session(_request_with_cookie(token), _SETTINGS)
    assert exc.value.status_code == 403


def test_search_token_rejected_on_edit_endpoints() -> None:
    token = _issue(album="2026-08-10")
    with pytest.raises(HTTPException) as exc:
        require_edit_session(_request_with_cookie(token), _SETTINGS)
    assert exc.value.status_code == 403


def test_edit_token_carries_workspace_and_album() -> None:
    token = _issue(album="2026-08-10", role=ROLE_EDIT, workspace_id=_WID)
    es = require_edit_session(_request_with_cookie(token), _SETTINGS)
    assert str(es.workspace_id) == _WID
    assert es.album == "2026-08-10"
    assert es.dev


def test_missing_cookie_is_401() -> None:
    for checker in (require_session, require_edit_session):
        with pytest.raises(HTTPException) as exc:
            checker(_request_with_cookie(None), _SETTINGS)
        assert exc.value.status_code == 401
