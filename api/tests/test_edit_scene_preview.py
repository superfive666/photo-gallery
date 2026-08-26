"""候选视频段预览的路径防御：解析后必须落在 {media_root}/media 内。

media_asset.path 正常只来自建库流程，但预览端点直接拿它开文件，
必须有与 photos.original 同级别的防御纵深 —— 越界、缺失一律 None（api 统一 404）。
"""

from __future__ import annotations

from pathlib import Path

from api.app.routers.edit import _resolve_media_path


def _make_media(tmp_path: Path) -> Path:
    media = tmp_path / "media" / "2026-08-10"
    media.mkdir(parents=True)
    return media


def test_ok_path_inside_media_root(tmp_path: Path) -> None:
    video = _make_media(tmp_path) / "a.mp4"
    video.write_bytes(b"x")
    assert _resolve_media_path(str(tmp_path), str(video)) == video.resolve()


def test_escape_outside_media_root_rejected(tmp_path: Path) -> None:
    _make_media(tmp_path)
    outside = tmp_path / "output" / "secret.zip"
    outside.parent.mkdir()
    outside.write_bytes(b"x")
    # 同盘不同目录（output 是用户私有交付物）也不许通过 preview 读到
    assert _resolve_media_path(str(tmp_path), str(outside)) is None


def test_dotdot_traversal_rejected(tmp_path: Path) -> None:
    media = _make_media(tmp_path)
    outside = tmp_path / "etc-passwd"
    outside.write_bytes(b"x")
    sneaky = f"{media}/../../etc-passwd"
    assert _resolve_media_path(str(tmp_path), sneaky) is None


def test_missing_file_rejected(tmp_path: Path) -> None:
    media = _make_media(tmp_path)
    assert _resolve_media_path(str(tmp_path), str(media / "gone.mp4")) is None
