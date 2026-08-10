"""原图的短效签名链接。

绝不把源站裸地址直接返回给前端 —— 那等于把 photos.zrc.sg 的访问控制绕过，
private 相册会顺着检索结果泄露出去。见 CLAUDE.md 约束 #7。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

from gallery_core.config import Settings


def _mac(photo_id: str, expires_at: int, settings: Settings) -> str:
    digest = hmac.new(
        settings.signed_url_secret.encode(),
        f"{photo_id}:{expires_at}".encode(),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def sign(photo_id: str, settings: Settings) -> tuple[str, int]:
    """返回 (signature, expires_at)。"""
    expires_at = int(time.time()) + settings.signed_url_ttl_seconds
    return _mac(photo_id, expires_at, settings), expires_at


def verify(photo_id: str, expires_at: int, signature: str, settings: Settings) -> bool:
    if expires_at < int(time.time()):
        return False
    # 常量时间比较，避免时序侧信道
    return hmac.compare_digest(_mac(photo_id, expires_at, settings), signature)
