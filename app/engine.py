"""分析引擎：串接資料層 → 特徵 → 異常偵測 → 模型 → 評分 → 彙總。

引擎會維持一份快取結果，設定（權重、混合比例）調整時只重算評分，
不需重跑特徵工程與模型訓練，讓前端可即時互動。
"""
from __future__ import annotations

import json
import math
import threading
import time
from collections import Counter, defaultdict
from datetime import date, timedelta

from . import ai_agent, anomaly, config, datastore, features, forensics, model, nlp, scoring, stats
from .config import (AS_OF, DIMENSIONS, LABEL_WINDOW_DAYS, PRIORITY_BANDS, TRAIN_ASOF,
                     band_by_rank)

AI_REPORTS_FILE = config.OUTPUT_DIR / "ai_reports.json"

ANOMALY_FEATURES = [
    "r_personnel_ratio", "r_admin_ratio", "r_surplus_ratio", "r_teaching_ratio",
    "r_cost_per_student", "r_subsidy_dependency", "last2_score", "round_score",
    "yoy_expense_score",
]


def _impute_matrix(recs: list[dict], names: list[str]) -> list[list[float]]:
    cols: list[list[float]] = []
    for nm in names:
        cols.append(stats.clean([r["features"].get(nm) for r in recs]))
    meds = [stats.median(c) if c else 0.0 for c in cols]
    out = []
    for r in recs:
        row = []
        for i, nm in enumerate(names):
            v = r["features"].get(nm)
            try:
                v = float(v)
                if not math.isfinite(v):
                    v = meds[i]
            except (TypeError, ValueError):
                v = meds[i]
            row.append(v)
        out.append(row)
    return out


class Engine:
    def __init__(self):
        self.lock = threading.RLock()
        self.settings = self._load_settings()
        self.bundle: datastore.DataBundle | None = None
        self.status = "尚未載入"
        self.progress = 0
        self.ready = False
        self.error: str | None = None
        self.built_at = ""
        self.timing: dict[str, float] = {}

        self.cur_recs: list[dict] = []
        self.model: model.LogisticModel | None = None
        self.metrics: dict = {}
        self.rows: list[dict] = []
        self.details: dict[str, dict] = {}
        self.weight_calibration: dict = {}
        self.summary: dict = {}
        self.forensics_overview: dict = {}
        self.sentiment_overview: dict = {}

        self.ai_reports: dict[str, dict] = self._load_ai_reports()
        self.ai_progress = {"running": False, "done": 0, "total": 0,
                            "current": "", "message": "", "error": None}
        self.ai_lock = threading.Lock()

    # ------------------------------------------------------------ AI agent
    def _load_ai_reports(self) -> dict:
        try:
            if AI_REPORTS_FILE.exists():
                return json.loads(AI_REPORTS_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
        return {}

    def _save_ai_reports(self) -> None:
        try:
            config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            AI_REPORTS_FILE.write_text(
                json.dumps(self.ai_reports, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    def investigate_institution(self, inst_id: str) -> dict:
        api_key = (self.settings.get("anthropic_api_key") or "").strip()
        model_name = self.settings.get("anthropic_model") or ai_agent.DEFAULT_MODEL
        report = ai_agent.investigate(inst_id, self.cur_recs, api_key, model=model_name)
        report["_generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with self.ai_lock:
            self.ai_reports[str(inst_id)] = report
            self._save_ai_reports()
        return report

    def run_ai_batch(self, inst_ids: list[str]) -> None:
        with self.ai_lock:
            if self.ai_progress["running"]:
                return
            self.ai_progress = {"running": True, "done": 0, "total": len(inst_ids),
                                "current": "", "message": "", "error": None}

        def worker():
            for i, iid in enumerate(inst_ids):
                name = next((r["inst"].get("name") for r in self.cur_recs
                            if str(r["inst"]["inst_id"]) == str(iid)), iid)
                self.ai_progress["current"] = name
                self.ai_progress["message"] = f"正在深度偵查：{name}"
                try:
                    self.investigate_institution(iid)
                except Exception as exc:  # noqa: BLE001
                    self.ai_progress["error"] = f"{name}：{exc}"
                self.ai_progress["done"] = i + 1
            self.ai_progress["running"] = False
            self.ai_progress["message"] = "完成"

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------ 設定
    def _load_settings(self) -> dict:
        s = dict(config.DEFAULT_SETTINGS)
        s["weights"] = dict(config.DEFAULT_WEIGHTS)
        try:
            if config.SETTINGS_FILE.exists():
                raw = json.loads(config.SETTINGS_FILE.read_text(encoding="utf-8"))
                for k, v in raw.items():
                    if k == "weights" and isinstance(v, dict):
                        s["weights"].update({kk: float(vv) for kk, vv in v.items()
                                             if kk in config.DEFAULT_WEIGHTS})
                    elif k in s:
                        s[k] = v
        except Exception:  # noqa: BLE001
            pass
        return s

    def save_settings(self) -> None:
        try:
            config.SETTINGS_FILE.write_text(
                json.dumps(self.settings, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ 建置
    def build(self, reload_data: bool = True) -> None:
        with self.lock:
            try:
                self.ready = False
                self.error = None
                t0 = time.time()
                if reload_data or self.bundle is None:
                    self._set("載入公開資料集", 5)
                    self.bundle = datastore.load(self.settings)
                self.timing["load"] = round(time.time() - t0, 2)

                if not self.bundle["institutions"]:
                    # 本系統不使用虛構資料：data/ 資料夾內尚未放入真實 institutions
                    # 資料表時，直接以「尚無資料」狀態結束，不生成任何示範內容。
                    self.cur_recs = []
                    self.rows = []
                    self.details = {}
                    self.metrics = {}
                    self.summary = {
                        "as_of": AS_OF.isoformat(), "built_at": self.built_at,
                        "n_institutions": 0, "n_high_risk": 0, "high_risk_pct": 0,
                        "n_critical": 0, "avg_score": 0, "median_score": 0,
                        "n_penalized_4y": 0, "n_burst": 0, "n_priority_no_penalty": 0,
                        "priority_cut_score": 0,
                        "bands": [{"key": k, "label": lbl, "color": c, "count": 0,
                                  "pct": 0, "pct_range": f"前 {lo:g}% ~ {min(hi,100):g}%",
                                  "score_min": None, "score_max": None}
                                 for k, lbl, lo, hi, c in config.RISK_BANDS],
                        "histogram": [{"bucket": f"{i*10}-{i*10+9}", "count": 0}
                                     for i in range(10)],
                        "by_city": [], "by_org_type": [], "by_district": [],
                        "dimension_avg": [{"key": k, "label": lbl, "avg": 0, "p90": 0,
                                          "weight": self.settings["weights"].get(k)}
                                         for k, lbl, _ in DIMENSIONS],
                        "top": [], "weights": self.settings["weights"],
                        "model_blend": self.settings.get("model_blend"),
                        "data": self.bundle.meta, "timing": self.timing,
                        "kpi": {"auc": None, "precision_at_20pct": None,
                               "recall_at_20pct": None, "lead_days": None},
                    }
                    self.forensics_overview = {}
                    self.sentiment_overview = {}
                    self.built_at = time.strftime("%Y-%m-%d %H:%M:%S")
                    self._set("尚無資料：請於 data/ 資料夾放入真實資料檔案", 100)
                    self.ready = True
                    return

                # 構面資料可得性：本次資料集完全沒有裁罰／評鑑／輿情來源時，
                # 對應構面從加權平均中整個排除並重新正規化其餘權重（見
                # scoring.rule_score 之說明），而非以 0 分計入拖低所有機構分數。
                self.dim_availability_global = {
                    "compliance": bool(self.bundle["penalties"]),
                    "financial": True,
                    "evaluation": bool(self.bundle["evaluations"]),
                    "sentiment": bool(self.bundle["posts"]),
                }

                self._set("建立訓練期特徵（時間切分，避免資料洩漏）", 18)
                t = time.time()
                train_recs = features.build(self.bundle, TRAIN_ASOF)
                self._attach_anomaly(train_recs)
                self.timing["train_features"] = round(time.time() - t, 2)

                self._set("訓練監督式預警模型並交叉驗證", 45)
                t = time.time()
                y_map = features.labels_after(self.bundle, TRAIN_ASOF, LABEL_WINDOW_DAYS)
                sev_map = features.severe_labels_after(self.bundle, TRAIN_ASOF,
                                                       LABEL_WINDOW_DAYS)
                rows_f = [r["features"] for r in train_recs]
                y = [y_map.get(str(r["inst"]["inst_id"]), 0) for r in train_recs]
                self.model = model.LogisticModel().fit(rows_f, y, scoring.FEATURE_NAMES)
                cv = model.cross_validate(rows_f, y, scoring.FEATURE_NAMES, folds=4)
                self.timing["train_model"] = round(time.time() - t, 2)

                self._set("建立當期特徵並計算風險分數", 72)
                t = time.time()
                self.cur_recs = features.build(self.bundle, AS_OF)
                self._attach_anomaly(self.cur_recs)
                self.timing["current_features"] = round(time.time() - t, 2)

                self._set("彙整驗證指標", 88)
                self._build_metrics(train_recs, y, sev_map, cv)

                self._set("產生儀表板彙總", 94)
                self.rescore()
                self._build_forensics_overview()
                self._build_sentiment_overview()

                self.built_at = time.strftime("%Y-%m-%d %H:%M:%S")
                self.timing["total"] = round(time.time() - t0, 2)
                self._set("完成", 100)
                self.ready = True
            except Exception as exc:  # noqa: BLE001
                import traceback
                self.error = f"{exc}\n{traceback.format_exc()}"
                self.status = f"發生錯誤：{exc}"
                raise

    def _set(self, msg: str, pct: int) -> None:
        self.status = msg
        self.progress = pct



    def _availability(self, rec: dict) -> dict[str, bool]:
        """單一機構之構面可得性：全域無來源資料的構面一律不可得。"""
        g = getattr(self, "dim_availability_global", {})
        return {"compliance": g.get("compliance", True), "financial": True,
                "evaluation": g.get("evaluation", True),
                "sentiment": g.get("sentiment", True)}

    def _attach_anomaly(self, recs: list[dict]) -> None:
        if not recs:
            return
        X = _impute_matrix(recs, ANOMALY_FEATURES)
        res = anomaly.combined_anomaly(X)
        for i, r in enumerate(recs):
            r["features"]["anomaly_score"] = res["combined"][i]
            r["detail"]["anomaly"] = {
                "combined": res["combined"][i],
                "iforest_rank": res["iforest_rank"][i],
                "knn_rank": res["knn_rank"][i],
                "iforest_raw": res["iforest"][i],
            }

    # ------------------------------------------------------------ 驗證
    def _build_metrics(self, train_recs, y, sev_map, cv) -> None:
        oof = cv["oof"]
        weights = self.settings["weights"]
        rule_scores = []
        for r in train_recs:
            dims = scoring.dimension_scores(r["features"])
            rule_scores.append(scoring.rule_score(dims, weights, self._availability(r)))
        blend = float(self.settings.get("model_blend", config.DEFAULT_MODEL_BLEND))
        model_scaled = [stats.percentile_rank(p, oof) * 100 for p in oof]
        hybrid = [scoring.final_score(rule_scores[i], model_scaled[i], blend)
                  for i in range(len(train_recs))]

        y_sev = [sev_map.get(str(r["inst"]["inst_id"]), 0) for r in train_recs]
        n = len(train_recs)
        ks = tuple(k for k in (20, 50, 100, max(1, int(n * 0.2))) if k <= n)

        # 混合比例敏感度分析：以 out-of-fold 預測掃描 0~1，供使用者判斷
        # 「規則」與「模型」的最佳配比，避免憑感覺設定權重。
        blend_sweep = []
        for b10 in range(0, 11):
            b = b10 / 10
            sc = [scoring.final_score(rule_scores[i], model_scaled[i], b)
                  for i in range(n)]
            blend_sweep.append({
                "blend": b,
                "auc": round(stats.auc(sc, y), 4),
                "precision_at_20pct": round(
                    stats.precision_at_k(sc, y, max(1, int(n * 0.2))), 4),
            })
        best_blend = max(blend_sweep, key=lambda x: x["auc"])

        # 權重校準：直接以五構面分數為特徵訓練邏輯迴歸，把係數轉成建議權重。
        # 這是監督式模型在本系統最有價值的用法——不是取代規則，而是告訴
        # 監理人員「哪一個構面在實際裁罰結果上更有預測力」。
        dim_keys = [d[0] for d in DIMENSIONS]
        dim_rows = [scoring.dimension_scores(r["features"]) for r in train_recs]
        wm = model.LogisticModel(l2=0.05).fit(dim_rows, y, dim_keys)
        raw = [max(0.0, c) for c in wm.coef]
        total_raw = sum(raw)
        calibrated = ({k: round(v / total_raw, 4) for k, v in zip(dim_keys, raw)}
                      if total_raw > 0 else dict(self.settings["weights"]))
        cal_scores = [scoring.rule_score(d, calibrated, self._availability(r))
                     for d, r in zip(dim_rows, train_recs)]
        cal_cv = model.cross_validate(dim_rows, y, dim_keys, folds=4, l2=0.05)
        weight_calibration = {
            "weights": calibrated,
            "coefficients": {k: round(c, 4) for k, c in zip(dim_keys, wm.coef)},
            "auc_calibrated": round(stats.auc(cal_scores, y), 4),
            "auc_current": round(stats.auc(rule_scores, y), 4),
            "auc_oof": round(cal_cv["auc"], 4),
            "current_weights": dict(self.settings["weights"]),
            "labels": {k: lbl for k, lbl, _ in DIMENSIONS},
        }
        self.weight_calibration = weight_calibration

        # 預警提前天數：以混合分數前 20% 名單為預警對象
        cut = max(1, int(n * 0.2))
        order = sorted(range(n), key=lambda i: -hybrid[i])
        flagged = [train_recs[i]["inst"]["inst_id"] for i in order[:cut]]
        first_pen: dict[str, str] = {}
        end = TRAIN_ASOF + timedelta(days=LABEL_WINDOW_DAYS)
        for p in self.bundle["penalties"]:
            d = features._pdate(p.get("penalty_date"))
            if d and TRAIN_ASOF < d <= end:
                iid = str(p.get("inst_id"))
                if iid not in first_pen or p["penalty_date"] < first_pen[iid]:
                    first_pen[iid] = p["penalty_date"]
        leads = []
        for iid in flagged:
            if str(iid) in first_pen:
                d = date.fromisoformat(first_pen[str(iid)])
                leads.append((d - TRAIN_ASOF).days)

        self.metrics = {
            "train_as_of": TRAIN_ASOF.isoformat(),
            "label_window": f"{(TRAIN_ASOF + timedelta(days=1)).isoformat()} ~ {end.isoformat()}",
            "n_train": n,
            "current_blend": blend,
            "blend_sweep": blend_sweep,
            "best_blend": best_blend,
            "weight_calibration": weight_calibration,
            "hybrid": model.evaluate(hybrid, y, ks),
            "model_only": model.evaluate(oof, y, ks),
            "rule_only": model.evaluate(rule_scores, y, ks),
            "severe": model.evaluate(hybrid, y_sev, ks),
            "importance": [
                {**it, "label": scoring.FEATURE_LABELS.get(it["feature"], it["feature"])}
                for it in (self.model.importance() if self.model else [])
            ],
            "dimension_auc": [
                {"key": k, "label": lbl,
                 "auc": round(stats.auc([scoring.dimension_scores(r["features"])[k]
                                         for r in train_recs], y), 4)}
                for k, lbl, _ in DIMENSIONS
            ],
            "lead_time": {
                "n": len(leads),
                "mean_days": round(sum(leads) / len(leads), 1) if leads else 0,
                "median_days": round(stats.median(leads), 1) if leads else 0,
                "min_days": min(leads) if leads else 0,
                "max_days": max(leads) if leads else 0,
            },
            "coverage": {
                "flagged": cut,
                "flagged_pct": round(cut / n * 100, 1) if n else 0,
                "hit": len(leads),
                "total_positive": sum(y),
                "recall": round(len(leads) / max(1, sum(y)), 4),
            },
        }

    # ------------------------------------------------------------ 評分
    def rescore(self) -> None:
        """僅重算分數與彙總（權重調整時使用）。"""
        with self.lock:
            weights = self.settings["weights"]
            blend = float(self.settings.get("model_blend", config.DEFAULT_MODEL_BLEND))
            recs = self.cur_recs
            if not recs:
                return
            probs = self.model.predict_proba([r["features"] for r in recs]) \
                if self.model else [0.0] * len(recs)
            model_scaled = [stats.percentile_rank(p, probs) * 100 for p in probs]

            staged = []
            for i, r in enumerate(recs):
                dims = scoring.dimension_scores(r["features"])
                avail = self._availability(r)
                rule = scoring.rule_score(dims, weights, avail)
                staged.append((i, r, dims, rule, avail,
                               scoring.final_score(rule, model_scaled[i], blend)))
            # 先排序再分級：風險等級為族群相對位置，需在全體分數算完後才能決定
            staged.sort(key=lambda t: -t[5])
            total = len(staged)

            rows = []
            details = {}
            for pos, (i, r, dims, rule, avail, fin) in enumerate(staged, 1):
                inst = r["inst"]
                iid = str(inst.get("inst_id"))
                band, band_lbl, color = band_by_rank(pos, total)
                contribs = self.model.contributions(r["features"]) if self.model else []
                exp = scoring.explain(r, dims, weights, contribs, avail)
                act = scoring.action_suggestion(band, dims, exp["reasons"])
                rows.append({
                    "inst_id": iid,
                    "name": inst.get("name"),
                    "city": inst.get("city"),
                    "district": inst.get("district"),
                    "org_type": inst.get("org_type"),
                    # 組織型態（獨立園／國小附設／職場互助教保服務中心…）：
                    # 設立別看不出這項差異，但它決定了哪些指標適用
                    # （學校附設園沒有獨立決算），清單與詳情頁都需要顯示。
                    "inst_kind": inst.get("inst_kind"),
                    "enrolled": inst.get("enrolled"),
                    "capacity": inst.get("approved_capacity"),
                    "teacher_count": inst.get("teacher_count"),
                    "classes": inst.get("classes"),
                    "score": fin,
                    "rule_score": rule,
                    "model_score": round(model_scaled[i], 2),
                    "model_prob": round(probs[i], 4),
                    "anomaly_score": r["features"].get("anomaly_score", 0.0),
                    "band": band, "band_label": band_lbl, "color": color,
                    "rank": pos,
                    "percentile": round((1 - (pos - 1) / max(1, total)) * 100, 1),
                    "dims": dims,
                    "pen_count_4y": r["features"].get("pen_count_4y", 0),
                    "pen_count_1y": r["features"].get("pen_count_1y", 0),
                    "senti_negative": (r["detail"].get("sentiment") or {}).get("n_negative", 0),
                    "burst": bool(((r["detail"].get("sentiment") or {}).get("burst") or {}).get("burst")),
                    "top_reasons": [x["title"] for x in exp["reasons"][:3]],
                    "critical_flags": act["critical_flags"],
                    "urgency": act["urgency"],
                    "mode": act["mode"],
                })
                details[iid] = {"explain": exp, "action": act, "dims": dims,
                                "features": r["features"], "detail": r["detail"],
                                "inst": inst}
            self.rows = rows
            self.details = details
            self._build_summary()

    # ------------------------------------------------------------ 彙總
    def _build_summary(self) -> None:
        rows = self.rows
        n = len(rows)
        band_counter = Counter(r["band"] for r in rows)
        bands = []
        for k, lbl, lo, hi, color in config.RISK_BANDS:
            members = [r["score"] for r in rows if r["band"] == k]
            bands.append({
                "key": k, "label": lbl, "color": color,
                "count": band_counter.get(k, 0),
                "pct": round(band_counter.get(k, 0) / n * 100, 1) if n else 0,
                "pct_range": f"前 {lo:g}% ~ {min(hi, 100):g}%",
                "score_min": round(min(members), 2) if members else None,
                "score_max": round(max(members), 2) if members else None,
            })

        def _group():
            return {"s": [], "h": 0}

        by_city: dict[str, dict] = defaultdict(_group)
        by_type: dict[str, dict] = defaultdict(_group)
        by_district: dict[str, dict] = defaultdict(_group)
        for r in rows:
            prio = 1 if r["band"] in PRIORITY_BANDS else 0
            for bucket, key in ((by_city, r["city"] or "未分類"),
                                (by_type, r["org_type"] or "未分類")):
                bucket[key]["s"].append(r["score"])
                bucket[key]["h"] += prio
            if r["city"] == "新北市":
                d = by_district[r["district"] or "未分類"]
                d["s"].append(r["score"])
                d["h"] += prio

        hist = [0] * 10
        for r in rows:
            hist[min(9, int(r["score"] // 10))] += 1

        dim_avg = []
        for key, label, _ in DIMENSIONS:
            vals = [r["dims"].get(key, 0) for r in rows]
            dim_avg.append({"key": key, "label": label,
                            "avg": round(stats.mean(vals), 2),
                            "p90": round(stats.quantile(vals, 0.9), 2),
                            "weight": self.settings["weights"].get(key)})

        high = [r for r in rows if r["band"] in PRIORITY_BANDS]
        self.summary = {
            "as_of": AS_OF.isoformat(),
            "built_at": self.built_at,
            "n_institutions": n,
            "n_high_risk": len(high),
            "high_risk_pct": round(len(high) / n * 100, 1) if n else 0,
            "n_critical": sum(1 for r in rows if r["band"] == "critical"),
            "avg_score": round(stats.mean([r["score"] for r in rows]), 2),
            "median_score": round(stats.median([r["score"] for r in rows]), 2),
            "n_penalized_4y": sum(1 for r in rows if (r["pen_count_4y"] or 0) > 0),
            "n_burst": sum(1 for r in rows if r["burst"]),
            "n_priority_no_penalty": sum(
                1 for r in high if (r["pen_count_4y"] or 0) == 0),
            "priority_cut_score": round(min((r["score"] for r in high), default=0), 2),
            "bands": bands,
            "histogram": [{"bucket": f"{i*10}-{i*10+9}", "count": c}
                          for i, c in enumerate(hist)],
            "by_city": sorted(
                [{"city": c, "count": len(v["s"]), "avg": round(stats.mean(v["s"]), 2),
                  "high": v["h"],
                  "high_pct": round(v["h"] / len(v["s"]) * 100, 1) if v["s"] else 0}
                 for c, v in by_city.items()], key=lambda x: -x["avg"]),
            "by_org_type": sorted(
                [{"org_type": c, "count": len(v["s"]), "avg": round(stats.mean(v["s"]), 2),
                  "high": v["h"],
                  "high_pct": round(v["h"] / len(v["s"]) * 100, 1) if v["s"] else 0,
                  "p90": round(stats.quantile(v["s"], 0.9), 2)}
                 for c, v in by_type.items()], key=lambda x: -x["avg"]),
            "by_district": sorted(
                [{"district": c, "count": len(v["s"]), "avg": round(stats.mean(v["s"]), 2),
                  "high": v["h"]}
                 for c, v in by_district.items() if len(v["s"]) >= 2],
                key=lambda x: -x["avg"])[:20],
            "dimension_avg": dim_avg,
            "top": [{k: r[k] for k in ("rank", "inst_id", "name", "city", "district",
                                       "org_type", "score", "band", "band_label",
                                       "top_reasons", "urgency", "dims")}
                    for r in rows[:25]],
            "weights": self.settings["weights"],
            "model_blend": self.settings.get("model_blend"),
            "data": self.bundle.meta if self.bundle else {},
            "timing": self.timing,
            "kpi": {
                "auc": (self.metrics.get("hybrid") or {}).get("auc"),
                "precision_at_20pct": (self.metrics.get("hybrid") or {})
                    .get("confusion", {}).get("precision"),
                "recall_at_20pct": (self.metrics.get("hybrid") or {})
                    .get("confusion", {}).get("recall"),
                "lead_days": (self.metrics.get("lead_time") or {}).get("mean_days"),
            },
        }

    def _build_forensics_overview(self) -> None:
        recs = self.cur_recs
        all_amounts = [float(x.get("amount") or 0) for x in (self.bundle["ledger"] or [])
                       if x.get("amount")]
        overall = forensics.benford(all_amounts)
        offenders = sorted(
            ({"inst_id": r["inst"]["inst_id"], "name": r["inst"]["name"],
              "org_type": r["inst"]["org_type"],
              "n": (r["detail"].get("last_two") or {}).get("n"),
              "chi2": (r["detail"].get("last_two") or {}).get("chi2"),
              "p_value": (r["detail"].get("last_two") or {}).get("p_value"),
              "max_share": (r["detail"].get("last_two") or {}).get("max_share"),
              "top_endings": (r["detail"].get("last_two") or {}).get("top_endings"),
              "level": (r["detail"].get("last_two") or {}).get("level"),
              "round_ratio": (r["detail"].get("round_bias") or {}).get("ratio_1000"),
              "score": (r["detail"].get("last_two") or {}).get("score", 0)}
             for r in recs if (r["detail"].get("last_two") or {}).get("sufficient")),
            key=lambda x: -(x["score"] or 0))[:20]

        ratio_box = []
        for rn in ["personnel_ratio", "teaching_ratio", "admin_ratio", "surplus_ratio",
                   "meal_ratio", "facility_ratio"]:
            groups = []
            for ot in config.ORG_TYPES:
                vals = [r["features"].get(f"r_{rn}") for r in recs
                        if r["inst"].get("org_type") == ot]
                vals = stats.clean(vals)
                if len(vals) < 3:
                    continue
                groups.append({
                    "group": ot, "n": len(vals),
                    "min": round(stats.quantile(vals, 0.02), 4),
                    "q1": round(stats.quantile(vals, 0.25), 4),
                    "median": round(stats.median(vals), 4),
                    "q3": round(stats.quantile(vals, 0.75), 4),
                    "max": round(stats.quantile(vals, 0.98), 4),
                })
            ratio_box.append({"ratio": rn,
                              "label": forensics.RATIO_LABELS.get(rn, rn),
                              "groups": groups})

        anomaly_top = sorted(
            ({"inst_id": r["inst"]["inst_id"], "name": r["inst"]["name"],
              "org_type": r["inst"]["org_type"],
              **(r["detail"].get("anomaly") or {}),
              "personnel_ratio": r["features"].get("r_personnel_ratio"),
              "admin_ratio": r["features"].get("r_admin_ratio"),
              "surplus_ratio": r["features"].get("r_surplus_ratio")}
             for r in recs),
            key=lambda x: -(x.get("combined") or 0))[:20]

        scatter = [{"inst_id": r["inst"]["inst_id"], "name": r["inst"]["name"],
                    "org_type": r["inst"]["org_type"],
                    "x": r["features"].get("r_personnel_ratio"),
                    "y": r["features"].get("r_surplus_ratio"),
                    "anomaly": r["features"].get("anomaly_score", 0)}
                   for r in recs
                   if r["features"].get("r_personnel_ratio") is not None
                   and r["features"].get("r_surplus_ratio") is not None]

        # 首位數分布偏離同儕者（參考指標）
        first_digit_top = sorted(
            ({"inst_id": r["inst"]["inst_id"], "name": r["inst"]["name"],
              "org_type": r["inst"]["org_type"],
              **{k: (r["detail"].get("digit_conformity") or {}).get(k)
                 for k in ("n", "mad", "baseline_mad", "mad_ratio", "adjusted_ratio",
                           "population_median_mad", "p_value", "level", "score",
                           "basis", "observed_pct", "expected_pct")}}
             for r in recs
             if (r["detail"].get("digit_conformity") or {}).get("sufficient")),
            key=lambda x: -(x["score"] or 0))[:15]

        self.forensics_overview = {
            "overall_benford": overall,
            "methodology": {
                "portfolio_test": "首位數班佛定律檢定（適用於大樣本，用於整體資料可信度查核）",
                "institution_test": "末兩位數均勻性卡方檢定（不受金額量級集中影響，適用於單一機構決算）",
                "why": "單一機構的決算科目金額集中於少數量級、同一科目跨期重複，"
                       "獨立觀察數遠少於資料筆數，直接套用班佛定律絕對門檻會造成大量誤判；"
                       "末兩位數與量級無關，因此在機構層級仍保有檢定力。",
            },
            "last_two_offenders": offenders,
            "first_digit_offenders": first_digit_top,
            "ratio_box": ratio_box,
            "anomaly_top": anomaly_top,
            "scatter": scatter,
            "n_with_financials": sum(1 for r in recs if r["features"].get("fin_available")),
            "round_bias_top": sorted(
                ({"inst_id": r["inst"]["inst_id"], "name": r["inst"]["name"],
                  **(r["detail"].get("round_bias") or {})}
                 for r in recs if (r["detail"].get("round_bias") or {}).get("sufficient")),
                key=lambda x: -(x.get("score") or 0))[:15],
        }

    def _build_sentiment_overview(self) -> None:
        posts = self.bundle["posts"] or []
        neg_texts = []
        topic_cnt: Counter[str] = Counter()
        month_total: Counter[str] = Counter()
        month_neg: Counter[str] = Counter()
        source_cnt: Counter[str] = Counter()
        source_neg: Counter[str] = Counter()
        analyzed_n = 0
        for r in self.cur_recs:
            sen = r["detail"].get("sentiment") or {}
            for t in sen.get("topics", []):
                topic_cnt[t["topic"]] += t["count"]
        for p in posts:
            d = str(p.get("post_date") or "")[:7]
            if d:
                month_total[d] += 1
            src = p.get("source") or "其他"
            source_cnt[src] += 1
        # 負面文本以機構層級的分析結果彙整（避免重複跑 NLP）
        for r in self.cur_recs:
            sen = r["detail"].get("sentiment") or {}
            analyzed_n += sen.get("n_posts", 0)
            iname = str(r["inst"].get("name") or "")
            for t in sen.get("negative_texts", []):
                if not t:
                    continue
                # 移除機構名稱，避免園名字串主導關鍵詞統計
                neg_texts.append(t.replace(iname, "該園") if iname else t)
            for d in sen.get("negative_dates", []):
                if d:
                    month_neg[str(d)[:7]] += 1
            for s in sen.get("negative_sources", []):
                source_neg[s or "其他"] += 1

        top_neg = sorted(
            ({"inst_id": r["inst"]["inst_id"], "name": r["inst"]["name"],
              "city": r["inst"]["city"], "org_type": r["inst"]["org_type"],
              "score": (r["detail"]["sentiment"] or {}).get("score", 0),
              "n_negative": (r["detail"]["sentiment"] or {}).get("n_negative", 0),
              "neg_ratio": (r["detail"]["sentiment"] or {}).get("neg_ratio", 0),
              "burst": ((r["detail"]["sentiment"] or {}).get("burst") or {}).get("burst"),
              "topics": [t["topic"] for t in (r["detail"]["sentiment"] or {}).get("topics", [])[:3]],
              "example": ((r["detail"]["sentiment"] or {}).get("recent_examples") or [{}])[0]
                          .get("content")}
             for r in self.cur_recs),
            key=lambda x: -(x["score"] or 0))[:20]

        bursts = [x for x in top_neg if x["burst"]]
        self.sentiment_overview = {
            "n_posts": len(posts),
            "n_analyzed": analyzed_n,
            "topics": [{"topic": t, "count": c,
                        "severity": nlp.TOPICS.get(t, {}).get("severity", 1.0)}
                       for t, c in topic_cnt.most_common()],
            "keywords": nlp.keywords(neg_texts, 40),
            "timeline": [{"month": m, "total": month_total[m],
                          "negative": month_neg.get(m, 0)}
                         for m in sorted(month_total)][-30:],
            "sources": [{"source": s, "count": c, "negative": source_neg.get(s, 0),
                         "credibility": nlp.SOURCE_CREDIBILITY.get(s, 1.0)}
                        for s, c in source_cnt.most_common()],
            "top_negative": top_neg,
            "bursts": bursts,
            "lexicon_size": len(nlp.NEG_WORDS) + len(nlp.POS_WORDS),
            "topic_defs": [{"topic": t, "severity": v["severity"],
                            "words": v["words"][:12]} for t, v in nlp.TOPICS.items()],
        }

    # ------------------------------------------------------------ 查詢
    def get_detail(self, inst_id: str) -> dict | None:
        d = self.details.get(str(inst_id))
        if not d:
            return None
        row = next((r for r in self.rows if r["inst_id"] == str(inst_id)), None)
        return {"row": row, **d}


ENGINE = Engine()
