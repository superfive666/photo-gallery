from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from jobs.eval import asset_id_for, load_cases
from jobs.sources.local_dir import LocalDirAdapter


@pytest.mark.asyncio
async def test_asset_id_matches_local_dir_adapter(tmp_path: Path) -> None:
    """评估的 asset_id 复算必须与 local_dir adapter 完全一致。

    这两处算法一旦漂移，评估就会把每一张照片都判成 not_ingested，
    指标全为 0 —— 而且看起来像「检索坏了」，极容易误诊。
    """
    album = tmp_path / "2024-annual-dinner"
    album.mkdir()
    (album / "IMG_0001.jpg").write_bytes(b"fake")

    adapter = LocalDirAdapter(tmp_path)
    assets = [a async for a in adapter.list_assets("2024-annual-dinner")]

    assert len(assets) == 1
    assert assets[0].id == asset_id_for("2024-annual-dinner/IMG_0001.jpg")


@pytest.mark.asyncio
async def test_local_dir_skips_unknown_extensions(tmp_path: Path) -> None:
    album = tmp_path / "album"
    album.mkdir()
    (album / "photo.jpg").write_bytes(b"x")
    (album / "clip.mp4").write_bytes(b"x")
    (album / "notes.txt").write_bytes(b"x")

    adapter = LocalDirAdapter(tmp_path)
    assets = {a.filename: a.kind for a in [x async for x in adapter.list_assets("album")]}

    assert assets == {"photo.jpg": "image", "clip.mp4": "video"}


@pytest.mark.asyncio
async def test_local_dir_respects_since(tmp_path: Path) -> None:
    """增量同步：mtime 早于 since 的文件不该被再次列出。"""
    album = tmp_path / "album"
    album.mkdir()
    (album / "old.jpg").write_bytes(b"x")

    future = dt.datetime.now(tz=dt.UTC) + dt.timedelta(days=1)
    adapter = LocalDirAdapter(tmp_path)
    assets = [a async for a in adapter.list_assets("album", since=future)]

    assert assets == []


def test_load_cases_requires_labels(tmp_path: Path) -> None:
    (tmp_path / "queries").mkdir()
    with pytest.raises(FileNotFoundError, match=r"labels\.csv"):
        load_cases(tmp_path)


def test_load_cases_reads_truth_and_selfies(tmp_path: Path) -> None:
    (tmp_path / "labels.csv").write_text(
        "person_id,gallery_path,face_bbox\n"
        "person_01,album/a.jpg,\n"
        "person_01,album/b.jpg,\n"
        "person_02,album/c.jpg,\n",
        encoding="utf-8",
    )
    for person in ("person_01", "person_02"):
        d = tmp_path / "queries" / person
        d.mkdir(parents=True)
        (d / "selfie_1.jpg").write_bytes(b"x")

    cases = {c.person_id: c for c in load_cases(tmp_path)}

    assert cases["person_01"].truth_paths == {"album/a.jpg", "album/b.jpg"}
    assert cases["person_02"].truth_paths == {"album/c.jpg"}
    assert len(cases["person_01"].selfies) == 1
