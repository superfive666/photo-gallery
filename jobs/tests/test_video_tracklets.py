"""tracklet 聚合器的行为边界：串联、断段、多人不串、代表向量归一化。"""

from __future__ import annotations

import math

from gallery_core.embedding_client import DetectedFace
from jobs.video import Tracklet, build_tracklets

DIM = 8  # 聚合器不关心维度，用小向量让测试可读


def _vec(direction: int) -> list[float]:
    v = [0.0] * DIM
    v[direction] = 1.0
    return v


def _face(
    direction: int,
    bbox: tuple[int, int, int, int] = (100, 100, 80, 80),
    face_px: int = 80,
    det_score: float = 0.9,
) -> DetectedFace:
    return DetectedFace(
        bbox=bbox,
        det_score=det_score,
        face_px=face_px,
        embedding=_vec(direction),
        landmarks=None,
        model_name="m",
        model_version="v",
    )


def _build(frames: list[tuple[int, list[DetectedFace]]]) -> list[Tracklet]:
    return build_tracklets(frames, gap_ms=3000, sim_threshold=0.6, iou_threshold=0.3)


def test_continuous_face_forms_single_tracklet() -> None:
    frames = [(t, [_face(0)]) for t in (0, 1000, 2000, 3000)]
    tracks = _build(frames)
    assert len(tracks) == 1
    assert (tracks[0].t_start_ms, tracks[0].t_end_ms) == (0, 3000)
    assert tracks[0].count == 4


def test_gap_splits_into_two_segments() -> None:
    """同一个人离开画面超过 gap 再回来 —— 两个时间段，这正是需求要的效果。"""
    frames = [(0, [_face(0)]), (1000, [_face(0)]), (8000, [_face(0)]), (9000, [_face(0)])]
    tracks = _build(frames)
    assert [(t.t_start_ms, t.t_end_ms) for t in tracks] == [(0, 1000), (8000, 9000)]


def test_two_people_do_not_merge() -> None:
    """同帧两个人（向量正交、位置分开）必须是两个段 —— 串人是最坏的失败模式。"""
    a, b = (0, 0, 50, 50), (300, 300, 50, 50)
    frames = [
        (0, [_face(0, bbox=a), _face(1, bbox=b)]),
        (1000, [_face(0, bbox=a), _face(1, bbox=b)]),
    ]
    tracks = _build(frames)
    assert len(tracks) == 2
    assert all(t.count == 2 for t in tracks)


def test_iou_rescues_drifted_embedding() -> None:
    """侧脸帧 embedding 漂移（相似度不够）但位置几乎没动 —— IoU 兜底并入同段。"""
    frames = [
        (0, [_face(0, bbox=(100, 100, 80, 80))]),
        (1000, [_face(1, bbox=(105, 102, 80, 80))]),  # 正交向量：sim=0
    ]
    tracks = _build(frames)
    assert len(tracks) == 1
    assert tracks[0].count == 2


def test_mean_embedding_is_l2_normalized() -> None:
    frames = [(0, [_face(0)]), (1000, [_face(0)]), (2000, [_face(1, bbox=(102, 100, 80, 80))])]
    tracks = _build(frames)
    assert len(tracks) == 1
    norm = math.sqrt(sum(v * v for v in tracks[0].mean_embedding()))
    assert abs(norm - 1.0) < 1e-9


def test_best_face_prefers_larger_face() -> None:
    frames = [
        (0, [_face(0, face_px=60)]),
        (1000, [_face(0, face_px=120)]),
        (2000, [_face(0, face_px=90)]),
    ]
    tracks = _build(frames)
    assert tracks[0].best.face_px == 120
    assert tracks[0].best_t_ms == 1000


def test_one_track_claims_at_most_one_face_per_frame() -> None:
    """同帧出现两张同向量的脸（双胞胎/误检）：一段只能吃一个，另一个开新段。"""
    frames = [
        (0, [_face(0)]),
        (1000, [_face(0, bbox=(100, 100, 80, 80)), _face(0, bbox=(400, 100, 80, 80))]),
    ]
    tracks = _build(frames)
    assert len(tracks) == 2
