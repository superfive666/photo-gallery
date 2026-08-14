"""邀请码校验的三种失败形态必须可区分：

  · 输错邀请码        → False（路由层转成 401）
  · hash 未配置       → False（fail-closed，同上）
  · hash 本身格式非法 → HTTP 500 + 明确日志（运维配置错误，不能伪装成 401）

第三种在真实部署中出现过一次：argon2 hash 里全是 $，docker compose 读 .env 时
把 $argon2id / $v / $m 当变量引用替换成空串，容器拿到一个被啃烂的 hash。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from api.app.auth import hash_invite_code, verify_invite_code
from gallery_core.config import Settings


def _settings(invite_hash: str) -> Settings:
    return Settings(invite_code_hash=invite_hash)


def test_correct_code_verifies() -> None:
    s = _settings(hash_invite_code("sesame"))
    assert verify_invite_code("sesame", s) is True


def test_wrong_code_is_false_not_error() -> None:
    s = _settings(hash_invite_code("sesame"))
    assert verify_invite_code("open-barley", s) is False


def test_missing_hash_fails_closed() -> None:
    assert verify_invite_code("anything", _settings("")) is False


def test_compose_mangled_hash_raises_500_with_hint() -> None:
    # 模拟 compose 插值：$argon2id / $v / $m 等 bare-$ 引用被替换成空串
    import re

    mangled = re.sub(r"\$[A-Za-z_][A-Za-z0-9_]*", "", hash_invite_code("sesame"))
    with pytest.raises(HTTPException) as exc_info:
        verify_invite_code("sesame", _settings(mangled))
    assert exc_info.value.status_code == 500
    # 对外的信息不泄漏配置细节
    assert "argon2" not in exc_info.value.detail


def test_hash_invite_tool_output_is_env_safe() -> None:
    # 工具输出的整行必须带单引号 —— 否则贴进 .env 又会被 compose 啃一遍
    line = f"INVITE_CODE_HASH='{hash_invite_code('sesame')}'"
    assert line.startswith("INVITE_CODE_HASH='$argon2id$")
    assert line.endswith("'")


def test_session_cookie_secure_flag_respected() -> None:
    """http 直测阶段 SESSION_COOKIE_SECURE=false 时，cookie 不能带 Secure ——
    否则浏览器拒收，表现为「登录 200 但下一个请求 401」（真实发生过）。"""
    from fastapi import Response

    from api.app.auth import issue_session

    for secure in (True, False):
        resp = Response()
        issue_session(resp, Settings(session_cookie_secure=secure))
        header = resp.headers["set-cookie"]
        assert ("Secure" in header) is secure, header
