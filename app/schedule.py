"""稽查資源配置建議：把風險分數轉換為可執行的排程與人力規劃。

三個核心設計：
1. 分批（wave）：依風險級別給不同的處理時效與稽查模式。
2. 地理併批：同一行政區的案件合併出勤，降低交通與人力成本。
3. 效益量化：以「相同訪查次數下命中違規機構的數量」對比隨機／全面稽查。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

WAVE_SPEC = [
    ("critical", "第一批 · 立即專案稽查", 7, 8.0, "含社政協同，須錄音錄影並當場開立紀錄"),
    ("high", "第二批 · 優先實地稽查", 14, 6.0, "實地查核財務憑證與班級編制"),
    ("medium", "第三批 · 書面查核加抽訪", 30, 3.5, "先行書面調閱，必要時抽訪"),
]


def build(rows: list[dict], capacity: int, metrics: dict | None = None,
          start: date | None = None) -> dict:
    start = start or date.today()
    metrics = metrics or {}
    capacity = max(1, int(capacity))

    ordered = sorted(rows, key=lambda r: -r["score"])
    selected = ordered[:capacity]

    waves = []
    used = set()
    for band, name, days, hours, note in WAVE_SPEC:
        members = [r for r in selected if r["band"] == band]
        if not members:
            continue
        for r in members:
            used.add(r["inst_id"])
        waves.append({
            "band": band, "name": name, "note": note,
            "due": (start + timedelta(days=days)).isoformat(),
            "window_days": days,
            "count": len(members),
            "hours_each": hours,
            "estimated_hours": round(len(members) * hours, 1),
            "estimated_persondays": round(len(members) * hours / 7.5, 1),
            "institutions": [
                {k: r[k] for k in ("rank", "inst_id", "name", "city", "district",
                                   "org_type", "score", "band_label", "top_reasons",
                                   "mode", "urgency", "enrolled")}
                for r in members
            ],
        })
    rest = [r for r in selected if r["inst_id"] not in used]
    if rest:
        waves.append({
            "band": "low", "name": "第四批 · 常態抽查", "note": "併入年度例行訪視",
            "due": (start + timedelta(days=60)).isoformat(), "window_days": 60,
            "count": len(rest), "hours_each": 2.5,
            "estimated_hours": round(len(rest) * 2.5, 1),
            "estimated_persondays": round(len(rest) * 2.5 / 7.5, 1),
            "institutions": [
                {k: r[k] for k in ("rank", "inst_id", "name", "city", "district",
                                   "org_type", "score", "band_label", "top_reasons",
                                   "mode", "urgency", "enrolled")}
                for r in rest
            ],
        })

    # ---------------- 地理併批
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in selected:
        groups[(r["city"] or "-", r["district"] or "-")].append(r)
    routes = []
    for (city, dist), members in groups.items():
        members.sort(key=lambda r: -r["score"])
        routes.append({
            "city": city, "district": dist, "count": len(members),
            "max_score": members[0]["score"],
            "avg_score": round(sum(m["score"] for m in members) / len(members), 2),
            "trips_saved": max(0, len(members) - 1),
            "institutions": [{"inst_id": m["inst_id"], "name": m["name"],
                              "score": m["score"], "band_label": m["band_label"]}
                             for m in members],
        })
    routes.sort(key=lambda x: (-x["count"], -x["max_score"]))

    # ---------------- 效益量化
    n_total = len(rows)
    base_rate = (metrics.get("hybrid") or {}).get("base_rate") or 0.0
    at_k = (metrics.get("hybrid") or {}).get("at_k") or []
    prec = None
    for item in sorted(at_k, key=lambda x: abs(x["k"] - capacity)):
        prec = item["precision"]
        break
    if prec is None:
        prec = (metrics.get("hybrid") or {}).get("confusion", {}).get("precision") or 0.0
    model_hits = round(capacity * prec, 1)
    random_hits = round(capacity * base_rate, 1)
    total_hours = round(sum(w["estimated_hours"] for w in waves), 1)
    full_hours = round(n_total * 3.5, 1)

    efficiency = {
        "capacity": capacity,
        "n_total": n_total,
        "coverage_pct": round(capacity / n_total * 100, 1) if n_total else 0,
        "base_rate": round(base_rate, 4),
        "precision": round(prec, 4),
        "model_hits": model_hits,
        "random_hits": random_hits,
        "multiplier": round(prec / base_rate, 2) if base_rate else None,
        "extra_hits": round(model_hits - random_hits, 1),
        "estimated_hours": total_hours,
        "full_audit_hours": full_hours,
        "hours_saved": round(full_hours - total_hours, 1),
        "hours_saved_pct": round((1 - total_hours / full_hours) * 100, 1) if full_hours else 0,
        "trips_saved": sum(r["trips_saved"] for r in routes),
    }

    return {"start": start.isoformat(), "waves": waves, "routes": routes[:30],
            "efficiency": efficiency,
            "selected": [{k: r[k] for k in ("rank", "inst_id", "name", "city", "district",
                                            "org_type", "score", "band", "band_label",
                                            "urgency", "mode", "top_reasons")}
                         for r in selected]}
