"""從「公校」決算書（新北市地方教育發展基金 附屬單位決算，教育局主管冊）
擷取市立幼兒園（新北市立ＯＯ幼兒園，作為獨立所屬分決算單位編列）之收支
數字，寫入 data/institutions.csv 與 data/financials.csv（與非營利園資料合併）。

文件結構（以 113 年度決算書第一冊為例，OCR 探勘確認）：
* 每冊為單一基金（此處為教育局主管）之附屬單位決算，內含「所屬分決算單位
  來源、用途及餘絀概況表」，逐一列出所屬分決算單位（各級學校＋市立幼兒園）
  之來源決算數（收入）、用途決算數（支出）、餘絀等欄位。
* 市立幼兒園以單位代號 136xx、名稱「新北市立ＯＯ幼兒園」列示，通常集中在
  同一頁（緊接在國小之後、員工人數彙計表之前），代號區段穩定但實際頁碼
  隨年度內容增減略有偏移，故以關鍵字掃描定位而非固定頁碼。
* 文字層編碼同樣損毀，須以 OCR 讀取（沿用 extract_nonprofit_financials 的
  OCR／數字解析工具）。
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import extract_nonprofit_financials as base  # noqa: E402

import fitz  # noqa: E402

# ⚠️ 注意：本腳本已被 extract_public_schools.py 取代，僅保留供參考
# 路徑配置：支援環境變數覆蓋
import os
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_SOURCE = Path(os.getenv("DATA_SOURCE_PATH", r"C:\Users\lyw01\Desktop\w"))
ROOT = DATA_SOURCE / "公校"
OUT_DIR = BASE_DIR / "data"
LOG_PATH = BASE_DIR / ".tmp" / "extract_public_log.txt"

YEAR_DIRS = {"112年度決算書": 2023, "113年度決算書": 2024, "114年度決算書": 2025}

KG_LINE_RE = re.compile(r"(136\d{2})\D*新北市立([\u4e00-\u9fff]{2,4})幼兒園")


def find_kindergarten_pages(doc: "fitz.Document", lo: int = 85, hi: int = 140) -> list[tuple[int, str]]:
    """掃描教育局主管冊中列出市立幼兒園之頁面（回傳 (頁碼, OCR文字) 清單）。"""
    hits: list[tuple[int, str]] = []
    found_any = False
    for i in range(lo, min(hi, doc.page_count)):
        text = base.ocr_page(doc, i)
        compact = text.replace(" ", "")
        if "幼兒園" in compact and "136" in compact:
            hits.append((i, text))
            found_any = True
        elif found_any:
            # 幼兒園清單為連續區塊，離開區塊後即可停止掃描
            break
    return hits


def parse_kindergarten_rows(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        compact = line.replace(" ", "")
        m = KG_LINE_RE.search(compact)
        if not m:
            continue
        code, district_name = m.groups()
        nums = base.parse_numbers(line)
        # 行首的單位代號（如 13601）本身會被 parse_numbers 當成一個數字擷取，
        # 須先剔除，否則後續欄位會整組錯位一格。
        if nums and int(nums[0]) == int(code):
            nums = nums[1:]
        if len(nums) < 2:
            continue
        revenue, expense = nums[0], nums[1]
        out.append({
            "inst_id": f"G{code}",
            "name": f"新北市立{district_name}幼兒園",
            "district": f"{district_name}區" if not district_name.endswith("區") else district_name,
            "total_revenue": revenue,
            "total_expense": expense,
            "surplus": round(revenue - expense, 2),
        })
    return out


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
        pages = find_kindergarten_pages(doc)
        if not pages:
            log.write("  找不到市立幼兒園清單頁面\n")
            doc.close()
            continue
        seen_codes = set()
        for idx, text in pages:
            rows = parse_kindergarten_rows(text)
            for r in rows:
                if r["inst_id"] in seen_codes:
                    continue
                seen_codes.add(r["inst_id"])
                institutions.setdefault(r["inst_id"], {
                    "inst_id": r["inst_id"], "name": r["name"], "city": "新北市",
                    "district": r["district"], "org_type": "公立", "address": "",
                })
                financials.append({
                    "inst_id": r["inst_id"], "fiscal_year": fy,
                    "total_revenue": r["total_revenue"], "total_expense": r["total_expense"],
                    "surplus": r["surplus"],
                })
            log.write(f"  page {idx}: {len(rows)} 所幼兒園\n")
        doc.close()
        log.write(f"  累計 {len(seen_codes)} 所（本年度）\n")
        log.flush()
        print(f"{year_dir}: {len(seen_codes)} 所市立幼兒園", flush=True)

    # 併入既有（非營利園）institutions.csv／financials.csv，而非覆蓋
    inst_path = OUT_DIR / "institutions.csv"
    fin_path = OUT_DIR / "financials.csv"
    existing_inst: list[dict] = []
    inst_cols = ["inst_id", "name", "city", "district", "org_type", "address"]
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

    log.write(f"\n新增市立幼兒園 {len(institutions)} 所，決算列 {len(financials)} 筆\n")
    log.close()
    print(f"DONE: +{len(institutions)} public kindergartens, +{len(financials)} financial rows")


if __name__ == "__main__":
    main()
