"""向量不变量的测试。

L2 归一化是全库的不变量：pgvector 的 cosine 距离在向量非单位长时依然能算，但
`1 - distance` 就不再是我们以为的那个相似度，阈值会整体漂移 —— 而且不会报错。
所以这里把它钉住。
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from gallery_core.vector import cosine_similarity, l2_normalize, mean_embedding


def test_l2_normalize_produces_unit_vector() -> None:
    vec = l2_normalize([3.0, 4.0])
    assert math.isclose(float(np.linalg.norm(vec)), 1.0, abs_tol=1e-6)
    assert math.isclose(float(vec[0]), 0.6, abs_tol=1e-6)


def test_l2_normalize_handles_zero_vector() -> None:
    """零向量必须原样返回而不是产生 NaN —— NaN 一旦入库会污染整个索引。"""
    vec = l2_normalize([0.0, 0.0, 0.0])
    assert not np.isnan(vec).any()
    assert float(np.linalg.norm(vec)) == 0.0


def test_mean_embedding_renormalizes() -> None:
    """两个单位向量的均值不是单位向量，必须重新归一化。"""
    a = l2_normalize([1.0, 0.0]).tolist()
    b = l2_normalize([0.0, 1.0]).tolist()
    mean = mean_embedding([a, b])
    assert math.isclose(float(np.linalg.norm(mean)), 1.0, abs_tol=1e-6)


def test_mean_embedding_rejects_empty() -> None:
    with pytest.raises(ValueError, match="至少一个向量"):
        mean_embedding([])


def test_mean_of_selfies_sits_between_them() -> None:
    """多张自拍取均值应落在它们之间 —— 这是「更中性的查询点」的直观含义。"""
    a = l2_normalize([1.0, 0.2]).tolist()
    b = l2_normalize([1.0, -0.2]).tolist()
    mean = mean_embedding([a, b]).tolist()
    assert cosine_similarity(mean, a) > 0.9
    assert cosine_similarity(mean, b) > 0.9
    # 均值到两者的相似度应该相等（对称输入）
    assert math.isclose(cosine_similarity(mean, a), cosine_similarity(mean, b), abs_tol=1e-6)
