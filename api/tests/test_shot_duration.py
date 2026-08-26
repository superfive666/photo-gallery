"""镜头时长语义的守卫（真实事故回归）。

zephyr-hoa 项目：剧本给了每个镜头的目标时长，LLM 把它同时填进
min_seconds 与 max_seconds，检索被迫要求 scene 恰好等长 → 全部镜头 0 候选，
表现为「找不到任何匹配的视频」。duration_bounds 是防御层：
min == max 视为成片目标时长，只保留下限。
"""

from __future__ import annotations

from api.app.services.edit_flow import duration_bounds


def test_target_duration_becomes_lower_bound_only() -> None:
    """min == max（LLM 填了目标时长）→ 只留下限，素材可以更长、成片再剪。"""
    assert duration_bounds(18.0, 18.0) == (18000, None)


def test_inverted_bounds_drop_upper() -> None:
    assert duration_bounds(10.0, 5.0) == (10000, None)


def test_real_range_is_kept() -> None:
    assert duration_bounds(5.0, 12.0) == (5000, 12000)


def test_partial_and_absent_bounds() -> None:
    assert duration_bounds(None, None) == (None, None)
    assert duration_bounds(8.0, None) == (8000, None)
    assert duration_bounds(None, 20.0) == (None, 20000)
    # 0 与 None 等价（pydantic 允许 ge=0；0 不构成过滤条件）
    assert duration_bounds(0.0, 0.0) == (None, None)
