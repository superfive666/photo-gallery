"""缩略图生成。

存二进制 WebP 而非 base64：base64 会带来 33% 的体积膨胀，而且如果内联进搜索响应，
每次检索都要多传几百 KB。二进制存 BYTEA 时 Postgres 的 TOAST 会把 >2KB 的值移到行外，
主堆保持紧凑，顺序扫描不受影响；前端则按 ETag 缓存 /api/photos/{id}/thumb。
"""

from __future__ import annotations

import io

from PIL import Image, ImageOps

# 与 embedding 服务保持一致的解压炸弹上限
Image.MAX_IMAGE_PIXELS = 400_000_000


def make_thumbnail(image_bytes: bytes, max_edge: int, quality: int) -> tuple[bytes, int, int]:
    """返回 (webp_bytes, width, height)。"""
    with Image.open(io.BytesIO(image_bytes)) as src:
        # EXIF 方向要先矫正，否则缩略图会横躺。
        # exif_transpose 在没有方向标记时返回 None，此时用原图。
        oriented: Image.Image = ImageOps.exif_transpose(src) or src
        thumb = oriented.convert("RGB")
        thumb.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        # method=6 是最慢但压缩率最好的档。缩略图只生成一次、被读很多次，值得。
        thumb.save(buf, format="WEBP", quality=quality, method=6)
        return buf.getvalue(), thumb.width, thumb.height
