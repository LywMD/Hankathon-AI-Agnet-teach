"""Claude AI agent：自主呼叫工具、深入偵查單一機構之鑑識分析代理人。

與系統其餘部分（rule + 監督式模型混合評分）互補而非取代：
* 快速初篩（全體機構排序、稽查排程、Excel 匯出）仍由既有統計／機器學習
  流程負責，即時可用、不依賴網路或 API。
* 本模組提供「深度偵查」能力：給定單一機構，讓 Claude 自主決定要調閱哪些
  證據（財務比率、同儕偏離、班佛定律等基礎鑑識檢定、生師比、裁罰與評鑑
  歷程、社群輿情），並產出結構化的自然語言鑑識報告。

僅使用標準函式庫（urllib）呼叫 Anthropic Messages API，避免額外套件依賴；
執行時需要網路連線與使用者自備之 API 金鑰（設定頁輸入，存於 settings.json，
不會外傳至本應用程式以外的任何伺服器）。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable

from . import forensics, stats
from .scoring import FEATURE_LABELS

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5"
MAX_ROUNDS = 8

SYSTEM_PROMPT = """你是新北市教保機構的鑑識會計稽核專家，任務是針對「單一機構」進行深入的\
風險偵查，判斷其是否有值得優先稽查的跡象。

工作方式：
1. 你可以自主呼叫提供的工具，依需要調閱該機構的財務比率、同儕偏離程度、\
班佛定律與末兩位數等基礎鑑識會計檢定、裁罰與評鑑歷程、社群輿情。不必每個\
工具都呼叫——依你的專業判斷，只查你認為與風險研判相關的項目。
2. 對於財務數字，請留意可能的人工編造跡象（金額尾數過度集中、明顯偏離同儕\
比率、收支落差異常）但也要考慮合理的另類解釋（如新設機構人事費率偏低、\
一次性資本支出造成年度跳動），避免武斷。
3. 你可以呼叫 search_live_news 即時上網查詢最新新聞。裁罰紀錄的公告有時間差，\
新聞往往先揭露事件，因此當既有資料看起來平淡但你仍有疑慮時，這個工具特別有用。\
引用新聞時務必註明來源與日期，並提醒該資訊尚未經人工核實。
4. 下結論前請先呼叫 get_data_coverage 確認哪些構面其實沒有資料。\
「查無裁罰紀錄」與「已查核確認合規」是完全不同的兩件事；某構面無資料時，\
不得因其分數低而推論該面向安全，應在報告中明確指出資料限制，並據此調低信心程度。
5. 完成調查後，務必呼叫 submit_report 工具提交最終結論，不要只用文字回覆。
6. 不得臆測或杜撰未提供的數字。所有數字都必須來自工具回傳的內容。
"""

TOOLS = [
    {
        "name": "get_financial_overview",
        "description": "取得該機構近年決算收支金額、關鍵財務比率（人事費率、結餘率、行政管理費率等）"
                       "與年度增減幅度。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_peer_deviation",
        "description": "取得該機構各項財務比率相對「同類型、同規模機構」之穩健標準化偏離（z分數）"
                       "與同儕中位數，z 絕對值愈大代表愈偏離同儕常態。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_forensic_tests",
        "description": "取得基礎鑑識會計檢定結果：決算科目金額末兩位數均勻性檢定（Nigrini 方法，"
                       "偵測人工估列／填製）、金額整數偏誤、首位數分布與同儕基準之偏離。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_compliance_history",
        "description": "取得裁罰紀錄（日期、類別、法條、罰鍰金額、處分方式）與基礎評鑑歷程。"
                       "若查無資料代表本系統未取得該機構此類公開資料，並非合規。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_sentiment_summary",
        "description": "取得社群輿情負面聲量、主要負面主題與是否出現短期爆量之統計偵測結果。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_ratio_benchmark",
        "description": "取得指定財務比率在同類型機構中的完整分布（最小值、四分位數、中位數、"
                       "最大值），用於判斷該機構數值落在分布的哪個位置。",
        "input_schema": {
            "type": "object",
            "properties": {
                "ratio": {
                    "type": "string",
                    "enum": list(forensics.RATIO_LABELS.keys()),
                    "description": "比率代碼，例如 personnel_ratio（人事費率）、surplus_ratio（結餘率）",
                }
            },
            "required": ["ratio"],
        },
    },
    {
        "name": "search_live_news",
        "description": "即時上網搜尋該機構的最新新聞報導（Google News）。"
                       "用於查證系統既有資料之外的最新動態，例如剛發生但尚未反映在"
                       "裁罰紀錄中的事件。可自訂補充關鍵字以聚焦特定疑慮。"
                       "此工具會連線外部網站，回應時間較長，僅在需要最新資訊時使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "extra_terms": {
                    "type": "string",
                    "description": "補充查詢關鍵字（選填），例如「不當管教」「食安」「裁罰」",
                }
            },
        },
    },
    {
        "name": "compare_with_peers",
        "description": "取得同類型、同規模的具體同儕機構清單及其關鍵指標，"
                       "用於判斷該機構的數值是同業普遍現象還是個別異常。",
        "input_schema": {
            "type": "object",
            "properties": {
                "top_n": {"type": "integer",
                          "description": "回傳的同儕機構數量，預設 8"},
            },
        },
    },
    {
        "name": "get_data_coverage",
        "description": "取得該機構各風險構面的資料可得性。"
                       "重要：某構面「無資料」不等於「無風險」，"
                       "在下結論與評估信心程度前應先確認哪些構面其實沒有資料支撐。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "submit_report",
        "description": "完成調查後提交最終鑑識分析報告。務必在結束分析時呼叫此工具。",
        "input_schema": {
            "type": "object",
            "properties": {
                "risk_score": {"type": "number", "description": "0-100 綜合風險評分，分數愈高風險愈高"},
                "risk_level": {"type": "string",
                              "enum": ["極高風險", "高風險", "中風險", "低風險", "極低風險"]},
                "summary": {"type": "string", "description": "2-4 句總結研判，說明主要判斷依據"},
                "key_findings": {"type": "array", "items": {"type": "string"},
                                 "description": "具體風險發現，每項需附數字證據"},
                "recommended_actions": {"type": "array", "items": {"type": "string"},
                                        "description": "建議稽查作為"},
                "confidence": {"type": "string", "enum": ["高", "中", "低"],
                               "description": "基於現有公開資料完整度之信心程度"},
            },
            "required": ["risk_score", "risk_level", "summary", "key_findings",
                        "recommended_actions", "confidence"],
        },
    },
]


class AgentError(Exception):
    pass


def _post(api_key: str, body: dict) -> dict:
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": API_VERSION,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise AgentError(f"Claude API 錯誤（HTTP {exc.code}）：{detail}") from exc
    except urllib.error.URLError as exc:
        raise AgentError(f"無法連線至 Claude API：{exc.reason}") from exc


def _fmt_ratio(name: str, v) -> str:
    if v is None:
        return "無資料"
    if name in ("cost_per_student", "revenue_per_student", "personnel_per_student",
                "teaching_per_student"):
        return f"{v:,.0f} 元"
    return f"{v * 100:.1f}%"


def _tool_get_financial_overview(rec: dict) -> dict:
    det = rec["detail"]
    ratio_series = det.get("ratio_series") or []
    latest = ratio_series[-1] if ratio_series else {}
    yoy = det.get("yoy") or {}
    if not ratio_series:
        return {"available": False, "note": "本系統未取得該機構之決算財務資料"}
    return {
        "available": True,
        "fiscal_years": [r.get("fiscal_year") for r in ratio_series],
        "latest_fiscal_year": latest.get("fiscal_year"),
        "latest_total_revenue": latest.get("total_revenue"),
        "latest_total_expense": latest.get("total_expense"),
        "latest_surplus": latest.get("surplus"),
        "ratios": {forensics.RATIO_LABELS.get(k, k): _fmt_ratio(k, latest.get(k))
                  for k in forensics.WATCH_RATIOS},
        "yoy_expense_volatility": yoy.get("expense", {}).get("max_abs_change"),
        "yoy_personnel_volatility": yoy.get("personnel", {}).get("max_abs_change"),
    }


def _tool_get_peer_deviation(rec: dict) -> dict:
    pd = rec["detail"].get("peer_deviation") or []
    if not pd:
        return {"available": False, "note": "無同儕比較資料"}
    return {"available": True, "peer_group": rec.get("peer_key"),
            "deviations": [
                {"ratio": forensics.RATIO_LABELS.get(r["ratio"], r["ratio"]),
                 "value": _fmt_ratio(r["ratio"], r["value"]),
                 "peer_median": _fmt_ratio(r["ratio"], r["peer_median"]),
                 "robust_z": r["z"], "deviation_score_0_100": r["score"],
                 "peer_sample_size": r["peer_n"]}
                for r in pd]}


def _tool_get_forensic_tests(rec: dict) -> dict:
    det = rec["detail"]
    l2 = det.get("last_two") or {}
    rb = det.get("round_bias") or {}
    fd = det.get("digit_conformity") or {}
    return {
        "last_two_digit_test": {
            "sufficient_sample": l2.get("sufficient", False),
            "chi_square": l2.get("chi2"), "p_value": l2.get("p_value"),
            "level": l2.get("level"), "top_concentrated_endings": l2.get("top_endings"),
        } if l2.get("sufficient") else {"sufficient_sample": False},
        "round_number_bias": {
            "sufficient_sample": rb.get("sufficient", False),
            "thousand_ending_ratio": rb.get("ratio_1000"), "level": rb.get("level"),
        } if rb.get("sufficient") else {"sufficient_sample": False},
        "first_digit_conformity_vs_peers": {
            "sufficient_sample": fd.get("sufficient", False),
            "mad_ratio_vs_peer_median": fd.get("adjusted_ratio"), "level": fd.get("level"),
            "note": "此為輔助參考指標，單一機構樣本數有限，須與末兩位數檢定併同研判",
        } if fd.get("sufficient") else {"sufficient_sample": False},
    }


def _tool_get_compliance_history(rec: dict) -> dict:
    det = rec["detail"]
    return {
        "penalties": det.get("penalties") or [],
        "penalty_categories_summary": det.get("penalty_categories") or [],
        "evaluations": det.get("evaluations") or [],
        "note": "若上述皆為空陣列，代表本系統公開資料來源中未取得該機構之裁罰或評鑑紀錄，"
               "不代表機構完全合規。",
    }


def _tool_get_sentiment_summary(rec: dict) -> dict:
    sen = rec["detail"].get("sentiment") or {}
    if not sen.get("n_posts"):
        return {"available": False, "note": "無社群輿情資料"}
    return {
        "available": True, "n_posts": sen.get("n_posts"),
        "negative_ratio": sen.get("neg_ratio"), "sentiment_score_0_100": sen.get("score"),
        "top_negative_topics": [t["topic"] for t in (sen.get("topics") or [])[:3]],
        "burst_detected": (sen.get("burst") or {}).get("burst", False),
        "recent_examples": [ex.get("content") for ex in (sen.get("recent_examples") or [])[:3]],
    }


def _tool_get_ratio_benchmark(rec: dict, recs: list[dict], ratio: str) -> dict:
    pk = rec.get("peer_key")
    pool = [r["features"].get(f"r_{ratio}") for r in recs if r.get("peer_key") == pk]
    pool = stats.clean(pool)
    if len(pool) < 5:
        pool = stats.clean([r["features"].get(f"r_{ratio}") for r in recs])
    if not pool:
        return {"available": False}
    return {
        "available": True, "ratio_label": forensics.RATIO_LABELS.get(ratio, ratio),
        "sample_size": len(pool),
        "min": _fmt_ratio(ratio, min(pool)), "q1": _fmt_ratio(ratio, stats.quantile(pool, 0.25)),
        "median": _fmt_ratio(ratio, stats.median(pool)),
        "q3": _fmt_ratio(ratio, stats.quantile(pool, 0.75)),
        "max": _fmt_ratio(ratio, max(pool)),
        "this_institution_value": _fmt_ratio(ratio, rec["features"].get(f"r_{ratio}")),
    }


def _tool_search_live_news(rec: dict, extra_terms: str = "") -> dict:
    """即時查詢該機構的新聞報導。

    這是本代理人與純特徵查詢的關鍵差異：允許在偵查當下取得系統快取之外的
    最新資訊。裁罰紀錄有公告時間差，新聞往往先揭露事件。
    """
    from .collect import social
    from .collect.http_util import Session

    inst = rec["inst"]
    name = inst.get("name") or ""
    if not name:
        return {"available": False, "note": "缺少機構名稱，無法查詢"}
    try:
        sess = Session(delay=0.4, timeout=25, retry=2, verify_tls=False)
        queries = social.queries_for(name, inst.get("district") or "")
        if extra_terms:
            core = social.short_name(name) or name
            queries.append(f'"{core}" {extra_terms}')
        items: list[dict] = []
        seen: set[str] = set()
        for q in queries:
            for p in social.fetch_news(sess, q, limit=12):
                key = p.get("url") or p["content"][:60]
                if key in seen:
                    continue
                seen.add(key)
                items.append(p)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": f"新聞查詢失敗：{exc}"}

    if not items:
        return {"available": True, "n_found": 0,
                "note": "查無相關新聞報導。對多數小型園所而言這是常態，"
                        "不代表無風險。"}
    items.sort(key=lambda p: p.get("post_date") or "", reverse=True)
    return {
        "available": True,
        "n_found": len(items),
        "queries_used": queries,
        "articles": [{"date": p["post_date"], "source": p["source"],
                      "headline": p["content"][:160]} for p in items[:12]],
        "note": "此為即時查詢結果，尚未經人工核實；報導標題可能同名混淆，"
                "引用時請說明來源與日期。",
    }


def _tool_compare_with_peers(rec: dict, recs: list[dict], top_n: int = 8) -> dict:
    """列出同儕機構及其關鍵指標。"""
    pk = rec.get("peer_key")
    inst = rec["inst"]
    peers = [r for r in recs
             if r.get("peer_key") == pk
             and str(r["inst"]["inst_id"]) != str(inst["inst_id"])]
    if not peers:
        peers = [r for r in recs
                 if r["inst"].get("org_type") == inst.get("org_type")
                 and str(r["inst"]["inst_id"]) != str(inst["inst_id"])]
    if not peers:
        return {"available": False, "note": "找不到同儕機構"}

    def snap(r: dict) -> dict:
        f = r["features"]
        return {
            "name": r["inst"].get("name"),
            "org_type": r["inst"].get("org_type"),
            "district": r["inst"].get("district"),
            "enrolled": r["inst"].get("enrolled"),
            "personnel_ratio": _fmt_ratio("personnel_ratio",
                                         f.get("r_personnel_ratio")),
            "surplus_ratio": _fmt_ratio("surplus_ratio", f.get("r_surplus_ratio")),
            "penalties_4y": f.get("pen_count_4y"),
            "anomaly_score": round(f.get("anomaly_score") or 0, 2),
        }

    peers.sort(key=lambda r: -(r["features"].get("anomaly_score") or 0))
    return {
        "available": True,
        "peer_group": pk,
        "peer_count": len(peers),
        "this_institution": snap(rec),
        "peers_sorted_by_anomaly": [snap(r) for r in peers[:max(1, top_n)]],
    }


# 各構面的資料來源欄位：用於回報「無資料」與「無風險」的差別
_COVERAGE_KEYS = {
    "compliance": ("penalties", "裁罰紀錄"),
    "financial": ("ratio_series", "決算財務資料"),
    "evaluation": ("evaluations", "基礎評鑑紀錄"),
    "sentiment": ("sentiment", "社群與新聞輿情"),
}


def _tool_get_data_coverage(rec: dict) -> dict:
    """回報該機構各構面是否真的有資料支撐。

    這個工具存在的理由：五構面加權評分在某構面無資料時會以缺值處理，
    分數自然偏低，容易讓人誤讀成「該面向沒問題」。要求代理人在下結論前
    先確認覆蓋情形，才能給出誠實的信心程度。
    """
    det = rec["detail"]
    out: dict[str, dict] = {}
    for dim, (key, label) in _COVERAGE_KEYS.items():
        val = det.get(key)
        if key == "sentiment":
            has = bool((val or {}).get("n_posts"))
            detail_txt = f"{(val or {}).get('n_posts', 0)} 則貼文"
        elif key == "ratio_series":
            has = bool(val)
            detail_txt = f"{len(val or [])} 個年度決算"
        else:
            has = bool(val)
            detail_txt = f"{len(val or [])} 筆紀錄"
        out[dim] = {"source": label, "has_data": has, "detail": detail_txt}

    missing = [v["source"] for v in out.values() if not v["has_data"]]
    return {
        "coverage": out,
        "missing_sources": missing,
        "note": ("以下構面查無資料，其分數偏低僅代表『無訊號』而非『已查核無虞』："
                 + "、".join(missing)) if missing else "各構面均有資料支撐。",
    }


def _build_context(rec: dict) -> str:
    inst = rec["inst"]
    return (f"機構名稱：{inst.get('name')}（代碼 {inst.get('inst_id')}）\n"
           f"設立類型：{inst.get('org_type')}　所在地：{inst.get('city')}{inst.get('district') or ''}\n"
           f"請開始你的調查。")


def _execute_tool(name: str, tool_input: dict, rec: dict, recs: list[dict]) -> Any:
    if name == "get_financial_overview":
        return _tool_get_financial_overview(rec)
    if name == "get_peer_deviation":
        return _tool_get_peer_deviation(rec)
    if name == "get_forensic_tests":
        return _tool_get_forensic_tests(rec)
    if name == "get_compliance_history":
        return _tool_get_compliance_history(rec)
    if name == "get_sentiment_summary":
        return _tool_get_sentiment_summary(rec)
    if name == "get_ratio_benchmark":
        return _tool_get_ratio_benchmark(rec, recs, tool_input.get("ratio", ""))
    if name == "search_live_news":
        return _tool_search_live_news(rec, tool_input.get("extra_terms", ""))
    if name == "compare_with_peers":
        return _tool_compare_with_peers(rec, recs,
                                       int(tool_input.get("top_n") or 8))
    if name == "get_data_coverage":
        return _tool_get_data_coverage(rec)
    return {"error": f"unknown tool {name}"}


def investigate(inst_id: str, recs: list[dict], api_key: str,
                model: str = DEFAULT_MODEL,
                on_step: Callable[[str], None] | None = None) -> dict:
    """對單一機構執行完整的 agentic 工具呼叫迴圈，回傳結構化鑑識報告。

    未提供 API 金鑰時改走 `rule_based_review()`（規則式深度檢視）。
    兩者的回傳結構相同，但 `engine` 欄位會標示實際使用的方式，
    前端據此顯示，不會把規則式結果包裝成 AI 推論結果。
    """
    rec = next((r for r in recs if str(r["inst"]["inst_id"]) == str(inst_id)), None)
    if rec is None:
        raise AgentError(f"查無機構 {inst_id}")
    if not api_key:
        if on_step:
            on_step("未設定 API 金鑰，改用規則式深度檢視")
        return rule_based_review(rec, recs)

    messages = [{"role": "user", "content": _build_context(rec)}]
    transcript: list[dict] = []

    for round_i in range(MAX_ROUNDS):
        if on_step:
            on_step(f"第 {round_i + 1} 輪：呼叫 Claude…")
        resp = _post(api_key, {
            "model": model, "max_tokens": 2000, "system": SYSTEM_PROMPT,
            "tools": TOOLS, "messages": messages,
        })
        if resp.get("type") == "error":
            raise AgentError(resp.get("error", {}).get("message", str(resp)))
        content = resp.get("content", [])
        messages.append({"role": "assistant", "content": content})

        tool_uses = [b for b in content if b.get("type") == "tool_use"]
        texts = [b.get("text", "") for b in content if b.get("type") == "text"]
        transcript.append({"role": "assistant", "text": "\n".join(texts),
                           "tool_calls": [{"name": t["name"], "input": t.get("input")}
                                         for t in tool_uses]})

        submit = next((t for t in tool_uses if t["name"] == "submit_report"), None)
        if submit is not None:
            report = dict(submit.get("input") or {})
            report["_transcript"] = transcript
            report["_model"] = model
            report["engine"] = "claude"
            report["engine_label"] = f"Claude AI 代理人（{model}）"
            report["tools_used"] = sorted({
                c["name"] for t in transcript for c in t.get("tool_calls", [])
            })
            return report

        if not tool_uses:
            # 模型未呼叫任何工具也未提交報告：視為對話結束但缺乏結構化結果
            raise AgentError("Claude 未提交結構化報告（可能中途结束），請重試。")

        tool_results = []
        for t in tool_uses:
            if on_step:
                on_step(f"查詢：{t['name']}")
            try:
                result = _execute_tool(t["name"], t.get("input") or {}, rec, recs)
            except Exception as exc:  # noqa: BLE001
                result = {"error": str(exc)}
            tool_results.append({
                "type": "tool_result", "tool_use_id": t["id"],
                "content": json.dumps(result, ensure_ascii=False, default=str),
            })
            transcript.append({"role": "tool", "name": t["name"], "result": result})
        messages.append({"role": "user", "content": tool_results})

    raise AgentError(f"超過最大輪數（{MAX_ROUNDS}）仍未取得最終報告。")


# ---------------------------------------------------------------- 規則式檢視
# 未設定 API 金鑰時的替代路徑。刻意**不**冒充 AI 推論：它做的是把系統既有的
# 統計與鑑識檢定結果整理成同一份報告格式，並標示 engine="rules"，
# 讓使用者清楚知道這份結論來自明文規則而非語言模型。
_LEVEL_WEIGHT = {"critical": 40, "high": 22, "medium": 10, "low": 4}

_LEVEL_LABEL = {"critical": "重大", "high": "高", "medium": "中", "low": "低"}


def rule_based_review(rec: dict, recs: list[dict] | None = None) -> dict:
    """以既有特徵與鑑識檢定結果產生結構化檢視報告（不呼叫語言模型）。"""
    from . import scoring
    from .config import DEFAULT_WEIGHTS

    coverage = _tool_get_data_coverage(rec)
    # 把可得性一併傳給 explain()，讓報告中的構面權重與儀表板一致：
    # 查無資料的構面應顯示為 available=False、權重 0，而不是列出一個
    # 看似正常但實際上沒有資料支撐的 0 分。
    available = {k: v["has_data"]
                 for k, v in (coverage.get("coverage") or {}).items()}
    dims = scoring.dimension_scores(rec["features"])
    exp = scoring.explain(rec, dims, DEFAULT_WEIGHTS, None, available)
    reasons = exp["reasons"]
    inst = rec["inst"]

    # 風險分數：以各項發現的嚴重度累加，並以 100 為上限。
    # 與主評分流程的加權分數不同——這裡衡量的是「具體發現的嚴重程度」，
    # 而非在族群中的相對位置，兩者互為參照。
    raw = sum(_LEVEL_WEIGHT.get(r["level"], 0) for r in reasons)
    score = min(100.0, raw)

    if any(r["level"] == "critical" for r in reasons) or score >= 70:
        level = "極高風險"
    elif score >= 45:
        level = "高風險"
    elif score >= 22:
        level = "中風險"
    elif score > 0:
        level = "低風險"
    else:
        level = "極低風險"

    missing = coverage.get("missing_sources") or []
    n_dims = len(_COVERAGE_KEYS)
    if len(missing) >= n_dims - 1:
        confidence = "低"
    elif missing:
        confidence = "中"
    else:
        confidence = "高"

    top = [f"{_LEVEL_LABEL.get(r['level'], '')}｜{r['title']}：{r['text']}"
           for r in reasons[:8]]

    if reasons:
        summary = (
            f"{inst.get('name')}（{inst.get('org_type')}，{inst.get('district')}）"
            f"共辨識出 {len(reasons)} 項風險訊號，其中最嚴重者為"
            f"「{reasons[0]['title']}」。"
        )
    else:
        summary = (
            f"{inst.get('name')}（{inst.get('org_type')}，{inst.get('district')}）"
            "在現有公開資料中未觸發任何風險規則。"
        )
    if missing:
        summary += (f"惟下列資料來源查無內容：{'、'.join(missing)}，"
                    "上述判讀僅涵蓋有資料支撐的面向。")

    actions: list[str] = []
    if any(r["level"] == "critical" for r in reasons):
        actions.append("列入即時稽查名單，於一週內完成實地查訪。")
    for r in reasons:
        if r["level"] not in ("critical", "high"):
            continue
        t = r["title"]
        if "生師比" in t:
            actions.append("調閱各班幼生名冊與教保人員排班表，"
                           "分齡核算兩歲專班與三至五歲班之師生比。")
        elif "超收" in t:
            actions.append("比對核定人數與實際在園幼生名冊，查核是否超收。")
        elif "裁罰" in t or "重複違規" in t:
            actions.append("追蹤前次裁罰之改善計畫執行情形，確認已具體落實。")
        elif "末兩位數" in t or "整數偏誤" in t:
            actions.append("抽查該年度大額支出之原始憑證與付款紀錄，"
                           "確認金額是否為實際支出而非估列。")
        elif "同儕" in t:
            actions.append("就偏離同儕之費用科目要求說明並提供支持文件。")
        elif "交叉核對" in t:
            actions.append("核對公告收費標準、實際收費收據與決算收入三者一致性。")
        elif "輿情" in t:
            actions.append("查閱相關陳情案件處理紀錄，必要時進行家長訪談。")
        elif "評鑑" in t:
            actions.append("確認基礎評鑑待改善項目之追蹤複評結果。")
    if missing:
        actions.append(f"補齊缺漏之資料來源（{'、'.join(missing)}）後再行評估。")
    if not actions:
        actions.append("維持常態管理，依既定週期辦理例行查核。")

    # 去重但保留順序
    seen: set[str] = set()
    uniq_actions = [a for a in actions if not (a in seen or seen.add(a))]

    return {
        "risk_score": round(score, 1),
        "risk_level": level,
        "summary": summary,
        "key_findings": top or ["現有公開資料未觸發任何風險規則。"],
        "recommended_actions": uniq_actions,
        "confidence": confidence,
        "engine": "rules",
        "engine_label": "規則式深度檢視（未使用語言模型）",
        "data_coverage": coverage,
        "dimension_scores": dims,
        "dimensions": exp["dimensions"],
        "_transcript": [{
            "role": "system",
            "text": "本報告由明文規則彙整系統既有的統計與鑑識檢定結果產生，"
                    "未呼叫語言模型。設定 Claude API 金鑰後可改用 AI 代理人，"
                    "取得自主調閱證據與情境化研判。",
        }],
    }
