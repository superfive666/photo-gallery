"""上传的自拍的校验。

⚠️ 这个模块处理的字节是**用户人脸**。全流程只在内存中，任何情况下都不得写盘、写库、写日志。
见 CLAUDE.md 约束 #1 与 api/tests/test_no_persistence.py。
"""

from __future__ import annotations

from fastapi import HTTPException, UploadFile

from gallery_core.config import Settings

# 只接受这几种。用文件头判断，不信客户端给的 Content-Type。
_MAGIC_PREFIXES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"RIFF", "image/webp"),  # 需再校验偏移 8 处的 'WEBP'
)
# HEIC/HEIF（iPhone 默认格式）：ftyp box 里的 brand
_HEIF_BRANDS = (b"heic", b"heix", b"hevc", b"mif1", b"msf1")


def sniff_mime(head: bytes) -> str | None:
    """按文件头判断真实类型。返回 None 表示不受支持。"""
    for prefix, mime in _MAGIC_PREFIXES:
        if head.startswith(prefix):
            if mime == "image/webp" and head[8:12] != b"WEBP":
                continue
            return mime
    if len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in _HEIF_BRANDS:
        return "image/heif"
    return None


async def read_selfie(upload: UploadFile, settings: Settings) -> bytes:
    """读取并校验一张自拍，返回其字节。

    大小上限在读取时就卡住，不先整体读进内存再判断 —— 否则上限形同虚设。
    """
    limit = settings.max_upload_bytes
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(64 * 1024):
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status_code=413,
                detail=f"图片过大，请压缩到 {limit // (1024 * 1024)}MB 以内",
            )
        chunks.append(chunk)

    if total == 0:
        raise HTTPException(status_code=400, detail="空文件")

    payload = b"".join(chunks)
    if sniff_mime(payload[:16]) is None:
        raise HTTPException(status_code=415, detail="只支持 JPEG / PNG / WebP / HEIC 格式的图片")
    return payload


def validate_count(uploads: list[UploadFile], settings: Settings) -> None:
    if not uploads:
        raise HTTPException(status_code=400, detail="请至少上传一张自拍")
    if len(uploads) > settings.max_selfies_per_search:
        raise HTTPException(
            status_code=400,
            detail=f"最多支持 {settings.max_selfies_per_search} 张自拍",
        )
