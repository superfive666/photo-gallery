"""预渲染缓存（plans/0012）：指纹与 sidecar 的判定逻辑。

「可复用」的唯一判据是指纹一致 —— 任何会改变输出内容的输入（候选时码、
滤镜、编码参数、素材本身）变了，指纹必须变，否则会静默交付错内容。
"""

from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path

from gallery_core.config import Settings
from jobs.render import _PlannedShot, clip_fingerprint, is_cached, timecodes, write_fingerprint

_SETTINGS = Settings()


def _item(**overrides: object) -> _PlannedShot:
    base = _PlannedShot(
        shot_id=uuid.UUID("01890000-0000-7000-8000-000000000001"),
        idx=1,
        role="primary",
        description="切蛋糕",
        asset_kind="video",
        asset_path="/photo-gallery/media/a/v.mp4",
        asset_source_url="https://example.test/v.mp4",
        asset_album="a",
        asset_checksum="abc",
        asset_duration_ms=60_000,
        scene_start_ms=5_000,
        scene_end_ms=9_000,
        in_ms=None,
        out_ms=None,
        preset_slug="warm",
        preset_checksum="deadbeef",
        lut_bytes=b"LUT",
    )
    return dataclasses.replace(base, **overrides)  # type: ignore[arg-type]


def test_timecodes_padding_and_clamping() -> None:
    precise_in, precise_out, padded_in, padded_out = timecodes(_item(), _SETTINGS)
    assert (precise_in, precise_out) == (5_000, 9_000)
    assert padded_in == 5_000 - _SETTINGS.render_handle_ms
    assert padded_out == 9_000 + _SETTINGS.render_handle_ms

    # 段首贴近 0、段尾贴近素材末尾时余量要被钳住
    edge = _item(scene_start_ms=200, scene_end_ms=59_800)
    _, _, padded_in, padded_out = timecodes(edge, _SETTINGS)
    assert padded_in == 0
    assert padded_out == 60_000

    # 照片没有时码概念
    assert timecodes(_item(asset_kind="image"), _SETTINGS) == (0, 0, 0, 0)


def test_fingerprint_stable_and_sensitive() -> None:
    fp = clip_fingerprint(_item(), _SETTINGS)
    assert fp == clip_fingerprint(_item(), _SETTINGS)  # 同输入必须稳定

    # 会改变输出内容的每一类输入都要让指纹翻转
    assert fp != clip_fingerprint(_item(in_ms=5_500, out_ms=8_500), _SETTINGS)
    assert fp != clip_fingerprint(_item(preset_checksum="feedface"), _SETTINGS)
    assert fp != clip_fingerprint(_item(preset_slug=None, preset_checksum=None), _SETTINGS)
    assert fp != clip_fingerprint(_item(asset_checksum="changed"), _SETTINGS)
    assert fp != clip_fingerprint(_item(), Settings(render_crf=20))

    # role 不参与指纹：主/备选指向同一候选时内容相同（文件名已区分两者）
    assert fp == clip_fingerprint(_item(role="backup"), _SETTINGS)


def test_is_cached_requires_file_and_matching_sidecar(tmp_path: Path) -> None:
    dst = tmp_path / "01_x.mp4"
    fp = clip_fingerprint(_item(), _SETTINGS)

    assert not is_cached(dst, fp)  # 文件不存在

    dst.write_bytes(b"clip")
    assert not is_cached(dst, fp)  # 没有 sidecar（旧版本产物）→ 重剪

    write_fingerprint(dst, fp)
    assert is_cached(dst, fp)

    assert not is_cached(dst, "f" * 64)  # 入参变了 → 作废重剪
