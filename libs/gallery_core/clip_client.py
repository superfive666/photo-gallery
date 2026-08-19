"""embedding 服务 CLIP 端点的 HTTP 客户端。

与人脸一样，图文向量化**只**发生在 embedding 服务里（约束 3 延伸到剪辑域）：
`api`（查询文本 → 向量）与 `jobs`（关键帧 → 向量）都必须通过这个客户端调用，
不得在别处再实现一份 resize/归一化 —— 否则离线库和在线查询会落在不同的向量空间里。

两个入口：
  - `encode_texts`   文本批量（在线检索的 query 向量化）
  - `encode_images`  图片批量（离线建库的关键帧向量化）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Self

import httpx

from gallery_core.config import get_settings


class ClipServiceError(RuntimeError):
    """CLIP 端点不可用或返回了非预期结果。"""


@dataclass(frozen=True, slots=True)
class ClipImageResult:
    """一张图的向量化结果。error 非 None 表示这一张失败（解码错误等）。"""

    embedding: list[float] | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ClipBatch:
    results: list[ClipImageResult]
    model_name: str
    model_version: str
    dim: int


class ClipClient:
    """薄封装。生命周期与调用方进程一致，复用连接池。"""

    def __init__(self, base_url: str | None = None, timeout: float | None = None) -> None:
        s = get_settings()
        self._client = httpx.AsyncClient(
            base_url=(base_url or s.embedding_service_url).rstrip("/"),
            timeout=timeout or s.embedding_timeout_seconds,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def available(self) -> bool:
        """CLIP 双塔是否已加载。模型文件缺失时端点会在但报 clip_loaded=false。"""
        try:
            resp = await self._client.get("/healthz")
        except httpx.HTTPError:
            return False
        if resp.status_code != 200:
            return False
        payload: dict[str, Any] = resp.json()
        return bool(payload.get("clip_loaded"))

    async def encode_texts(self, texts: list[str]) -> tuple[list[list[float]], str, str]:
        """文本批量向量化。返回 (向量列表, model_name, model_version)，已 L2 归一化。"""
        if not texts:
            return [], "", ""
        try:
            resp = await self._client.post("/clip/text", json={"texts": texts})
        except httpx.HTTPError as exc:
            raise ClipServiceError(f"CLIP 端点不可达: {exc}") from exc
        if resp.status_code != 200:
            raise ClipServiceError(f"CLIP 端点返回 {resp.status_code}: {resp.text[:200]}")
        payload = resp.json()
        embeddings: list[list[float]] = payload["embeddings"]
        if len(embeddings) != len(texts):
            raise ClipServiceError(
                f"文本批量返回数量不符：发出 {len(texts)} 条，收到 {len(embeddings)} 条"
            )
        return embeddings, payload["model_name"], payload["model_version"]

    async def encode_images(self, images: list[tuple[str, bytes]]) -> ClipBatch:
        """图片批量向量化。返回值与入参一一对应、顺序一致。"""
        if not images:
            return ClipBatch(results=[], model_name="", model_version="", dim=0)
        files = [("images", (name, data, "application/octet-stream")) for name, data in images]
        try:
            resp = await self._client.post("/clip/image/batch", files=files)
        except httpx.HTTPError as exc:
            raise ClipServiceError(f"CLIP 端点不可达: {exc}") from exc
        if resp.status_code != 200:
            raise ClipServiceError(f"CLIP 端点返回 {resp.status_code}: {resp.text[:200]}")
        payload = resp.json()
        results = [
            ClipImageResult(embedding=item.get("embedding"), error=item.get("error"))
            for item in payload["results"]
        ]
        if len(results) != len(images):
            # 顺序对应关系是调用方把向量写回正确 scene 的唯一依据，对不上必须炸而不是猜
            raise ClipServiceError(
                f"图片批量返回数量不符：发出 {len(images)} 张，收到 {len(results)} 条"
            )
        return ClipBatch(
            results=results,
            model_name=payload["model_name"],
            model_version=payload["model_version"],
            dim=int(payload.get("dim", 512)),
        )
