"""`embedding` 服务的 HTTP 客户端。

`api` 与 `jobs` 都只能通过这个客户端获取人脸与向量。**不要**在任何其他地方实现
人脸检测、对齐、resize 或归一化 —— 那会让离线库和在线查询落在不同的向量空间里，
表现为「检索能跑但结果全错」，且极难定位。见 CLAUDE.md 约束 #3。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Self

import httpx

from gallery_core.config import get_settings


class EmbeddingServiceError(RuntimeError):
    """embedding 服务不可用或返回了非预期结果。"""


@dataclass(frozen=True, slots=True)
class DetectedFace:
    """一张检测到的人脸。`embedding` 已在服务端完成 L2 归一化。"""

    bbox: tuple[int, int, int, int]  # x, y, w, h
    det_score: float
    face_px: int
    embedding: list[float]
    landmarks: dict[str, Any] | None
    model_name: str
    model_version: str

    @property
    def dim(self) -> int:
        return len(self.embedding)


@dataclass(frozen=True, slots=True)
class ExtractResult:
    faces: list[DetectedFace]
    # 通过检测但被质量门控丢弃的人脸数。「后排的人搜不到」的量化依据，
    # 会写进 photo.faces_discarded 与 job_run.stats。
    discarded: int
    image_width: int
    image_height: int


class EmbeddingClient:
    """薄封装。生命周期与调用方进程一致，复用连接池。"""

    def __init__(self, base_url: str | None = None, timeout: float | None = None) -> None:
        s = get_settings()
        self._base_url = (base_url or s.embedding_service_url).rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout or s.embedding_timeout_seconds,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def healthy(self) -> bool:
        try:
            resp = await self._client.get("/healthz")
            return resp.status_code == 200 and bool(resp.json().get("model_loaded"))
        except httpx.HTTPError:
            return False

    async def extract(self, image_bytes: bytes, filename: str = "upload") -> ExtractResult:
        """提取图片中的全部人脸。

        调用方不需要（也不应该）对 image_bytes 做任何预处理 —— resize、EXIF 旋转矫正、
        色彩空间转换全部由服务端负责，以保证离线与在线完全一致。
        """
        try:
            resp = await self._client.post(
                "/extract",
                files={"image": (filename, image_bytes, "application/octet-stream")},
            )
        except httpx.HTTPError as exc:
            raise EmbeddingServiceError(f"embedding 服务不可达: {exc}") from exc

        if resp.status_code != 200:
            raise EmbeddingServiceError(f"embedding 服务返回 {resp.status_code}: {resp.text[:200]}")

        payload = resp.json()
        faces = []
        for f in payload["faces"]:
            x, y, w, h = f["bbox"]
            faces.append(
                DetectedFace(
                    bbox=(int(x), int(y), int(w), int(h)),
                    det_score=float(f["det_score"]),
                    face_px=int(f["face_px"]),
                    embedding=f["embedding"],
                    landmarks=f.get("landmarks"),
                    model_name=payload["model_name"],
                    model_version=payload["model_version"],
                )
            )
        return ExtractResult(
            faces=faces,
            discarded=payload.get("discarded", 0),
            image_width=payload.get("image_width", 0),
            image_height=payload.get("image_height", 0),
        )
