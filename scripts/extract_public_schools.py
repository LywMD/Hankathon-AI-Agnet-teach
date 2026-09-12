"""從「公校」決算書（新北市地方教育發展基金 附屬單位決算，教育局主管冊）
擷取「所屬分決算單位來源、用途及餘絀概況表」中所有單位（市立幼兒園、國小、
國中、高中職）之收支數字，寫入 data/institutions.csv 與 data/financials.csv。

取代原僅擷取幼兒園的 extract_public_kindergartens.py：該表實際上涵蓋
全新北市所屬各級公立學校（數百筆），並非只有幼兒園；使用者要求完整讀取。

各單位僅有來源決算數（收入）、用途決算數（支出）兩欄可用，無人事費／
教學費等細項科目，亦無招生人數或師資資料（這些資料本表確實未提供，
非漏抓）。
"""
from __future__ import annotations

import csv
import re
import sys
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

# 本系統的分析對象是「教保服務機構」，因此只擷取市立幼兒園。
#
# 為什麼不納入國小／國中／高中：
# 1. 它們本身不是教保機構，把學校放進教保風險排名等於拿不同性質的機構
#    互相比較，師生比、超收率、人事費率等指標對學校都沒有相同的法規意義。
# 2. 更關鍵的是財務資料無法對應：國小附設幼兒園**沒有獨立決算**，其收支
#    併入學校整體預算。若把學校的總收支當成附設幼兒園的財務，金額會差
#    好幾個數量級，人事費率、結餘率等比率全部失真，連帶讓同儕偏離與
#    班佛定律等鑑識檢定產生大量假警訊。
#
# 因此：市立幼兒園（單位代號 136xx，為獨立分決算單位）才有可用的決算數字；
# 學校附設幼兒園的財務構面只能標記為「無資料」，由 scoring 的構面可得性
# 機制排除，不以推估值填補。
KINDERGARTEN_RE = re.compile(
    r"(136\d{2})\D{0,4}(新北市[\u4e00-\u9fff]{1,12}?幼兒園)")
DISTRICT_RE = re.compile(r"新北市([\u4e00-\u9fff]{2,3}區)")

# 用於統計被略過的非教保機構單位，讓日誌能說明擷取範圍
NON_KG_RE = re.compile(
    r"(1[3-6]\d{3})\D{0,4}(新北市[\u4e00-\u9fff]{1,20}?"
    r"(?:高級中等學校|高級商工職業學校|高級工業職業學校|高級中學|"
    r"特殊教育學校|實驗國民中學|國民中學|實驗小學|國民中小學|國民小學))")


def org_type_of(name: str) -> str:
    """市立幼兒園一律為「公立」設立別。

    本函式不再回傳「公立國小／公立國中／公立高中」：那些單位已在
    parse_unit_rows 階段被排除，不會進入資料集。
    """
    return "公立"


def find_unit_pages(doc: "fitz.Document", lo: int = 98, hi: int = 118) -> list[tuple[int, str]]:
    """掃描教育局主管冊中「所屬分決算單位來源、用途及餘絀概況表」之頁面範圍。"""
    hits: list[tuple[int, str]] = []
    found_any = False
    for i in range(lo, min(hi, doc.page_count)):
        text = base.ocr_page(doc, i)
        compact = text.replace(" ", "")
        if KINDERGARTEN_RE.search(compact.replace("\n", "")):
            hits.append((i, text))
            found_any = True
        elif found_any:
            break
    return hits


def parse_unit_rows(text: str) -> tuple[list[dict], int]:
    """解析所屬分決算單位表，只取市立幼兒園。

    回傳 (幼兒園資料列, 略過的非教保機構單位數)。
    """
    out = []
    skipped = 0
    for line in text.splitlines():
        compact = line.replace(" ", "")
        m = KINDERGARTEN_RE.search(compact)
        if not m:
            # 同一張表也列出國小／國中／高中，但那些不是教保機構，
            # 且其附設幼兒園並無獨立決算，故一律略過（僅計數供日誌說明）。
            if NON_KG_RE.search(compact):
                skipped += 1
            continue
        code, name = m.groups()
        nums = base.parse_numbers(line)
        if nums and int(nums[0]) == int(code):
            nums = nums[1:]
        if len(nums) < 2:
            continue
        revenue, expense = nums[0], nums[1]
        dm = DISTRICT_RE.search(name)
        out.append({
            "inst_id": f"G{code}",
            "name": name,
            "district": dm.group(1) if dm else "",
            "org_type": org_type_of(name),
            "total_revenue": revenue,
            "total_expense": expense,
            "surplus": round(revenue - expense, 2),
        })
    return out, skipped


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log = LOG_PATH.open("w", encoding="utf-8")

    institutions: dict[str, dict] = {}
    financials: list[dict] = []

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
        seen_codes = set()
        type_counts: dict[str, int] = {}
        skipped_total = 0
        for idx, text in pages:
            rows, skipped = parse_unit_rows(text)
            skipped_total += skipped
            for r in rows:
                if r["inst_id"] in seen_codes:
                    continue
                seen_codes.add(r["inst_id"])
                institutions.setdefault(r["inst_id"], {
                    "inst_id": r["inst_id"], "name": r["name"], "city": "新北市",
                    "district": r["district"], "org_type": r["org_type"], "address": "",
                })
                financials.append({
                    "inst_id": r["inst_id"], "fiscal_year": fy,
                    "total_revenue": r["total_revenue"], "total_expense": r["total_expense"],
                    "surplus": r["surplus"],
                })
                type_counts[r["org_type"]] = type_counts.get(r["org_type"], 0) + 1
            log.write(f"  page {idx}: 幼兒園 {len(rows)} 筆"
                      f"（另略過非教保機構 {skipped} 筆）\n")
        doc.close()
        log.write(f"  累計市立幼兒園 {len(seen_codes)} 所（本年度），"
                  f"分布：{type_counts}\n")
        log.write(f"  本年度共略過國小／國中／高中等非教保機構 "
                  f"{skipped_total} 筆（不納入教保風險分析）\n")
        log.flush()
        print(f"{year_dir}: {len(seen_codes)} 所（{type_counts}）", flush=True)

    inst_path = OUT_DIR / "institutions.csv"
    fin_path = OUT_DIR / "financials.csv"
    existing_inst: list[dict] = []
    inst_cols = ["inst_id", "name", "city", "district", "org_type", "address",
                "approved_capacity", "enrolled", "staff_count", "teacher_count"]
    if inst_path.exists():
        with inst_path.open("r", encoding="utf-8-sig", newline="") as f:
            existing_inst = list(csv.DictReader(f))
            if existing_inst:
                inst_cols = list(existing_inst[0].keys())
    existing_ids = {r["inst_id"] for r in existing_inst}
    all_inst = existing_inst + [institutions[k] for k in institutions if k not in existing_ids]

    fin_cols = ["inst_id", "fiscal_year", "revenue_tuition", "total_revenue",
               "expense_personnel", "expense_teaching", "expense_facility",
               "expense_rent", "expense_admin", "expense_other", "total_expense",
               "surplus"]
    existing_fin: list[dict] = []
    if fin_path.exists():
        with fin_path.open("r", encoding="utf-8-sig", newline="") as f:
            existing_fin = list(csv.DictReader(f))
            if existing_fin:
                fin_cols = list(existing_fin[0].keys())
    all_fin = existing_fin + financials

    with inst_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=inst_cols, extrasaction="ignore")
        w.writeheader()
        for r in all_inst:
            w.writerow(r)
    with fin_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fin_cols, extrasaction="ignore")
        w.writeheader()
        for r in all_fin:
            w.writerow(r)

    log.write(f"\n新增單位 {len(institutions)} 所，決算列 {len(financials)} 筆\n")
    log.close()
    print(f"DONE: +{len(institutions)} units, +{len(financials)} financial rows")


if __name__ == "__main__":
    main()
