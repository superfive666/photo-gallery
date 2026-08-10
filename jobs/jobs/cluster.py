"""把 face embedding 聚类成 person。

这是两段式检索的基础。簇心（成员向量均值再归一化）包含同一人的正脸、侧脸、不同光照的多个
样本，比任何单张照片都更接近这个人的「平均长相」，因此对查询自拍的角度差异更鲁棒 ——
召回率明显高于逐脸比对。

用 DBSCAN 而非 KMeans：人数事先未知，且必须允许「噪声点」存在（只出现一次的人、
质量差的脸）。噪声点不进任何簇，检索时由「直接命中脸」那一路兜底。
"""

from __future__ import annotations

import datetime as dt

import numpy as np
from sklearn.cluster import DBSCAN
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from gallery_core.config import Settings
from gallery_core.logging import get_logger
from gallery_core.models import Face, JobRun, Person
from gallery_core.uuid7 import uuid7
from gallery_core.vector import mean_embedding

log = get_logger(__name__)


async def recluster(session: AsyncSession, settings: Settings) -> dict[str, object]:
    """全量重聚类。

    为什么是全量而不是增量：DBSCAN 的簇结构依赖全局密度，新增少量点也可能让两个簇合并或
    分裂。在十万级向量的规模下全量重跑只需数十秒，不值得为增量引入正确性风险。
    数据量再上一个量级时才需要考虑增量方案（例如先用簇心分配、周期性全量校正）。
    """
    run = JobRun(kind="cluster", stats={})
    session.add(run)
    await session.commit()

    stats: dict[str, object] = {}
    try:
        rows = (
            await session.execute(
                select(Face.id, Face.album, Face.embedding).where(
                    Face.model_name == settings.model_name,
                    Face.model_version == settings.model_version,
                )
            )
        ).all()

        if not rows:
            stats = {"faces": 0, "persons": 0, "noise": 0}
            run.status = "succeeded"
            return stats

        face_keys = [(r.album, r.id) for r in rows]
        matrix = np.asarray([r.embedding for r in rows], dtype=np.float32)

        # 向量已 L2 归一化，所以 cosine 距离 = 1 - 点积，可以直接用 metric="cosine"。
        # eps 是 cosine 距离阈值：越小簇越纯但越碎。需要用评估集标定，见 docs/evaluation.md。
        labels = DBSCAN(
            eps=settings.cluster_eps,
            min_samples=settings.cluster_min_samples,
            metric="cosine",
            n_jobs=-1,
        ).fit_predict(matrix)

        # 先清空旧的 person。face.person_id 是 ON DELETE SET NULL，会自动置空。
        await session.execute(delete(Person))
        await session.flush()

        noise = int(np.sum(labels == -1))
        person_count = 0

        for label in sorted(set(labels.tolist()) - {-1}):
            member_idx = [i for i, lb in enumerate(labels) if lb == label]
            centroid = mean_embedding([matrix[i].tolist() for i in member_idx])

            person = Person(
                id=uuid7(),
                centroid=centroid.tolist(),
                face_count=len(member_idx),
                model_name=settings.model_name,
                model_version=settings.model_version,
            )
            session.add(person)
            await session.flush()

            # face 是按 album 分区的，UPDATE 必须带上 album 才能裁剪到相关分区；
            # 只按 id 更新会扫描全部分区。一个簇通常跨多个相册，所以按 album 分组批量更新。
            by_album: dict[str, list[str]] = {}
            for i in member_idx:
                album, face_id = face_keys[i]
                by_album.setdefault(album, []).append(str(face_id))
            for album, ids in by_album.items():
                await session.execute(
                    text(
                        "UPDATE face SET person_id = :pid "
                        "WHERE album = :album AND id = ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"pid": str(person.id), "album": album, "ids": ids},
                )
            person_count += 1

        await session.commit()

        stats = {
            "faces": len(face_keys),
            "persons": person_count,
            # 噪声点比例是聚类质量的直接信号：过高说明 eps 太小或人脸质量太差。
            # 这些脸只能靠检索的「直接命中」那一路被找到。
            "noise": noise,
            "noise_ratio": round(noise / len(face_keys), 4),
        }
        log.info("cluster_done", **stats)
        run.status = "succeeded"
        return stats

    except Exception as exc:
        run.status = "failed"
        run.error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        run.stats = stats
        run.finished_at = dt.datetime.now(tz=dt.UTC)
        await session.commit()
