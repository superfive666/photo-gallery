"""向量工具。

⚠️ embedding 的 L2 归一化由 `embedding` 服务在出口统一完成，下游拿到的向量已经是单位向量。
这里的 `l2_normalize` 只用于两个场景：
  1. `embedding` 服务自己的出口归一化；
  2. 对多个单位向量求均值后重新归一化（求均值会破坏单位模长）。

不要在别处对已经归一化的向量再调用它 —— 那说明数据流向出了问题。
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from numpy.typing import NDArray

_EPS = 1e-12


def l2_normalize(vec: Sequence[float] | NDArray[np.float32]) -> NDArray[np.float32]:
    """把向量缩放到单位模长。零向量原样返回（避免除零产生 NaN 污染整库）。"""
    arr = np.asarray(vec, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    if norm < _EPS:
        return arr
    return (arr / norm).astype(np.float32)


def mean_embedding(vectors: Sequence[Sequence[float]]) -> NDArray[np.float32]:
    """多个单位向量求均值再归一化，得到一个更稳的查询点 / 簇心。

    用于两处：
      - 用户上传多张自拍时合成单一查询向量（等价于一个角度更中性的「平均长相」）；
      - person 簇心。

    均值本身不是单位向量，所以必须重新归一化，否则 cosine 相似度的量纲会漂。
    """
    if not vectors:
        raise ValueError("mean_embedding 需要至少一个向量")
    stacked = np.asarray(vectors, dtype=np.float32)
    return l2_normalize(stacked.mean(axis=0))


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """两个**已归一化**向量的 cosine 相似度，即点积。

    仅用于测试与离线评估。生产检索走 pgvector 的 `<=>`（cosine 距离），
    相似度 = 1 - distance。
    """
    return float(np.dot(np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)))
