"""從「公校」決算書（新北市地方教育發展基金 附屬單位決算，教育局主管冊）
擷取「所屬分決算單位來源、用途及餘絀概況表」中所有單位（市立幼兒園、國小、
國中、高中職）之收支數字，寫入 data/institutions.csv 與 data/financials.csv。

取代原僅擷取幼兒園的 extract_public_kindergartens.py：該表實際上涵蓋
全新北市所屬各級公立學校（數百筆），並非只有幼兒園；使用者要求完整讀取。

各單位僅有來源決算數（收入）、用途決算數（支出）兩欄可用，無人事費／
教學費等細項科目，亦無招生人數或師資資料（這些資料本表確實未提供，
非漏抓）。

國小／國中／高中附設幼兒園的處理方式（使用者明確指示）
--------------------------------------------------
附設幼兒園本身沒有獨立決算，其收支併入學校整體預算，兩者金額量級差異
極大（一整所國小 vs. 附設的一個幼兒園班）。這會讓人事費率、結餘率等
「比率型」指標失真，也可能讓班佛定律等鑑識檢定產生假警訊——這點沒有
改變。使用者已明確要求即便如此也要把學校決算數字接上附設幼兒園的財務
構面，不要留空，因此本版本會：

1. 解析同一張表中的國小／國中／高中列（NON_KG_RE），取得該校决算收支。
2. 用 school_core() 把校名正規化（去掉「新北市」「XX區」「立」等修飾字），
   與 data/institutions.csv 中「國小/國中/高中職大專附設幼兒園」機構的
   校名部分做比對，比對成功者將學校總決算數字寫入該機構在 financials.csv
   的列（inst_id 用該機構既有代碼，不另外新增機構）。
3. 找不到對應附設幼兒園的學校（本身沒有附幼、或名稱比對不到）則略過，
   不建立新機構——這張表是全市所有公立學校，本系統只分析教保機構。
"""
from __future__ import annotations

import csv
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import extract_nonprofit_financials as base  # noqa: E402

import fitz  # noqa: E402

# 路徑配置：支援環境變數覆蓋
import os
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_SOURCE = Path(os.getenv("DATA_SOURCE_PATH", r"C:\Users\lyw01\Desktop\w"))
ROOT = DATA_SOURCE / "公校"
OUT_DIR = BASE_DIR / "data"
LOG_PATH = BASE_DIR / ".tmp" / "extract_public_schools_log.txt"

YEAR_DIRS = {"112年度決算書": 2023, "113年度決算書": 2024, "114年度決算書": 2025}

KINDERGARTEN_RE = re.compile(
    r"(136\d{2})\D{0,4}(新北市[\u4e00-\u9fff]{1,12}?幼兒園)")
DISTRICT_RE = re.compile(r"新北市([\u4e00-\u9fff]{2,3}區)")

# 同一張表中的國小／國中／高中列：用來把學校總決算接上其附設幼兒園
NON_KG_RE = re.compile(
    r"(1[3-6]\d{3})\D{0,4}(新北市[\u4e00-\u9fff]{1,20}?"
    r"(?:高級中等學校|高級商工職業學校|高級工業職業學校|高級中學|"
    r"特殊教育學校|實驗國民中學|國民中學|實驗小學|國民中小學|國民小學))")

_SCHOOL_SUFFIX = ("高級中等學校", "高級商工職業學校", "高級工業職業學校",
                  "高級中學", "特殊教育學校", "實驗國民中學", "國民中學",
                  "實驗小學", "國民中小學", "國民小學")


def school_core(name: str) -> str:
    """把校名正規化成可跨表比對的核心字串。

    決算表校名格式不一致：國小多為「新北市XX區OO國民小學」，
    國中／高中常為「新北市立OO國民中學」（無區名，但有「立」字）。
    附設幼兒園機構名稱則是「新北市XX區OO國民小學附設幼兒園」或
    「新北市立OO國民中學附設幼兒園」。兩邊都先去掉「新北市」，
    再去掉開頭的區名或「立」字，留下的核心字串即可互相比對。
    """
    n = name.replace("新北市", "", 1)
    n = re.sub(r"^[\u4e00-\u9fff]{2,3}區", "", n)
    n = re.sub(r"^立", "", n)
    n = re.sub(r"附設.*$", "", n)
    return n


def build_school_lookup(inst_rows: list[dict]) -> dict[str, str]:
    """從既有 institutions.csv 建立「校名核心 → 附設幼兒園 inst_id」對照表。"""
    lookup: dict[str, str] = {}
    kinds = {"國小附設幼兒園", "國中附設幼兒園", "高中職大專附設幼兒園"}
    for r in inst_rows:
        if r.get("inst_kind") not in kinds:
            continue
        core = school_core(r["name"])
        if core and core.endswith(_SCHOOL_SUFFIX):
            lookup[core] = r["inst_id"]
    return lookup


def find_unit_pages(doc: "fitz.Document", lo: int = 90, hi: int = 118) -> list[tuple[int, str]]:
    """掃描教育局主管冊中「所屬分決算單位來源、用途及餘絀概況表」之頁面範圍。

    該表以單位代號排序，國小／國中（13xxx）在前，市立幼兒園（136xx）
    排在最後幾頁。若只以 KINDERGARTEN_RE 判斷頁面範圍，會只抓到表尾
    1-2 頁、漏掉前面一大段國小／國中列。故只要該頁同時符合任一規則即
    視為表格範圍內。
    """
    hits: list[tuple[int, str]] = []
    found_any = False
    for i in range(lo, min(hi, doc.page_count)):
        text = base.ocr_page(doc, i)
        compact = text.replace(" ", "").replace("\n", "")
        if KINDERGARTEN_RE.search(compact) or NON_KG_RE.search(compact):
            hits.append((i, text))
            found_any = True
        elif found_any:
            break
    return hits


_STRAY_SLASH_RE = re.compile(r"(?<=\d)/(?=\d)")


def _declean(line: str) -> str:
    """這張表的 OCR 常把千分位符號讀成「/」，而 parse_numbers 不認得
    「/」是分隔符號，會把一個完整金額硬切成好幾段（例如
    「37/7/7﹒328﹒331」應為 377,328,331，卻被切成 37、7、7328331 三段）。
    數字之間的「/」先去掉，讓 parse_numbers 能把同一個金額合併回來。
    """
    return _STRAY_SLASH_RE.sub("", line)


def _plausible(revenue: float, expense: float) -> bool:
    """粗略合理性檢查：同一年度的決算收入與支出應同量級（公務預算執行率
    通常在八九成到一百多趴之間），差距太大代表 OCR 把數字切壞或誤讀，
    寧可略過也不要把明顯錯誤的金額寫進正式資料。
    """
    if revenue <= 0 or expense <= 0:
        return False
    if revenue < 500_000 or expense < 500_000:
        return False
    ratio = max(revenue, expense) / min(revenue, expense)
    return ratio <= 3.0


def parse_unit_rows(text: str, kg_lookup: dict[str, str],
                     school_lookup: dict[str, str]
                     ) -> tuple[list[dict], list[str], list[str]]:
    """解析所屬分決算單位表，比對到既有機構者回傳決算列。

    回傳 (財務列, 比對不到機構的名稱列表, 因數字不合理而略過的名稱列表)。
    財務列一律使用既有機構的 inst_id，不建立新機構。
    """
    out: list[dict] = []
    unmatched: list[str] = []
    implausible: list[str] = []
    for line in text.splitlines():
        compact = line.replace(" ", "")
        m = KINDERGARTEN_RE.search(compact)
        kind = "kg"
        if not m:
            m = NON_KG_RE.search(compact)
            kind = "school"
        if not m:
            continue
        code, name = m.groups()
        nums = base.parse_numbers(_declean(line))
        if nums and int(nums[0]) == int(code):
            nums = nums[1:]
        if len(nums) < 2:
            continue
        revenue, expense = nums[0], nums[1]
        if kind == "kg":
            inst_id = kg_lookup.get(name)
        else:
            inst_id = school_lookup.get(school_core(name))
        if not inst_id:
            unmatched.append(name)
            continue
        if not _plausible(revenue, expense):
            implausible.append(f"{name}（收入={revenue:,.0f} 支出={expense:,.0f}）")
            continue
        out.append({
            "inst_id": inst_id,
            "total_revenue": revenue,
            "total_expense": expense,
            "surplus": round(revenue - expense, 2),
        })
    return out, unmatched, implausible


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log = LOG_PATH.open("w", encoding="utf-8")

    inst_path = OUT_DIR / "institutions.csv"
    fin_path = OUT_DIR / "financials.csv"
    with inst_path.open("r", encoding="utf-8-sig", newline="") as f:
        existing_inst = list(csv.DictReader(f))

    kg_lookup = {r["name"]: r["inst_id"] for r in existing_inst
                 if r.get("org_type") == "公立" and r.get("inst_kind") == "獨立幼兒園"}
    school_lookup = build_school_lookup(existing_inst)
    log.write(f"既有機構可比對：市立幼兒園 {len(kg_lookup)} 所、"
              f"學校附設幼兒園 {len(school_lookup)} 所\n\n")

    financials: list[dict] = []
    all_unmatched: Counter = Counter()
    all_implausible: Counter = Counter()

    for year_dir, fy in YEAR_DIRS.items():
        vol1 = ROOT / year_dir / "第1冊"
        pdfs = list(vol1.glob("*決算書第一冊.pdf"))
        if not pdfs:
            log.write(f"{year_dir}: 找不到第一冊 PDF\n")
            continue
        path = pdfs[0]
        log.write(f"=== {year_dir} ({path.name}) ===\n")
        doc = fitz.open(path)
        pages = find_unit_pages(doc)
        if not pages:
            log.write("  找不到所屬分決算單位表頁面\n")
            doc.close()
            continue
        seen_ids: set[str] = set()
        for idx, text in pages:
            rows, unmatched, implausible = parse_unit_rows(text, kg_lookup, school_lookup)
            for name in unmatched:
                all_unmatched[name] += 1
            for name in implausible:
                all_implausible[name] += 1
            matched_this_page = 0
            for r in rows:
                if r["inst_id"] in seen_ids:
                    continue
                seen_ids.add(r["inst_id"])
                financials.append({
                    "inst_id": r["inst_id"], "fiscal_year": fy,
                    "total_revenue": r["total_revenue"], "total_expense": r["total_expense"],
                    "surplus": r["surplus"],
                })
                matched_this_page += 1
            log.write(f"  page {idx}: 比對成功 {matched_this_page} 筆"
                      f"（比對不到機構 {len(unmatched)} 筆、"
                      f"數字不合理略過 {len(implausible)} 筆）\n")
        doc.close()
        log.write(f"  本年度累計比對成功 {len(seen_ids)} 所\n")
        log.flush()
        print(f"{year_dir}: 比對成功 {len(seen_ids)} 所", flush=True)

    fin_cols = ["inst_id", "fiscal_year", "revenue_tuition", "revenue_subsidy",
               "revenue_other", "total_revenue", "expense_personnel",
               "expense_teaching", "expense_facility", "expense_rent",
               "expense_admin", "expense_other", "total_expense", "surplus",
               "verified"]
    existing_fin: list[dict] = []
    if fin_path.exists():
        with fin_path.open("r", encoding="utf-8-sig", newline="") as f:
            existing_fin = list(csv.DictReader(f))
            if existing_fin:
                fin_cols = list(existing_fin[0].keys())
    # 同一機構同一年度若已有決算資料（例如非營利園既有財報），保留原資料，
    # 不覆蓋——本腳本只補齊「原本沒有任何決算資料」的公立機構。
    existing_keys = {(r["inst_id"], r["fiscal_year"]) for r in existing_fin}
    new_fin = [r for r in financials
               if (r["inst_id"], str(r["fiscal_year"])) not in existing_keys]
    all_fin = existing_fin + new_fin

    with fin_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fin_cols, extrasaction="ignore")
        w.writeheader()
        for r in all_fin:
            w.writerow(r)

    log.write(f"\n新增決算列 {len(new_fin)} 筆（含跳過重複 {len(financials) - len(new_fin)} 筆）\n")
    log.write(f"比對不到機構的單位（可能本身無附設幼兒園）共 {len(all_unmatched)} 種名稱：\n")
    for name, cnt in all_unmatched.most_common(50):
        log.write(f"    {name}  (x{cnt})\n")
    log.write(f"\n收支數字不合理而略過（OCR 誤讀，非漏抓）共 {len(all_implausible)} 種：\n")
    for name, cnt in all_implausible.most_common(80):
        log.write(f"    {name}  (x{cnt})\n")
    log.close()
    print(f"DONE: +{len(new_fin)} financial rows"
          f"（比對不到 {len(all_unmatched)} 種單位名稱、"
          f"數字不合理略過 {len(all_implausible)} 種，詳見 {LOG_PATH}）")


if __name__ == "__main__":
    main()
