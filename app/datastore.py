"""資料層：彈性匯入器。

本系統不使用任何虛構／模擬資料。支援兩種真實資料來源，優先順序為
使用者資料 > 遠端(AWS)：

1. 本機 data/ 資料夾：CSV / TSV / JSON / XLSX（XLSX 以標準函式庫 zipfile+ElementTree 解析）
2. 遠端 HTTPS / AWS S3 公開或預簽章 URL（data/manifest.json 或設定頁指定）

找不到真實資料時，相關資料表一律留空並於前端明確提示，不會以生成的示範
資料頂替。

匯入器具備：
* 中文欄名別名映射（直接吃政府公開檔案的原始表頭）
* 編碼自動偵測（utf-8-sig / cp950 / big5 / utf-16）
* 民國年 ↔ 西元年自動換算、金額字串清理（千分位、全形、括號負數）
"""
from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
import urllib.request
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET

from . import config
from .config import DATA_DIR

# ---------------------------------------------------------------- 欄位別名
# key = 標準欄名，value = 可能出現在公開資料中的別名（比對時會忽略空白與符號）
ALIASES: dict[str, list[str]] = {
    "inst_id": ["機構代碼", "園所代碼", "幼兒園代碼", "統一編號", "id", "code", "instid", "園所編號", "機構統編"],
    "name": ["機構名稱", "園所名稱", "幼兒園名稱", "名稱", "instname", "園名"],
    "city": ["縣市", "縣市別", "所在縣市", "city", "地區"],
    "district": ["鄉鎮市區", "區域", "行政區", "鄉鎮", "district"],
    "org_type": ["設立類型", "園所類型", "機構類型", "屬性", "type", "orgtype", "設立別"],
    # 組織型態：獨立幼兒園／國小附設／國中附設／職場互助教保服務中心…
    # 與設立別（公立、私立、非營利、準公共）是兩個不同的分類軸；
    # 未列入別名表的欄位會在 normalize() 階段被丟棄，故必須在此宣告。
    "inst_kind": ["組織型態", "機構型態", "機構種類", "instkind"],
    "found_year": ["設立年", "核准設立年", "成立年", "foundyear", "設立日期"],
    "classes": ["班級數", "核定班數", "班數", "classes"],
    "approved_capacity": ["核定招收人數", "核定人數", "招收人數", "capacity", "核定總人數"],
    "enrolled": ["實際招收人數", "現有幼生數", "在園幼生數", "招收現況", "enrolled", "幼生人數"],
    "teacher_count": ["教保服務人員數", "教師人數", "教保員人數", "teachers", "師資人數"],
    "staff_count": ["員工人數", "職員人數", "總員額", "staff"],
    # 兩歲專班與三至五歲班之人數／教保員數須分別列出（幼照法第16條師生比規定不同，
    # 不可用全園混合平均互相稀釋），對齊全國教保資訊網之分齡欄位。
    "classes_age2": ["兩歲專班班級數", "兩歲班班級數", "2歲專班班級數"],
    "enrolled_age2": ["兩歲專班幼生數", "兩歲專班人數", "兩歲以上未滿三歲人數", "2歲專班人數"],
    "teacher_count_age2": ["兩歲專班教保服務人員數", "兩歲專班教保員數", "兩歲專班教師數"],
    "enrolled_age35": ["三至五歲幼生數", "三足歲以上未滿入國小人數", "三歲以上人數"],
    "teacher_count_age35": ["三至五歲教保服務人員數", "三至五歲教保員數", "三歲以上教保員數"],
    "principal": ["負責人", "園長", "代表人", "principal"],
    "address": ["地址", "機構地址", "園所地址", "address"],
    "phone": ["電話", "聯絡電話", "phone", "tel"],

    "penalty_date": ["違規日期", "裁處日期", "處分日期", "查獲日期", "日期", "date"],
    "published_date": ["公布日期", "公告日期", "刊登日期"],
    "category": ["違規類別", "裁罰類別", "事由類別", "違反事項", "類別"],
    "law_article": ["法條", "違反法條", "違反條款", "依據法條"],
    "fine_amount": ["罰鍰金額", "罰款金額", "處分金額", "罰鍰", "金額"],
    "disposition": ["處分內容", "處理情形", "裁處方式", "處分種類"],
    "description": ["違規事實", "說明", "裁罰事由", "事實摘要", "備註"],

    "eval_year": ["評鑑年度", "評鑑學年度", "年度"],
    "result": ["評鑑結果", "結果", "評鑑等第", "等第"],
    "items_failed": ["待改善項目數", "未通過項目數", "缺失項目數"],
    "followup_required": ["需追蹤", "追蹤複評", "是否追蹤"],
    "score": ["評鑑分數", "分數", "得分"],

    "school_year": ["學年度", "收費學年度", "年度"],
    "tuition": ["學費", "月費", "學費收費"],
    "misc_fee": ["雜費"],
    "meal_fee": ["餐點費", "點心費", "午餐費", "餐費"],
    "transport_fee": ["交通費", "娃娃車費"],
    "material_fee": ["材料費", "活動費", "學用品費"],
    "other_fee": ["其他費用", "代辦費", "課後延托費"],
    "months": ["收費月數", "月數"],
    "declared_extra_items": ["額外收費項目數", "未公告收費項目數"],

    "fiscal_year": ["會計年度", "決算年度", "年度", "fiscalyear"],
    "students_avg": ["平均幼生數", "在園人數", "幼生數"],
    "revenue_tuition": ["學雜費收入", "學費收入", "家長繳費收入"],
    "revenue_subsidy": ["政府補助收入", "補助收入", "公款補助"],
    "revenue_other": ["其他收入", "雜項收入"],
    "total_revenue": ["收入合計", "本期收入", "收入總計", "歲入合計"],
    "expense_personnel": ["人事費", "人事支出", "薪資支出", "用人費用"],
    "expense_teaching": ["教學費", "教學支出", "教材費", "業務費"],
    "expense_meal": ["餐點費支出", "膳食費", "食材費"],
    "expense_facility": ["設備及維護費", "設備費", "維護費", "資本支出"],
    "expense_rent": ["租金支出", "房租", "租賃費"],
    "expense_admin": ["行政管理費", "管理費用", "辦公費"],
    "expense_other": ["其他支出", "雜項支出"],
    "total_expense": ["支出合計", "本期支出", "支出總計", "歲出合計"],
    "surplus": ["賸餘", "結餘", "本期賸餘", "餘絀", "本期餘絀"],

    "account": ["科目", "會計科目", "項目", "款項", "科目名稱"],
    "kind": ["收支別", "類型", "屬性"],
    "amount": ["金額", "決算數", "決算金額", "實支數"],
    "period": ["期別", "分期", "季別", "月份"],

    "post_id": ["貼文編號", "編號", "id"],
    "source": ["來源", "平台", "資料來源"],
    "post_date": ["發文日期", "日期", "時間", "發布日期"],
    "content": ["內容", "貼文內容", "文本", "留言", "文字"],
    "engagement": ["互動數", "按讚數", "熱度", "回覆數"],

    "change_date": ["異動日期", "生效日期", "日期"],
    "change_type": ["異動類別", "異動別", "類型"],
    "role": ["職稱", "身分", "職務"],
}

TABLES: dict[str, dict[str, Any]] = {
    "institutions": {
        "patterns": ["institution", "基本資料", "園所", "機構"],
        "key": "inst_id",
        "ints": ["found_year", "classes", "approved_capacity", "enrolled",
                 "teacher_count", "staff_count", "classes_age2", "enrolled_age2",
                 "teacher_count_age2", "enrolled_age35", "teacher_count_age35"],
    },
    "penalties": {
        "patterns": ["penalt", "裁罰", "處分", "違規"],
        "ints": ["fine_amount"],
        "dates": ["penalty_date", "published_date"],
    },
    "evaluations": {
        "patterns": ["evaluation", "評鑑"],
        "ints": ["eval_year", "items_failed", "followup_required"],
        "floats": ["score"],
    },
    "fees": {
        "patterns": ["fee", "收費"],
        "ints": ["school_year", "tuition", "misc_fee", "meal_fee",
                 "transport_fee", "material_fee", "other_fee", "months",
                 "declared_extra_items"],
    },
    "financials": {
        "patterns": ["financial", "決算", "財務", "budget"],
        "ints": ["fiscal_year", "students_avg", "revenue_tuition", "revenue_subsidy",
                 "revenue_other", "total_revenue", "expense_personnel", "expense_teaching",
                 "expense_meal", "expense_facility", "expense_rent", "expense_admin",
                 "expense_other", "total_expense", "surplus"],
    },
    "ledger": {
        "patterns": ["ledger", "科目", "明細"],
        "ints": ["fiscal_year", "amount", "period"],
    },
    "posts": {
        "patterns": ["post", "輿情", "社群", "sentiment"],
        "ints": ["engagement"],
        "dates": ["post_date"],
    },
    "staff_changes": {
        "patterns": ["staff", "異動", "人員"],
        "dates": ["change_date"],
    },
}

_NORM_RE = re.compile(r"[\s_\-()（）\[\]／/:：.．、]+")


def _norm_key(s: str) -> str:
    s = unicodedata.normalize("NFKC", str(s or "")).strip().lower()
    return _NORM_RE.sub("", s)


_ALIAS_INDEX: dict[str, str] = {}
for std, alist in ALIASES.items():
    _ALIAS_INDEX[_norm_key(std)] = std
    for a in alist:
        _ALIAS_INDEX.setdefault(_norm_key(a), std)


def map_header(h: str) -> str | None:
    return _ALIAS_INDEX.get(_norm_key(h))


# ---------------------------------------------------------------- 值清理
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def to_number(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = unicodedata.normalize("NFKC", str(v)).strip()
    if not s or s in {"-", "--", "－", "無", "N/A", "na", "null"}:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    s = s.replace(",", "").replace("$", "").replace("元", "").replace("NT", "")
    s = s.replace("%", "")
    m = _NUM_RE.search(s)
    if not m:
        return None
    val = float(m.group())
    return -val if neg else val


def to_int(v: Any) -> int | None:
    n = to_number(v)
    return None if n is None else int(round(n))


def to_year(v: Any) -> int | None:
    """民國年自動轉西元年（<1911 視為民國）。"""
    n = to_int(v)
    if n is None:
        return None
    if n < 200:
        return n + 1911
    return n


_DATE_PATTERNS = ["%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d", "%Y-%m-%dT%H:%M:%S"]


def to_date(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d")
    s = unicodedata.normalize("NFKC", str(v)).strip()
    if not s:
        return None
    s = s.replace("年", "-").replace("月", "-").replace("日", "").strip("-")
    for p in _DATE_PATTERNS:
        try:
            return datetime.strptime(s, p).strftime("%Y-%m-%d")
        except ValueError:
            pass
    m = re.match(r"^(\d{2,4})\D+(\d{1,2})\D+(\d{1,2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 200:
            y += 1911
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------- 讀檔
ENCODINGS = ["utf-8-sig", "utf-8", "cp950", "big5", "utf-16", "latin-1"]


def _decode(raw: bytes) -> str:
    for enc in ENCODINGS:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _read_csv_bytes(raw: bytes) -> list[dict]:
    text = _decode(raw)
    sample = text[:4096]
    delim = "\t" if sample.count("\t") > sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    return [dict(r) for r in reader]


_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _read_xlsx_bytes(raw: bytes) -> list[dict]:
    """以標準函式庫解析 xlsx 第一個工作表（不需 openpyxl/pandas）。"""
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall(f"{_XLSX_NS}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{_XLSX_NS}t")))
        sheets = sorted(n for n in zf.namelist()
                        if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        if not sheets:
            return []
        root = ET.fromstring(zf.read(sheets[0]))
        rows: list[list[str]] = []
        for row in root.iter(f"{_XLSX_NS}row"):
            cells: dict[int, str] = {}
            for c in row.findall(f"{_XLSX_NS}c"):
                ref = c.get("r") or ""
                col = _col_index(re.sub(r"\d", "", ref))
                t = c.get("t")
                v = c.find(f"{_XLSX_NS}v")
                if t == "s" and v is not None:
                    try:
                        cells[col] = shared[int(v.text)]
                    except (ValueError, IndexError):
                        cells[col] = ""
                elif t == "inlineStr":
                    is_el = c.find(f"{_XLSX_NS}is")
                    cells[col] = "".join(x.text or "" for x in (is_el.iter(f"{_XLSX_NS}t") if is_el is not None else []))
                else:
                    cells[col] = v.text if v is not None and v.text else ""
            if cells:
                width = max(cells) + 1
                rows.append([cells.get(i, "") for i in range(width)])
    if not rows:
        return []
    header = rows[0]
    out = []
    for r in rows[1:]:
        if not any(str(x).strip() for x in r):
            continue
        out.append({header[i] if i < len(header) else f"col{i}": r[i] for i in range(len(r))})
    return out


def _col_index(letters: str) -> int:
    idx = 0
    for ch in letters:
        if "A" <= ch <= "Z":
            idx = idx * 26 + (ord(ch) - 64)
    return max(0, idx - 1)


def _read_any(raw: bytes, name: str) -> list[dict]:
    low = name.lower()
    if low.endswith(".json"):
        data = json.loads(_decode(raw))
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    return v
            return []
        return data
    if low.endswith(".xlsx") or low.endswith(".xlsm"):
        return _read_xlsx_bytes(raw)
    return _read_csv_bytes(raw)


def _fetch_url(url: str, timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "KREWS/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read()


# ---------------------------------------------------------------- 正規化
def normalize(table: str, rows: Iterable[dict]) -> list[dict]:
    spec = TABLES[table]
    ints = set(spec.get("ints", []))
    floats = set(spec.get("floats", []))
    dates = set(spec.get("dates", []))
    year_fields = {"fiscal_year", "eval_year", "school_year", "found_year"}
    out: list[dict] = []
    for raw in rows:
        rec: dict[str, Any] = {}
        for k, v in raw.items():
            std = map_header(k) or (k if k in ALIASES or str(k).startswith("_") else None)
            if std is None:
                continue
            if std in dates:
                rec[std] = to_date(v)
            elif std in year_fields:
                rec[std] = to_year(v)
            elif std in ints:
                rec[std] = to_int(v)
            elif std in floats:
                rec[std] = to_number(v)
            else:
                rec[std] = v if not isinstance(v, str) else v.strip()
        if rec.get("inst_id") is not None:
            rec["inst_id"] = str(rec["inst_id"]).strip()
        if rec:
            out.append(rec)
    return out


# ---------------------------------------------------------------- 主載入
class DataBundle:
    def __init__(self, tables: dict[str, list[dict]], meta: dict[str, Any]):
        self.tables = tables
        self.meta = meta

    def __getitem__(self, k: str) -> list[dict]:
        return self.tables.get(k, [])


def _find_local(table: str) -> Path | None:
    """在資料搜尋路徑中找出對應某張表的檔案。

    依 config.data_search_dirs() 的順序逐一尋找（exe 同層的 data/ 優先於
    打包在程式內的 data/），**第一個有命中的目錄就決定結果**，不會跨目錄
    合併候選；否則外部只更新了部分表格時，會出現同一份分析混用不同版本
    資料的情形。

    同一目錄內有多個候選時取檔名最短者，避免抓到 *_official / *_nonprofit
    等中間產物或備份檔。
    """
    pats = TABLES[table]["patterns"]
    exts = {".csv", ".tsv", ".json", ".xlsx", ".xlsm", ".txt"}
    for root in config.data_search_dirs():
        cands: list[Path] = []
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in exts:
                continue
            low = p.name.lower()
            if any(pat.lower() in low for pat in pats):
                cands.append(p)
        if cands:
            cands.sort(key=lambda x: (len(x.name), x.name))
            return cands[0]
    return None


def load(settings: dict | None = None) -> DataBundle:
    settings = settings or {}
    config.ensure_dirs()
    tables: dict[str, list[dict]] = {}
    sources: dict[str, str] = {}
    warnings: list[str] = []

    manifest: dict[str, str] = {}
    mf = next((d / "manifest.json" for d in config.data_search_dirs()
               if (d / "manifest.json").exists()), DATA_DIR / "manifest.json")
    if mf.exists():
        try:
            manifest = json.loads(mf.read_text(encoding="utf-8-sig"))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"manifest.json 解析失敗：{exc}")
    base_url = (settings.get("aws_base_url") or "").strip().rstrip("/")

    for table in TABLES:
        rows: list[dict] = []
        src = ""
        path = _find_local(table)
        if path:
            try:
                rows = normalize(table, _read_any(path.read_bytes(), path.name))
                src = f"本機檔案：{path.name}"
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"{path.name} 讀取失敗：{exc}")
        if not rows:
            url = manifest.get(table) or (f"{base_url}/{table}.csv" if base_url else "")
            if url:
                try:
                    rows = normalize(table, _read_any(_fetch_url(url), url))
                    src = f"遠端資料：{url}"
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"{table} 遠端讀取失敗：{exc}")
        if rows:
            tables[table] = rows
            sources[table] = src

    # 本系統不使用任何虛構／模擬資料：找不到真實資料時，缺漏的表格一律留空，
    # 由前端明確提示「尚未提供資料」，而非以生成的示範資料頂替（避免使用者
    # 誤將展示用假資料當作真實分析結果）。
    mode = "external" if "institutions" in tables else "empty"
    if mode == "empty":
        warnings.append("data/ 資料夾內尚未找到 institutions 資料表，請放入真實資料檔案"
                        "（見 data/manifest.json 或設定頁之 AWS 網址設定）。")
    missing = [t for t in TABLES if t not in tables]
    if missing:
        warnings.append("以下資料表未提供，分析時將以缺值處理：" + "、".join(missing))
    for t in missing:
        tables[t] = []
        sources[t] = "未提供"

    meta = {
        "mode": mode,
        "sources": sources,
        "warnings": warnings,
        "counts": {k: len(v) for k, v in tables.items()},
        "as_of": config.AS_OF.isoformat(),
        "loaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    return DataBundle(tables, meta)


# ---------------------------------------------------------------- 匯出樣板
def export_templates(bundle: DataBundle, target: Path | None = None) -> list[str]:
    """把目前資料集寫成 CSV 欄位範本。

    刻意寫到 sample_data/（而非 data/），避免載入器下次啟動時讀到系統
    自己產出的示範資料，造成真實資料被覆蓋或資料版本混淆。
    """
    target = target or config.SAMPLE_DIR
    target.mkdir(parents=True, exist_ok=True)
    written = []
    for table, rows in bundle.tables.items():
        if not rows:
            continue
        cols: list[str] = []
        for r in rows[:200]:
            for k in r:
                if k not in cols:
                    cols.append(k)
        path = target / f"{table}.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)
        written.append(path.name)
    return written
