"""本機 HTTP 服務：提供儀表板靜態檔與 JSON API（僅使用標準函式庫）。"""
from __future__ import annotations

import json
import mimetypes
import posixpath
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import ai_agent, config, datastore, export, schedule
from .config import DIMENSIONS, WEB_DIR
from .engine import ENGINE
from .scoring import FEATURE_LABELS, FEATURE_NAMES

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")


def _json_default(o):
    try:
        return float(o)
    except (TypeError, ValueError):
        return str(o)


class Handler(BaseHTTPRequestHandler):
    server_version = "KREWS/1.0"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------ 工具
    def log_message(self, fmt, *args):  # 靜音，避免主控台噪音
        pass

    def _send(self, body: bytes, ctype: str, code: int = 200, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False, default=_json_default).encode("utf-8")
        self._send(body, "application/json; charset=utf-8", code)

    def _query(self) -> dict[str, str]:
        parsed = urllib.parse.urlparse(self.path)
        return {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    # ------------------------------------------------ 路由
    def do_GET(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            if path.startswith("/api/"):
                return self._api_get(path)
            return self._static(path)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc), "trace": traceback.format_exc()}, 500)

    def do_POST(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/settings":
                data = self._body()
                w = data.get("weights")
                if isinstance(w, dict):
                    for k in config.DEFAULT_WEIGHTS:
                        if k in w:
                            try:
                                ENGINE.settings["weights"][k] = max(0.0, float(w[k]))
                            except (TypeError, ValueError):
                                pass
                if "model_blend" in data:
                    try:
                        ENGINE.settings["model_blend"] = max(0.0, min(1.0, float(data["model_blend"])))
                    except (TypeError, ValueError):
                        pass
                if "inspection_capacity" in data:
                    try:
                        ENGINE.settings["inspection_capacity"] = max(1, int(data["inspection_capacity"]))
                    except (TypeError, ValueError):
                        pass
                if "anthropic_api_key" in data:
                    ENGINE.settings["anthropic_api_key"] = str(data["anthropic_api_key"] or "").strip()
                if "anthropic_model" in data:
                    ENGINE.settings["anthropic_model"] = str(data["anthropic_model"] or "").strip() \
                        or ai_agent.DEFAULT_MODEL
                ENGINE.save_settings()
                ENGINE.rescore()
                return self._json({"ok": True, "summary": ENGINE.summary})
            if path == "/api/apply-calibrated-weights":
                cal = (ENGINE.weight_calibration or {}).get("weights")
                if not cal:
                    return self._json({"ok": False, "error": "尚無校準結果"}, 400)
                ENGINE.settings["weights"] = {k: float(v) for k, v in cal.items()}
                ENGINE.save_settings()
                ENGINE.rescore()
                return self._json({"ok": True, "weights": ENGINE.settings["weights"],
                                   "summary": ENGINE.summary})
            if path == "/api/reset-weights":
                ENGINE.settings["weights"] = dict(config.DEFAULT_WEIGHTS)
                ENGINE.settings["model_blend"] = config.DEFAULT_MODEL_BLEND
                ENGINE.save_settings()
                ENGINE.rescore()
                return self._json({"ok": True, "summary": ENGINE.summary})
            if path == "/api/reload":
                data = self._body()
                if "aws_base_url" in data:
                    ENGINE.settings["aws_base_url"] = str(data["aws_base_url"] or "").strip()
                    ENGINE.save_settings()
                threading.Thread(target=_safe_build, kwargs={"reload_data": True},
                                 daemon=True).start()
                return self._json({"ok": True})
            if path == "/api/export-templates":
                if ENGINE.bundle:
                    names = datastore.export_templates(ENGINE.bundle)
                    return self._json({"ok": True, "files": names,
                                       "dir": str(config.DATA_DIR)})
                return self._json({"ok": False, "error": "資料尚未載入"}, 400)
            if path == "/api/ai/investigate":
                data = self._body()
                iid = str(data.get("id") or "")
                if not iid:
                    return self._json({"ok": False, "error": "缺少機構代碼"}, 400)
                try:
                    report = ENGINE.investigate_institution(iid)
                    return self._json({"ok": True, "report": report})
                except ai_agent.AgentError as exc:
                    return self._json({"ok": False, "error": str(exc)}, 400)
            if path == "/api/ai/batch":
                data = self._body()
                ids = data.get("ids")
                if ids == "all":
                    ids = [r["inst_id"] for r in ENGINE.rows]
                elif not ids:
                    # 未指定時，預設僅分析目前分數最高的一批機構（避免對數百所機構
                    # 全數執行深度偵查造成過長等待與過高 API 費用）；如需全部分析，
                    # 前端可傳入 {"ids": "all"}。
                    top_n = int(data.get("top_n") or 30)
                    ids = [r["inst_id"] for r in sorted(ENGINE.rows, key=lambda r: -r["score"])[:top_n]]
                if not (ENGINE.settings.get("anthropic_api_key") or "").strip():
                    return self._json({"ok": False, "error": "尚未設定 Claude API 金鑰"}, 400)
                if ENGINE.ai_progress.get("running"):
                    return self._json({"ok": False, "error": "已有批次分析執行中"}, 400)
                ENGINE.run_ai_batch(ids)
                return self._json({"ok": True, "n": len(ids)})
            self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc), "trace": traceback.format_exc()}, 500)

    # ------------------------------------------------ API
    def _api_get(self, path: str):
        q = self._query()
        if path == "/api/status":
            return self._json({
                "ready": ENGINE.ready, "status": ENGINE.status,
                "progress": ENGINE.progress, "error": ENGINE.error,
                "built_at": ENGINE.built_at,
                "app": {"name": config.APP_NAME, "version": config.APP_VERSION,
                        "subtitle": config.APP_SUBTITLE},
            })
        if not ENGINE.ready and path != "/api/meta":
            return self._json({"error": "分析尚未完成", "status": ENGINE.status,
                               "progress": ENGINE.progress}, 503)

        if path == "/api/meta":
            safe_settings = dict(ENGINE.settings)
            has_key = bool((safe_settings.get("anthropic_api_key") or "").strip())
            safe_settings["anthropic_api_key"] = ""
            safe_settings["anthropic_api_key_set"] = has_key
            return self._json({
                "app": {"name": config.APP_NAME, "version": config.APP_VERSION,
                        "subtitle": config.APP_SUBTITLE},
                "dimensions": [{"key": k, "label": l, "desc": d} for k, l, d in DIMENSIONS],
                "bands": [{"key": k, "label": l, "lo": lo, "hi": hi, "color": c}
                          for k, l, lo, hi, c in config.RISK_BANDS],
                "features": [{"key": k, "label": FEATURE_LABELS.get(k, k)}
                             for k in FEATURE_NAMES],
                "penalty_categories": config.PENALTY_CATEGORIES,
                "org_types": config.ORG_TYPES,
                "settings": safe_settings,
                "as_of": config.AS_OF.isoformat(),
                "data": (ENGINE.bundle.meta if ENGINE.bundle else {}),
                "ready": ENGINE.ready,
            })
        if path == "/api/summary":
            return self._json(ENGINE.summary)
        if path == "/api/institutions":
            return self._json(self._filter_rows(q))
        if path == "/api/institution":
            d = ENGINE.get_detail(q.get("id", ""))
            if not d:
                return self._json({"error": "查無此機構"}, 404)
            d["ai_report"] = ENGINE.ai_reports.get(str(q.get("id", "")))
            return self._json(d)
        if path == "/api/ai/status":
            return self._json(ENGINE.ai_progress)
        if path == "/api/ai/report":
            rep = ENGINE.ai_reports.get(str(q.get("id", "")))
            if not rep:
                return self._json({"error": "尚無分析結果"}, 404)
            return self._json(rep)
        if path == "/api/metrics":
            return self._json(ENGINE.metrics)
        if path == "/api/forensics":
            return self._json(ENGINE.forensics_overview)
        if path == "/api/sentiment":
            return self._json(ENGINE.sentiment_overview)
        if path == "/api/schedule":
            cap = q.get("capacity")
            try:
                cap = int(cap) if cap else int(ENGINE.settings.get("inspection_capacity", 40))
            except (TypeError, ValueError):
                cap = 40
            return self._json(schedule.build(ENGINE.rows, cap, ENGINE.metrics))
        if path == "/api/export.xlsx":
            cap = int(ENGINE.settings.get("inspection_capacity", 40))
            sched = schedule.build(ENGINE.rows, cap, ENGINE.metrics)
            body, fname, ctype = export.risk_workbook(ENGINE.rows, sched,
                                                      ENGINE.summary, ENGINE.metrics)
            return self._send(body, ctype, 200, {
                "Content-Disposition":
                    "attachment; filename*=UTF-8''" + urllib.parse.quote(fname)})
        if path == "/api/report":
            d = ENGINE.get_detail(q.get("id", ""))
            if not d:
                return self._json({"error": "查無此機構"}, 404)
            body, fname, ctype = export.institution_report(d)
            return self._send(body, ctype, 200, {
                "Content-Disposition":
                    "attachment; filename*=UTF-8''" + urllib.parse.quote(fname)})
        return self._json({"error": "not found"}, 404)

    def _filter_rows(self, q: dict) -> dict:
        rows = ENGINE.rows
        city = q.get("city")
        org = q.get("org_type")
        band = q.get("band")
        kw = (q.get("q") or "").strip()
        try:
            lo = float(q.get("min", 0) or 0)
        except ValueError:
            lo = 0.0
        try:
            hi = float(q.get("max", 100) or 100)
        except ValueError:
            hi = 100.0
        out = []
        for r in rows:
            if city and r["city"] != city:
                continue
            if org and r["org_type"] != org:
                continue
            if band and r["band"] != band:
                continue
            if not (lo <= r["score"] <= hi):
                continue
            if kw and kw not in str(r["name"]) and kw not in str(r["inst_id"]) \
                    and kw not in str(r.get("district") or ""):
                continue
            out.append(r)
        sort = q.get("sort") or "score"
        reverse = (q.get("order") or "desc") == "desc"
        if sort in ("score", "rule_score", "model_score", "anomaly_score", "enrolled",
                    "pen_count_4y", "pen_count_1y", "senti_negative", "rank"):
            out = sorted(out, key=lambda r: (r.get(sort) is None, r.get(sort) or 0),
                         reverse=reverse)
        elif sort in ("name", "city", "district", "org_type"):
            out = sorted(out, key=lambda r: str(r.get(sort) or ""), reverse=reverse)
        try:
            limit = int(q.get("limit", 0) or 0)
        except ValueError:
            limit = 0
        total = len(out)
        if limit > 0:
            out = out[:limit]
        return {"total": total, "returned": len(out), "rows": out,
                "cities": sorted({r["city"] for r in rows if r["city"]}),
                "org_types": sorted({r["org_type"] for r in rows if r["org_type"]})}

    # ------------------------------------------------ 靜態檔
    def _static(self, path: str):
        if path in ("/", ""):
            path = "/index.html"
        safe = posixpath.normpath(urllib.parse.unquote(path)).lstrip("/")
        target = (WEB_DIR / safe).resolve()
        try:
            target.relative_to(WEB_DIR.resolve())
        except ValueError:
            return self._json({"error": "forbidden"}, 403)
        if not target.exists() or not target.is_file():
            return self._json({"error": "not found", "path": safe}, 404)
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",
                                                  "image/svg+xml", "application/json"):
            ctype += "; charset=utf-8"
        self._send(target.read_bytes(), ctype)


def _safe_build(reload_data: bool = True):
    try:
        ENGINE.build(reload_data=reload_data)
    except Exception:  # noqa: BLE001
        try:
            config.LOG_FILE.write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass


def start(port: int = 0) -> tuple[ThreadingHTTPServer, int, threading.Thread]:
    """啟動伺服器（port=0 時自動挑選可用埠）並在背景執行分析。"""
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    actual = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    threading.Thread(target=_safe_build, kwargs={"reload_data": True}, daemon=True).start()
    return httpd, actual, t
