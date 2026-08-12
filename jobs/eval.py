"""阈值标定与准确率评估。

没有这个东西，项目最后会变成「看起来能跑，但没人知道准不准」。方法论见 docs/evaluation.md。

关键设计决定：**评估走的是 api 里那份真实的检索函数**（`search_by_embedding`），
不是另写一份近似实现。否则测出来的指标和线上行为无关，标定出的阈值也就没有意义。

输入目录结构（EVAL_DIR，不进 git，含真人照片）：

    gallery/<album>/<file>.jpg      模拟照片库，需先用 local_dir adapter ingest 进库
    queries/person_01/selfie_1.jpg  模拟自拍，每人 1~3 张
    labels.csv                      person_id,gallery_path
                                    gallery_path 相对 gallery/，形如 2026-08-10/IMG_0001.jpg
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.app.services.search import search_by_embedding
from gallery_core.config import Settings
from gallery_core.embedding_client import EmbeddingClient
from gallery_core.logging import get_logger
from gallery_core.vector import mean_embedding
from jobs.sources.local_dir import photo_url_for

log = get_logger(__name__)

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}

# 阈值扫描网格。单段式检索只有一个阈值要标定。
_FACE_GRID = (0.30, 0.34, 0.38, 0.42, 0.46, 0.50, 0.54)

# precision 的下限。串人（把别人的照片给你）的体验远差于漏图，
# 所以标定时固定守住 precision，再在此前提下最大化 recall。
_MIN_PRECISION = 0.95


@dataclass
class QueryCase:
    person_id: str
    selfies: list[Path]
    # 该人在 gallery 中出现的全部照片（相对 gallery/ 的路径）
    truth_paths: set[str] = field(default_factory=set)


@dataclass
class PersonMetrics:
    person_id: str
    truth_total: int
    hits: int
    returned: int
    recall_at_20: float
    reciprocal_rank: float
    # 漏检归因：small_face | detect_fail | low_similarity | not_ingested
    misses_by_reason: dict[str, int] = field(default_factory=dict)

    @property
    def recall(self) -> float:
        return self.hits / self.truth_total if self.truth_total else 0.0

    @property
    def precision(self) -> float:
        return self.hits / self.returned if self.returned else 0.0


def load_cases(eval_dir: Path) -> list[QueryCase]:
    labels_path = eval_dir / "labels.csv"
    if not labels_path.is_file():
        raise FileNotFoundError(f"找不到 {labels_path}。评估集结构见 docs/evaluation.md。")

    truth: dict[str, set[str]] = defaultdict(set)
    with labels_path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            person = (row.get("person_id") or "").strip()
            gallery_path = (row.get("gallery_path") or "").strip()
            if person and gallery_path:
                truth[person].add(gallery_path)

    queries_root = eval_dir / "queries"
    cases: list[QueryCase] = []
    for person_dir in sorted(p for p in queries_root.iterdir() if p.is_dir()):
        selfies = sorted(p for p in person_dir.iterdir() if p.suffix.lower() in _IMAGE_SUFFIXES)
        if not selfies:
            log.warning("eval_person_without_selfie", person=person_dir.name)
            continue
        if person_dir.name not in truth:
            log.warning("eval_person_without_labels", person=person_dir.name)
        cases.append(
            QueryCase(
                person_id=person_dir.name,
                selfies=selfies,
                truth_paths=truth.get(person_dir.name, set()),
            )
        )
    return cases


async def _query_vector(case: QueryCase, embedding: EmbeddingClient) -> list[float] | None:
    """把一个人的多张自拍合成单一查询向量。

    必须与线上完全一致：每张自拍只取最明显的一张脸（primary_only=True），
    多张再取均值。差一点点，标定出的阈值就不适用于线上。
    """
    vectors: list[list[float]] = []
    for selfie in case.selfies:
        result = await embedding.extract(
            selfie.read_bytes(), filename=selfie.name, primary_only=True
        )
        if result.faces:
            vectors.append(result.faces[0].embedding)
    if not vectors:
        return None
    merged: list[float] = mean_embedding(vectors).tolist()
    return merged


async def _attribute_misses(session: AsyncSession, missed_urls: set[str]) -> dict[str, int]:
    """给漏检归因。

    这是整个评估里最有诊断价值的一项：如果漏检主要来自「小脸被质量门控丢弃」，
    那调相似度阈值是白费力气 —— 该做的是降 MIN_FACE_PX 或调大检测输入分辨率。
    只有当漏检主要是「相似度不足」时，调阈值才有意义。
    """
    reasons: dict[str, int] = defaultdict(int)
    if not missed_urls:
        return dict(reasons)

    rows = (
        await session.execute(
            text(
                """
                SELECT photo_url, face_count, faces_discarded, processing_status
                FROM photo
                WHERE photo_url = ANY(CAST(:urls AS text[]))
                """
            ),
            {"urls": list(missed_urls)},
        )
    ).all()

    found = {r.photo_url for r in rows}
    # 标注里有、但库里根本没有 —— 说明评估集没 ingest 完整，先修这个再看别的指标
    reasons["not_ingested"] += len(missed_urls - found)

    for row in rows:
        if row.processing_status != "embedded":
            reasons["not_ingested"] += 1
        elif row.face_count == 0 and row.faces_discarded > 0:
            reasons["small_face"] += 1
        elif row.face_count == 0:
            reasons["detect_fail"] += 1
        else:
            # 脸检出来了、也入库了，但没被这次查询召回 → 纯粹是相似度不够
            reasons["low_similarity"] += 1

    return dict(reasons)


async def evaluate(
    session: AsyncSession,
    embedding: EmbeddingClient,
    settings: Settings,
    eval_dir: Path,
    attribute: bool = True,
) -> tuple[list[PersonMetrics], dict[str, object]]:
    cases = load_cases(eval_dir)
    if not cases:
        raise ValueError("评估集里没有可用的查询用例")

    metrics: list[PersonMetrics] = []
    for case in cases:
        vector = await _query_vector(case, embedding)
        if vector is None:
            log.warning("eval_no_face_in_selfie", person=case.person_id)
            metrics.append(
                PersonMetrics(
                    person_id=case.person_id,
                    truth_total=len(case.truth_paths),
                    hits=0,
                    returned=0,
                    recall_at_20=0.0,
                    reciprocal_rank=0.0,
                    misses_by_reason={"query_no_face": len(case.truth_paths)},
                )
            )
            continue

        outcome = await search_by_embedding(session, vector, settings)

        # labels 里的相对路径换算成库里的 photo_url。local_dir adapter 的 URL 规则
        # 由 photo_url_for 统一提供 —— 两处各写一遍就会全判成 not_ingested。
        truth_urls = {photo_url_for(p) for p in case.truth_paths}
        returned_urls = [m.photo_url for m in outcome.matches]

        hit_ranks = [i for i, url in enumerate(returned_urls, start=1) if url in truth_urls]
        hits = len(hit_ranks)
        top20_hits = len([r for r in hit_ranks if r <= 20])
        missed_urls = truth_urls - set(returned_urls)

        metrics.append(
            PersonMetrics(
                person_id=case.person_id,
                truth_total=len(case.truth_paths),
                hits=hits,
                returned=len(returned_urls),
                recall_at_20=top20_hits / len(case.truth_paths) if case.truth_paths else 0.0,
                reciprocal_rank=1.0 / hit_ranks[0] if hit_ranks else 0.0,
                misses_by_reason=await _attribute_misses(session, missed_urls) if attribute else {},
            )
        )

    return metrics, _summarize(metrics, settings)


def _summarize(metrics: list[PersonMetrics], settings: Settings) -> dict[str, object]:
    total_truth = sum(m.truth_total for m in metrics)
    total_hits = sum(m.hits for m in metrics)
    total_returned = sum(m.returned for m in metrics)

    reasons: dict[str, int] = defaultdict(int)
    for m in metrics:
        for reason, count in m.misses_by_reason.items():
            reasons[reason] += count

    return {
        "persons": len(metrics),
        "thresholds": {
            "face": settings.face_match_threshold,
            "min_face_px": settings.min_face_px,
            "min_det_score": settings.min_det_score,
            "search_candidates": settings.search_candidates,
        },
        "recall": round(total_hits / total_truth, 4) if total_truth else 0.0,
        "precision": round(total_hits / total_returned, 4) if total_returned else 0.0,
        "recall_at_20": round(sum(m.recall_at_20 for m in metrics) / len(metrics), 4),
        "mrr": round(sum(m.reciprocal_rank for m in metrics) / len(metrics), 4),
        # 最有诊断价值的一项，见 _attribute_misses 的说明
        "miss_attribution": dict(reasons),
    }


async def sweep(
    session: AsyncSession,
    embedding: EmbeddingClient,
    settings: Settings,
    eval_dir: Path,
) -> dict[str, object]:
    """在阈值网格上扫描，给出「precision 达标前提下 recall 最大」的建议值。"""
    results: list[dict[str, object]] = []

    for face_th in _FACE_GRID:
        # 复制一份 settings，避免改到全局单例
        trial = settings.model_copy(update={"face_match_threshold": face_th})
        # 扫描阶段不做归因（每个格点都查一遍库太慢），只要 precision/recall
        _, summary = await evaluate(session, embedding, trial, eval_dir, attribute=False)
        results.append(
            {
                "face_threshold": face_th,
                "recall": summary["recall"],
                "precision": summary["precision"],
            }
        )
        log.info(
            "sweep_point",
            face=face_th,
            recall=summary["recall"],
            precision=summary["precision"],
        )

    acceptable = [r for r in results if float(str(r["precision"])) >= _MIN_PRECISION]
    best = max(acceptable, key=lambda r: float(str(r["recall"])), default=None)

    return {
        "grid": results,
        "min_precision": _MIN_PRECISION,
        "recommended": best,
        "note": (
            "recommended 为空说明网格内没有任何组合能达到 precision 下限 —— "
            "此时问题不在阈值，先看 miss_attribution 和评估集里是否有长得像的人。"
        ),
    }
