"""全國教保資訊網（ap.ece.moe.edu.tw/webecems/）採集器。

抓取四類公開資料：
1. 裁罰紀錄查詢  punishSearch.aspx   → penalties（含每筆處分明細）
2. 評鑑結果查詢  evaSearch.aspx      → evaluations（評鑑學年度／結果）
3. 基本資料查詢  pubSearch.aspx      → institutions（擴充分析母體）
4. 未立案裁罰    unRUnitSearch.aspx  → 未立案機構裁罰（參考用）

實作要點
--------
* 站台為 ASP.NET WebForms，查詢與翻頁以 __doPostBack 驅動，需維持 cookie
  session 並逐次帶回 __VIEWSTATE / __EVENTVALIDATION。
* 三個查詢頁共用同一套 GridView1 版面，且每個欄位都有穩定的 element id
  （如 GridView1_lblSchName_0、GridView1_lblPub_0）。因此解析一律以
  **id 錨點**取值，而非依賴 <tr>/<td> 結構——後者會被巢狀表格（評鑑紀錄
  內嵌於機構列中）破壞。
* 裁罰明細不是 postback，而是 window.open 開啟獨立頁
  `dtl/punish_view.aspx?sch=<加密識別碼>`，可直接 GET，不影響清單分頁狀態。
"""
from __future__ import annotations

import re

from .http_util import (Session, has_postback, hidden_fields, postback_payload,
                        select_options, strip_tags, unescape)

BASE = "https://ap.ece.moe.edu.tw/webecems/"
PUNISH_URL = BASE + "punishSearch.aspx"      # 裁罰紀錄查詢
EVAL_URL = BASE + "evaSearch.aspx"           # 評鑑結果查詢
PRESCHOOL_URL = BASE + "pubSearch.aspx"      # 基本資料查詢
UNREG_URL = BASE + "unRUnitSearch.aspx"      # 未立案機構裁罰查詢

CITY_NEW_TAIPEI = "03"
_NEXT_TARGET = "PageControl1$lbNextPage"

# 設立別 checkbox：勾選後才會納入該類機構（全不勾＝不限）
ORG_CHECKBOXES = {"公立": "ckSPub0", "私立": "ckSPub1",
                  "非營利": "ckSPub2", "準公共": "ckSPub3"}


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", s))).strip()


# ---------------------------------------------------------------- id 取值
def _row_indices(html: str) -> list[int]:
    """回傳當頁所有機構列的索引（以機構名稱欄位的 id 為準）。"""
    return sorted({int(m) for m in
                   re.findall(r'id="GridView1_lblSchName_(\d+)"', html)})


def _field(html: str, field: str, idx: int) -> str:
    """依 element id 取出單一欄位文字。"""
    m = re.search(rf'id="GridView1_{field}_{idx}"[^>]*>(.*?)</(?:span|a|div|h4)>',
                  html, re.S)
    return _clean(m.group(1)) if m else ""


def _row_block(html: str, idx: int, indices: list[int]) -> str:
    """取出第 idx 列的 HTML 片段（用於搜尋該列專屬的連結／巢狀表格）。"""
    start = html.find(f'id="GridView1_lblSchName_{idx}"')
    if start < 0:
        return ""
    nxt = indices[indices.index(idx) + 1] if idx != indices[-1] else None
    end = (html.find(f'id="GridView1_lblSchName_{nxt}"')
           if nxt is not None else len(html))
    return html[start:end if end > start else len(html)]


def _base_institution(html: str, idx: int) -> dict:
    """解析機構基本欄位（三個查詢頁共用）。"""
    return {
        "name": _field(html, "lblSchName", idx),
        "city": _field(html, "lblCity", idx),
        "district": _field(html, "lblArea", idx),
        "org_type": _field(html, "lblPub", idx),
        "address": _field(html, "hlAddr", idx),
        "phone": _field(html, "lblTel", idx),
        "website": _field(html, "hlUrl", idx),
        "approved_capacity": _field(html, "lblGenStd", idx),
        "child_service": _field(html, "lblChildSvc", idx),
        "status": _field(html, "lblStatus", idx) or _field(html, "lblSchStatus", idx),
    }


# ---------------------------------------------------------------- 查詢與翻頁
def city_options(sess: Session, url: str = PUNISH_URL) -> list[tuple[str, str]]:
    """回傳查詢頁的縣市下拉選項。"""
    return select_options(sess.get(url), "ddlCityS")


def _build_query(html: str, city_code: str,
                 org_types: tuple[str, ...] = ()) -> dict[str, str]:
    """依查詢頁的實際欄位組出查詢 payload。"""
    payload = hidden_fields(html)
    payload["ddlCityS"] = city_code
    payload["ddlAreaS"] = ""
    # 下拉選單一律從頁面實際選項中挑值：ASP.NET 會做事件驗證，送出
    # 選項清單中不存在的值（例如裁罰頁的 ddlKey 並沒有空字串選項）
    # 會直接回 HTTP 500。
    for sel in ("ddlKey", "ddlEResult"):
        if f'name="{sel}"' not in html:
            continue
        opts = [v for v, _ in select_options(html, sel)]
        if not opts:
            continue
        payload[sel] = "" if "" in opts else opts[0]
    for f in ("txtKeyNameS", "txtSchNameS"):
        if f'name="{f}"' in html:
            payload[f] = ""
    # 設立別：明確指定時逐一勾選；未指定則全不勾（＝不限）
    for ot in org_types:
        cb = ORG_CHECKBOXES.get(ot)
        if cb and f'name="{cb}"' in html:
            payload[cb] = "on"
    payload["btnSearch"] = "搜尋"
    return payload


def _post_with_refresh(sess: Session, url: str, payload: dict,
                       rebuild, attempts: int = 3) -> str:
    """送出 postback；遇 HTTP 500 時重新取得表單狀態後再試。

    此站台的 __VIEWSTATE / __EVENTVALIDATION 具時效與一次性特性，
    逾時或狀態不符時會回 500 而非導向錯誤頁，單純重送同一份 payload
    無法恢復，必須重新 GET 頁面取得新的隱藏欄位。
    """
    import urllib.error
    last: Exception | None = None
    for i in range(attempts):
        try:
            return sess.post(url, payload, referer=url)
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code != 500 or i == attempts - 1:
                raise
            payload = rebuild()
    raise last if last else RuntimeError(f"查詢失敗：{url}")


def _search_pages(sess: Session, url: str, city_code: str, max_pages: int,
                  org_types: tuple[str, ...] = (), on_progress=None,
                  label: str = ""):
    """送出查詢並逐頁 yield 結果頁 HTML。"""
    def fresh_query() -> dict[str, str]:
        return _build_query(sess.get(url), city_code, org_types)

    form_html = sess.get(url)
    html = _post_with_refresh(sess, url,
                              _build_query(form_html, city_code, org_types),
                              fresh_query)

    for page in range(1, max_pages + 1):
        if not _row_indices(html):
            break
        if on_progress:
            on_progress(f"{label} 第 {page} 頁：{len(_row_indices(html))} 筆")
        yield html
        if not has_postback(html, _NEXT_TARGET):
            break
        cur = html
        html = _post_with_refresh(
            sess, url, postback_payload(cur, _NEXT_TARGET),
            lambda: postback_payload(sess.get(url), _NEXT_TARGET))


# ---------------------------------------------------------------- 裁罰紀錄
_VIEW_URL_RE = re.compile(
    r"window\.open\(&#39;\.?/?(dtl/punish_view\.aspx\?sch=[^&']+)&#39;")

_DATE_RE = re.compile(r"(\d{2,4})[./年-](\d{1,2})[./月-](\d{1,2})")
_MONEY_RE = re.compile(r"([\d,]{3,})\s*元")
_LAW_RE = re.compile(r"第\s*\d+\s*條(?:之\d+)?(?:第\s*\d+\s*項)?(?:第\s*\d+\s*款)?")
_ROW_SPLIT_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)


def _to_iso(s: str) -> str:
    """把民國／西元各式日期轉為 ISO 格式。"""
    m = _DATE_RE.search(s)
    if not m:
        return ""
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 1911:
        y += 1911
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return ""
    return f"{y:04d}-{mo:02d}-{d:02d}"


# 明細表表頭（2026 年版）：
#   處分日期 | 處分時園名 | 裁處文號 | 處分依據 | 違反之規定 | 負責人/行為人 | 處分內容
_HEADER_KEYS = ("處分日期", "裁處文號", "處分依據", "違反之規定", "處分內容")


def _map_header(header: list[str]) -> dict[str, int]:
    """表頭文字 → 標準欄位索引。

    以完整詞優先比對，避免『處分時園名』『裁處文號』這類同樣含
    「處分／裁處」的欄位搶到 disposition 的位置。
    """
    idx: dict[str, int] = {}
    for i, h in enumerate(header):
        h = h.strip()
        if "處分日期" in h or ("日期" in h and "公" not in h):
            idx.setdefault("penalty_date", i)
        elif "公告" in h or "公布" in h:
            idx.setdefault("published_date", i)
        elif "園名" in h:
            idx.setdefault("name_at_penalty", i)
        elif "文號" in h:
            idx.setdefault("doc_no", i)
        elif "處分依據" in h or "依據" in h or "法條" in h:
            idx.setdefault("law_article", i)
        elif "違反" in h:
            idx.setdefault("violation", i)
        elif "負責人" in h or "行為人" in h or "對象" in h:
            idx.setdefault("target", i)
        elif "處分內容" in h or "內容" in h:
            idx.setdefault("disposition", i)
        elif "金額" in h or "罰鍰" in h:
            idx.setdefault("fine_amount", i)
    return idx


def _parse_punish_detail(html: str, inst_name: str) -> list[dict]:
    """解析裁罰明細頁（punish_view.aspx）。

    罰鍰金額不是獨立欄位，而寫在「處分內容」中（例「罰鍰：50,000 元」，
    亦可能為「停止招生」「廢止設立許可」等非金錢處分），故以正規表示式
    從處分內容抽取；抽不到即視為無罰鍰之處分，而非 0 元。
    """
    records: list[dict] = []
    for tbl in re.findall(r"<table[^>]*>(.*?)</table>", html, re.S):
        rows = _ROW_SPLIT_RE.findall(tbl)
        if len(rows) < 2:
            continue

        head_i, header = -1, []
        for i, r in enumerate(rows):
            cells = [_clean(c) for c in re.findall(
                r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)]
            if len(cells) >= 4 and sum(
                    1 for k in _HEADER_KEYS if any(k in c for c in cells)) >= 2:
                head_i, header = i, cells
                break
        if head_i < 0:
            continue
        idx = _map_header(header)

        for r in rows[head_i + 1:]:
            cells = [_clean(c) for c in re.findall(
                r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)]
            if not any(cells) or "查無" in " ".join(cells):
                continue
            joined = " ".join(cells)

            def cell(key: str) -> str:
                i = idx.get(key, -1)
                return cells[i] if 0 <= i < len(cells) else ""

            pdate = _to_iso(cell("penalty_date")) or _to_iso(joined)
            if not pdate:
                continue

            disposition = cell("disposition") or joined
            mm = _MONEY_RE.search(disposition)
            fine = mm.group(1).replace(",", "") if mm else ""

            law = cell("law_article")
            if not law:
                lm = _LAW_RE.search(joined)
                law = lm.group(0) if lm else ""

            violation = cell("violation")
            records.append({
                "name": inst_name,
                "name_at_penalty": cell("name_at_penalty") or inst_name,
                "penalty_date": pdate,
                "published_date": _to_iso(cell("published_date")),
                "doc_no": cell("doc_no"),
                "law_article": law,
                "violation": violation,
                "fine_amount": fine,
                "disposition": disposition,
                "description": violation or joined[:300],
                "target": re.sub(r"^(負責人|行為人)\s*[:：]\s*", "",
                                 cell("target")).strip(),
            })
    return records


# ---------------------------------------------------------------- 違規分類
# 對應 config.PENALTY_CATEGORIES，使法遵構面能依「對兒童權益的衝擊程度」
# 加權，而非僅計件數或罰鍰金額。列表順序即優先序。
_CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("兒少保護事件", ("身心虐待", "兒童及少年福利與權益保障法", "性騷擾", "性侵",
                 "妨害幼兒身心", "傷害幼兒", "兒少保護", "第30條")),
    ("不當管理行為", ("不當管教", "不當對待", "體罰", "教保服務人員條例",
                 "妨害人格發展", "言語脅迫", "處罰幼兒")),
    ("師生比不足", ("師生比", "教保服務人員配置", "第16條", "編班", "年齡規定",
                "未依規定配置")),
    ("超收幼生", ("超收", "招收人數超過", "逾核定人數", "超過核定")),
    ("人員資格不符", ("資格不符", "未具教保服務人員資格", "不得擔任",
                 "消極資格", "未依規定進用", "教職員資料", "第15條")),
    ("設施安全缺失", ("建築物", "消防", "公共安全", "設施設備", "空間不符",
                 "使用執照", "逃生")),
    ("餐飲衛生違規", ("食品", "餐飲", "衛生", "膳食", "食安", "廚房")),
    ("收退費違規", ("收退費", "收費", "退費", "超收費用", "未依公告收費",
                "代辦費", "第38條")),
    ("未依法通報", ("未通報", "通報義務", "未依規定通報", "知悉未報")),
]


def classify_violation(text: str) -> str:
    """依裁罰文字判斷違規類別；無法判斷時歸為『行政管理缺失』。"""
    t = re.sub(r"\s+", "", str(text or ""))
    if not t:
        return "行政管理缺失"
    for cat, kws in _CATEGORY_RULES:
        if any(k.replace(" ", "") in t for k in kws):
            return cat
    return "行政管理缺失"


def fetch_penalties(sess: Session, city_code: str = CITY_NEW_TAIPEI,
                    max_pages: int = 200, with_detail: bool = True,
                    on_progress=None) -> tuple[list[dict], list[dict]]:
    """抓取指定縣市的裁罰紀錄。

    回傳 (penalties, institutions)：
    * penalties    — 逐筆處分明細（含機構名稱，由呼叫端對應 inst_id）
    * institutions — 受裁罰機構之基本資料（可用於擴充分析母體）
    """
    all_pen: list[dict] = []
    all_inst: list[dict] = []
    seen: set[str] = set()

    for html in _search_pages(sess, PUNISH_URL, city_code, max_pages,
                              on_progress=on_progress, label="裁罰紀錄"):
        indices = _row_indices(html)
        for idx in indices:
            inst = _base_institution(html, idx)
            nm = inst["name"]
            if not nm or nm in seen:
                continue
            seen.add(nm)
            all_inst.append(inst)

            if not with_detail:
                continue
            block = _row_block(html, idx, indices)
            m = _VIEW_URL_RE.search(block)
            if not m:
                continue
            try:
                detail = sess.get(BASE + unescape(m.group(1)), referer=PUNISH_URL)
                recs = _parse_punish_detail(detail, nm)
                for rec in recs:
                    rec["category"] = classify_violation(
                        f"{rec.get('violation', '')} {rec.get('law_article', '')} "
                        f"{rec.get('disposition', '')}")
                all_pen.extend(recs)
                if on_progress:
                    on_progress(f"    {nm}：{len(recs)} 筆處分")
            except Exception as exc:  # noqa: BLE001
                if on_progress:
                    on_progress(f"    {nm}：明細擷取失敗（{exc}）")

    return all_pen, all_inst


# ---------------------------------------------------------------- 評鑑結果
_EVAL_TABLE_RE = "GridView1_GridView11_{idx}"


def _parse_eval_records(block: str, idx: int) -> list[dict]:
    """解析單一機構列中內嵌的評鑑紀錄表。

    表頭固定為：評鑑學年度 | 評鑑完成日 | 評鑑結果 | 評鑑報告
    其中「評鑑完成日」與「評鑑結果」有穩定 id（lblDate_M / lblResult_M），
    學年度則以列內第一個純數字欄位取得。
    """
    m = re.search(rf'id="{_EVAL_TABLE_RE.format(idx=idx)}"[^>]*>(.*?)</table>',
                  block, re.S)
    if not m:
        return []
    tbl = m.group(1)
    out: list[dict] = []
    for row in _ROW_SPLIT_RE.findall(tbl):
        dm = re.search(r'id="[^"]*lblDate_(\d+)"[^>]*>(.*?)</span>', row, re.S)
        rm = re.search(r'id="[^"]*lblResult_\d+"[^>]*>(.*?)</span>', row, re.S)
        if not (dm or rm):
            continue
        cells = [_clean(c) for c in re.findall(
            r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]
        year = ""
        for c in cells:
            digits = re.sub(r"[^\d]", "", c)
            if digits and 2 <= len(digits) <= 4 and "/" not in c:
                year = digits
                break
        result = _clean(rm.group(1)) if rm else ""
        out.append({
            "eval_year": year,
            "eval_date": _to_iso(_clean(dm.group(2))) if dm else "",
            "result": result,
        })
    return out


def eval_flags(result: str) -> tuple[int, int]:
    """由評鑑結果文字判斷 (是否存在未通過指標, 是否屬追蹤複評)。

    站台實際出現的結果字樣有三類：
      * 「基礎評鑑－全數指標通過」→ 全數通過
      * 「基礎評鑑－部分指標通過」→ 僅部分通過，即有指標未通過（須追蹤）
      * 「追蹤評鑑－全數指標通過」→ 係前次未全數通過後的追蹤複評結果

    注意「部分指標通過」字面雖含「通過」，實質是未全數通過，必須判為
    有缺失；若只用「未通過」比對會漏判。站台未提供待改善項目數，
    因此僅回傳旗標，不臆測數量。
    """
    t = re.sub(r"\s+", "", str(result or ""))
    if not t:
        return 0, 0
    followup = 1 if "追蹤評鑑" in t else 0
    if "全數指標通過" in t:
        return 0, followup
    if "部分" in t or "未通過" in t or "不通過" in t or "待改善" in t:
        return 1, 1
    return 0, followup


def fetch_evaluations(sess: Session, city_code: str = CITY_NEW_TAIPEI,
                      max_pages: int = 400,
                      on_progress=None) -> tuple[list[dict], list[dict]]:
    """抓取基礎評鑑結果。

    回傳 (evaluations, institutions)：評鑑紀錄逐筆展開（一機構可有多年度），
    同時回傳機構基本資料（此頁涵蓋全部設立別，是取得完整母體的主要來源）。
    """
    evals: list[dict] = []
    insts: list[dict] = []
    seen: set[str] = set()

    for html in _search_pages(sess, EVAL_URL, city_code, max_pages,
                              on_progress=on_progress, label="評鑑結果"):
        indices = _row_indices(html)
        for idx in indices:
            inst = _base_institution(html, idx)
            nm = inst["name"]
            if not nm:
                continue
            if nm not in seen:
                seen.add(nm)
                insts.append(inst)
            block = _row_block(html, idx, indices)
            for rec in _parse_eval_records(block, idx):
                failed, followup = eval_flags(rec["result"])
                evals.append({
                    "name": nm,
                    "city": inst["city"],
                    "district": inst["district"],
                    "org_type": inst["org_type"],
                    "eval_year": rec["eval_year"],
                    "eval_date": rec["eval_date"],
                    "result": rec["result"],
                    "items_failed": failed,
                    "followup_required": followup,
                })
    return evals, insts


# ---------------------------------------------------------------- 基本資料
def fetch_institutions(sess: Session, city_code: str = CITY_NEW_TAIPEI,
                       max_pages: int = 500, org_types: tuple[str, ...] = (),
                       on_progress=None) -> list[dict]:
    """抓取幼兒園基本資料（擴充分析母體，含公立／私立／非營利／準公共）。"""
    out: list[dict] = []
    seen: set[str] = set()
    for html in _search_pages(sess, PRESCHOOL_URL, city_code, max_pages,
                              org_types=org_types, on_progress=on_progress,
                              label="基本資料"):
        for idx in _row_indices(html):
            inst = _base_institution(html, idx)
            nm = inst["name"]
            if nm and nm not in seen:
                seen.add(nm)
                out.append(inst)
    return out
