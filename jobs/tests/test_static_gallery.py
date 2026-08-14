"""相册页解析的回归测试。

HTML 样例来自 photos.zrc.sg 的真实相册页（2026-08-14 由站主提供），
data-lightbox 那段视频条目是**原样复制**的 —— 包括 onclick 里的转义反斜杠。
页面结构若变更，这里会先红。
"""

from __future__ import annotations

from jobs.sources.base import SourceAsset
from jobs.sources.static_gallery import StaticGalleryAdapter

PAGE_URL = "https://photos.zrc.sg/album/2026-08-12"

# 真实的视频条目（原样），加一个按同一结构合成的照片条目
ALBUM_HTML = """
<html><body>
<div class="grid">
  <div class="group relative aspect-[4/5] cursor-pointer overflow-hidden rounded-lg sm:rounded-xl bg-muted shadow-md hover:shadow-xl"
       onclick="ZephyrLightbox.show('\\/album\\/2026-08-12\\/20260812215627863.mp4',  true , 'Stone', '\\/album\\/2026-08-12\\/20260812215627863.mp4', null)"
       data-thumb="/album/thumb/2026-08-12/20260812215627863.mp4"
       data-id="20260812215627863" data-lightbox
       data-src="/album/2026-08-12/20260812215627863.mp4"
       data-is-video="true" data-uploader="Stone"
       data-original-src="/album/2026-08-12/20260812215627863.mp4"></div>
  <div class="group relative aspect-[4/5] cursor-pointer"
       data-thumb="/album/thumb/2026-08-12/20260812101112000.jpg"
       data-id="20260812101112000" data-lightbox
       data-src="/album/2026-08-12/20260812101112000.jpg"
       data-is-video="false" data-uploader="Ann"
       data-original-src="/album/2026-08-12/20260812101112000.jpg"></div>
  <div class="sidebar"><img src="/static/logo.png"><a href="/about">关于</a></div>
</div>
</body></html>
"""


def _parse(html: str) -> list[SourceAsset]:
    adapter = StaticGalleryAdapter(base_url="https://photos.zrc.sg")
    return adapter._parse_album_page("2026-08-12", PAGE_URL, html)


def test_parses_lightbox_items_only() -> None:
    assets = _parse(ALBUM_HTML)
    # 侧栏的 logo/链接不是 data-lightbox 节点，不该被捞进来
    assert len(assets) == 2


def test_video_kept_as_video_kind() -> None:
    """视频照常产出但 kind=video —— pipeline 对它只登记不提取，这是建库过滤的实现点。"""
    video = next(a for a in _parse(ALBUM_HTML) if a.filename.endswith(".mp4"))
    assert video.kind == "video"
    assert video.photo_url == "https://photos.zrc.sg/album/2026-08-12/20260812215627863.mp4"
    assert (
        video.thumbnail_url == "https://photos.zrc.sg/album/thumb/2026-08-12/20260812215627863.mp4"
    )


def test_photo_fields_mapped() -> None:
    photo = next(a for a in _parse(ALBUM_HTML) if a.filename.endswith(".jpg"))
    assert photo.kind == "image"
    assert photo.album == "2026-08-12"
    assert photo.filename == "20260812101112000.jpg"
    assert photo.photo_url == "https://photos.zrc.sg/album/2026-08-12/20260812101112000.jpg"
    assert (
        photo.thumbnail_url == "https://photos.zrc.sg/album/thumb/2026-08-12/20260812101112000.jpg"
    )


def test_missing_is_video_attr_falls_back_to_suffix() -> None:
    html = ALBUM_HTML.replace(' data-is-video="true"', "").replace(' data-is-video="false"', "")
    kinds = {a.filename: a.kind for a in _parse(html)}
    assert kinds["20260812215627863.mp4"] == "video"
    assert kinds["20260812101112000.jpg"] == "image"


def test_original_src_preferred_over_src() -> None:
    html = ALBUM_HTML.replace(
        'data-original-src="/album/2026-08-12/20260812101112000.jpg"',
        'data-original-src="/album/original/2026-08-12/20260812101112000.jpg"',
    )
    photo = next(a for a in _parse(html) if a.filename.endswith(".jpg"))
    assert "/album/original/" in photo.photo_url


def test_structure_change_yields_empty_not_garbage() -> None:
    """没有 data-lightbox 节点时返回空（并打 error 日志），绝不退回猜测式解析。"""
    html = "<html><body><a href='/album/x/1.jpg'>p</a><img src='/album/x/2.jpg'></body></html>"
    assert _parse(html) == []
