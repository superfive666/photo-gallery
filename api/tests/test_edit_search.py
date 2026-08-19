"""融合检索的纯逻辑部分：RRF 融合。SQL 层的两层结构由 CI 的迁移实跑保障形状。"""

from __future__ import annotations

import uuid

from api.app.services.edit_search import SceneHit, rrf_merge


def _hit(scene: uuid.UUID, score: float) -> SceneHit:
    return SceneHit(
        scene_id=scene,
        similarity=score,
        quality=0.5,
        final_score=score,
        start_ms=0,
        end_ms=1000,
        kind="video",
    )


def test_rrf_prefers_scene_ranked_high_in_multiple_lists() -> None:
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    # a 在两个列表里都排第 1~2；b/c 各只出现一次
    merged = rrf_merge([[_hit(a, 0.9), _hit(b, 0.8)], [_hit(a, 0.85), _hit(c, 0.7)]], top_k=3)
    assert merged[0].scene_id == a
    assert {m.scene_id for m in merged} == {a, b, c}


def test_rrf_dedupes_and_keeps_best_metadata() -> None:
    a = uuid.uuid4()
    merged = rrf_merge([[_hit(a, 0.6)], [_hit(a, 0.9)]], top_k=5)
    assert len(merged) == 1
    assert merged[0].final_score == 0.9


def test_rrf_respects_top_k() -> None:
    hits = [_hit(uuid.uuid4(), 1.0 - i * 0.01) for i in range(10)]
    assert len(rrf_merge([hits], top_k=5)) == 5


def test_rrf_empty_input() -> None:
    assert rrf_merge([], top_k=5) == []
    assert rrf_merge([[]], top_k=5) == []
