"""结构化日志。

⚠️ 日志里只允许出现 id、计数、耗时、错误类型。
**绝不允许**出现 embedding 向量、图片字节、base64、人脸裁剪图。见 CLAUDE.md 约束 #2。

`scrub_processor` 会在序列化前拦掉可疑字段，作为最后一道防线 ——
但它是补救措施，不是许可：不要依赖它，一开始就不要把这些东西传进日志。
"""

from __future__ import annotations

import logging
from collections.abc import MutableMapping
from typing import Any

import structlog

# 字段名里出现这些片段的一律不记录
_FORBIDDEN_KEY_PARTS = (
    "embedding",
    "vector",
    "centroid",
    "image_bytes",
    "selfie",
    "base64",
    "thumb",
    "descriptor",
)

# 超过这个长度的字符串值截断 —— 防止有人把 base64 塞进普通字段
_MAX_VALUE_LEN = 512


def scrub_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        lowered = key.lower()
        if any(part in lowered for part in _FORBIDDEN_KEY_PARTS):
            event_dict[key] = "<redacted>"
            continue
        value = event_dict[key]
        if isinstance(value, str) and len(value) > _MAX_VALUE_LEN:
            event_dict[key] = f"{value[:_MAX_VALUE_LEN]}…<truncated>"
        elif isinstance(value, bytes):
            event_dict[key] = f"<{len(value)} bytes>"
    return event_dict


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", level=getattr(logging, level.upper(), logging.INFO))
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            scrub_processor,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
