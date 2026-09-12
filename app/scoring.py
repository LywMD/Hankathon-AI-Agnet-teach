"""風險評分：五構面規則分數 + 監督式模型分數混合，並產生可解釋說明。

設計原則
* 規則分數：法規與鑑識會計上有明確依據，可直接寫進稽查通知書。
* 模型分數：由歷史「事後被裁罰」結果反向學習權重，補捉規則沒有涵蓋的組合訊號。
* 兩者混合可調（設定頁的 model_blend），避免模型黑箱獨大，也避免規則僵化。
"""
from __future__ import annotations

from . import forensics
from .config import DIMENSIONS, band_label, eval_result_level

# 進入監督式模型的特徵（順序即報表顯示順序）
FEATURE_NAMES = [
    "pen_decay", "pen_count_4y", "pen_count_1y", "pen_severe", "pen_repeat",
    "pen_recency", "pen_fine_total_log", "pen_disp_sev",
    "dev_max", "dev_mean", "dev_personnel_ratio", "dev_admin_ratio",
    "dev_surplus_ratio", "dev_teaching_ratio", "dev_cost_per_student",
    "last2_score", "round_score", "first_digit_score",
    "yoy_expense_score", "yoy_personnel_score",
    "inst_age", "capacity",
    "eval_penalty", "eval_items_failed", "eval_fail_history",
    "senti_score", "senti_neg_ratio", "senti_burst", "senti_volume",
    "senti_child_topic",
    "anomaly_score",
]

FEATURE_LABELS = {
    "pen_decay": "裁罰時間衰減加權強度",
    "pen_count_4y": "近四年裁罰次數",
    "pen_count_1y": "近一年裁罰次數",
    "pen_severe": "重大違規次數",
    "pen_repeat": "同法條再犯次數",
    "pen_recency": "最近一次裁罰近期性",
    "pen_fine_total_log": "累計罰鍰金額（對數）",
    "pen_disp_sev": "最重處分嚴重度",
    "dev_max": "財務比率同儕偏離（最大）",
    "dev_mean": "財務比率同儕偏離（平均）",
    "dev_personnel_ratio": "人事費率偏離同儕",
    "dev_admin_ratio": "行政管理費率偏離同儕",
    "dev_surplus_ratio": "結餘率偏離同儕",
    "dev_teaching_ratio": "教學費率偏離同儕",
    "dev_cost_per_student": "每生單位成本偏離同儕",
    "last2_score": "末兩位數群聚異常",
    "round_score": "金額整數偏誤",
    "first_digit_score": "首位數分布偏離同儕",
    "yoy_expense_score": "支出年度跳動幅度",
    "yoy_personnel_score": "人事費年度跳動幅度",
    "inst_age": "機構成立年數",
    "capacity": "核定招收規模",
    "eval_penalty": "最近評鑑結果扣分",
    "eval_items_failed": "評鑑待改善項目數",
    "eval_fail_history": "歷次評鑑未通過次數",
    "senti_score": "輿情負面綜合分數",
    "senti_neg_ratio": "負面貼文占比",
    "senti_burst": "輿情爆量程度",
    "senti_volume": "社群聲量",
    "senti_child_topic": "兒少保護主題命中數",
    "anomaly_score": "多維異常偵測分數",
}


def _c(v, lo=0.0, hi=100.0):
    if v is None:
        return None
    return max(lo, min(hi, float(v)))


def _wavg(pairs: list[tuple[float | None, float]]) -> float:
    num = den = 0.0
    for v, w in pairs:
        if v is None:
            continue
        num += v * w
        den += w
    return round(num / den, 2) if den > 0 else 0.0


def dimension_scores(f: dict) -> dict[str, float]:
    """五大構面規則分數（0~100）。"""
    # ---------------- 法遵
    comp = _wavg([
        (_c((f.get("pen_decay") or 0) / 6.0 * 100), 0.28),
        (_c((f.get("pen_count_1y") or 0) * 35), 0.18),
        (_c((f.get("pen_severe") or 0) * 40), 0.16),
        (_c((f.get("pen_repeat") or 0) * 30), 0.12),
        (_c(10 ** (f.get("pen_fine_total_log") or 0) / 3000), 0.08),
        (_c((f.get("pen_recency") or 0) * 100), 0.11),
        (_c(((f.get("pen_disp_sev") or 1.0) - 1.0) / 2.0 * 100), 0.07),
    ])

    # ---------------- 財務（鑑識會計）
    fin = _wavg([
        (_c(f.get("dev_max")), 0.23),
        (_c(f.get("dev_mean")), 0.11),
        (_c(f.get("last2_score")), 0.20),
        (_c(f.get("round_score")), 0.12),
        (_c(f.get("yoy_expense_score")), 0.13),
        (_c(f.get("anomaly_score")), 0.15),
        (_c(f.get("first_digit_score")), 0.06),
    ])
    if not f.get("fin_available"):
        # 無決算資料：以「資料不足」處理，僅用可得訊號並標註
        fin = _wavg([
            (_c(f.get("anomaly_score")), 1.0),
        ])

    # ---------------- 評鑑
    ev = _wavg([
        (_c(f.get("eval_penalty")), 0.55),
        (_c((f.get("eval_items_failed") or 0) * 20), 0.25),
        (_c((f.get("eval_fail_history") or 0) * 45), 0.20),
    ])

    # ---------------- 輿情
    sen = _wavg([
        (_c(f.get("senti_score")), 0.72),
        (_c(f.get("senti_burst")), 0.16),
        (_c((f.get("senti_child_topic") or 0) * 35), 0.12),
    ])

    return {"compliance": comp, "financial": fin,
            "evaluation": ev, "sentiment": sen}


def rule_score(dims: dict[str, float], weights: dict[str, float],
              available: dict[str, bool] | None = None) -> float:
    """五構面加權平均。

    available 標示各構面「本次資料集是否有該類型公開資料」（例如未提供裁罰
    紀錄時 compliance 為 False）。缺資料的構面會整個排除在加權平均之外並
    重新正規化其餘權重，而非以 0 分計入——後者會系統性壓低所有機構的分數，
    讓「真正有訊號的構面」被稀釋到無法反映風險高低（例如只有財務資料時，
    分數會被裁罰／評鑑／輿情三個構面的 0 分拖到只剩四分之一左右），並非
    該機構本身風險較低，只是這些公開資料本系統未取得。
    """
    if available:
        weights = {k: w for k, w in weights.items() if available.get(k, True)}
    tot = sum(weights.values()) or 1.0
    return round(sum(dims.get(k, 0.0) * w for k, w in weights.items()) / tot, 2)


def final_score(rule: float, model_score: float | None, blend: float) -> float:
    if model_score is None:
        return round(rule, 2)
    b = max(0.0, min(1.0, blend))
    return round(rule * (1 - b) + model_score * b, 2)


# ---------------------------------------------------------------- 說明
def explain(rec: dict, dims: dict[str, float], weights: dict[str, float],
            model_contribs: list[dict] | None = None,
            available: dict[str, bool] | None = None) -> dict:
    """產生風險因子貢獻與自然語言預警說明。"""
    f = rec["features"]
    detail = rec["detail"]
    active_weights = ({k: w for k, w in weights.items() if (available or {}).get(k, True)}
                      if available else dict(weights))
    tot = sum(active_weights.values()) or 1.0
    dim_rows = []
    for key, label, desc in DIMENSIONS:
        is_available = (available or {}).get(key, True)
        w = active_weights.get(key, 0.0)
        dim_rows.append({
            "key": key, "label": label, "desc": desc,
            "score": dims.get(key, 0.0),
            "available": is_available,
            "weight": round(w / tot, 4) if is_available else 0.0,
            "points": round(dims.get(key, 0.0) * w / tot, 2) if is_available else 0.0,
        })
    dim_rows.sort(key=lambda r: -r["points"])

    reasons: list[dict] = []

    def add(level: str, title: str, text: str, evidence: str = ""):
        reasons.append({"level": level, "title": title, "text": text,
                        "evidence": evidence})

    # 法遵
    if (f.get("pen_count_1y") or 0) >= 1:
        pens = detail.get("penalties") or []
        last = pens[0] if pens else {}
        add("high", "近一年內有裁罰紀錄",
            f"近一年共 {int(f['pen_count_1y'])} 件裁罰，最近一次為 {last.get('penalty_date','-')}"
            f"（{last.get('category','-')}）。",
            f"{last.get('law_article','')}／{last.get('disposition','')}")
    if (f.get("pen_repeat") or 0) >= 1:
        add("high", "同一法條重複違規",
            f"同一違規事由重複出現 {int(f['pen_repeat'])+1} 次，顯示改善措施未落實。")
    if (f.get("pen_severe") or 0) >= 1:
        add("critical", "涉及重大違規類別",
            f"曾有 {int(f['pen_severe'])} 件屬兒少保護或不當管理等重大類別之裁罰。")

    # 財務
    pd = detail.get("peer_deviation") or []
    for row in pd[:2]:
        if row["score"] >= 35:
            direction = "高於" if (row["z"] or 0) > 0 else "低於"
            add("medium" if row["score"] < 60 else "high",
                f"{row['label']}顯著偏離同儕",
                f"{row['label']}為 {_fmt_ratio(row['ratio'], row['value'])}，"
                f"{direction}同類型同規模機構中位數 {_fmt_ratio(row['ratio'], row['peer_median'])}"
                f"（穩健 z = {row['z']}）。",
                f"同儕樣本數 {row['peer_n']}")
    l2 = detail.get("last_two") or {}
    if l2.get("sufficient") and l2.get("score", 0) >= 30:
        tops = "、".join(f"{t['ending']}（{t['share']*100:.1f}%）"
                        for t in (l2.get("top_endings") or [])[:3])
        add("medium" if l2["score"] < 60 else "high", "決算金額末兩位數出現異常群聚",
            f"末兩位數均勻性檢定 卡方 = {l2['chi2']}、p = {l2['p_value']}（{l2['level']}）；"
            f"最集中的尾數為 {tops}，期望值各為 1.0%。真實支出金額的末位數應接近均勻，"
            "群聚通常代表人工估列、套用固定金額或事後填製。",
            f"樣本數 {l2['n']} 筆決算科目分期執行數")
    rb = detail.get("round_bias") or {}
    if rb.get("sufficient") and rb.get("score", 0) >= 30:
        add("medium", "金額整數偏誤偏高",
            f"以千元整數結尾之科目占 {rb['ratio_1000']*100:.1f}%（{rb['level']}），"
            "常見於估列或事後填製。")
    fd = detail.get("digit_conformity") or {}
    if fd.get("sufficient") and fd.get("score", 0) >= 40:
        add("low", "首位數分布偏離同儕（參考指標）",
            f"首位數分布 MAD 為全體機構中位數的 {fd.get('adjusted_ratio')} 倍"
            f"（{fd['level']}）。因單一機構科目金額集中於少數量級，此指標僅供輔助判讀，"
            "須與末兩位數檢定併同研判。")
    yoy = (detail.get("yoy") or {}).get("expense") or {}
    if yoy.get("score", 0) >= 40:
        add("medium", "年度支出出現異常跳動",
            f"支出年增減最大幅度達 {yoy['max_abs_change']*100:.1f}%，須確認是否有一次性或不實列帳。")

    # 評鑑
    # 以 features.eval_result_level 判讀描述性結果字串，不可用
    # `result != "通過"` 之類的精確比對——實際資料是「基礎評鑑－全數指標通過」
    # 這類字句，精確比對會把全數通過者也標成未通過。
    evs = detail.get("evaluations") or []
    if evs:
        e = evs[-1]
        level = eval_result_level(e.get("result"))
        if level == "fail":
            n_failed = e.get("items_failed")
            add("high", "最近一次基礎評鑑未全數通過",
                f"{e.get('eval_year')} 學年度評鑑結果為「{e.get('result')}」"
                + (f"，待改善項目 {n_failed} 項。" if n_failed else "。"))
        elif "追蹤評鑑" in str(e.get("result") or ""):
            # 追蹤評鑑本身即代表前次未全數通過，即使追蹤結果通過仍值得留意
            add("low", "曾接受追蹤評鑑",
                f"{e.get('eval_year')} 學年度為「{e.get('result')}」，"
                "顯示前次基礎評鑑有指標未通過，已完成追蹤複評。")

    # 輿情
    sen = detail.get("sentiment") or {}
    if sen.get("score", 0) >= 40:
        tp = sen.get("topics") or []
        tname = tp[0]["topic"] if tp else "負面反映"
        add("high" if sen["score"] >= 60 else "medium", "社群輿情負面聲量偏高",
            f"近期共 {sen['n_negative']} 則負面貼文（占 {sen['neg_ratio']*100:.0f}%），"
            f"主要集中於「{tname}」。")
    if (sen.get("burst") or {}).get("burst"):
        b = sen["burst"]
        add("critical", "輿情出現爆量預警",
            f"近 30 日負面聲量 {b['recent']} 則，顯著高於歷史基線期望值 {b['expected']} 則"
            f"（Poisson p = {b['p_value']}）。")

    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    reasons.sort(key=lambda r: order.get(r["level"], 9))

    return {
        "dimensions": dim_rows,
        "reasons": reasons,
        "model_contributions": [
            {**c, "label": FEATURE_LABELS.get(c["feature"], c["feature"])}
            for c in (model_contribs or [])[:10]
        ],
    }


def _fmt_ratio(name: str, v) -> str:
    if v is None:
        return "-"
    if name in ("cost_per_student", "revenue_per_student", "personnel_per_student",
                "teaching_per_student"):
        return f"{v:,.0f} 元"
    return f"{v*100:.1f}%"


def action_suggestion(band: str, dims: dict[str, float], reasons: list[dict]) -> dict:
    """依風險等級與構面組成給出稽查手法建議。"""
    band_name = band_label(band)
    top = max(dims, key=dims.get) if dims else "compliance"
    playbook = {
        "compliance": ["調閱歷次裁處卷宗與改善計畫執行情形", "現場複查前次缺失項目",
                       "訪談教保服務人員了解通報流程落實度"],
        "financial": ["調閱決算原始憑證與傳票，抽核異常科目",
                      "確認人事費與投保、薪資轉帳紀錄一致性"],
        "evaluation": ["追蹤基礎評鑑待改善項目之改善證據", "安排輔導訪視並限期複評"],
        "sentiment": ["就社群反映事項要求書面說明並保全監視紀錄",
                      "必要時啟動不定期突擊稽查", "同步聯繫社政單位確認有無通報案件"],
    }
    urgency = {"critical": "7 日內", "high": "14 日內", "medium": "30 日內",
               "low": "常規排程", "minimal": "常規排程"}.get(band, "常規排程")
    mode = {"critical": "立即專案稽查（含社政協同）", "high": "優先實地稽查",
            "medium": "書面查核加抽訪", "low": "常態抽查",
            "minimal": "免額外查核"}.get(band, "常態抽查")
    return {
        "band": band, "band_label": band_name, "urgency": urgency, "mode": mode,
        "focus": DIMENSIONS[[d[0] for d in DIMENSIONS].index(top)][1] if top else "",
        "actions": playbook.get(top, [])[:3],
        "critical_flags": [r["title"] for r in reasons if r["level"] == "critical"],
    }
