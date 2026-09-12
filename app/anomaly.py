"""無監督異常偵測 — 純標準函式庫實作。

1. Isolation Forest：隨機切分樹的平均路徑長度越短代表越容易被孤立，即越異常。
2. kNN 距離異常：標準化特徵空間中，與第 k 個最近鄰的距離越大越異常。

兩者互補：Isolation Forest 擅長全域稀疏離群，kNN 擅長局部密度異常。
最終以兩者百分位排名平均，得到 0~100 的異常分數。
"""
from __future__ import annotations

import math
import random
from typing import Sequence

from . import stats


def _c(n: int) -> float:
    if n <= 1:
        return 1.0
    return 2 * (math.log(n - 1) + 0.5772156649) - 2 * (n - 1) / n


class _Node:
    __slots__ = ("feat", "split", "left", "right", "size", "depth")

    def __init__(self):
        self.feat = -1
        self.split = 0.0
        self.left = None
        self.right = None
        self.size = 0
        self.depth = 0


class IsolationForest:
    def __init__(self, n_trees: int = 120, sample_size: int = 256, seed: int = 7):
        self.n_trees = n_trees
        self.sample_size = sample_size
        self.rng = random.Random(seed)
        self.trees: list[_Node] = []
        self.height_limit = 1

    def fit(self, X: Sequence[Sequence[float]]) -> "IsolationForest":
        n = len(X)
        if n == 0:
            return self
        m = min(self.sample_size, n)
        self.height_limit = max(1, int(math.ceil(math.log2(max(2, m)))))
        for _ in range(self.n_trees):
            idx = self.rng.sample(range(n), m) if m < n else list(range(n))
            sub = [X[i] for i in idx]
            self.trees.append(self._build(sub, 0))
        return self

    def _build(self, data: list[Sequence[float]], depth: int) -> _Node:
        node = _Node()
        node.size = len(data)
        node.depth = depth
        if depth >= self.height_limit or len(data) <= 1:
            return node
        d = len(data[0])
        feats = list(range(d))
        self.rng.shuffle(feats)
        for f in feats:
            vals = [row[f] for row in data]
            lo, hi = min(vals), max(vals)
            if hi - lo > 1e-12:
                node.feat = f
                node.split = self.rng.uniform(lo, hi)
                left = [r for r in data if r[f] < node.split]
                right = [r for r in data if r[f] >= node.split]
                if not left or not right:
                    node.feat = -1
                    return node
                node.left = self._build(left, depth + 1)
                node.right = self._build(right, depth + 1)
                return node
        return node

    def _path_length(self, node: _Node, row: Sequence[float]) -> float:
        depth = 0
        while node.feat >= 0:
            node = node.left if row[node.feat] < node.split else node.right
            depth += 1
        return depth + _c(node.size)

    def score(self, X: Sequence[Sequence[float]]) -> list[float]:
        if not self.trees:
            return [0.5] * len(X)
        cn = _c(min(self.sample_size, max(2, len(X))))
        out = []
        for row in X:
            h = sum(self._path_length(t, row) for t in self.trees) / len(self.trees)
            out.append(2 ** (-h / cn))
        return out


def knn_outlier(X: Sequence[Sequence[float]], k: int = 8, max_ref: int = 900,
                seed: int = 11) -> list[float]:
    """kNN 距離異常分數（為控制計算量，參考集最多取 max_ref 筆）。"""
    n = len(X)
    if n < k + 2:
        return [0.0] * n
    rng = random.Random(seed)
    ref_idx = list(range(n)) if n <= max_ref else rng.sample(range(n), max_ref)
    ref = [X[i] for i in ref_idx]
    out = []
    for i, row in enumerate(X):
        dists = []
        for j, other in enumerate(ref):
            if ref_idx[j] == i:
                continue
            s = 0.0
            for a, b in zip(row, other):
                d = a - b
                s += d * d
            dists.append(s)
        dists.sort()
        kk = min(k, len(dists))
        if kk == 0:
            out.append(0.0)
            continue
        out.append(math.sqrt(sum(dists[:kk]) / kk))
    return out


def standardize(rows: Sequence[Sequence[float]]) -> list[list[float]]:
    """以中位數 / MAD 穩健標準化，並裁切至 ±5 以抑制極端值主導。"""
    if not rows:
        return []
    d = len(rows[0])
    meds, scales = [], []
    for f in range(d):
        col = [r[f] for r in rows]
        m = stats.median(col)
        s = stats.mad(col) or stats.stdev(col) or 1.0
        meds.append(m)
        scales.append(s)
    out = []
    for r in rows:
        out.append([max(-5.0, min(5.0, (r[f] - meds[f]) / scales[f])) for f in range(d)])
    return out


def combined_anomaly(rows: Sequence[Sequence[float]]) -> dict:
    """回傳 Isolation Forest / kNN / 綜合異常分數（皆為 0~100 百分位尺度）。"""
    n = len(rows)
    if n == 0:
        return {"iforest": [], "knn": [], "combined": []}
    Z = standardize(rows)
    iso = IsolationForest().fit(Z).score(Z)
    knn = knn_outlier(Z)
    iso_rank = [stats.percentile_rank(v, iso) * 100 for v in iso]
    knn_rank = [stats.percentile_rank(v, knn) * 100 for v in knn]
    comb = [round(0.6 * a + 0.4 * b, 2) for a, b in zip(iso_rank, knn_rank)]
    return {
        "iforest": [round(v, 4) for v in iso],
        "iforest_rank": [round(v, 2) for v in iso_rank],
        "knn_rank": [round(v, 2) for v in knn_rank],
        "combined": comb,
    }
