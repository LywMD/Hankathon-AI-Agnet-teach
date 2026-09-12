"""把各來源的採集結果整合成主程式可直接載入的資料集。

輸入（由前置腳本產生）
--------------------
    data/institutions_official.csv   ← scripts/collect_ece.py（機構主檔，權威來源）
    data/penalties.csv               ← scripts/collect_ece.py
    data/evaluations.csv             ← scripts/collect_ece.py
    data/posts.csv                   ← scripts/collect_sentiment.py
    data/institutions_nonprofit.csv  ← scripts/extract_nonprofit_financials.py
    data/financials_nonprofit.csv    ← 同上
    data/ledger_nonprofit.csv        ← 同上

輸出（app/datastore.py 會載入這三個）
---------------------------------
    data/institutions.csv
    data/financials.csv
    data/ledger.csv

命名注意事項
-----------
app/datastore.py 以檔名關鍵字尋找資料表，並在多個候選中取**檔名最短者**
（避免誤抓備份檔）。因此最終檔名 institutions.csv / financials.csv /
ledger.csv 一定會勝過 *_official.csv、*_nonprofit.csv 等中間檔，
中間檔可留在 data/ 供追溯而不會被誤載。

機構識別的合併邏輯
----------------
PDF 決算報告用的是檔名代碼（N01、N02…），與官方主檔的代碼體系不同，
因此以 textnorm.canonical_name() 產生的正規化名稱作為橋樑。比對不到的
PDF 機構會**保留為獨立機構**並在報告中列出，不會被丟棄，也不會硬塞給
某個相近的園所——財務資料掛錯機構的代價遠高於少掛一筆。
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app import textnorm  # noqa: E402

DATA = BASE_DIR / "data"
LOG_PATH = BASE_DIR / ".tmp" / "build_dataset_log.txt"

INST_COLS = ["inst_id", "name", "city", "district", "org_type", "inst_kind",
             "address", "phone", "website", "approved_capacity", "enrolled",
             "staff_count", "teacher_count", "status", "child_service",
             "data_sources"]

FIN_COLS = ["inst_id", "fiscal_year", "revenue_tuition", "revenue_subsidy",
            "revenue_other", "total_revenue", "expense_personnel",
            "expense_teaching", "expense_facility", "expense_rent",
            "expense_admin", "expense_other", "total_expense", "surplus",
            "verified"]

LEDGER_COLS = ["inst_id", "fiscal_year", "account", "kind", "amount"]


# 非教保機構的名稱特徵：各級學校本體。學校**附設幼兒園**不在此列
# （名稱含「幼兒園」者一律視為教保機構）。
_NON_CHILDCARE_SUFFIX = (
    "國民小學", "國民中學", "國民中小學", "實驗小學", "實驗國民中學",
    "高級中學", "高級中等學校", "高級商工職業學校", "高級工業職業學校",
    "特殊教育學校", "專科學校", "科技大學", "大學",
)


def institution_kind(name: str) -> str:
    """判斷教保服務機構的類型與組織型態。

    幼兒教育及照顧法第 10 條所定的教保服務機構有三類，不只幼兒園：
      * 幼兒園
      * 社區／部落互助教保服務中心
      * 職場互助教保服務中心

    把「教保服務中心」誤判為非教保機構會漏掉合法的監理對象（實測新北市
    有 8 所職場互助教保服務中心，設於台電發電廠、郵局、軍營等場所）。
    它們同樣受師生比、收費與人員資格規範，本來就該納入風險評估。

    幼兒園再依組織型態細分：設立別（公立／私立／非營利／準公共）只說明
    經費與管理歸屬，看不出「獨立園所」與「學校附設」的差別，而兩者營運
    條件差異很大——學校附設園共用校舍與行政人力、且沒有獨立決算，
    財務構面只能標記為無資料。
    """
    n = textnorm.clean_ocr_text(name)
    if "職場互助教保服務中心" in n:
        return "職場互助教保服務中心"
    if "互助教保服務中心" in n:
        return "社區部落互助教保服務中心"
    if not any(k in n for k in ("幼兒園", "幼稚園", "托兒所")):
        return "非教保機構"
    if "附設" not in n:
        return "獨立幼兒園"
    if "國民小學" in n or "實驗小學" in n or "國民中小學" in n:
        return "國小附設幼兒園"
    if "國民中學" in n:
        return "國中附設幼兒園"
    if any(k in n for k in ("高級中", "高中", "職業學校", "專科", "大學",
                            "特殊教育學校")):
        return "高中職大專附設幼兒園"
    return "法人公司附設幼兒園"


def is_childcare(name: str) -> bool:
    """判斷是否為教保服務機構。

    納入：幼兒園（含學校附設）、各類互助教保服務中心。
    排除：各級學校本體——名稱以學校名稱結尾且不含教保機構字樣者。

    這是一道防禦性檢查：即使 data/ 內殘留舊版萃取腳本產生的學校資料
    （舊版 extract_public_schools.py 會把國小／國中／高中一併抓進來），
    也不會被計入教保風險排名。
    """
    n = textnorm.clean_ocr_text(name)
    if not n:
        return False
    if any(k in n for k in ("幼兒園", "幼稚園", "托兒所", "教保服務中心")):
        return True
    return not n.endswith(_NON_CHILDCARE_SUFFIX)


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f)]


def write_csv(path: Path, rows: list[dict], cols: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="新北市")
    args = ap.parse_args()

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log = LOG_PATH.open("w", encoding="utf-8")

    def out(msg: str = "") -> None:
        print(msg)
        log.write(str(msg) + "\n")

    # ------------------------------------------------------------ 機構主檔
    official = read_csv(DATA / "institutions_official.csv")
    if not official:
        out("錯誤：找不到 data/institutions_official.csv")
        out("請先執行：python scripts/collect_ece.py")
        log.close()
        return 1

    # 機構代碼一律在此重算，不沿用採集階段寫入的值。
    # 理由：代碼演算法若調整（例如修正同名不同設立別的碰撞問題），只要各表
    # 都保留 name 欄，就能在整合階段一次校正，不必重跑數十分鐘的網路採集；
    # 也確保 institutions／penalties／evaluations／posts 四張表的代碼必然一致。
    master: dict[str, dict] = {}
    name_of_id: dict[str, set[str]] = {}
    excluded: list[str] = []
    for r in official:
        name = textnorm.clean_ocr_text(r.get("name", ""))
        if not name:
            continue
        if not is_childcare(name):
            excluded.append(name)
            continue
        iid = textnorm.stable_inst_id(name, r.get("city") or args.city)
        name_of_id.setdefault(iid, set()).add(name)
        master[iid] = {
            "inst_id": iid,
            "name": name,
            "city": r.get("city") or args.city,
            "district": r.get("district", ""),
            "org_type": r.get("org_type", ""),
            "inst_kind": institution_kind(name),
            "address": r.get("address", ""),
            "phone": r.get("phone", ""),
            "website": r.get("website", ""),
            "approved_capacity": r.get("approved_capacity", ""),
            "enrolled": "",
            "staff_count": "",
            "teacher_count": "",
            "status": r.get("status", ""),
            "child_service": r.get("child_service", ""),
            "data_sources": "官方主檔",
        }
    out(f"官方機構主檔：{len(master)} 所教保機構")
    if excluded:
        out(f"  已排除非教保機構 {len(excluded)} 筆（各級學校本體，"
            f"不適用教保法規指標）")
        for nm in excluded[:10]:
            log.write(f"      排除：{nm}\n")
    collisions = {i: sorted(ns) for i, ns in name_of_id.items() if len(ns) > 1}
    if collisions:
        out(f"  ⚠ 代碼碰撞 {len(collisions)} 組（不同機構共用代碼，需修正"
            f"textnorm.stable_inst_id）")
        for iid, ns in collisions.items():
            log.write(f"      {iid}: {' / '.join(ns)}\n")
    else:
        out("  代碼唯一性檢查通過")

    # 完整名稱 → 代碼。裁罰／評鑑／輿情一律用完整名稱回查，確保跨表一致。
    exact_index = {rec["name"]: iid for iid, rec in master.items()}

    def relink(rows: list[dict], label: str,
               keep_unattributed: bool = False) -> list[dict]:
        """以完整機構名稱重新對應各表的 inst_id。

        keep_unattributed：是否保留「沒有機構名稱」的紀錄。輿情資料需要開啟
        此選項——議題掃描抓到的市級背景報導本來就沒有指名園所，它們不計入
        任何機構分數，但構成輿情頁的整體聲量與關鍵詞基礎。若一併丟棄，
        會把大部分採集成果扔掉（實測 388 則只剩 114 則）。
        """
        linked, orphan, unattributed = [], 0, 0
        for r in rows:
            nm = textnorm.clean_ocr_text(r.get("name", ""))
            iid = exact_index.get(nm)
            if not iid and nm:
                # 名稱不在主檔（例如已廢止的機構仍有裁罰紀錄）：
                # 保留該筆並自成一個代碼，不丟棄歷史裁罰事實。
                iid = textnorm.stable_inst_id(nm, args.city)
                orphan += 1
            if not iid:
                if not keep_unattributed:
                    continue
                unattributed += 1
                iid = ""
            linked.append({**r, "inst_id": iid})
        if orphan:
            log.write(f"      {label}：{orphan} 筆的機構不在主檔內"
                      f"（多為已廢止或改名之機構）\n")
        if unattributed:
            log.write(f"      {label}：{unattributed} 筆未指名機構"
                      f"（市級背景輿情，保留但不計入機構分數）\n")
        return linked

    # canonical name → inst_id（供 PDF 資料掛載）
    name_index: dict[str, str] = {}
    dup_canon: set[str] = set()
    for iid, rec in master.items():
        ck = textnorm.canonical_name(rec["name"])
        if not ck:
            continue
        if ck in name_index and name_index[ck] != iid:
            dup_canon.add(ck)
        name_index.setdefault(ck, iid)
    if dup_canon:
        out(f"  注意：{len(dup_canon)} 個正規化名稱對應多所機構，"
            f"這些名稱不用於自動掛載")
        for ck in sorted(dup_canon):
            log.write(f"      重複正規化名稱：{ck}\n")
        for ck in dup_canon:
            name_index.pop(ck, None)

    # ------------------------------------------------------------ PDF 機構
    np_insts = read_csv(DATA / "institutions_nonprofit.csv")
    pdf_to_official: dict[str, str] = {}
    matched = unmatched = 0
    for r in np_insts:
        pdf_id = r.get("inst_id", "")
        name = r.get("name", "")
        if not pdf_id or not name:
            continue
        target = textnorm.match_institution(name, name_index)
        if target:
            matched += 1
            pdf_to_official[pdf_id] = target
            rec = master[target]
            # PDF 提供官方主檔沒有的園務規模欄位
            for col in ("enrolled", "staff_count", "teacher_count"):
                if r.get(col):
                    rec[col] = r[col]
            # 官方核定人數優先；官方缺值時才用 PDF 的
            if not rec.get("approved_capacity") and r.get("approved_capacity"):
                rec["approved_capacity"] = r["approved_capacity"]
            rec["data_sources"] = "官方主檔＋決算報告"
        else:
            # 比對不到就自成一筆，不硬塞給相近的園所
            unmatched += 1
            new_id = textnorm.stable_inst_id(name, args.city)
            pdf_to_official[pdf_id] = new_id
            master.setdefault(new_id, {
                "inst_id": new_id, "name": name,
                "city": r.get("city") or args.city, "district": "",
                "org_type": r.get("org_type", "非營利"),
                "inst_kind": institution_kind(name), "address": "",
                "phone": "", "website": "",
                "approved_capacity": r.get("approved_capacity", ""),
                "enrolled": r.get("enrolled", ""),
                "staff_count": r.get("staff_count", ""),
                "teacher_count": r.get("teacher_count", ""),
                "status": "", "child_service": "",
                "data_sources": "僅決算報告",
            })
            log.write(f"      未對應到官方主檔：{pdf_id} {name}\n")

    if np_insts:
        out(f"決算報告機構：{len(np_insts)} 所"
            f"（對應成功 {matched}、未對應 {unmatched}）")

    # ------------------------------------------------------------ 財務資料
    fin_rows: list[dict] = []
    dropped_fin = 0
    for r in read_csv(DATA / "financials_nonprofit.csv"):
        iid = pdf_to_official.get(r.get("inst_id", ""))
        if not iid:
            dropped_fin += 1
            continue
        fin_rows.append({**r, "inst_id": iid})

    ledger_rows: list[dict] = []
    dropped_led = 0
    for r in read_csv(DATA / "ledger_nonprofit.csv"):
        iid = pdf_to_official.get(r.get("inst_id", ""))
        if not iid:
            dropped_led += 1
            continue
        ledger_rows.append({**r, "inst_id": iid})

    # ------------------------------------------------------------ 輸出
    insts = sorted(master.values(),
                   key=lambda r: (r.get("district", ""), r.get("name", "")))
    write_csv(DATA / "institutions.csv", insts, INST_COLS)
    write_csv(DATA / "financials.csv", fin_rows, FIN_COLS)
    write_csv(DATA / "ledger.csv", ledger_rows, LEDGER_COLS)

    # ------------------------------------------------------------ 摘要
    raw_pen = read_csv(DATA / "penalties.csv")
    raw_eval = read_csv(DATA / "evaluations.csv")
    raw_post = read_csv(DATA / "posts.csv")
    n_pen_raw, n_eval_raw, n_post_raw = len(raw_pen), len(raw_eval), len(raw_post)

    penalties = relink(raw_pen, "裁罰")
    evaluations = relink(raw_eval, "評鑑")
    posts = relink(raw_post, "輿情", keep_unattributed=True)
    # 重新對應後寫回，讓主程式載入的四張表代碼一致。
    #
    # 寫回前必須確認筆數沒有減少：本腳本會就地覆寫採集腳本的產出，
    # 一旦對應邏輯有瑕疵而丟掉資料列，原始採集成果就永久消失，且重新採集
    # 可能受外部速率限制而無法立即補回（實測曾因此損失 274 則市級輿情）。
    def write_back(name: str, rows: list[dict], original: int) -> None:
        if not rows:
            return
        if len(rows) < original:
            out(f"  ⚠ {name} 對應後由 {original} 筆減為 {len(rows)} 筆，"
                f"為避免遺失原始採集資料，**不覆寫**該檔")
            log.write(f"      {name}: 拒絕覆寫（{original} → {len(rows)}）\n")
            return
        write_csv(DATA / name, rows, list(rows[0].keys()))

    write_back("penalties.csv", penalties, n_pen_raw)
    write_back("evaluations.csv", evaluations, n_eval_raw)
    write_back("posts.csv", posts, n_post_raw)

    inst_ids = {r["inst_id"] for r in insts}
    pen_ids = {r["inst_id"] for r in penalties if r.get("inst_id")}
    eval_ids = {r["inst_id"] for r in evaluations if r.get("inst_id")}
    post_ids = {r["inst_id"] for r in posts if r.get("inst_id")}
    fin_ids = {r["inst_id"] for r in fin_rows}
    verified = sum(1 for r in fin_rows if str(r.get("verified")) == "1")

    out("")
    out("=" * 66)
    out(f"機構主檔        {len(insts):>6} 所（皆為教保服務機構）")
    out(f"  設立別        {dict(Counter(r['org_type'] or '未分類' for r in insts))}")
    out(f"  組織型態      {dict(Counter(r['inst_kind'] for r in insts))}")
    out(f"  資料來源      {dict(Counter(r['data_sources'] for r in insts))}")
    out("")
    out(f"裁罰紀錄        {len(penalties):>6} 筆　涵蓋 {len(pen_ids)} 所"
        f"（{len(pen_ids & inst_ids)} 所在主檔內）")
    out(f"評鑑紀錄        {len(evaluations):>6} 筆　涵蓋 {len(eval_ids)} 所"
        f"（{len(eval_ids & inst_ids)} 所在主檔內）")
    out(f"輿情貼文        {len(posts):>6} 則　涵蓋 {len(post_ids)} 所"
        f"（{len(post_ids & inst_ids)} 所在主檔內）")
    out(f"決算彙總        {len(fin_rows):>6} 列　涵蓋 {len(fin_ids)} 所"
        f"（恆等式通過 {verified} 列）")
    out(f"科目明細        {len(ledger_rows):>6} 筆")
    if dropped_fin or dropped_led:
        out(f"  未掛載        決算 {dropped_fin} 列、明細 {dropped_led} 筆")
    out("")

    # 各構面的資料覆蓋率——這是判斷分數是否有鑑別力的關鍵指標
    n = max(1, len(insts))
    out("構面資料覆蓋率（覆蓋率過低的構面，其分數差異主要來自少數機構）")
    out(f"  法遵（裁罰）  {len(pen_ids & inst_ids) / n * 100:>5.1f}%")
    out(f"  評鑑          {len(eval_ids & inst_ids) / n * 100:>5.1f}%")
    out(f"  輿情          {len(post_ids & inst_ids) / n * 100:>5.1f}%")
    out(f"  財務          {len(fin_ids & inst_ids) / n * 100:>5.1f}%")
    out("=" * 66)

    if not penalties:
        out("\n提醒：尚無裁罰資料，監督式模型的訓練標籤會全為 0。"
            "請執行 python scripts/collect_ece.py")
    if not posts:
        out("\n提醒：尚無輿情資料，輿情構面分數會全為 0。"
            "請執行 python scripts/collect_sentiment.py")

    out(f"\n輸出：{DATA}\\institutions.csv, financials.csv, ledger.csv")
    out(f"日誌：{LOG_PATH}")
    log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
