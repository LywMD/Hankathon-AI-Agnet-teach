"""從全國教保資訊網採集機構主檔、裁罰紀錄與評鑑結果。

產出（寫入 data/）：
* institutions_official.csv — 機構主檔（名稱／行政區／設立別／地址／核定人數）
* penalties.csv             — 裁罰紀錄（逐筆處分）
* evaluations.csv           — 基礎評鑑結果（逐年度）

為什麼機構主檔要以此為權威來源
------------------------------
原本的機構清單只有非營利園（PDF 檔名）與公校決算單位，但裁罰紀錄幾乎
全部集中在**私立**幼兒園。母體不含私立園時，裁罰資料對不上任何機構，
監督式模型的標籤會全為 0，AUC 失去意義，法遵構面分數也恆為零——這正是
「所有機構分數看起來都一樣」的根本原因。

改以本站的基本資料查詢作為母體（涵蓋公立／私立／非營利／準公共），
機構屬性（行政區、地址、核定人數）也一併採用官方值，不再依賴 OCR 猜測。

用法：
    python scripts/collect_ece.py                # 新北市，全部設立別
    python scripts/collect_ece.py --city 02      # 臺北市
    python scripts/collect_ece.py --max-pages 5  # 只抓前 5 頁（快速驗證）
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app import textnorm  # noqa: E402
from app.collect import ece  # noqa: E402
from app.collect.http_util import Session  # noqa: E402

OUT_DIR = BASE_DIR / "data"
LOG_PATH = BASE_DIR / ".tmp" / "collect_ece_log.txt"


def write_csv(path: Path, rows: list[dict], cols: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def normalize_institution(raw: dict, city_name: str) -> dict:
    """把採集到的機構列正規化成主檔格式。"""
    name = textnorm.clean_ocr_text(raw.get("name", ""))
    address = textnorm.clean_address(raw.get("address", ""))
    district = textnorm.normalize_district(raw.get("district", ""), address)
    return {
        "inst_id": textnorm.stable_inst_id(name, city_name),
        "name": name,
        "city": textnorm.clean_ocr_text(raw.get("city", "")) or city_name,
        "district": district,
        "org_type": textnorm.clean_ocr_text(raw.get("org_type", "")),
        "address": address,
        "phone": raw.get("phone", ""),
        "website": raw.get("website", ""),
        "approved_capacity": raw.get("approved_capacity", ""),
        "status": raw.get("status", ""),
        "child_service": raw.get("child_service", ""),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default=ece.CITY_NEW_TAIPEI,
                    help="縣市代碼（新北市=03，臺北市=02）")
    ap.add_argument("--city-name", default="新北市")
    ap.add_argument("--max-pages", type=int, default=500)
    ap.add_argument("--delay", type=float, default=0.7,
                    help="每次請求間隔秒數（禮貌性延遲）")
    ap.add_argument("--skip-penalties", action="store_true")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log = LOG_PATH.open("w", encoding="utf-8")

    def prog(msg: str) -> None:
        log.write(msg + "\n")
        log.flush()
        print("  " + msg, flush=True)

    sess = Session(delay=args.delay, verify_tls=False)
    master: dict[str, dict] = {}
    # 完整機構名稱 → inst_id。裁罰與評鑑紀錄一律用**完整名稱**回查代碼，
    # 而非各自重算，這樣三個查詢頁對同一所園必然得到同一個代碼，
    # 也不會因為代碼演算法調整而讓各表對不起來。
    name_to_id: dict[str, str] = {}
    # inst_id → 已見過的名稱集合，用於偵測代碼碰撞（曾發生國小附設園全部
    # 併成一筆、以及市立與私立同名園互相覆蓋的情形）。
    id_names: dict[str, set[str]] = {}

    def register(raw: dict) -> str:
        """把採集到的機構列納入主檔，回傳其 inst_id。"""
        rec = normalize_institution(raw, args.city_name)
        if not rec["name"]:
            return ""
        iid = rec["inst_id"]
        name_to_id.setdefault(rec["name"], iid)
        id_names.setdefault(iid, set()).add(rec["name"])
        master.setdefault(iid, rec)
        return iid

    def lookup(name: str) -> str:
        """以完整名稱回查代碼；主檔沒有時即時建立（僅有名稱的最小紀錄）。"""
        nm = textnorm.clean_ocr_text(name)
        if not nm:
            return ""
        if nm in name_to_id:
            return name_to_id[nm]
        return register({"name": nm, "city": args.city_name})

    # ---------------------------------------------------------- 1. 機構主檔
    print(f"\n[1/3] 機構基本資料（{args.city_name}）")
    try:
        raw_insts = ece.fetch_institutions(
            sess, args.city, max_pages=args.max_pages, on_progress=prog)
    except Exception as exc:  # noqa: BLE001
        print(f"  基本資料採集失敗：{exc}")
        log.write(f"FAIL institutions: {exc}\n")
        raw_insts = []
    for r in raw_insts:
        register(r)
    print(f"  => 機構 {len(master)} 所")

    # ---------------------------------------------------------- 2. 評鑑結果
    print(f"\n[2/3] 評鑑結果")
    evaluations: list[dict] = []
    try:
        evals, eval_insts = ece.fetch_evaluations(
            sess, args.city, max_pages=args.max_pages, on_progress=prog)
    except Exception as exc:  # noqa: BLE001
        print(f"  評鑑採集失敗：{exc}")
        log.write(f"FAIL evaluations: {exc}\n")
        evals, eval_insts = [], []

    # 評鑑查詢頁同樣涵蓋全部設立別，可補齊基本資料頁漏抓的機構
    for r in eval_insts:
        register(r)

    for e in evals:
        evaluations.append({
            "inst_id": lookup(e["name"]),
            "name": e["name"],
            "eval_year": e["eval_year"],
            "eval_date": e["eval_date"],
            "result": e["result"],
            "items_failed": e["items_failed"],
            "followup_required": e["followup_required"],
        })
    print(f"  => 評鑑紀錄 {len(evaluations)} 筆；主檔累計 {len(master)} 所")

    # ---------------------------------------------------------- 3. 裁罰紀錄
    penalties: list[dict] = []
    if args.skip_penalties:
        print("\n[3/3] 裁罰紀錄（已略過）")
    else:
        print(f"\n[3/3] 裁罰紀錄")
        try:
            pens, pen_insts = ece.fetch_penalties(
                sess, args.city, max_pages=args.max_pages, on_progress=prog)
        except Exception as exc:  # noqa: BLE001
            print(f"  裁罰採集失敗：{exc}")
            log.write(f"FAIL penalties: {exc}\n")
            pens, pen_insts = [], []

        for r in pen_insts:
            register(r)

        for p in pens:
            penalties.append({
                "inst_id": lookup(p["name"]),
                "name": p["name"],
                "penalty_date": p["penalty_date"],
                "published_date": p["published_date"],
                "category": p.get("category", ""),
                "law_article": p["law_article"],
                "description": p["description"],
                "fine_amount": p["fine_amount"],
                "disposition": p["disposition"],
                "doc_no": p.get("doc_no", ""),
                "target": p.get("target", ""),
            })
        print(f"  => 裁罰紀錄 {len(penalties)} 筆；主檔累計 {len(master)} 所")

    # ---------------------------------------------------------- 輸出
    insts = sorted(master.values(), key=lambda r: (r["district"], r["name"]))
    write_csv(OUT_DIR / "institutions_official.csv", insts,
              ["inst_id", "name", "city", "district", "org_type", "address",
               "phone", "website", "approved_capacity", "status",
               "child_service"])
    write_csv(OUT_DIR / "penalties.csv", penalties,
              ["inst_id", "name", "penalty_date", "published_date", "category",
               "law_article", "description", "fine_amount", "disposition",
               "doc_no", "target"])
    write_csv(OUT_DIR / "evaluations.csv", evaluations,
              ["inst_id", "name", "eval_year", "eval_date", "result",
               "items_failed", "followup_required"])

    # ---------------------------------------------------------- 摘要
    from collections import Counter
    by_type = Counter(r["org_type"] or "未分類" for r in insts)
    pen_by_cat = Counter(p["category"] for p in penalties)
    penalized = len({p["inst_id"] for p in penalties})

    collisions = {iid: sorted(ns) for iid, ns in id_names.items() if len(ns) > 1}

    summary = [
        "",
        "=" * 60,
        f"機構主檔      {len(insts)} 所",
        f"  設立別分布  {dict(by_type)}",
        f"  代碼碰撞    {len(collisions)} 組"
        + ("（正常）" if not collisions else "  ← 需檢查 canonical_name 規則"),
        f"裁罰紀錄      {len(penalties)} 筆，涉及 {penalized} 所機構"
        f"（占母體 {penalized / len(insts) * 100:.1f}%）" if insts else "",
        f"  類別分布    {dict(pen_by_cat)}",
        f"評鑑紀錄      {len(evaluations)} 筆",
        "=" * 60,
    ]
    for line in summary:
        print(line)
        log.write(str(line) + "\n")
    if collisions:
        log.write("\n代碼碰撞明細（同一代碼對到多個機構名稱）：\n")
        for iid, names in collisions.items():
            log.write(f"  {iid}\n")
            for nm in names:
                log.write(f"      {nm}\n")
        print(f"  碰撞明細見 {LOG_PATH}")
    log.close()
    print(f"\n輸出：{OUT_DIR}\\institutions_official.csv, penalties.csv, "
          f"evaluations.csv\n日誌：{LOG_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
