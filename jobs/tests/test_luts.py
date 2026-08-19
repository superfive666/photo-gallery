from __future__ import annotations

import numpy as np
import pytest

from jobs.luts import (
    Lut,
    LutError,
    apply_lut,
    builtin_presets,
    parse_cube,
    preview_test_image,
    to_cube_bytes,
)


def _identity_cube(size: int = 8) -> bytes:
    lines = [f"LUT_3D_SIZE {size}"]
    axis = np.linspace(0, 1, size)
    for b in axis:
        for g in axis:
            for r in axis:
                lines.append(f"{r:.6f} {g:.6f} {b:.6f}")
    return "\n".join(lines).encode()


def test_parse_roundtrip() -> None:
    lut = parse_cube(_identity_cube())
    assert lut.size == 8
    again = parse_cube(to_cube_bytes(lut, "id"))
    assert again.checksum == lut.checksum


def test_parse_rejects_wrong_row_count() -> None:
    payload = b"LUT_3D_SIZE 8\n0 0 0\n"
    with pytest.raises(LutError, match="不符"):
        parse_cube(payload)


def test_parse_rejects_1d_lut() -> None:
    with pytest.raises(LutError, match="1D"):
        parse_cube(b"LUT_1D_SIZE 256\n")


def test_parse_rejects_weird_size() -> None:
    with pytest.raises(LutError, match="支持范围"):
        parse_cube(b"LUT_3D_SIZE 7\n" + b"0 0 0\n" * (7**3))


def test_parse_rejects_binary_garbage() -> None:
    with pytest.raises(LutError):
        parse_cube(b"\xff\xfe\x00\x01binary")


def test_apply_identity_lut_is_noop() -> None:
    lut = parse_cube(_identity_cube(17))
    img = preview_test_image(64, 36)
    out = apply_lut(img, lut)
    # 三线性插值在网格点之间有 ±1 的量化误差，恒等 LUT 不应超过它
    assert int(np.abs(out.astype(int) - img.astype(int)).max()) <= 1


def test_builtin_presets_shapes_and_semantics() -> None:
    presets = {slug: lut for slug, _, lut in builtin_presets()}
    assert {"original", "warm", "cool", "bw", "film"} <= set(presets)

    img = preview_test_image(64, 36)
    # original 是恒等变换
    assert int(np.abs(apply_lut(img, presets["original"]).astype(int) - img.astype(int)).max()) <= 1
    # bw 输出三通道相等（黑白）
    bw = apply_lut(img, presets["bw"]).astype(int)
    assert int(np.abs(bw[..., 0] - bw[..., 1]).max()) <= 1
    assert int(np.abs(bw[..., 1] - bw[..., 2]).max()) <= 1
    # warm 提升 R/B 比值；cool 相反（在中性灰上看最干净）
    gray = np.full((4, 4, 3), 128, dtype=np.uint8)
    warm = apply_lut(gray, presets["warm"]).astype(int)
    cool = apply_lut(gray, presets["cool"]).astype(int)
    assert warm[0, 0, 0] > warm[0, 0, 2]
    assert cool[0, 0, 0] < cool[0, 0, 2]


def test_lut_checksum_distinguishes_presets() -> None:
    checksums = {lut.checksum for _, _, lut in builtin_presets()}
    assert len(checksums) == len(builtin_presets())


def test_lut_dataclass_layout() -> None:
    lut = parse_cube(_identity_cube(8))
    assert isinstance(lut, Lut)
    # 布局 [b, g, r, 3]：table[0,0,-1] 是 r=1 的角 → (1,0,0)
    corner = lut.table[0, 0, -1]
    assert corner[0] == pytest.approx(1.0)
    assert corner[1] == pytest.approx(0.0)
