"""captcha 的安全性质：签名不可伪造、会过期、验证通过即作废（防重放）。

答错**不**作废 —— 用户可以对同一张图重试，只有验证成功才登记 nonce。
这一条写成测试是因为它反直觉：先烧号再验答案的实现更「顺手」，但会让
手滑输错一次的用户必须换图重来。
"""

from __future__ import annotations

import time
from unittest.mock import patch

from api.app import captcha

SECRET = "test-secret-key-for-captcha"


def test_roundtrip() -> None:
    answer, token = captcha.issue(SECRET, ttl_seconds=60)
    assert captcha.verify(token, answer, SECRET) is True


def test_case_insensitive() -> None:
    answer, token = captcha.issue(SECRET, ttl_seconds=60)
    assert captcha.verify(token, answer.lower(), SECRET) is True


def test_wrong_answer_rejected_but_retryable() -> None:
    answer, token = captcha.issue(SECRET, ttl_seconds=60)
    assert captcha.verify(token, "WRNG", SECRET) is False
    # 答错不烧号：同一张图用正确答案仍然能过
    assert captcha.verify(token, answer, SECRET) is True


def test_replay_rejected() -> None:
    answer, token = captcha.issue(SECRET, ttl_seconds=60)
    assert captcha.verify(token, answer, SECRET) is True
    # 验证通过即作废 —— 拿同一个 token 重放必须失败
    assert captcha.verify(token, answer, SECRET) is False


def test_expired_rejected() -> None:
    answer, token = captcha.issue(SECRET, ttl_seconds=60)
    with patch("api.app.captcha.time") as mock_time:
        mock_time.time.return_value = time.time() + 61
        assert captcha.verify(token, answer, SECRET) is False


def test_tampered_token_rejected() -> None:
    answer, token = captcha.issue(SECRET, ttl_seconds=60)
    header, signature = token.rsplit(".", 1)
    flipped = ("0" if signature[0] != "0" else "1") + signature[1:]
    assert captcha.verify(f"{header}.{flipped}", answer, SECRET) is False


def test_wrong_secret_rejected() -> None:
    answer, token = captcha.issue(SECRET, ttl_seconds=60)
    assert captcha.verify(token, answer, "another-secret") is False


def test_malformed_tokens_do_not_crash() -> None:
    for junk in ("", "abc", "a.b", "!!!.???", "0" * 200):
        assert captcha.verify(junk, "ABCD", SECRET) is False


def test_answer_not_in_token() -> None:
    """token 是发给客户端的 —— 答案绝不能从里面还原出来。"""
    answer, token = captcha.issue(SECRET, ttl_seconds=60)
    assert answer not in token
    assert answer.lower() not in token


def test_svg_renders_all_answer_chars() -> None:
    answer, _ = captcha.issue(SECRET, ttl_seconds=60)
    svg = captcha.render_svg(answer)
    assert svg.startswith("<svg")
    for ch in answer:
        assert ch in svg
