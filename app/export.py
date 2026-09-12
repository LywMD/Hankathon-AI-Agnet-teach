"""報表匯出：風險清冊、稽查排程與機構預警單。

優先產生 Excel（xlsxwriter），若環境缺少該套件則自動退回 CSV，
確保在任何評審端環境都能取得檔案。
"""
from __future__ import annotations

import csv
import io

from .config import DIMENSIONS

try:  # pragma: no cover
    import xlsxwriter  # type: ignore
    HAS_XLSX = True
except Exception:  # noqa: BLE001
    HAS_XLSX = False

RISK_COLUMNS = [
    ("rank", "風險排名"), ("inst_id", "機構代碼"), ("name", "機構名稱"),
    ("city", "縣市"), ("district", "行政區"), ("org_type", "設立類型"),
    ("score", "綜合風險分數"), ("band_label", "風險等級"),
    ("rule_score", "規則分數"), ("model_score", "模型分數"),
    ("anomaly_score", "異常偵測分數"),
    ("enrolled", "實際招收"), ("capacity", "核定人數"),
    ("pen_count_4y", "近四年裁罰"), ("pen_count_1y", "近一年裁罰"),
    ("senti_negative", "負面輿情則數"), ("urgency", "建議處理時效"),
    ("mode", "建議稽查模式"),
]


def risk_workbook(rows: list[dict], schedule: dict, summary: dict,
                  metrics: dict) -> tuple[bytes, str, str]:
    """回傳 (檔案內容, 檔名, MIME)。"""
    if not HAS_XLSX:
        return _risk_csv(rows)
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    fmt_title = wb.add_format({"bold": True, "font_size": 14})
    fmt_head = wb.add_format({"bold": True, "bg_color": "#1f3a5f", "font_color": "white",
                              "border": 1, "align": "center", "valign": "vcenter",
                              "text_wrap": True})
    fmt_cell = wb.add_format({"border": 1})
    fmt_num = wb.add_format({"border": 1, "num_format": "0.00"})
    fmt_int = wb.add_format({"border": 1, "num_format": "#,##0"})
    band_fmt = {
        "critical": wb.add_format({"border": 1, "bg_color": "#ffd7d3"}),
        "high": wb.add_format({"border": 1, "bg_color": "#ffe8cc"}),
        "medium": wb.add_format({"border": 1, "bg_color": "#fff3bf"}),
        "low": wb.add_format({"border": 1, "bg_color": "#e6fcf5"}),
        "minimal": wb.add_format({"border": 1}),
    }

    # ---- 風險清冊
    ws = wb.add_worksheet("風險清冊")
    ws.write(0, 0, "教保機構風險預警清冊", fmt_title)
    ws.write(1, 0, f"資料截止日 {summary.get('as_of','')}｜產出時間 {summary.get('built_at','')}")
    head_row = 3
    for c, (_k, label) in enumerate(RISK_COLUMNS):
        ws.write(head_row, c, label, fmt_head)
    for c, (key, _lbl) in enumerate(DIMENSIONS_COLS := [(d[0], d[1]) for d in DIMENSIONS]):
        ws.write(head_row, len(RISK_COLUMNS) + c, _lbl, fmt_head)
    ws.write(head_row, len(RISK_COLUMNS) + len(DIMENSIONS), "主要風險因子", fmt_head)
    for i, r in enumerate(rows):
        rr = head_row + 1 + i
        bf = band_fmt.get(r.get("band"), fmt_cell)
        for c, (key, _lbl) in enumerate(RISK_COLUMNS):
            v = r.get(key)
            if isinstance(v, float):
                ws.write_number(rr, c, v, fmt_num if c == 6 else bf)
            elif isinstance(v, int):
                ws.write_number(rr, c, v, bf)
            else:
                ws.write(rr, c, "" if v is None else str(v), bf)
        for c, (key, _lbl) in enumerate(DIMENSIONS_COLS):
            ws.write_number(rr, len(RISK_COLUMNS) + c,
                            float((r.get("dims") or {}).get(key) or 0), fmt_num)
        ws.write(rr, len(RISK_COLUMNS) + len(DIMENSIONS),
                 "；".join(r.get("top_reasons") or []), fmt_cell)
    ws.freeze_panes(head_row + 1, 3)
    ws.set_column(0, 0, 9)
    ws.set_column(1, 1, 11)
    ws.set_column(2, 2, 32)
    ws.set_column(3, 5, 10)
    ws.set_column(6, len(RISK_COLUMNS) + len(DIMENSIONS), 12)
    ws.set_column(len(RISK_COLUMNS) + len(DIMENSIONS),
                  len(RISK_COLUMNS) + len(DIMENSIONS), 60)
    ws.autofilter(head_row, 0, head_row + len(rows), len(RISK_COLUMNS) - 1)

    # ---- 稽查排程
    ws2 = wb.add_worksheet("稽查排程建議")
    ws2.write(0, 0, "稽查資源配置建議", fmt_title)
    eff = schedule.get("efficiency", {})
    info = [
        ("可投入訪查量（案）", eff.get("capacity")),
        ("覆蓋率（%）", eff.get("coverage_pct")),
        ("名單命中率 precision", eff.get("precision")),
        ("隨機抽查命中率", eff.get("base_rate")),
        ("命中效率倍數", eff.get("multiplier")),
        ("預估投入工時", eff.get("estimated_hours")),
        ("全面稽查工時", eff.get("full_audit_hours")),
        ("節省工時（%）", eff.get("hours_saved_pct")),
    ]
    for i, (k, v) in enumerate(info):
        ws2.write(2 + i, 0, k, fmt_cell)
        ws2.write(2 + i, 1, "" if v is None else v, fmt_cell)
    r0 = 4 + len(info)
    heads = ["批次", "處理時效", "機構代碼", "機構名稱", "縣市", "行政區", "設立類型",
             "風險分數", "風險等級", "建議稽查模式", "主要風險因子"]
    for c, h in enumerate(heads):
        ws2.write(r0, c, h, fmt_head)
    rr = r0 + 1
    for w in schedule.get("waves", []):
        for inst in w.get("institutions", []):
            vals = [w["name"], w["due"], inst["inst_id"], inst["name"], inst["city"],
                    inst["district"], inst["org_type"], inst["score"],
                    inst["band_label"], inst["mode"],
                    "；".join(inst.get("top_reasons") or [])]
            for c, v in enumerate(vals):
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    ws2.write_number(rr, c, v, fmt_num)
                else:
                    ws2.write(rr, c, str(v), fmt_cell)
            rr += 1
    ws2.set_column(0, 1, 22)
    ws2.set_column(2, 2, 11)
    ws2.set_column(3, 3, 32)
    ws2.set_column(4, 9, 12)
    ws2.set_column(10, 10, 60)

    # ---- 模型驗證
    ws3 = wb.add_worksheet("模型驗證")
    ws3.write(0, 0, "模型驗證與有效性指標", fmt_title)
    hyb = metrics.get("hybrid", {})
    ws3.write(2, 0, "訓練資料截止", fmt_cell)
    ws3.write(2, 1, metrics.get("train_as_of", ""), fmt_cell)
    ws3.write(3, 0, "標籤觀察期", fmt_cell)
    ws3.write(3, 1, metrics.get("label_window", ""), fmt_cell)
    ws3.write(4, 0, "AUC（混合分數）", fmt_cell)
    ws3.write(4, 1, hyb.get("auc", 0), fmt_num)
    ws3.write(5, 0, "AUC（僅模型）", fmt_cell)
    ws3.write(5, 1, (metrics.get("model_only") or {}).get("auc", 0), fmt_num)
    ws3.write(6, 0, "AUC（僅規則）", fmt_cell)
    ws3.write(6, 1, (metrics.get("rule_only") or {}).get("auc", 0), fmt_num)
    ws3.write(7, 0, "平均預警提前天數", fmt_cell)
    ws3.write(7, 1, (metrics.get("lead_time") or {}).get("mean_days", 0), fmt_num)
    ws3.write(9, 0, "K", fmt_head)
    ws3.write(9, 1, "Precision@K", fmt_head)
    ws3.write(9, 2, "Recall@K", fmt_head)
    ws3.write(9, 3, "Lift", fmt_head)
    for i, item in enumerate(hyb.get("at_k", [])):
        ws3.write_number(10 + i, 0, item["k"], fmt_int)
        ws3.write_number(10 + i, 1, item["precision"], fmt_num)
        ws3.write_number(10 + i, 2, item["recall"], fmt_num)
        ws3.write_number(10 + i, 3, item["lift"], fmt_num)
    r1 = 12 + len(hyb.get("at_k", []))
    ws3.write(r1, 0, "特徵", fmt_head)
    ws3.write(r1, 1, "係數", fmt_head)
    ws3.write(r1, 2, "勝算比", fmt_head)
    for i, item in enumerate(metrics.get("importance", [])[:25]):
        ws3.write(r1 + 1 + i, 0, item.get("label", item["feature"]), fmt_cell)
        ws3.write_number(r1 + 1 + i, 1, item["coef"], fmt_num)
        ws3.write_number(r1 + 1 + i, 2, item["odds_ratio"], fmt_num)
    ws3.set_column(0, 0, 30)
    ws3.set_column(1, 3, 14)

    wb.close()
    return buf.getvalue(), "教保機構風險預警報表.xlsx", \
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _risk_csv(rows: list[dict]) -> tuple[bytes, str, str]:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([lbl for _k, lbl in RISK_COLUMNS] + [d[1] for d in DIMENSIONS] + ["主要風險因子"])
    for r in rows:
        w.writerow([r.get(k) for k, _ in RISK_COLUMNS]
                   + [round(float((r.get("dims") or {}).get(d[0]) or 0), 2) for d in DIMENSIONS]
                   + ["；".join(r.get("top_reasons") or [])])
    return ("\ufeff" + out.getvalue()).encode("utf-8"), "教保機構風險預警清冊.csv", "text/csv"


def institution_report(detail: dict) -> tuple[bytes, str, str]:
    """單一機構預警單（純文字，方便直接貼入公文或列印）。"""
    inst = detail["inst"]
    row = detail.get("row") or {}
    exp = detail["explain"]
    act = detail["action"]
    lines = []
    push = lines.append
    push("=" * 64)
    push("教保機構風險預警單")
    push("=" * 64)
    push(f"機構名稱：{inst.get('name')}（{inst.get('inst_id')}）")
    push(f"設立類型：{inst.get('org_type')}　所在地：{inst.get('city')}{inst.get('district')}")
    push(f"核定人數：{inst.get('approved_capacity')}　實際招收：{inst.get('enrolled')}"
         f"　教保人員：{inst.get('teacher_count')}")
    push("")
    push(f"綜合風險分數：{row.get('score')}（{row.get('band_label')}）"
         f"　全體排名：第 {row.get('rank')} 名／{row.get('percentile')} 百分位")
    push(f"建議處理：{act.get('mode')}（{act.get('urgency')}）")
    push("")
    push("[ 構面分數 ]")
    for d in exp["dimensions"]:
        push(f"  {d['label']}：{d['score']:.1f}（權重 {d['weight']*100:.0f}%，貢獻 {d['points']:.1f} 分）")
    push("")
    push("[ 風險因子與證據 ]")
    for i, r in enumerate(exp["reasons"], 1):
        push(f"  {i}. [{r['level'].upper()}] {r['title']}")
        push(f"     {r['text']}")
        if r.get("evidence"):
            push(f"     證據：{r['evidence']}")
    push("")
    push("[ 建議查核重點 ]")
    for a in act.get("actions", []):
        push(f"  - {a}")
    push("")
    push("[ 模型貢獻度前 8 ]")
    for c in exp.get("model_contributions", [])[:8]:
        push(f"  {c['label']}：標準化值 {c['z']}，係數 {c['coef']}，貢獻 {c['contribution']}")
    push("")
    push("※ 本預警單由 AI 風險模型與鑑識會計指標自動產生，僅供稽查排程參考，")
    push("　 實際違規事實仍應以現場查核與正式行政程序認定。")
    text = "\n".join(lines)
    fname = f"預警單_{inst.get('name')}.txt"
    return ("\ufeff" + text).encode("utf-8"), fname, "text/plain; charset=utf-8"
