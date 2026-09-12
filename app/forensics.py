"""基礎鑑識會計（Forensic Accounting）模組。

實作四類常見的數字鑑識檢定，並針對教保機構決算特性設計交叉核對：

1. 班佛定律首位數檢定（Benford's Law）：偵測人工編造或調整過的金額。
2. 尾數／整數偏誤（Round-number bias）：偵測「湊整數」的估列或虛列。
3. 比率分析與同儕基準偏離（Ratio analysis + robust z-score）。
4. 收費標準 × 實際幼生數 與 決算學雜費收入 的交叉核對落差。
"""
from __future__ import annotations

import math
from typing import Sequence

from . import stats

BENFORD_EXPECTED = [math.log10(1 + 1 / d) for d in range(1, 10)]


def first_digit(x: float) -> int | None:
    x = abs(float(x))
    if x < 1:
        return None
    while x >= 10:
        x /= 10
    d = int(x)
    return d if 1 <= d <= 9 else None


def digit_counts(amounts: Sequence[float]) -> list[int]:
    out = [0] * 9
    for a in amounts:
        d = first_digit(a) if a else None
        if d:
            out[d - 1] += 1
    return out


def baseline_mad(n: int, expected: Sequence[float]) -> float:
    """給定期望分布下，MAD 純因抽樣誤差產生的期望值。"""
    if n <= 0:
        return 1.0
    return sum(math.sqrt(2 * p * (1 - p) / (math.pi * n)) for p in expected) / 9


def digit_conformity(observed: Sequence[int], expected: Sequence[float],
                     min_n: int = 25, basis: str = "同儕實證分布") -> dict:
    """首位數分布適合度檢定（可指定期望分布）。

    為什麼不是直接套用班佛定律？
    單一機構的決算科目金額只有數百筆，且集中在少數量級（薪資百萬級、
    文具費萬元級），本質上就不會服從班佛定律；若直接與理論值比較，
    幾乎所有機構都會被判為異常。

    因此個別機構改與「同儕實證首位數分布」比較——同類機構共享相同的
    科目結構與量級分布，偏離該分布才真正代表數字被人工調整過。
    班佛定律則保留在整體資料層級使用（大樣本下才成立）。
    """
    n = sum(observed)
    exp = list(expected)
    s = sum(exp) or 1.0
    exp = [p / s for p in exp]
    base = baseline_mad(max(1, n), exp)
    if n < min_n:
        return {"n": n, "sufficient": False, "basis": basis,
                "observed": list(observed), "observed_pct": [0.0] * 9,
                "expected_pct": [round(p * 100, 3) for p in exp],
                "chi2": 0.0, "p_value": 1.0, "mad": 0.0,
                "baseline_mad": round(base, 5), "mad_ratio": 0.0,
                "level": "資料不足", "score": 0.0}
    obs_pct = [o / n for o in observed]
    chi2 = 0.0
    for i in range(9):
        e = exp[i] * n
        if e > 0:
            chi2 += (observed[i] - e) ** 2 / e
    p = stats.chi2_sf(chi2, 8)
    mad_val = sum(abs(obs_pct[i] - exp[i]) for i in range(9)) / 9
    ratio = mad_val / base if base > 0 else 0.0
    if ratio < 1.2:
        level = "符合"
    elif ratio < 1.6:
        level = "輕微偏離"
    elif ratio < 2.2:
        level = "明顯偏離"
    else:
        level = "高度偏離"
    p_component = min(1.0, max(0.0, -math.log10(max(p, 1e-12)) / 5.0)) * 100
    ratio_component = max(0.0, min(100.0, (ratio - 1.1) / 1.5 * 100))
    score = 0.6 * p_component + 0.4 * ratio_component
    return {"n": n, "sufficient": True, "basis": basis,
            "observed": list(observed),
            "observed_pct": [round(v * 100, 3) for v in obs_pct],
            "expected_pct": [round(v * 100, 3) for v in exp],
            "chi2": round(chi2, 3), "p_value": round(p, 8),
            "mad": round(mad_val, 5), "baseline_mad": round(base, 5),
            "mad_ratio": round(ratio, 3), "level": level,
            "score": round(score, 2)}


def benford_baseline_mad(n: int) -> float:
    """在資料完全符合班佛定律的前提下，MAD 因抽樣誤差而產生的期望值。

    Nigrini 常用的 MAD 絕對門檻（0.006 / 0.012 / 0.015）是針對大樣本
    （數千筆以上）而訂。單一機構的決算科目往往只有一兩百筆，此時純粹
    的抽樣雜訊就會讓 MAD 超過 0.015，若直接套用絕對門檻會造成大量誤判。

    因此本系統改以「MAD ÷ 該樣本數下的期望 MAD」作為偏離倍數。
    期望值取自半常態分布的平均絕對偏差：E|p̂ - p| ≈ sqrt(2p(1-p)/(πn))。
    """
    if n <= 0:
        return 1.0
    total = 0.0
    for p in BENFORD_EXPECTED:
        total += math.sqrt(2 * p * (1 - p) / (math.pi * n))
    return total / 9


def benford(amounts: Sequence[float], min_n: int = 25) -> dict:
    """班佛定律首位數檢定（樣本數校正版）。

    回傳三個互補指標：
    * mad_ratio：MAD 相對於同樣本數期望雜訊的倍數（主要判讀依據）
    * p_value：卡方適合度檢定的右尾機率（樣本數已內含於統計量）
    * score：0~100 風險分數，由上述兩者加權而成
    """
    digits = [d for d in (first_digit(a) for a in amounts if a) if d]
    n = len(digits)
    observed = [0] * 9
    for d in digits:
        observed[d - 1] += 1
    base = benford_baseline_mad(max(1, n))
    if n < min_n:
        return {
            "n": n, "sufficient": False, "observed": observed,
            "observed_pct": [0.0] * 9,
            "expected_pct": [round(p * 100, 3) for p in BENFORD_EXPECTED],
            "chi2": 0.0, "p_value": 1.0, "mad": 0.0,
            "baseline_mad": round(base, 5), "mad_ratio": 0.0,
            "level": "資料不足", "score": 0.0,
        }
    obs_pct = [o / n for o in observed]
    chi2 = 0.0
    for i in range(9):
        exp = BENFORD_EXPECTED[i] * n
        chi2 += (observed[i] - exp) ** 2 / exp
    p = stats.chi2_sf(chi2, 8)
    mad_val = sum(abs(obs_pct[i] - BENFORD_EXPECTED[i]) for i in range(9)) / 9
    ratio = mad_val / base if base > 0 else 0.0

    # 判讀規則依樣本數選擇：
    # * 大樣本（>=5000 筆）：卡方檢定對極小偏離也會顯著（大樣本敏感性問題），
    #   因此採 Nigrini 建議的 MAD 絕對門檻。
    # * 小樣本：MAD 絕對門檻會被抽樣雜訊淹沒，改用 MAD／期望雜訊倍數。
    if n >= 5000:
        criterion = "Nigrini MAD 絕對門檻"
        if mad_val < 0.006:
            level = "高度符合"
        elif mad_val < 0.012:
            level = "可接受"
        elif mad_val < 0.015:
            level = "邊際偏離"
        else:
            level = "明顯偏離"
        score = max(0.0, min(100.0, (mad_val - 0.006) / 0.024 * 100))
    else:
        criterion = "MAD／抽樣雜訊倍數"
        if ratio < 1.15:
            level = "符合"
        elif ratio < 1.5:
            level = "輕微偏離"
        elif ratio < 2.0:
            level = "明顯偏離"
        else:
            level = "高度偏離"
        p_component = min(1.0, max(0.0, -math.log10(max(p, 1e-12)) / 4.0)) * 100
        ratio_component = max(0.0, min(100.0, (ratio - 1.1) / 1.2 * 100))
        score = 0.65 * p_component + 0.35 * ratio_component
    return {
        "n": n, "sufficient": True, "observed": observed,
        "observed_pct": [round(v * 100, 3) for v in obs_pct],
        "expected_pct": [round(p_ * 100, 3) for p_ in BENFORD_EXPECTED],
        "chi2": round(chi2, 3), "p_value": round(p, 8),
        "mad": round(mad_val, 5), "baseline_mad": round(base, 5),
        "mad_ratio": round(ratio, 3), "criterion": criterion,
        "level": level, "score": round(score, 2),
    }


def last_two_digits(amounts: Sequence[float], min_n: int = 60) -> dict:
    """末兩位數均勻性檢定（Nigrini 的 Last-Two-Digits Test）。

    這是本系統在「單一機構層級」最主要的數字鑑識工具，原因是：
    * 真實金額的末兩位數受含稅、零星品項、實際用量影響，理論上均勻分布於 00~99。
    * 末位數與金額量級無關，因此不受決算科目集中在少數量級的影響，
      不像首位數（班佛）檢定會因科目金額重複而失去檢定力。
    * 人工估列、湊整數、套用固定單價或事後填製，都會在末兩位數留下明顯群聚。

    檢定：卡方適合度（df = 99，期望各格 n/100）。
    """
    vals = [abs(int(round(a))) for a in amounts if a and abs(a) >= 100]
    n = len(vals)
    if n < min_n:
        return {"n": n, "sufficient": False, "chi2": 0.0, "p_value": 1.0,
                "top_endings": [], "max_share": 0.0, "score": 0.0,
                "level": "資料不足", "expected_share": 0.01}
    bins = [0] * 100
    for v in vals:
        bins[v % 100] += 1
    exp = n / 100
    chi2 = sum((o - exp) ** 2 / exp for o in bins)
    p = stats.chi2_sf(chi2, 99)
    order = sorted(range(100), key=lambda i: -bins[i])
    top = [{"ending": f"{i:02d}", "count": bins[i], "share": round(bins[i] / n, 4)}
           for i in order[:5] if bins[i] > 0]
    max_share = bins[order[0]] / n if n else 0.0
    # 分數：以 p 值為主（已內含樣本數校正），最大集中度為輔
    p_component = min(1.0, max(0.0, -math.log10(max(p, 1e-12)) / 6.0)) * 100
    conc_component = max(0.0, min(100.0, (max_share - 0.02) / 0.13 * 100))
    score = 0.6 * p_component + 0.4 * conc_component
    if score < 20:
        level = "均勻（正常）"
    elif score < 50:
        level = "輕微群聚"
    elif score < 75:
        level = "明顯群聚"
    else:
        level = "高度群聚"
    return {"n": n, "sufficient": True, "chi2": round(chi2, 2),
            "p_value": round(p, 8), "top_endings": top,
            "max_share": round(max_share, 4), "expected_share": 0.01,
            "distribution": bins, "level": level, "score": round(score, 2)}


def round_number_bias(amounts: Sequence[float]) -> dict:
    """整數偏誤：統計以 000 / 00 結尾的金額比例。

    自然發生的支出金額（含稅、含零星品項）以 1000 整數結尾的比例通常
    低於 8%；比例顯著偏高常見於估列、虛列或事後調整。
    """
    vals = [abs(int(round(a))) for a in amounts if a and abs(a) >= 100]
    n = len(vals)
    if n < 15:
        return {"n": n, "sufficient": False, "ratio_1000": 0.0, "ratio_100": 0.0,
                "score": 0.0, "level": "資料不足"}
    r1000 = sum(1 for v in vals if v % 1000 == 0) / n
    r100 = sum(1 for v in vals if v % 100 == 0) / n
    excess = max(0.0, r1000 - 0.02)
    score = max(0.0, min(100.0, excess / 0.15 * 100))
    level = "正常" if score < 20 else ("偏高" if score < 55 else "顯著偏高")
    return {"n": n, "sufficient": True, "ratio_1000": round(r1000, 4),
            "ratio_100": round(r100, 4), "score": round(score, 2), "level": level}


# ---------------------------------------------------------------- 比率
def safe_div(a, b, default=None):
    try:
        a = float(a)
        b = float(b)
    except (TypeError, ValueError):
        return default
    if b == 0:
        return default
    return a / b


def ratios(fin: dict, students: int | None = None) -> dict:
    """由單一年度決算計算關鍵財務比率。"""
    total_rev = fin.get("total_revenue")
    total_exp = fin.get("total_expense")
    if not total_rev:
        total_rev = sum(fin.get(k) or 0 for k in
                        ("revenue_tuition", "revenue_subsidy", "revenue_other")) or None
    if not total_exp:
        total_exp = sum(fin.get(k) or 0 for k in
                        ("expense_personnel", "expense_teaching", "expense_meal",
                         "expense_facility", "expense_rent", "expense_admin",
                         "expense_other")) or None
    stu = students or fin.get("students_avg") or None
    surplus = fin.get("surplus")
    if surplus is None and total_rev and total_exp:
        surplus = total_rev - total_exp
    return {
        "fiscal_year": fin.get("fiscal_year"),
        "total_revenue": total_rev,
        "total_expense": total_exp,
        "surplus": surplus,
        "personnel_ratio": safe_div(fin.get("expense_personnel"), total_exp),
        "teaching_ratio": safe_div(fin.get("expense_teaching"), total_exp),
        "meal_ratio": safe_div(fin.get("expense_meal"), total_exp),
        "admin_ratio": safe_div(fin.get("expense_admin"), total_exp),
        "facility_ratio": safe_div(fin.get("expense_facility"), total_exp),
        "surplus_ratio": safe_div(surplus, total_rev),
        "subsidy_dependency": safe_div(fin.get("revenue_subsidy"), total_rev),
        "cost_per_student": safe_div(total_exp, stu),
        "revenue_per_student": safe_div(total_rev, stu),
        "personnel_per_student": safe_div(fin.get("expense_personnel"), stu),
        "teaching_per_student": safe_div(fin.get("expense_teaching"), stu),
    }


def yoy_volatility(series: Sequence[float]) -> dict:
    """年度變動率與最大跳動幅度。"""
    vals = [v for v in series if v]
    if len(vals) < 2:
        return {"changes": [], "max_abs_change": 0.0, "score": 0.0}
    changes = []
    for i in range(1, len(vals)):
        prev, cur = vals[i - 1], vals[i]
        if prev:
            changes.append((cur - prev) / abs(prev))
    if not changes:
        return {"changes": [], "max_abs_change": 0.0, "score": 0.0}
    mx = max(abs(c) for c in changes)
    score = max(0.0, min(100.0, (mx - 0.15) / 0.65 * 100))
    return {"changes": [round(c, 4) for c in changes],
            "max_abs_change": round(mx, 4), "score": round(score, 2)}


def per_child_year_fee(fee: dict | None) -> float:
    """依公告收費明細推估單一幼生之年度應繳金額。

    月費類（學費）以 (月數 - 1) 計（多數園所寒暑假月份減收），
    學期制費用（材料費、其他代辦費）按年計，交通費以 40% 使用率折算。
    """
    if not fee:
        return 0.0
    months = fee.get("months") or 12
    m = max(1, months - 1)
    return ((fee.get("tuition") or 0) + (fee.get("misc_fee") or 0) / max(1, months) * 3) * m \
        + (fee.get("meal_fee") or 0) * m \
        + (fee.get("material_fee") or 0) + (fee.get("other_fee") or 0) \
        + (fee.get("transport_fee") or 0) * 0.4


def fee_cross_check(fee: dict | None, fin: dict | None, enrolled: int | None) -> dict:
    """收費明細 × 實際幼生數 vs 決算學雜費收入 交叉核對。

    落差為正（申報收入 < 推估收入）→ 可能收入未完整入帳；
    落差為負（申報收入 > 推估收入）→ 可能超收或有未公告收費項目。
    """
    if not fee or not fin or not enrolled:
        return {"available": False, "gap_ratio": None, "score": 0.0,
                "estimated": None, "reported": None, "direction": "資料不足"}
    estimated = per_child_year_fee(fee) * enrolled
    reported = fin.get("revenue_tuition") or 0
    if estimated <= 0:
        return {"available": False, "gap_ratio": None, "score": 0.0,
                "estimated": None, "reported": reported, "direction": "資料不足"}
    gap = (estimated - reported) / estimated
    score = max(0.0, min(100.0, (abs(gap) - 0.08) / 0.35 * 100))
    direction = "申報收入低於推估（疑收入未完整入帳）" if gap > 0.10 else \
        ("申報收入高於推估（疑超收或未公告收費）" if gap < -0.10 else "落差在合理範圍")
    return {"available": True, "gap_ratio": round(gap, 4), "score": round(score, 2),
            "estimated": int(estimated), "reported": int(reported),
            "direction": direction}


def peer_key(inst: dict) -> str:
    """同儕群組：設立類型 + 規模級距（跨縣市可比）。"""
    cap = inst.get("approved_capacity") or 0
    if cap < 60:
        size = "小型"
    elif cap < 120:
        size = "中型"
    elif cap < 200:
        size = "大型"
    else:
        size = "特大型"
    return f"{inst.get('org_type') or '未分類'}|{size}"


# 需雙向偵測（過高或過低皆異常）的比率
TWO_SIDED = {"personnel_ratio", "cost_per_student", "revenue_per_student",
             "personnel_per_student", "teaching_per_student"}
# 僅偏高為異常
HIGH_SIDED = {"admin_ratio", "surplus_ratio", "facility_ratio"}
# 僅偏低為異常
LOW_SIDED = {"teaching_ratio", "meal_ratio"}

RATIO_LABELS = {
    "personnel_ratio": "人事費率",
    "teaching_ratio": "教學費率",
    "meal_ratio": "餐點費率",
    "admin_ratio": "行政管理費率",
    "facility_ratio": "設備維護費率",
    "surplus_ratio": "結餘率",
    "subsidy_dependency": "補助依賴度",
    "cost_per_student": "每生單位成本",
    "revenue_per_student": "每生單位收入",
    "personnel_per_student": "每生人事投入",
    "teaching_per_student": "每生教學投入",
}

WATCH_RATIOS = list(TWO_SIDED | HIGH_SIDED | LOW_SIDED)


def peer_deviation(value: float | None, pool: Sequence[float], ratio_name: str) -> float:
    """回傳 0~100 的同儕偏離風險分數。"""
    if value is None:
        return 0.0
    z = stats.robust_z(value, pool)
    if ratio_name in HIGH_SIDED:
        eff = max(0.0, z)
    elif ratio_name in LOW_SIDED:
        eff = max(0.0, -z)
    else:
        eff = abs(z)
    return max(0.0, min(100.0, (eff - 1.0) / 3.0 * 100))
