"""画质指标：分辨率档位、清晰度、曝光、拍摄稳定性。

建库时逐 scene 计算一次，入库为列，融合排序在 SQL 外层直接用。
全部是便宜的 CPU 计算，纯 numpy 实现（无 cv2 依赖，方便单测）。

⚠️ 各指标到 0~1 的映射参数是经验值，待真实素材标定（P0，沿用 make eval 模式）。
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

# 综合画质分权重。画质在融合排序里的总权重被上限约束（settings.edit_quality_weight），
# 这里只决定画质内部四项的相对比例。
_W_RESOLUTION = 0.30
_W_STABILITY = 0.30
_W_SHARPNESS = 0.25
_W_EXPOSURE = 0.15


def resolution_tier(height: int | None) -> float:
    """4K→1.0 / 2K→0.85 / 1080p→0.7 / 720p→0.4 / 更低→0.2。"""
    if height is None or height <= 0:
        return 0.2
    if height >= 2000:
        return 1.0
    if height >= 1400:
        return 0.85
    if height >= 1000:
        return 0.7
    if height >= 700:
        return 0.4
    return 0.2


def laplacian_variance(gray: NDArray[np.float32]) -> float:
    """四邻域拉普拉斯的方差。清晰图上百，糊图接近 0。"""
    g = gray.astype(np.float32)
    lap = -4.0 * g[1:-1, 1:-1] + g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:]
    return float(lap.var())


def sharpness_score(gray: NDArray[np.float32]) -> float:
    """拉普拉斯方差 → 0~1。500 以上视为足够清晰（经验值）。"""
    return float(min(laplacian_variance(gray) / 500.0, 1.0))


def exposure_score(gray: NDArray[np.float32]) -> float:
    """过曝/欠曝像素占比 → 0~1。裁剪比例超过 25% 记 0。"""
    g = gray.astype(np.float32)
    total = g.size
    if total == 0:
        return 0.0
    clipped = float(((g < 8.0).sum() + (g > 247.0).sum()) / total)
    return float(max(0.0, 1.0 - clipped * 4.0))


def phase_shift(a: NDArray[np.float32], b: NDArray[np.float32]) -> tuple[float, float]:
    """两帧间的全局平移（相位相关，FFT 实现）。返回 (dx, dy)，单位像素。"""
    fa = np.fft.fft2(a)
    fb = np.fft.fft2(b)
    cross = fa * np.conj(fb)
    denom = np.abs(cross)
    denom[denom == 0] = 1.0
    corr = np.fft.ifft2(cross / denom)
    peak = np.unravel_index(int(np.argmax(np.abs(corr))), corr.shape)
    dy, dx = float(peak[0]), float(peak[1])
    h, w = a.shape
    if dy > h / 2:
        dy -= h
    if dx > w / 2:
        dx -= w
    return dx, dy


def stability_score(shifts: list[tuple[float, float]], frame_height: int) -> float:
    """帧间平移序列 → 稳定性 0~1。

    只惩罚**高频抖动**（相邻位移的二阶差分），不惩罚低频运动 ——
    有意的匀速运镜（pan/推拉）是平滑位移，二阶差分接近零，不会被误伤；
    手持抖是高频往复，二阶差分大。
    """
    if len(shifts) < 3 or frame_height <= 0:
        return 1.0
    arr = np.asarray(shifts, dtype=np.float32)
    jerk = np.diff(arr, n=2, axis=0)
    rms = float(np.sqrt((jerk**2).sum(axis=1).mean()))
    ratio = rms / frame_height
    return float(np.clip(1.0 - ratio * 40.0, 0.0, 1.0))


def combined_quality(tier: float, stability: float, sharpness: float, exposure: float) -> float:
    return float(
        _W_RESOLUTION * tier
        + _W_STABILITY * stability
        + _W_SHARPNESS * sharpness
        + _W_EXPOSURE * exposure
    )


def to_gray(rgb: NDArray[np.uint8]) -> NDArray[np.float32]:
    weights = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    result: NDArray[np.float32] = (rgb.astype(np.float32) @ weights).astype(np.float32)
    return result
