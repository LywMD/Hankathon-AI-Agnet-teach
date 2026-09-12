"""特徵工程：把分散的公開資料整合成每所機構的風險特徵向量。

所有特徵都以 as_of 日期為界，只使用該日期之前可取得的資訊，
確保訓練／驗證時不會發生資料洩漏（look-ahead bias）。
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date

from . import config, forensics, nlp, stats  # noqa: F401
from .config import PENALTY_CATEGORIES, eval_result_level

DISPOSITION_SEVERITY = {
    "停止招生": 3.0, "減招": 2.6, "公布姓名及名稱": 2.2,
    "罰鍰並限期改善": 1.8, "罰鍰": 1.4, "限期改善": 1.1,
}
# 評鑑結果 → 風險扣分。unknown 不給預設扣分，改回傳 None 由上層以缺值處理，
# 避免像先前一樣把「無法判讀」當成「有缺失」。
EVAL_LEVEL_PENALTY = {"pass": 0.0, "fail": 100.0}


def _pdate(s) -> date | None:
    try:
        return date.fromisoformat(str(s))
    except (ValueError, TypeError):
        return None


def _index(rows, key="inst_id"):
    out = defaultdict(list)
    for r in rows:
        out[str(r.get(key))].append(r)
    return out


def build(bundle, as_of: date) -> list[dict]:
    """回傳每所機構的特徵與鑑識明細（list of dict）。"""
    insts = bundle["institutions"]
    pen_by = _index(bundle["penalties"])
    fin_by = _index(bundle["financials"])
    led_by = _index(bundle["ledger"])
    ev_by = _index(bundle["evaluations"])
    post_by = _index(bundle["posts"])

    fiscal_cut = as_of.year - 1 if as_of.month <= 6 else as_of.year

    records: list[dict] = []
    for inst in insts:
        iid = str(inst.get("inst_id"))
        f: dict[str, float] = {}
        detail: dict = {}

        # ---------------------------------------------- 法遵：裁罰
        pens = [p for p in pen_by.get(iid, []) if (_pdate(p.get("penalty_date")) or date(1900, 1, 1)) <= as_of]
        pens.sort(key=lambda p: p.get("penalty_date") or "")
        n_all = len(pens)
        n_1y = n_2y = n_4y = 0
        decay = 0.0
        fine_total = fine_max = 0.0
        severe = 0
        art_cnt: dict[str, int] = defaultdict(int)
        cat_cnt: dict[str, int] = defaultdict(int)
        disp_sev_max = 0.0
        last_days = None
        for p in pens:
            d = _pdate(p.get("penalty_date"))
            if not d:
                continue
            age = (as_of - d).days
            if age <= 365:
                n_1y += 1
            if age <= 730:
                n_2y += 1
            if age <= 1460:
                n_4y += 1
            cat = p.get("category") or "行政管理缺失"
            sev = PENALTY_CATEGORIES.get(cat, 1.2)
            decay += sev * math.exp(-age / 540)
            fine = float(p.get("fine_amount") or 0)
            fine_total += fine
            fine_max = max(fine_max, fine)
            if sev >= 2.5:
                severe += 1
            art_cnt[str(p.get("law_article") or cat)] += 1
            cat_cnt[cat] += 1
            disp_sev_max = max(disp_sev_max,
                               DISPOSITION_SEVERITY.get(str(p.get("disposition")), 1.0))
            last_days = age if last_days is None else min(last_days, age)

        repeat_max = max(art_cnt.values()) if art_cnt else 0
        f["pen_count_4y"] = n_4y
        f["pen_count_2y"] = n_2y
        f["pen_count_1y"] = n_1y
        f["pen_decay"] = round(decay, 4)
        f["pen_fine_total_log"] = math.log10(1 + fine_total)
        f["pen_fine_max_log"] = math.log10(1 + fine_max)
        f["pen_severe"] = severe
        f["pen_repeat"] = max(0, repeat_max - 1)
        f["pen_disp_sev"] = disp_sev_max
        f["pen_recency"] = 0.0 if last_days is None else max(0.0, 1 - min(last_days, 1460) / 1460)
        detail["penalties"] = [
            {k: p.get(k) for k in ("penalty_date", "published_date", "category",
                                   "law_article", "fine_amount", "disposition", "description")}
            for p in reversed(pens)
        ][:30]
        detail["penalty_categories"] = sorted(
            ({"category": c, "count": n} for c, n in cat_cnt.items()),
            key=lambda x: -x["count"])

        # ---------------------------------------------- 財務：決算與收費
        fins = [x for x in fin_by.get(iid, []) if (x.get("fiscal_year") or 0) <= fiscal_cut]
        fins.sort(key=lambda x: x.get("fiscal_year") or 0)
        ratio_series = [forensics.ratios(x) for x in fins]
        detail["ratio_series"] = ratio_series
        latest = ratio_series[-1] if ratio_series else {}
        for rn in forensics.WATCH_RATIOS + ["subsidy_dependency"]:
            f[f"r_{rn}"] = latest.get(rn) if latest.get(rn) is not None else None

        led = [x for x in led_by.get(iid, []) if (x.get("fiscal_year") or 0) <= fiscal_cut]
        amounts = [float(x.get("amount") or 0) for x in led if x.get("amount")]
        if not amounts and fins:
            amounts = [float(v) for x in fins for k, v in x.items()
                       if k.startswith(("revenue_", "expense_")) and v]
        # 首位數（班佛）僅作參考顯示：單一機構科目金額集中於少數量級且
        # 同一科目跨期重複，獨立觀察數遠低於筆數，檢定力不足。
        detail["benford"] = forensics.benford(amounts)
        detail["digit_counts"] = forensics.digit_counts(amounts)
        # 末兩位數均勻性：機構層級主要的數字鑑識檢定
        last2 = forensics.last_two_digits(amounts)
        detail["last_two"] = last2
        f["last2_score"] = last2["score"]
        rnd = forensics.round_number_bias(amounts)
        detail["round_bias"] = rnd
        f["round_score"] = rnd["score"]

        exp_series = [x.get("total_expense") or 0 for x in fins]
        per_series = [x.get("expense_personnel") or 0 for x in fins]
        rev_series = [x.get("total_revenue") or 0 for x in fins]
        yoy_exp = forensics.yoy_volatility(exp_series)
        yoy_per = forensics.yoy_volatility(per_series)
        yoy_rev = forensics.yoy_volatility(rev_series)
        detail["yoy"] = {"expense": yoy_exp, "personnel": yoy_per, "revenue": yoy_rev}
        f["yoy_expense_score"] = yoy_exp["score"]
        f["yoy_personnel_score"] = yoy_per["score"]
        f["yoy_revenue_score"] = yoy_rev["score"]

        f["fin_available"] = 1.0 if fins else 0.0

        # ---------------------------------------------- 規模／年齡（一般特徵）
        enrolled = inst.get("enrolled") or 0
        cap = inst.get("approved_capacity") or 0
        teachers = inst.get("teacher_count") or 0
        f["capacity"] = cap
        f["inst_age"] = max(0, as_of.year - (inst.get("found_year") or as_of.year))

        # ---------------------------------------------- 評鑑
        evs = [e for e in ev_by.get(iid, []) if (e.get("eval_year") or 0) <= as_of.year]
        evs.sort(key=lambda e: e.get("eval_year") or 0)
        detail["evaluations"] = evs
        if evs:
            last_ev = evs[-1]
            level = eval_result_level(last_ev.get("result"))
            f["eval_penalty"] = EVAL_LEVEL_PENALTY.get(level)
            f["eval_items_failed"] = float(last_ev.get("items_failed") or 0)
            f["eval_followup"] = float(last_ev.get("followup_required") or 0)
            f["eval_score"] = float(last_ev.get("score") or 0) or None
            # 歷次未全數通過次數：只計可明確判為 fail 者，
            # 判讀不出來的紀錄不算缺失
            f["eval_fail_history"] = float(
                sum(1 for e in evs if eval_result_level(e.get("result")) == "fail"))
        else:
            f["eval_penalty"] = None
            f["eval_items_failed"] = None
            f["eval_followup"] = None
            f["eval_score"] = None
            f["eval_fail_history"] = None

        # ---------------------------------------------- 輿情
        posts = [p for p in post_by.get(iid, [])
                 if (_pdate(p.get("post_date")) or date(1900, 1, 1)) <= as_of]
        senti = nlp.summarize_institution(posts, as_of)
        detail["sentiment"] = senti
        f["senti_score"] = senti["score"]
        f["senti_neg_ratio"] = senti["neg_ratio"]
        f["senti_weighted"] = senti["weighted_negative"]
        f["senti_burst"] = senti["burst"]["score"]
        f["senti_volume"] = float(senti["n_posts"])
        f["senti_child_topic"] = float(sum(t["count"] for t in senti["topics"]
                                           if t["topic"] == "兒少保護"))

        records.append({
            "inst": inst,
            "features": f,
            "detail": detail,
            "peer_key": forensics.peer_key(inst),
        })

    _attach_peer_deviation(records)
    _attach_digit_conformity(records)
    return records


def _attach_digit_conformity(records: list[dict]) -> None:
    """以同儕實證首位數分布為基準，檢定各機構決算科目金額是否被人工調整。

    採留一法（leave-one-out）建立期望分布，避免受檢機構自身資料影響基準。
    """
    by_group: dict[str, list[int]] = {}
    for rec in records:
        g = str(rec["inst"].get("org_type") or "全體")
        counts = rec["detail"].get("digit_counts") or [0] * 9
        agg = by_group.setdefault(g, [0] * 9)
        for i in range(9):
            agg[i] += counts[i]
    overall = [0] * 9
    for agg in by_group.values():
        for i in range(9):
            overall[i] += agg[i]

    for rec in records:
        counts = rec["detail"].get("digit_counts") or [0] * 9
        g = str(rec["inst"].get("org_type") or "全體")
        pool = by_group.get(g, overall)
        # 同類型樣本不足時退回全體
        exp_counts = [max(0, pool[i] - counts[i]) for i in range(9)]
        if sum(exp_counts) < 2000:
            exp_counts = [max(0, overall[i] - counts[i]) for i in range(9)]
        total = sum(exp_counts)
        if total <= 0:
            exp_pct = list(forensics.BENFORD_EXPECTED)
            basis = "班佛定律理論值"
        else:
            exp_pct = [c / total for c in exp_counts]
            basis = f"同儕實證分布（{g}，{total:,} 筆）"
        conf = forensics.digit_conformity(counts, exp_pct, basis=basis)
        rec["detail"]["digit_conformity"] = conf

    # 經驗校正：單一機構的科目金額並非獨立觀察（同一科目跨期重複），
    # 實際 MAD 普遍高於理論抽樣雜訊。故再以「全體機構 MAD 中位數」為基準
    # 重新標準化，使正常機構落在 1.0 附近，只有真正的離群者才被標記。
    mads = [rec["detail"]["digit_conformity"]["mad"] for rec in records
            if rec["detail"]["digit_conformity"].get("sufficient")]
    ref = stats.median(mads) if mads else 0.0
    for rec in records:
        conf = rec["detail"]["digit_conformity"]
        if not conf.get("sufficient") or ref <= 0:
            rec["features"]["first_digit_score"] = 0.0
            continue
        adj = conf["mad"] / ref
        conf["population_median_mad"] = round(ref, 5)
        conf["adjusted_ratio"] = round(adj, 3)
        conf["level"] = ("符合" if adj < 1.25 else
                         "輕微偏離" if adj < 1.6 else
                         "明顯偏離" if adj < 2.0 else "高度偏離")
        conf["score"] = round(max(0.0, min(100.0, (adj - 1.2) / 1.0 * 100)), 2)
        conf["basis"] = conf.get("basis", "") + "＋全體 MAD 中位數校正"
        rec["features"]["first_digit_score"] = conf["score"]


def _attach_peer_deviation(records: list[dict]) -> None:
    """計算同儕群組（設立類型 × 規模）內的穩健標準化偏離。"""
    pools: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for rec in records:
        pk = rec["peer_key"]
        for rn in forensics.WATCH_RATIOS + ["subsidy_dependency"]:
            v = rec["features"].get(f"r_{rn}")
            if v is not None:
                pools[pk][rn].append(float(v))
    global_pool: dict[str, list[float]] = defaultdict(list)
    for pk, d in pools.items():
        for rn, vs in d.items():
            global_pool[rn].extend(vs)

    for rec in records:
        pk = rec["peer_key"]
        devs: dict[str, float] = {}
        zs: dict[str, float] = {}
        for rn in forensics.WATCH_RATIOS:
            pool = pools[pk][rn] if len(pools[pk][rn]) >= 8 else global_pool[rn]
            v = rec["features"].get(f"r_{rn}")
            devs[rn] = forensics.peer_deviation(v, pool, rn)
            zs[rn] = round(stats.robust_z(v, pool), 3) if v is not None else 0.0
            rec["features"][f"dev_{rn}"] = devs[rn]
        rec["detail"]["peer_deviation"] = [
            {"ratio": rn, "label": forensics.RATIO_LABELS.get(rn, rn),
             "value": rec["features"].get(f"r_{rn}"),
             "z": zs[rn], "score": devs[rn],
             "peer_median": round(stats.median(pools[pk][rn] or global_pool[rn]), 4),
             "peer_n": len(pools[pk][rn] or global_pool[rn])}
            for rn in sorted(devs, key=lambda x: -devs[x])
        ]
        rec["features"]["dev_max"] = max(devs.values()) if devs else 0.0
        rec["features"]["dev_mean"] = (sum(devs.values()) / len(devs)) if devs else 0.0


# ---------------------------------------------------------------- 標籤
def labels_after(bundle, start: date, days: int = 365) -> dict[str, int]:
    """標籤：start 之後 days 天內是否出現裁罰紀錄（1 = 有）。"""
    from datetime import timedelta
    end = start + timedelta(days=days)
    hit: dict[str, int] = {}
    for inst in bundle["institutions"]:
        hit[str(inst.get("inst_id"))] = 0
    for p in bundle["penalties"]:
        d = _pdate(p.get("penalty_date"))
        if d and start < d <= end:
            hit[str(p.get("inst_id"))] = 1
    return hit


def severe_labels_after(bundle, start: date, days: int = 365) -> dict[str, int]:
    """重大違規標籤：僅計嚴重度 >= 2.0 之裁罰。"""
    from datetime import timedelta
    end = start + timedelta(days=days)
    hit = {str(i.get("inst_id")): 0 for i in bundle["institutions"]}
    for p in bundle["penalties"]:
        d = _pdate(p.get("penalty_date"))
        sev = PENALTY_CATEGORIES.get(p.get("category") or "", 1.0)
        if d and start < d <= end and sev >= 2.0:
            hit[str(p.get("inst_id"))] = 1
    return hit
