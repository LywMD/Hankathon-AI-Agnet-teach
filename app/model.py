"""監督式風險模型：L2 正則化邏輯迴歸（純標準函式庫，含缺值補值與標準化）。

為什麼用邏輯迴歸？
* 監理場域需要「可解釋」：每個特徵的係數方向與貢獻度可直接向被監理對象說明。
* 資料量級（數百至數千所機構）下，線性模型的泛化表現穩定，不易過擬合。
* 係數可直接轉換為勝算比（odds ratio），便於撰寫稽查理由。

訓練標籤採「時間切分」設計：以 T 時點之前的特徵，預測 T 之後 12 個月內
是否出現裁罰紀錄，因此模型學到的是「事前預警」而非「事後描述」。
"""
from __future__ import annotations

import math
import random
from typing import Sequence

from . import stats


def _solve(A: list[list[float]], b: list[float]) -> list[float]:
    """高斯消去法解線性系統（含部分樞軸選擇）。"""
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-12:
            M[col][col] += 1e-8
            piv = col
        M[col], M[piv] = M[piv], M[col]
        pv = M[col][col]
        for r in range(col + 1, n):
            factor = M[r][col] / pv
            if factor == 0.0:
                continue
            row_r, row_c = M[r], M[col]
            for c in range(col, n + 1):
                row_r[c] -= factor * row_c[c]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        s = M[i][n] - sum(M[i][j] * x[j] for j in range(i + 1, n))
        x[i] = s / M[i][i]
    return x


class LogisticModel:
    """以 Newton-Raphson（IRLS）求解的 L2 正則化邏輯迴歸。

    相較梯度下降，IRLS 在特徵數不多（數十個）時只需約 10 次迭代即收斂，
    在純 Python 環境下可把訓練時間壓到毫秒～秒級。
    """

    # l2 預設值由交叉驗證選定：特徵間高度相關（如裁罰次數與裁罰強度），
    # 過弱的正則化會產生方向與法規常識相反的係數，不利於向被監理對象說明。
    def __init__(self, l2: float = 0.35, max_iter: int = 30, tol: float = 1e-7,
                 class_weight: bool = True):
        self.l2 = l2
        self.max_iter = max_iter
        self.tol = tol
        self.class_weight = class_weight
        self.names: list[str] = []
        self.center: list[float] = []
        self.scale: list[float] = []
        self.coef: list[float] = []
        self.bias = 0.0
        self.trained = False

    # ------------------------------------------------ 前處理
    def _prepare(self, rows: Sequence[dict], fit: bool) -> list[list[float]]:
        if fit:
            self.center = []
            self.scale = []
            for name in self.names:
                col = [r.get(name) for r in rows]
                col = stats.clean(col)
                med = stats.median(col) if col else 0.0
                sc = (stats.mad(col) or stats.stdev(col) or 1.0) if col else 1.0
                self.center.append(med)
                self.scale.append(sc if abs(sc) > 1e-9 else 1.0)
        X = []
        for r in rows:
            row = []
            for i, name in enumerate(self.names):
                v = r.get(name)
                try:
                    v = float(v)
                    if not math.isfinite(v):
                        v = self.center[i]
                except (TypeError, ValueError):
                    v = self.center[i]
                row.append(max(-6.0, min(6.0, (v - self.center[i]) / self.scale[i])))
            X.append(row)
        return X

    # ------------------------------------------------ 訓練
    def fit(self, rows: Sequence[dict], y: Sequence[int], names: Sequence[str]):
        self.names = list(names)
        Xd = self._prepare(rows, fit=True)
        n, d = len(Xd), len(self.names)
        self.coef = [0.0] * d
        self.bias = 0.0
        if n == 0 or d == 0:
            return self
        # 加上截距項欄位
        X = [[1.0] + row for row in Xd]
        p_dim = d + 1
        pos = sum(y) or 1
        neg = (n - pos) or 1
        w_pos = (n / (2 * pos)) if self.class_weight else 1.0
        w_neg = (n / (2 * neg)) if self.class_weight else 1.0
        sw = [w_pos if y[i] == 1 else w_neg for i in range(n)]
        beta = [0.0] * p_dim
        lam = self.l2

        wtot = sum(sw) or 1.0
        for _ in range(self.max_iter):
            grad = [0.0] * p_dim
            H = [[0.0] * p_dim for _ in range(p_dim)]
            for i in range(n):
                xi = X[i]
                z = 0.0
                for j in range(p_dim):
                    z += beta[j] * xi[j]
                p = stats.logistic(z)
                wi = sw[i]
                r = wi * (p - y[i])
                w2 = wi * max(1e-6, p * (1 - p))
                for j in range(p_dim):
                    xj = xi[j]
                    if xj == 0.0:
                        continue
                    grad[j] += r * xj
                    wx = w2 * xj
                    Hj = H[j]
                    for k in range(j, p_dim):
                        Hj[k] += wx * xi[k]
            # 以加權樣本數正規化，使 l2 超參數的量級與資料筆數無關
            for j in range(p_dim):
                grad[j] /= wtot
                for k in range(j, p_dim):
                    H[j][k] /= wtot
                for k in range(j):
                    H[j][k] = H[k][j]
            for j in range(1, p_dim):          # 截距不做正則化
                grad[j] += lam * beta[j]
                H[j][j] += lam
            H[0][0] += 1e-9
            try:
                step = _solve(H, grad)
            except ZeroDivisionError:
                break
            delta = 0.0
            for j in range(p_dim):
                beta[j] -= step[j]
                delta = max(delta, abs(step[j]))
            if delta < self.tol:
                break

        self.bias = beta[0]
        self.coef = beta[1:]
        self.trained = True
        return self

    def predict_proba(self, rows: Sequence[dict]) -> list[float]:
        if not self.trained:
            return [0.0] * len(rows)
        X = self._prepare(rows, fit=False)
        out = []
        for row in X:
            z = self.bias + sum(self.coef[j] * row[j] for j in range(len(self.names)))
            out.append(stats.logistic(z))
        return out

    def contributions(self, row: dict) -> list[dict]:
        """單筆樣本的線性貢獻拆解（近似 SHAP 的可加性解釋）。"""
        if not self.trained:
            return []
        X = self._prepare([row], fit=False)[0]
        items = []
        for j, name in enumerate(self.names):
            items.append({"feature": name, "z": round(X[j], 3),
                          "coef": round(self.coef[j], 4),
                          "contribution": round(self.coef[j] * X[j], 4)})
        items.sort(key=lambda x: -abs(x["contribution"]))
        return items

    def importance(self) -> list[dict]:
        if not self.trained:
            return []
        out = [{"feature": n, "coef": round(c, 4), "abs": abs(c),
                "odds_ratio": round(math.exp(max(-20, min(20, c))), 3)}
               for n, c in zip(self.names, self.coef)]
        out.sort(key=lambda x: -x["abs"])
        return out


def cross_validate(rows: Sequence[dict], y: Sequence[int], names: Sequence[str],
                   folds: int = 5, seed: int = 3, **kw) -> dict:
    """分層 K 折交叉驗證，回傳 out-of-fold 預測與評估指標。"""
    n = len(rows)
    idx_pos = [i for i in range(n) if y[i] == 1]
    idx_neg = [i for i in range(n) if y[i] == 0]
    rng = random.Random(seed)
    rng.shuffle(idx_pos)
    rng.shuffle(idx_neg)
    assign = [0] * n
    for k, i in enumerate(idx_pos):
        assign[i] = k % folds
    for k, i in enumerate(idx_neg):
        assign[i] = k % folds
    oof = [0.0] * n
    for f in range(folds):
        tr = [i for i in range(n) if assign[i] != f]
        te = [i for i in range(n) if assign[i] == f]
        if not te or not tr:
            continue
        m = LogisticModel(**kw)
        m.fit([rows[i] for i in tr], [y[i] for i in tr], names)
        for i, p in zip(te, m.predict_proba([rows[i] for i in te])):
            oof[i] = p
    return {"oof": oof, "auc": stats.auc(oof, y)}


def evaluate(scores: Sequence[float], y: Sequence[int], ks=(20, 50, 100)) -> dict:
    n = len(scores)
    pos = sum(y)
    res = {
        "n": n, "positives": pos,
        "base_rate": round(pos / n, 4) if n else 0.0,
        "auc": round(stats.auc(scores, y), 4),
        "roc": stats.roc_curve(scores, y),
        "pr": stats.pr_curve(scores, y),
        "lift": stats.lift_curve(scores, y),
        "at_k": [],
    }
    for k in ks:
        if k > n:
            continue
        p = stats.precision_at_k(scores, y, k)
        r = stats.recall_at_k(scores, y, k)
        base = res["base_rate"] or 1e-9
        res["at_k"].append({
            "k": k, "precision": round(p, 4), "recall": round(r, 4),
            "lift": round(p / base, 2),
        })
    # 以分數前 20% 為預警名單的混淆矩陣
    cut = max(1, int(n * 0.2))
    order = sorted(range(n), key=lambda i: -scores[i])
    flagged = set(order[:cut])
    tp = sum(1 for i in flagged if y[i] == 1)
    fp = cut - tp
    fn = pos - tp
    tn = n - cut - fn
    res["confusion"] = {"threshold_pct": 20, "tp": tp, "fp": fp, "fn": fn, "tn": tn,
                        "precision": round(tp / max(1, cut), 4),
                        "recall": round(tp / max(1, pos), 4),
                        "f1": round(2 * tp / max(1, (2 * tp + fp + fn)), 4)}
    return res
