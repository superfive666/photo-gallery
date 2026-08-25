"""local:// URL↔路径换算的守卫。

api 的本地原图分发直接拿 resolve_local_path 的结果读文件，
这里测的越界样例每一条都是「不挡住就能读任意文件」的攻击面。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gallery_core.local_source import is_local_url, photo_url_for, resolve_local_path


def test_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "2026-08-10" / "IMG_0001.jpg"
    target.parent.mkdir()
    target.write_bytes(b"x")

    url = photo_url_for("2026-08-10/IMG_0001.jpg")
    assert url == "local://album/2026-08-10/IMG_0001.jpg"
    assert is_local_url(url)
    assert resolve_local_path(tmp_path, url) == target.resolve()


def test_nested_paths_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "a" / "sub" / "deep.jpg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")
    assert resolve_local_path(tmp_path, photo_url_for("a/sub/deep.jpg")) == target.resolve()


def test_remote_urls_are_not_local() -> None:
    assert not is_local_url("https://photos.zrc.sg/album/x/1.jpg")
    assert resolve_local_path("/data", "https://photos.zrc.sg/album/x/1.jpg") is None
    # 剪辑域合成键（local://<album>/<file>，无 album 段）不是本地相册 URL 形态
    assert not is_local_url("local://2026-08-10/clip.mp4")


@pytest.mark.parametrize(
    "url",
    [
        "local://album/../etc/passwd",  # 相对段逃逸
        "local://album/a/../../etc/passwd",  # 深一层的相对段逃逸
        "local://album//etc/passwd",  # 绝对路径拼接（Path 会整个替换掉根）
        "local://album/",  # 空相对路径
        "local://album/..",  # 解析结果是根的父目录
        "local://album/.",  # 解析结果等于根目录本身
    ],
)
def test_traversal_is_rejected(tmp_path: Path, url: str) -> None:
    assert resolve_local_path(tmp_path, url) is None


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.jpg"
    secret.write_bytes(b"x")

    root = tmp_path / "root"
    (root / "album1").mkdir(parents=True)
    (root / "album1" / "link.jpg").symlink_to(secret)

    # resolve() 会跟随符号链接，落点在根外 → 拒绝
    assert resolve_local_path(root, "local://album/album1/link.jpg") is None
