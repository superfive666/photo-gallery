"""api 与 jobs 共享的核心代码。

这个包存在的唯一理由：防止 `api` 与 `jobs` 各自维护一份 DB 模型 / 向量处理 / 阈值常量。
复制两份的失效模式极其隐蔽 —— 只在一边改了归一化或阈值，在线检索仍会「正常返回结果」，
只是结果全错，没有任何测试会红。详见 docs/architecture.md。
"""

from gallery_core.config import Settings, get_settings
from gallery_core.embedding_client import (
    DetectedFace,
    EmbeddingClient,
    EmbeddingServiceError,
    ExtractResult,
)
from gallery_core.uuid7 import uuid7
from gallery_core.vector import l2_normalize, mean_embedding

__all__ = [
    "DetectedFace",
    "EmbeddingClient",
    "EmbeddingServiceError",
    "ExtractResult",
    "Settings",
    "get_settings",
    "l2_normalize",
    "mean_embedding",
    "uuid7",
]
