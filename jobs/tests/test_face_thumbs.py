"""人脸小图裁剪的关键性质：bbox 坐标系必须与 embedding 服务一致（exif_transpose 后）。

竖拍照片带 EXIF Orientation=6：解码原始像素是横的，矫正后才是竖的。
embedding 服务在矫正后的坐标系里给 bbox —— 裁剪若不做同样的矫正，框整体错位 90°。
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from jobs.thumbnails import FACE_THUMB_EDGE, crop_face


def _jpeg(width: int, height: int, orientation: int | None = None) -> bytes:
    img = Image.new("RGB", (width, height), (10, 20, 30))
    # 左上角画一块白色，用来验证裁剪位置
    for x in range(min(60, width)):
        for y in range(min(60, height)):
            img.putpixel((x, y), (255, 255, 255))
    buf = io.BytesIO()
    exif = Image.Exif()
    if orientation is not None:
        exif[0x0112] = orientation
    img.save(buf, format="JPEG", exif=exif.tobytes())
    return buf.getvalue()


def test_crop_returns_webp_within_edge() -> None:
    data = crop_face(_jpeg(400, 300), (0, 0, 60, 60))
    with Image.open(io.BytesIO(data)) as out:
        assert out.format == "WEBP"
        assert max(out.size) <= FACE_THUMB_EDGE


def test_crop_respects_exif_orientation() -> None:
    """Orientation=6（顺时针 90°）：矫正后 400x300 变 300x400。
    矫正后坐标系里 (250, 350) 附近的框必须能裁 —— 不矫正的话 y=350 超出原始高度 300。
    """
    payload = _jpeg(400, 300, orientation=6)
    data = crop_face(payload, (240, 340, 50, 50))
    with Image.open(io.BytesIO(data)) as out:
        assert out.format == "WEBP"


def test_bbox_fully_outside_raises() -> None:
    with pytest.raises(ValueError):
        crop_face(_jpeg(100, 100), (500, 500, 50, 50))


def test_margin_clamped_at_image_border() -> None:
    """贴边的脸：外扩后夹回边界，不报错、不产生负坐标。"""
    data = crop_face(_jpeg(200, 200), (0, 0, 40, 40))
    with Image.open(io.BytesIO(data)) as out:
        assert out.size[0] > 0 and out.size[1] > 0
