"""3D LUT（.cube）的解析、生成与应用。

滤镜库的统一形态：一切滤镜都是 3D LUT ——
  · 管理员自备的 .cube 模版由 filters-import 校验入库；
  · 内置预设（暖调/冷调/黑白/胶片）在导入时由这里的变换函数**生成**为 LUT。
预览（numpy 套用到关键帧）与渲染（ffmpeg lut3d 读同一份字节）共用一份数据，
保证所见即所得。

纯 numpy 实现，无 cv2/ffmpeg 依赖 —— 方便单测，也能在 api 进程里做单帧预览。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

# .cube 规范允许 2~256，实际滤镜模版几乎都是这三档。限制范围顺带挡住畸形文件。
_ALLOWED_SIZES = frozenset({8, 16, 17, 32, 33, 64, 65})

_FloatImage = NDArray[np.float32]


class LutError(ValueError):
    """不是合法的 .cube 3D LUT。"""


@dataclass(frozen=True, slots=True)
class Lut:
    """table 形状 (N, N, N, 3)，索引顺序 [b, g, r] —— 与 .cube 的「红最快」次序一致。"""

    size: int
    table: NDArray[np.float32]

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.table.tobytes()).hexdigest()


def parse_cube(payload: bytes) -> Lut:
    """解析并校验 .cube。畸形文件必须明确报错跳过，不静默入库。"""
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise LutError("不是 UTF-8 文本（.cube 是文本格式）") from exc

    size = 0
    rows: list[tuple[float, float, float]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        upper = line.upper()
        if upper.startswith("TITLE") or upper.startswith("DOMAIN_"):
            continue
        if upper.startswith("LUT_1D_SIZE"):
            raise LutError("是 1D LUT —— 只支持 3D（LUT_3D_SIZE）")
        if upper.startswith("LUT_3D_SIZE"):
            parts = line.split()
            if len(parts) != 2 or not parts[1].isdigit():
                raise LutError(f"第 {lineno} 行：LUT_3D_SIZE 格式错误")
            size = int(parts[1])
            if size not in _ALLOWED_SIZES:
                raise LutError(f"LUT_3D_SIZE={size} 不在支持范围 {sorted(_ALLOWED_SIZES)}")
            continue
        parts = line.split()
        if len(parts) != 3:
            raise LutError(f"第 {lineno} 行：期望 3 个数值，得到 {len(parts)} 个")
        try:
            r, g, b = (float(p) for p in parts)
        except ValueError as exc:
            raise LutError(f"第 {lineno} 行：数值解析失败") from exc
        rows.append((r, g, b))

    if size == 0:
        raise LutError("缺少 LUT_3D_SIZE")
    if len(rows) != size**3:
        raise LutError(f"数据行数 {len(rows)} 与 LUT_3D_SIZE³={size**3} 不符")

    flat = np.asarray(rows, dtype=np.float32)
    if not np.isfinite(flat).all():
        raise LutError("包含 NaN/Inf")
    # .cube 的行序是红通道最快 → reshape 成 [b, g, r, 3]
    table = flat.reshape(size, size, size, 3)
    return Lut(size=size, table=table)


def to_cube_bytes(lut: Lut, title: str) -> bytes:
    """序列化回 .cube 文本。内置预设生成后与导入模版走完全相同的存储与渲染路径。"""
    lines = [f'TITLE "{title}"', f"LUT_3D_SIZE {lut.size}"]
    flat = lut.table.reshape(-1, 3)
    lines.extend(f"{r:.6f} {g:.6f} {b:.6f}" for r, g, b in flat)
    return ("\n".join(lines) + "\n").encode("utf-8")


def apply_lut(image_rgb: NDArray[np.uint8], lut: Lut) -> NDArray[np.uint8]:
    """把 LUT 套用到 H×W×3 的 RGB uint8 图上（三线性插值）。

    预览路径用它（api 单帧、filters-import 生成预览图）；
    渲染路径不用它 —— ffmpeg 的 lut3d 读同一份 .cube 字节，结果一致。
    """
    n = lut.size
    coords = image_rgb.astype(np.float32) / 255.0 * (n - 1)
    i0 = np.floor(coords).astype(np.int32)
    i0 = np.clip(i0, 0, n - 2)
    frac = coords - i0
    i1 = i0 + 1

    r0, g0, b0 = i0[..., 0], i0[..., 1], i0[..., 2]
    r1, g1, b1 = i1[..., 0], i1[..., 1], i1[..., 2]
    fr, fg, fb = frac[..., 0:1], frac[..., 1:2], frac[..., 2:3]

    t = lut.table  # 索引顺序 [b, g, r]
    c000 = t[b0, g0, r0]
    c001 = t[b0, g0, r1]
    c010 = t[b0, g1, r0]
    c011 = t[b0, g1, r1]
    c100 = t[b1, g0, r0]
    c101 = t[b1, g0, r1]
    c110 = t[b1, g1, r0]
    c111 = t[b1, g1, r1]

    c00 = c000 * (1 - fr) + c001 * fr
    c01 = c010 * (1 - fr) + c011 * fr
    c10 = c100 * (1 - fr) + c101 * fr
    c11 = c110 * (1 - fr) + c111 * fr
    c0 = c00 * (1 - fg) + c01 * fg
    c1 = c10 * (1 - fg) + c11 * fg
    out = c0 * (1 - fb) + c1 * fb

    result: NDArray[np.uint8] = (np.clip(out, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    return result


# ---------------------------------------------------------------------------
# 内置预设：变换函数 → 采样成 LUT。清单在 P0 与需求方最终敲定，这里是兜底集。
# ---------------------------------------------------------------------------

_Transform = Callable[[_FloatImage], _FloatImage]


def _luma(rgb: _FloatImage) -> _FloatImage:
    weights = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    result: _FloatImage = (rgb @ weights)[..., None].astype(np.float32)
    return result


def _warm(rgb: _FloatImage) -> _FloatImage:
    gains = np.array([1.08, 1.01, 0.90], dtype=np.float32)
    return np.clip(rgb * gains, 0.0, 1.0).astype(np.float32)


def _cool(rgb: _FloatImage) -> _FloatImage:
    gains = np.array([0.90, 1.00, 1.08], dtype=np.float32)
    return np.clip(rgb * gains, 0.0, 1.0).astype(np.float32)


def _bw(rgb: _FloatImage) -> _FloatImage:
    return np.repeat(_luma(rgb), 3, axis=-1).astype(np.float32)


def _film(rgb: _FloatImage) -> _FloatImage:
    # S 曲线提对比 + 轻微降饱和 + 一点暖意，是「胶片感」最稳的近似
    curved = rgb * rgb * (3.0 - 2.0 * rgb)  # smoothstep
    desat = curved * 0.85 + _luma(curved) * 0.15
    gains = np.array([1.03, 1.0, 0.97], dtype=np.float32)
    return np.clip(desat * gains, 0.0, 1.0).astype(np.float32)


def _identity(rgb: _FloatImage) -> _FloatImage:
    return rgb


_BUILTINS: list[tuple[str, str, _Transform]] = [
    ("original", "原色", _identity),
    ("warm", "暖调", _warm),
    ("cool", "冷调", _cool),
    ("bw", "黑白", _bw),
    ("film", "胶片", _film),
]

_BUILTIN_SIZE = 17


def builtin_presets() -> list[tuple[str, str, Lut]]:
    """(slug, 显示名, Lut)。由变换函数在 17³ 网格上采样生成。"""
    axis = np.linspace(0.0, 1.0, _BUILTIN_SIZE, dtype=np.float32)
    # 网格索引 [b, g, r]，最后一维是 (r, g, b) 值 —— 与 parse_cube 的布局一致
    b, g, r = np.meshgrid(axis, axis, axis, indexing="ij")
    grid = np.stack([r, g, b], axis=-1).astype(np.float32)

    presets = []
    for slug, display_name, transform in _BUILTINS:
        table = transform(grid.reshape(-1, 3)).reshape(grid.shape)
        presets.append((slug, display_name, Lut(size=_BUILTIN_SIZE, table=table)))
    return presets


def preview_test_image(width: int = 320, height: int = 180) -> NDArray[np.uint8]:
    """生成预览用的标准测试图：水平色相渐变 × 垂直明度渐变，能同时看出
    偏色、对比和高光表现。所有预设的预览图都基于同一张图，可横向比较。"""
    x = np.linspace(0.0, 1.0, width, dtype=np.float32)[None, :]
    y = np.linspace(0.15, 1.0, height, dtype=np.float32)[:, None]

    # 简化 HSV→RGB（S=0.8, H=x, V=y）
    h6 = (x * 6.0) % 6.0
    c = np.broadcast_to(y * 0.8, (height, width)).astype(np.float32)
    v = np.broadcast_to(y, (height, width)).astype(np.float32)
    xx = c * (1 - np.abs(h6 % 2 - 1))
    m = v - c

    r = np.select([h6 < 1, h6 < 2, h6 < 3, h6 < 4, h6 < 5], [c, xx, 0.0, 0.0, xx], c)
    g = np.select([h6 < 1, h6 < 2, h6 < 3, h6 < 4, h6 < 5], [xx, c, c, xx, 0.0], 0.0)
    b = np.select([h6 < 1, h6 < 2, h6 < 3, h6 < 4, h6 < 5], [0.0, 0.0, xx, c, c], xx)

    rgb = np.stack([r + m, g + m, b + m], axis=-1)
    # 底部加一条灰阶，看黑白/对比类预设最直观
    bar = int(height * 0.18)
    gray = np.repeat(np.linspace(0, 1, width, dtype=np.float32)[None, :, None], 3, axis=2)
    rgb[-bar:] = np.broadcast_to(gray, (bar, width, 3))
    result: NDArray[np.uint8] = (np.clip(rgb, 0, 1) * 255).astype(np.uint8)
    return result
