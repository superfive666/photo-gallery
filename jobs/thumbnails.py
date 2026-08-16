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


# 人脸小图的参数不做配置项：160px 够浏览弹层里认人，margin 让下巴/头发进画面。
# 改这两个值不影响已存的小图 —— 想统一就重跑 `jobs face-thumbs --full`。
FACE_THUMB_EDGE = 160
FACE_THUMB_MARGIN = 0.35


def crop_face(image_bytes: bytes, bbox: tuple[int, int, int, int]) -> bytes:
    """按 bbox 从原图裁出人脸小图（WebP）。

    ⚠️ bbox 是 embedding 服务在 **exif_transpose 之后**的坐标系里给出的
    （见 embedding/app/model.py），这里必须做同样的方向矫正再裁，
    否则竖拍照片的框会整体错位 90°。

    这是展示用途的裁剪，不喂给任何模型 —— 不属于约束 #3 说的识别预处理。
    """
    x, y, w, h = bbox
    with Image.open(io.BytesIO(image_bytes)) as src:
        oriented: Image.Image = ImageOps.exif_transpose(src) or src
        img = oriented.convert("RGB")
        # 外扩 margin 后夹回图像边界。bbox 来自检测器，贴脸太紧，直接裁很难认。
        mx, my = int(w * FACE_THUMB_MARGIN), int(h * FACE_THUMB_MARGIN)
        left = max(0, x - mx)
        top = max(0, y - my)
        right = min(img.width, x + w + mx)
        bottom = min(img.height, y + h + my)
        if right <= left or bottom <= top:
            raise ValueError(f"bbox 落在图像之外: {bbox} vs {img.width}x{img.height}")
        crop = img.crop((left, top, right, bottom))
        crop.thumbnail((FACE_THUMB_EDGE, FACE_THUMB_EDGE), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        crop.save(buf, format="WEBP", quality=82, method=6)
        return buf.getvalue()
