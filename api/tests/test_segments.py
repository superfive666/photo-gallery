"""视频命中段的合并语义：邻近合并、远段保留、分数取最大、容忍 jsonb 回成 str。"""

from __future__ import annotations

from api.app.services.search import Segment, _merge_segments


def test_adjacent_segments_merge() -> None:
    raw = [
        {"start_ms": 1000, "end_ms": 5000, "score": 0.61},
        {"start_ms": 6500, "end_ms": 9000, "score": 0.55},  # 间隔 1.5s ≤ 2s → 合并
    ]
    assert _merge_segments(raw) == (Segment(start_ms=1000, end_ms=9000, score=0.61),)


def test_distant_segments_stay_apart() -> None:
    raw = [
        {"start_ms": 1000, "end_ms": 5000, "score": 0.61},
        {"start_ms": 60000, "end_ms": 65000, "score": 0.7},
    ]
    merged = _merge_segments(raw)
    assert len(merged) == 2
    assert merged[1].score == 0.7


def test_overlapping_segments_take_max_end_and_score() -> None:
    raw = [
        {"start_ms": 1000, "end_ms": 8000, "score": 0.5},
        {"start_ms": 3000, "end_ms": 6000, "score": 0.9},  # 完全被包住
    ]
    assert _merge_segments(raw) == (Segment(start_ms=1000, end_ms=8000, score=0.9),)


def test_none_and_json_string_inputs() -> None:
    assert _merge_segments(None) == ()
    assert _merge_segments('[{"start_ms": 0, "end_ms": 1000, "score": 0.5}]') == (
        Segment(start_ms=0, end_ms=1000, score=0.5),
    )
