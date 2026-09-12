"""純標準函式庫統計工具（取代 numpy / scipy 的必要功能）。"""
from __future__ import annotations

import math
from typing import Iterable, Sequence


def clean(xs: Iterable) -> list[float]:
    out = []
    for x in xs:
        if x is None:
            continue
        try:
            v = float(x)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v):
            out.append(v)
    return out


def mean(xs: Sequence[float]) -> float:
    xs = clean(xs)
    return sum(xs) / len(xs) if xs else 0.0


def stdev(xs: Sequence[float]) -> float:
    xs = clean(xs)
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def median(xs: Sequence[float]) -> float:
    xs = sorted(clean(xs))
    n = len(xs)
    if n == 0:
        return 0.0
    if n % 2:
        return xs[n // 2]
    return (xs[n // 2 - 1] + xs[n // 2]) / 2


def quantile(xs: Sequence[float], q: float) -> float:
    xs = sorted(clean(xs))
    if not xs:
        return 0.0
    if len(xs) == 1:
        return xs[0]
    pos = q * (len(xs) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return xs[lo] * (1 - frac) + xs[hi] * frac


def mad(xs: Sequence[float]) -> float:
    """Median Absolute Deviation（乘上 1.4826 使其與常態標準差可比）。"""
    xs = clean(xs)
    if not xs:
        return 0.0
    m = median(xs)
    return 1.4826 * median([abs(x - m) for x in xs])


def robust_z(value: float | None, pool: Sequence[float]) -> float:
    """穩健標準化分數：以中位數與 MAD 計算，對離群值不敏感。"""
    if value is None:
        return 0.0
    pool = clean(pool)
    if len(pool) < 5:
        return 0.0
    m = median(pool)
    s = mad(pool)
    if s <= 1e-9:
        s = stdev(pool)
    if s <= 1e-9:
        return 0.0
    return (float(value) - m) / s


def norm_cdf(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def logistic(z: float) -> float:
    if z >= 0:
        return 1 / (1 + math.exp(-min(60.0, z)))
    e = math.exp(max(-60.0, z))
    return e / (1 + e)


# ---------------------------------------------------------------- 卡方
def _gammap(a: float, x: float) -> float:
    """正規化下不完全 gamma 函數 P(a, x)。"""
    if x <= 0:
        return 0.0
    if x < a + 1:
        # 級數展開
        ap, total, delta = a, 1.0 / a, 1.0 / a
        for _ in range(500):
            ap += 1
            delta *= x / ap
            total += delta
            if abs(delta) < abs(total) * 1e-12:
                break
        return total * math.exp(-x + a * math.log(x) - math.lgamma(a))
    return 1.0 - _gammaq_cf(a, x)


def _gammaq_cf(a: float, x: float) -> float:
    """正規化上不完全 gamma 函數 Q(a, x)，連分數展開。"""
    tiny = 1e-300
    b = x + 1 - a
    c = 1 / tiny
    d = 1 / b if abs(b) > tiny else 1 / tiny
    h = d
    for i in range(1, 500):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-12:
            break
    return math.exp(-x + a * math.log(x) - math.lgamma(a)) * h


def chi2_sf(x: float, df: int) -> float:
    """卡方分布右尾機率（p-value）。"""
    if x <= 0 or df <= 0:
        return 1.0
    return max(0.0, min(1.0, 1.0 - _gammap(df / 2.0, x / 2.0)))


# ---------------------------------------------------------------- 模型評估
def auc(scores: Sequence[float], labels: Sequence[int]) -> float:
    """ROC AUC（以 Mann-Whitney U 統計量計算，含同分處理）。"""
    pairs = [(s, y) for s, y in zip(scores, labels) if s is not None]
    pos = sum(1 for _, y in pairs if y == 1)
    neg = len(pairs) - pos
    if pos == 0 or neg == 0:
        return 0.5
    pairs.sort(key=lambda t: t[0])
    ranks: list[float] = [0.0] * len(pairs)
    i = 0
    while i < len(pairs):
        j = i
        while j + 1 < len(pairs) and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        avg_rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[k] = avg_rank
        i = j + 1
    rank_sum = sum(r for r, (_, y) in zip(ranks, pairs) if y == 1)
    return (rank_sum - pos * (pos + 1) / 2) / (pos * neg)


def roc_curve(scores: Sequence[float], labels: Sequence[int], points: int = 60):
    data = sorted(zip(scores, labels), key=lambda t: -t[0])
    pos = sum(1 for _, y in data if y == 1) or 1
    neg = len(data) - pos or 1
    out = [{"fpr": 0.0, "tpr": 0.0}]
    tp = fp = 0
    step = max(1, len(data) // points)
    for i, (_, y) in enumerate(data, 1):
        if y == 1:
            tp += 1
        else:
            fp += 1
        if i % step == 0 or i == len(data):
            out.append({"fpr": fp / neg, "tpr": tp / pos})
    out.append({"fpr": 1.0, "tpr": 1.0})
    return out


def pr_curve(scores: Sequence[float], labels: Sequence[int], points: int = 60):
    data = sorted(zip(scores, labels), key=lambda t: -t[0])
    pos = sum(1 for _, y in data if y == 1) or 1
    out = []
    tp = fp = 0
    step = max(1, len(data) // points)
    for i, (_, y) in enumerate(data, 1):
        if y == 1:
            tp += 1
        else:
            fp += 1
        if i % step == 0 or i == len(data):
            out.append({"recall": tp / pos, "precision": tp / (tp + fp)})
    return out


def precision_at_k(scores: Sequence[float], labels: Sequence[int], k: int) -> float:
    data = sorted(zip(scores, labels), key=lambda t: -t[0])[:k]
    if not data:
        return 0.0
    return sum(1 for _, y in data if y == 1) / len(data)


def recall_at_k(scores: Sequence[float], labels: Sequence[int], k: int) -> float:
    total_pos = sum(1 for y in labels if y == 1)
    if total_pos == 0:
        return 0.0
    data = sorted(zip(scores, labels), key=lambda t: -t[0])[:k]
    return sum(1 for _, y in data if y == 1) / total_pos


def lift_curve(scores: Sequence[float], labels: Sequence[int], buckets: int = 10):
    data = sorted(zip(scores, labels), key=lambda t: -t[0])
    n = len(data)
    base = (sum(1 for _, y in data if y == 1) / n) if n else 0
    out = []
    if n == 0 or base == 0:
        return out
    size = max(1, n // buckets)
    for b in range(buckets):
        chunk = data[b * size:(b + 1) * size]
        if not chunk:
            break
        rate = sum(1 for _, y in chunk if y == 1) / len(chunk)
        out.append({"decile": b + 1, "rate": rate, "lift": rate / base})
    return out


def percentile_rank(value: float, pool: Sequence[float]) -> float:
    pool = clean(pool)
    if not pool:
        return 0.0
    below = sum(1 for p in pool if p < value)
    equal = sum(1 for p in pool if p == value)
    return (below + 0.5 * equal) / len(pool)
