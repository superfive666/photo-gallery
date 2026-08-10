"""`embedding` 服务的 HTTP 客户端。

`api` 与 `jobs` 都只能通过这个客户端获取人脸与向量。**不要**在任何其他地方实现
人脸检测、对齐、resize 或归一化 —— 那会让离线库和在线查询落在不同的向量空间里，
表现为「检索能跑但结果全错」，且极难定位。见 CLAUDE.md 约束 #3。

两个入口：
  - `extract`       单张，在线检索用，延迟优先
  - `extract_batch` 批量，离线建库用，吞吐优先（整批人脸拼一次识别前向）
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
    # 非 None 表示这一张处理失败（解码/检测出错），而不是「没检测到人脸」。
    # 调用方必须区分：前者标 failed 待重试，后者是正常的 0 张脸。
    error: str | None = None


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

    async def health(self) -> dict[str, Any]:
        try:
            resp = await self._client.get("/healthz")
        except httpx.HTTPError as exc:
            raise EmbeddingServiceError(f"embedding 服务不可达: {exc}") from exc
        if resp.status_code != 200:
            raise EmbeddingServiceError(f"healthz 返回 {resp.status_code}")
        payload: dict[str, Any] = resp.json()
        return payload

    async def healthy(self) -> bool:
        try:
            return bool((await self.health()).get("model_loaded"))
        except EmbeddingServiceError:
            return False

    async def max_batch_images(self) -> int:
        """服务端允许的单次批量上限。jobs 用它决定分块大小，避免硬编码两处。"""
        try:
            return int((await self.health()).get("max_batch_images", 1))
        except (EmbeddingServiceError, TypeError, ValueError):
            return 1

    # ------------------------------------------------------------------ 单张

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
        return _parse_image_result(payload, payload["model_name"], payload["model_version"])

    # ------------------------------------------------------------------ 批量

    async def extract_batch(self, images: list[tuple[str, bytes]]) -> list[ExtractResult]:
        """一批 (filename, bytes)。返回值与入参一一对应、顺序一致。

        整批照片里的人脸会在服务端拼成一个 batch 做一次识别前向 —— 这是离线建库
        提速的主要来源，GPU 上尤其明显。
        """
        if not images:
            return []

        files = [("images", (name, data, "application/octet-stream")) for name, data in images]
        try:
            resp = await self._client.post("/extract/batch", files=files)
        except httpx.HTTPError as exc:
            raise EmbeddingServiceError(f"embedding 服务不可达: {exc}") from exc

        if resp.status_code != 200:
            raise EmbeddingServiceError(f"embedding 服务返回 {resp.status_code}: {resp.text[:200]}")

        payload = resp.json()
        results = [
            _parse_image_result(item, payload["model_name"], payload["model_version"])
            for item in payload["results"]
        ]
        if len(results) != len(images):
            # 顺序对应关系是调用方把结果写回正确照片的唯一依据，对不上必须炸而不是猜
            raise EmbeddingServiceError(
                f"批量返回数量不符：发出 {len(images)} 张，收到 {len(results)} 条"
            )
        return results


def _parse_image_result(item: dict[str, Any], model_name: str, model_version: str) -> ExtractResult:
    faces = []
    for f in item["faces"]:
        x, y, w, h = f["bbox"]
        faces.append(
            DetectedFace(
                bbox=(int(x), int(y), int(w), int(h)),
                det_score=float(f["det_score"]),
                face_px=int(f["face_px"]),
                embedding=f["embedding"],
                landmarks=f.get("landmarks"),
                model_name=model_name,
                model_version=model_version,
            )
        )
    return ExtractResult(
        faces=faces,
        discarded=item.get("discarded", 0),
        image_width=item.get("image_width", 0),
        image_height=item.get("image_height", 0),
        error=item.get("error"),
    )
