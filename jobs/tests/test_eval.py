from __future__ import annotations

from pathlib import Path

import pytest

from jobs.eval import load_cases
from jobs.sources.local_dir import LocalDirAdapter, photo_url_for


@pytest.mark.asyncio
async def test_photo_url_matches_local_dir_adapter(tmp_path: Path) -> None:
    """评估的 photo_url 复算必须与 local_dir adapter 完全一致。

    这两处一旦漂移，评估会把每一张照片都判成 not_ingested，指标全为 0 ——
    而且看起来像「检索坏了」，极容易误诊。
    """
    album = tmp_path / "2026-08-10"
    album.mkdir()
    (album / "IMG_0001.jpg").write_bytes(b"fake")

    adapter = LocalDirAdapter(tmp_path)
    assets = [a async for a in adapter.list_assets("2026-08-10")]

    assert len(assets) == 1
    assert assets[0].photo_url == photo_url_for("2026-08-10/IMG_0001.jpg")
    assert assets[0].album == "2026-08-10"


@pytest.mark.asyncio
async def test_local_dir_lists_albums_as_slugs(tmp_path: Path) -> None:
    for name in ("2026-08-10", "2026-07-20"):
        (tmp_path / name).mkdir()
    (tmp_path / "loose.jpg").write_bytes(b"x")

    adapter = LocalDirAdapter(tmp_path)
    assert await adapter.list_albums() == ["2026-07-20", "2026-08-10"]


@pytest.mark.asyncio
async def test_local_dir_classifies_images_and_videos(tmp_path: Path) -> None:
    album = tmp_path / "2026-08-10"
    album.mkdir()
    (album / "photo.jpg").write_bytes(b"x")
    (album / "clip.mp4").write_bytes(b"x")
    (album / "notes.txt").write_bytes(b"x")

    adapter = LocalDirAdapter(tmp_path)
    kinds = {a.filename: a.kind for a in [x async for x in adapter.list_assets("2026-08-10")]}

    assert kinds == {"photo.jpg": "image", "clip.mp4": "video"}


def test_load_cases_requires_labels(tmp_path: Path) -> None:
    (tmp_path / "queries").mkdir()
    with pytest.raises(FileNotFoundError, match=r"labels\.csv"):
        load_cases(tmp_path)


def test_load_cases_reads_truth_and_selfies(tmp_path: Path) -> None:
    (tmp_path / "labels.csv").write_text(
        "person_id,gallery_path\n"
        "person_01,2026-08-10/a.jpg\n"
        "person_01,2026-08-10/b.jpg\n"
        "person_02,2026-07-20/c.jpg\n",
        encoding="utf-8",
    )
    for person in ("person_01", "person_02"):
        d = tmp_path / "queries" / person
        d.mkdir(parents=True)
        (d / "selfie_1.jpg").write_bytes(b"x")

    cases = {c.person_id: c for c in load_cases(tmp_path)}

    assert cases["person_01"].truth_paths == {"2026-08-10/a.jpg", "2026-08-10/b.jpg"}
    assert cases["person_02"].truth_paths == {"2026-07-20/c.jpg"}
    assert len(cases["person_01"].selfies) == 1
