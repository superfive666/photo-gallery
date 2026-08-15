"""登录用的自研 captcha：扰动 SVG + 无状态 HMAC token。

为什么不用 reCAPTCHA/hCaptcha：前端 CSP 是 `default-src 'self'`，引第三方脚本
要开洞，而且把「谁在用人脸检索」告诉了第三方 —— 与本项目的隐私立场冲突。
威胁模型是「抬高脚本批量登录/爆破的成本」，不是对抗专业打码平台。

工作方式：
  签发   GET /session/captcha → 随机 4 字符 → SVG（扰动渲染）+ token
         token = base64(nonce.exp).hmac(answer|nonce|exp)  —— 答案不在 token 里
  验证   登录时带 token + 用户输入的答案，服务端重算 HMAC 比对，无状态。
  单次   进程内 used-nonce 集合（带过期清理）。单实例 api 足够 ——
         与限流器同一假设，横向扩容时一并换 Redis。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time

# 去掉易混淆字符（0O1I、B8、5S、2Z）。全大写比对，用户大小写随意。
_ALPHABET = "34679ACDEFHJKLMNPQRTUVWXY"
_CODE_LEN = 4

_used_nonces: dict[str, float] = {}
_lock = threading.Lock()


def _sign(answer: str, nonce: str, expires_at: int, secret: str) -> str:
    msg = f"{answer.upper()}|{nonce}|{expires_at}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def issue(secret: str, ttl_seconds: int) -> tuple[str, str]:
    """生成一张 captcha。返回 (答案, token)。答案交给渲染层画图，绝不进响应体。"""
    answer = "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LEN))
    nonce = secrets.token_hex(8)
    expires_at = int(time.time()) + ttl_seconds
    header = base64.urlsafe_b64encode(f"{nonce}.{expires_at}".encode()).decode()
    token = f"{header}.{_sign(answer, nonce, expires_at, secret)}"
    return answer, token


def verify(token: str, answer: str, secret: str) -> bool:
    """校验答案。过期 / 篡改 / 重放都返回 False —— 调用方统一给一句友好文案。"""
    try:
        header, signature = token.rsplit(".", 1)
        nonce, raw_exp = base64.urlsafe_b64decode(header.encode()).decode().split(".")
        expires_at = int(raw_exp)
    except (ValueError, UnicodeDecodeError):
        return False

    now = time.time()
    if now > expires_at:
        return False
    if not hmac.compare_digest(signature, _sign(answer, nonce, expires_at, secret)):
        return False

    # 通过校验后才登记 nonce：答错不烧号，用户可以对同一张图重试
    with _lock:
        _prune_locked(now)
        if nonce in _used_nonces:
            return False
        _used_nonces[nonce] = expires_at
    return True


def _prune_locked(now: float) -> None:
    expired = [n for n, exp in _used_nonces.items() if exp < now]
    for n in expired:
        del _used_nonces[n]


def render_svg(answer: str) -> str:
    """把答案渲染成带扰动的 SVG。

    每个字符独立随机旋转/偏移/配色，再叠两条干扰线。目标是挡住「直接读文本」
    和朴素 OCR 的脚本 —— 不是对抗专业识别（见模块 docstring 的威胁模型）。
    """
    width, height = 200, 72
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="验证码图片">',
        '<rect width="100%" height="100%" fill="#16181d"/>',
    ]
    rnd = secrets.SystemRandom()
    for line in range(2):
        y1, y2 = rnd.randint(10, height - 10), rnd.randint(10, height - 10)
        mid = rnd.randint(20, width - 20)
        parts.append(
            f'<path d="M0 {y1} Q {mid} {rnd.randint(0, height)} {width} {y2}" '
            f'stroke="#3a3f4d" stroke-width="{1 + line}" fill="none"/>'
        )
    step = width // (_CODE_LEN + 1)
    for i, ch in enumerate(answer):
        x = step * (i + 1) + rnd.randint(-6, 6)
        y = height // 2 + rnd.randint(-8, 8)
        angle = rnd.randint(-28, 28)
        hue = rnd.randint(190, 280)
        parts.append(
            f'<text x="{x}" y="{y}" font-family="monospace" font-size="{rnd.randint(30, 38)}" '
            f'font-weight="bold" fill="hsl({hue} 45% 72%)" text-anchor="middle" '
            f'dominant-baseline="central" transform="rotate({angle} {x} {y})">{ch}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)
