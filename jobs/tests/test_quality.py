from __future__ import annotations

import numpy as np

from jobs.quality import (
    combined_quality,
    exposure_score,
    phase_shift,
    resolution_tier,
    sharpness_score,
    stability_score,
)


def test_resolution_tiers() -> None:
    assert resolution_tier(2160) == 1.0
    assert resolution_tier(1440) == 0.85
    assert resolution_tier(1080) == 0.7
    assert resolution_tier(720) == 0.4
    assert resolution_tier(480) == 0.2
    assert resolution_tier(None) == 0.2


def test_sharpness_orders_sharp_above_blurred() -> None:
    rng = np.random.default_rng(7)
    sharp = (rng.random((120, 160)) * 255).astype(np.float32)
    # 均值滤波模糊：滑窗平均
    kernel = 9
    padded = np.pad(sharp, kernel // 2, mode="edge")
    blurred = np.zeros_like(sharp)
    for dy in range(kernel):
        for dx in range(kernel):
            blurred += padded[dy : dy + 120, dx : dx + 160]
    blurred /= kernel * kernel
    assert sharpness_score(sharp) > sharpness_score(blurred)


def test_exposure_penalizes_clipping() -> None:
    good = np.full((50, 50), 128.0, dtype=np.float32)
    blown = np.full((50, 50), 255.0, dtype=np.float32)
    assert exposure_score(good) == 1.0
    assert exposure_score(blown) == 0.0


def test_phase_shift_recovers_translation() -> None:
    rng = np.random.default_rng(3)
    base = (rng.random((64, 64)) * 255).astype(np.float32)
    shifted = np.roll(np.roll(base, 5, axis=0), -3, axis=1)
    dx, dy = phase_shift(base, shifted)
    # base 相对 shifted 的位移
    assert round(dy) in (-5, 5)
    assert round(dx) in (-3, 3)


def test_stability_pan_not_punished_jitter_is() -> None:
    # 匀速 pan：位移线性增长，二阶差分为零 → 满分
    pan = [(float(i * 4), 0.0) for i in range(8)]
    # 手持抖：高频往复
    jitter = [((-1.0) ** i * 12.0, (-1.0) ** i * 9.0) for i in range(8)]
    assert stability_score(pan, 240) == 1.0
    assert stability_score(jitter, 240) < 0.5
    # 帧不足时不惩罚
    assert stability_score([], 240) == 1.0


def test_combined_quality_weights_sum_to_one() -> None:
    assert combined_quality(1.0, 1.0, 1.0, 1.0) == 1.0
    assert combined_quality(0.0, 0.0, 0.0, 0.0) == 0.0
