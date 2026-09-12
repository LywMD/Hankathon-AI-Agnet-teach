"""從「非營利園財報」PDF 擷取收支餘絀表的**逐項會計科目**金額。

產出三個檔案（寫入 data/）：
* financials_nonprofit.csv — 彙總欄位（收入／支出／餘絀等，供比率計算）
* ledger_nonprofit.csv     — 逐筆科目明細（供班佛定律、末兩位數等鑑識檢定）
* institutions_nonprofit.csv — 招收人數與教保人員數（機構屬性以官方主檔為準，
                               此處僅補充官方未提供的園務規模欄位）

為什麼要有 ledger
-----------------
原本只存 11 個彙總欄位，末兩位數均勻性檢定與班佛定律根本沒有足夠的獨立
觀察數可用（單一機構僅 11 個數字），鑑識構面實質上是空轉。改為逐項擷取
後，每所園每年約有 17 個科目金額，四個學年度合計約 68 筆，才足以支撐
機構層級的數字型態檢定。

擷取策略
--------
1. 掃描前段頁面找出「當學年度」收支餘絀表（第一張；同一份報告的第二張
   是上一學年度的比較表，該年度數字會在該學年度自己的報告中擷取，
   不重複計入）。
2. 以章節標記（「收入」／「支出」）切分段落，段落內用模糊比對辨識科目，
   避開 OCR 誤字（教保費→教係費、業務費→緒務費…）。
3. 表格欄位順序為：預算數 | 決算數 | 比較增減 | 執行率，取**決算數**
   （第 2 個數字）。
4. 以會計恆等式交叉驗證，並用 textnorm.sane_amount 擋掉 OCR 位數截斷。
"""
from __future__ import annotations

import csv
import os
import re
import sys
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import textnorm  # noqa: E402
from ocr_common import (check_ocr_ready, fuzzy_pick, ocr_page,  # noqa: E402
                        parse_numbers, strip_spaces)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_SOURCE = Path(os.getenv("DATA_SOURCE_PATH", r"C:\Users\lyw01\Desktop\w"))
SRC_ROOT = DATA_SOURCE / "非營利園財報"
OUT_DIR = BASE_DIR / "data"
LOG_PATH = BASE_DIR / ".tmp" / "extract_nonprofit_log.txt"

YEAR_MAP = {"110學年度": 2021, "111學年度": 2022, "112學年度": 2023,
            "113學年度": 2024, "114學年度": 2025}

NAME_RE = re.compile(r"^(N\d+)([^_]+)_(\d+)學年度財務報告\.pdf$")

# ---------------------------------------------------------------- 科目定義
# 收支餘絀表的標準科目（依報表出現順序），分收入／支出兩段。
# 註：110～111 學年度的報表把課後留園服務稱為「延後托育」，
# 112 學年度起改稱「延長照顧服務」。兩種名稱都須列入，否則舊年度會漏抓
# 該科目，造成收入／支出明細與合計對不起來。
REVENUE_ACCOUNTS = [
    "教保費收入減項",      # 需排在「教保費收入」之前，避免前綴比對先命中
    "教保費收入",
    "政府補助收入",
    "延長照顧服務收入",
    "延後托育收入",
    "利息收入",
    "其他收入",
]
REVENUE_TOTAL = "收入合計"

EXPENSE_ACCOUNTS = [
    "人事費",
    "業務發展費",          # 需排在「業務費」之前
    "業務費",
    "材料費",
    "維護費",
    "修繕購置費",
    "雜支",
    "行政管理費",
    "公共事務管理費",
    "延長照顧服務支出",
    "延後托育支出",
    "其他支出",
    "呆帳損失",
    "所得稅費用",
]
EXPENSE_TOTAL = "支出合計"

SURPLUS_ACCOUNTS = ["本期稅前餘絀", "本期稅後餘絀"]

# 彙總欄位如何由科目組成（對應 app/datastore.py 的標準欄名）
AGGREGATION = {
    "revenue_tuition": ("教保費收入", "教保費收入減項"),
    "revenue_subsidy": ("政府補助收入",),
    "revenue_other": ("延長照顧服務收入", "延後托育收入", "利息收入",
                      "其他收入"),
    "expense_personnel": ("人事費",),
    "expense_teaching": ("材料費", "業務費"),
    "expense_facility": ("維護費", "修繕購置費"),
    "expense_admin": ("行政管理費", "公共事務管理費", "雜支"),
    "expense_other": ("業務發展費", "延長照顧服務支出", "延後托育支出",
                      "其他支出", "呆帳損失", "所得稅費用"),
}


def _section_of(line: str) -> str | None:
    """判斷是否為段落標記行（單獨的「收入」或「支出」）。

    必須容忍尾端雜訊：實測 OCR 把段落標記讀成「支 出 ﹌」，若用精確比對，
    段落不會切換，接著整個支出段的科目都只會拿去比對收入科目而全部落空
    （實測 N01/N06/N13/N21/N23/N24 的支出明細因此全為 0）。
    因此改為「只保留中文字後是否恰為 收入／支出」，且該行不得含數字
    （避免把「收入合計 …金額…」誤判成段落標記）。
    """
    t = strip_spaces(line)
    if any(ch.isdigit() for ch in t):
        return None
    cjk = re.sub(r"[^\u4e00-\u9fff]", "", t)
    if cjk == "收入":
        return "revenue"
    if cjk == "支出":
        return "expense"
    return None


def _label_of(line: str) -> str:
    """取出行首的科目名稱（第一個數字之前的中文字）。"""
    t = strip_spaces(line)
    # 截到第一個數字或貨幣符號為止
    m = re.search(r"[0-9$\uFFE5\uFE69(\uFF08]", t)
    if m:
        t = t[:m.start()]
    # 科目後常接附註編號（「二、三」「二、五」等），以頓號切掉
    t = re.split(r"[、﹒·．,，]", t)[0]
    return re.sub(r"[^\u4e00-\u9fff]", "", t)


def _settled_amount(nums: list[float]) -> float | None:
    """從一列的數字中取「決算數」。

    表格欄位為：預算數 | 決算數 | 比較增減 | 執行率。但**並非每列都四欄齊全**，
    取值位置必須依實際數字個數判斷：

    * 4 個 → [預算, 決算, 增減, 執行率]              取 index 1
    * 3 個 → [預算, 決算, 增減] 或 [預算, 決算, 執行率]  取 index 1
    * 2 個 → 預算欄為「-」的科目（利息收入、其他收入、延後托育…），
             實際是 [決算, 增減] 且兩者相等          取 index 0
    * 1 個 → 只有決算數                            取 index 0

    若固定取 index 1，2 欄的情形會拿到「比較增減」欄。實測 N03 的
    「其他收入」該兩欄為 1,675,731 與 675,731（後者被 OCR 掉了首位數），
    取 index 1 會得到少一位數的金額，並讓收入明細與合計差距近百萬。

    兩欄但數值不相等時，若較小者剛好是較大者的尾數，即為典型的 OCR
    掉字，取較大者。
    """
    if not nums:
        return None
    if len(nums) == 1:
        return nums[0]
    if len(nums) == 2:
        a, b = nums
        if a == b:
            return a
        sa, sb = f"{abs(a):.0f}", f"{abs(b):.0f}"
        if len(sa) > len(sb) and sa.endswith(sb):
            return a
        if len(sb) > len(sa) and sb.endswith(sa):
            return b
        return a
    return nums[1]


def extract_income_statement(lines: list[str]) -> tuple[dict, dict]:
    """解析收支餘絀表，回傳 (科目金額 dict, 診斷資訊 dict)。"""
    section: str | None = None
    accounts: dict[str, float] = {}
    diag = {"unmatched": [], "section_seen": []}

    for line in lines:
        sec = _section_of(line)
        if sec:
            section = sec
            diag["section_seen"].append(sec)
            continue

        label = _label_of(line)
        if len(label) < 2:
            continue
        nums = parse_numbers(line)
        if not nums:
            continue

        # 合計／餘絀列不分段落，優先比對
        pick = fuzzy_pick(label, [REVENUE_TOTAL, EXPENSE_TOTAL] + SURPLUS_ACCOUNTS)
        if pick is None:
            # 先在當前段落內比對；比對不到再試另一段。
            # 段落優先是為了處理「延長照顧服務收入／支出」這種只差尾字的科目；
            # 但段落標記本身可能被 OCR 吃掉，所以不能只靠段落，需有跨段退路。
            # fuzzy_pick 要求候選科目的尾字必須出現在標籤中，因此跨段比對
            # 仍不會把「…收入」誤認為「…支出」。
            if section == "revenue":
                order = (REVENUE_ACCOUNTS, EXPENSE_ACCOUNTS)
            elif section == "expense":
                order = (EXPENSE_ACCOUNTS, REVENUE_ACCOUNTS)
            else:
                order = (REVENUE_ACCOUNTS + EXPENSE_ACCOUNTS,)
            for cand in order:
                pick = fuzzy_pick(label, cand)
                if pick is not None:
                    break

        if pick is None:
            if len(label) >= 3:
                diag["unmatched"].append(label)
            continue

        val = _settled_amount(nums)
        if val is None:
            continue
        # 同一科目重複出現（跨頁重印）時保留先出現者
        accounts.setdefault(pick, val)

    return accounts, diag


def pick_surplus(acc: dict[str, float]) -> float | None:
    """在稅前／稅後餘絀之間選出與收支合計一致者。

    實測 N01 113 學年度的「本期稅前餘絀」被 OCR 讀成 1,088,771，
    而「本期稅後餘絀」為 1,988,771；由 收入合計 20,155,978 −
    支出合計 22,144,749 = −1,988,771 可知後者才對。既然報表本身提供了
    可驗證的冗餘資訊，就應該用它來自我校正，而不是固定取某一欄。

    所得稅費用為 0 或未編列時，稅前與稅後應相等；兩者不等即代表其中
    一個被誤讀，此時選擇與 收入合計−支出合計 較接近者。
    """
    pre = acc.get("本期稅前餘絀")
    post = acc.get("本期稅後餘絀")
    rev, exp = acc.get(REVENUE_TOTAL), acc.get(EXPENSE_TOTAL)
    cands = [v for v in (post, pre) if v is not None]
    if not cands:
        return None
    if rev is None or exp is None:
        return cands[0]
    implied = rev - exp
    return min(cands, key=lambda v: abs(v - implied))


def validate_statement(acc: dict[str, float]) -> dict:
    """以會計恆等式驗證擷取結果。

    三項檢核：
    1. 收入明細加總 ≈ 收入合計
    2. 支出明細加總 ≈ 支出合計
    3. 收入合計 − 支出合計 ≈ 本期稅後餘絀

    容差設為合計的 2%（絕對值下限 1,000 元）：OCR 偶有個別科目漏抓，
    容許小幅落差，但位數級的錯誤（差一個數量級）一定會被抓出來。
    """
    out = {"checks": [], "ok": True}

    def close(a, b, base):
        tol = max(1000.0, abs(base) * 0.02)
        return abs(a - b) <= tol

    rev_total = acc.get(REVENUE_TOTAL)
    exp_total = acc.get(EXPENSE_TOTAL)
    surplus = pick_surplus(acc)

    if rev_total is not None:
        detail = sum(acc.get(a, 0.0) for a in REVENUE_ACCOUNTS)
        good = close(detail, rev_total, rev_total)
        out["checks"].append({"name": "收入明細=合計", "detail": detail,
                              "total": rev_total, "ok": good})
        out["ok"] &= good
    if exp_total is not None:
        detail = sum(acc.get(a, 0.0) for a in EXPENSE_ACCOUNTS)
        good = close(detail, exp_total, exp_total)
        out["checks"].append({"name": "支出明細=合計", "detail": detail,
                              "total": exp_total, "ok": good})
        out["ok"] &= good
    if rev_total is not None and exp_total is not None and surplus is not None:
        good = close(rev_total - exp_total, surplus, rev_total)
        out["checks"].append({"name": "收支差=餘絀",
                              "detail": rev_total - exp_total,
                              "total": surplus, "ok": good})
        out["ok"] &= good
    return out


def build_financial_row(inst_id: str, fy: int, acc: dict[str, float]) -> dict:
    """把科目金額彙總成 financials 表的一列。"""
    rev_total = acc.get(REVENUE_TOTAL)
    exp_total = acc.get(EXPENSE_TOTAL)
    row: dict = {"inst_id": inst_id, "fiscal_year": fy}

    for col, parts in AGGREGATION.items():
        vals = [acc[p] for p in parts if p in acc]
        if not vals:
            # 該科目在本年度報表中不存在：留空，不填 0。
            # 填 0 會讓「未提供」與「確實為零」無法區分，也會製造
            # 整欄常數而讓該特徵完全失去鑑別力。
            row[col] = ""
            continue
        base = exp_total if col.startswith("expense") else rev_total
        v = textnorm.sane_amount(sum(vals), total=base)
        row[col] = "" if v is None else round(v, 2)

    # 非營利園依實施辦法由政府無償提供場地，決算表未編列租金科目。
    # 保持空值而非 0，理由同上。
    row["expense_rent"] = ""
    row["total_revenue"] = "" if rev_total is None else round(rev_total, 2)
    row["total_expense"] = "" if exp_total is None else round(exp_total, 2)
    surplus = pick_surplus(acc)
    row["surplus"] = "" if surplus is None else round(surplus, 2)
    return row


# ---------------------------------------------------------------- 招收人數
def extract_enrollment(doc, page_idx: int = 9) -> dict:
    """自財務報表附註「一、一般概況」擷取核定/實際招收人數與人員數。

    此段落緊接四大財表之後，位置在各機構報告中皆穩定為第 10 頁（索引 9）。
    句子常跨行，故先合併整頁文字再以關鍵字定位窗口擷取。
    """
    out = {"approved_capacity": None, "enrolled": None,
           "staff_count": None, "teacher_count": None}
    if doc.page_count <= page_idx:
        return out
    compact = strip_spaces(ocr_page(doc, page_idx))

    def window(start: int, terminators: tuple[str, ...], maxlen: int) -> str:
        end = start + maxlen
        for t in terminators:
            m = compact.find(t, start)
            if m != -1:
                end = min(end, m)
        return compact[start:end]

    i = compact.find("核定")
    j = compact.find("招收")
    if i >= 0 and 0 <= j - i <= 6:
        # 以句號或下一個項目編號為界，避免擷取範圍跨入下一段而把
        # 分齡班級人數當成全園總數。
        seg = window(i, ("。", "七﹚", "七)", "七］", "七】"), 40)
        nums = [int(n) for n in parse_numbers(seg) if 1 <= n <= 2000]
        if len(nums) >= 2:
            out["approved_capacity"], out["enrolled"] = nums[0], nums[1]
        elif len(nums) == 1:
            out["approved_capacity"] = out["enrolled"] = nums[0]

    k = compact.find("員工人數")
    if k >= 0:
        seg = window(k, ("。",), 40)
        nums = [int(n) for n in parse_numbers(seg) if 1 <= n <= 500]
        if len(nums) >= 2:
            out["staff_count"], out["teacher_count"] = nums[0], nums[1]
        elif len(nums) == 1:
            out["staff_count"] = nums[0]

    # 以核定人數為基準檢核實際招收數，擋掉 OCR 位數截斷
    out["enrolled"] = textnorm.sane_headcount(out["enrolled"],
                                              cap=out["approved_capacity"])
    out["approved_capacity"] = textnorm.sane_headcount(out["approved_capacity"])
    return out


def extract_with_retry(doc, page_idx: int, log=None) -> tuple[dict, dict, dict]:
    """擷取收支餘絀表，恆等式未通過時以更高解析度重新 OCR。

    OCR 對金額的位數誤讀（實測 N04 的收入合計 7,116,321 被讀成 71,163,219、
    N20 的教保費收入只讀到 65,218）多半能靠提高解析度解決。既然報表自帶
    可驗證的冗餘（明細加總＝合計、收支差＝餘絀），就用它當品質關卡：
    依序嘗試多組 OCR 參數，取第一組通過恆等式的結果；全部失敗時回傳
    科目數最多的那組並標記 verified=0，讓下游知道這列不可信。

    psm 4（假設為單欄可變大小文字）對表格的欄位切分方式與 psm 6（單一
    統一文字區塊）不同，實測可救回部分被 psm 6 誤併的數字欄。
    """
    attempts = ((300, 6), (400, 6), (400, 4))
    best: tuple[dict, dict, dict] | None = None

    for dpi, psm in attempts:
        text = ocr_page(doc, page_idx, dpi=dpi, psm=psm)
        lines = [l for l in text.splitlines() if l.strip()]
        acc, diag = extract_income_statement(lines)
        if not acc:
            continue
        check = validate_statement(acc)
        diag["ocr"] = {"dpi": dpi, "psm": psm}
        if check["ok"]:
            if log and (dpi, psm) != attempts[0]:
                log.write(f"      恆等式重試成功：dpi={dpi} psm={psm}\n")
            return acc, diag, check
        if best is None or len(acc) > len(best[0]):
            best = (acc, diag, check)

    if best is None:
        return {}, {"unmatched": [], "section_seen": []}, {"checks": [], "ok": False}
    if log:
        log.write(f"      恆等式重試後仍未通過（採用 "
                  f"dpi={best[1]['ocr']['dpi']} psm={best[1]['ocr']['psm']}）\n")
    return best


def find_income_statement(doc, log=None) -> tuple[int, list[str]] | None:
    """找出「當學年度」收支餘絀表頁面。

    以實測樣本為準，四大財表位於索引 5~8；收支餘絀表為第一張含
    「收入合計／支出合計」的頁面。依可能性排序掃描以減少 OCR 次數。
    """
    for idx in (5, 6, 4, 7, 3, 8, 9, 2):
        if idx >= doc.page_count:
            continue
        text = ocr_page(doc, idx)
        lines = [l for l in text.splitlines() if l.strip()]
        joined = strip_spaces(text)
        # 需同時出現收入與支出的合計列，才確定是收支餘絀表
        # （資產負債表也會有「合計」但沒有這兩個科目）
        has_rev = any(fuzzy_pick(_label_of(l), [REVENUE_TOTAL]) for l in lines)
        has_exp = any(fuzzy_pick(_label_of(l), [EXPENSE_TOTAL]) for l in lines)
        if has_rev and has_exp:
            if log:
                log.write(f"      收支餘絀表 = page index {idx}\n")
            return idx, lines
    return None


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    ready, msg = check_ocr_ready()
    print(f"OCR 環境：{msg}")
    if not ready:
        print("錯誤：OCR 環境未就緒，無法擷取（PDF 文字層損毀，必須 OCR）。")
        sys.exit(1)

    log = LOG_PATH.open("w", encoding="utf-8")
    log.write(f"OCR: {msg}\n來源: {SRC_ROOT}\n\n")

    if not SRC_ROOT.exists():
        print(f"錯誤：找不到來源目錄 {SRC_ROOT}")
        log.write(f"來源目錄不存在\n")
        log.close()
        sys.exit(1)

    institutions: dict[str, dict] = {}
    financials: list[dict] = []
    ledger: list[dict] = []
    failures: list[str] = []
    unmatched_all: dict[str, int] = {}

    files = sorted(SRC_ROOT.glob("*學年度/*.pdf"))
    total = len(files)
    print(f"共 {total} 份 PDF")

    # 招收人數／人員數只需取各機構「最新學年度」的值：這些欄位描述的是
    # 當前園務規模，逐年重複 OCR 附註頁會讓整體時間多出四成，卻只會被
    # 較新年度覆蓋掉。先算出每個機構的最新年度，之後僅對該份報告額外 OCR。
    latest_fy: dict[str, int] = {}
    for p in files:
        mm = NAME_RE.match(p.name)
        fyy = YEAR_MAP.get(p.parent.name)
        if mm and fyy:
            code = mm.group(1)
            latest_fy[code] = max(latest_fy.get(code, 0), fyy)

    for n, path in enumerate(files, 1):
        m = NAME_RE.match(path.name)
        fy = YEAR_MAP.get(path.parent.name)
        if not m or not fy:
            failures.append(f"{path.name}: 檔名或年度資料夾格式不符")
            log.write(f"[{n}/{total}] SKIP {path.name}\n")
            continue
        code, raw_name, _ = m.groups()
        inst_id = code
        display_name = f"新北市{textnorm.clean_ocr_text(raw_name)}非營利幼兒園"
        institutions.setdefault(inst_id, {
            "inst_id": inst_id, "name": display_name, "city": "新北市",
            "org_type": "非營利",
        })

        log.write(f"[{n}/{total}] {path.name}  (FY{fy})\n")
        try:
            doc = pymupdf.open(path)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{path.name}: 無法開啟（{exc}）")
            log.write(f"      FAIL 無法開啟：{exc}\n")
            continue

        try:
            found = find_income_statement(doc, log)
            if not found:
                failures.append(f"{path.name}: 找不到收支餘絀表")
                log.write("      FAIL 找不到收支餘絀表\n")
                continue

            idx, lines = found
            acc, diag, check = extract_with_retry(doc, idx, log)
            for u in diag["unmatched"]:
                unmatched_all[u] = unmatched_all.get(u, 0) + 1

            if not acc:
                failures.append(f"{path.name}: 收支餘絀表無可辨識科目")
                log.write("      FAIL 無可辨識科目\n")
                continue

            log.write(f"      科目 {len(acc)} 項：{sorted(acc)}\n")
            for c in check["checks"]:
                flag = "OK " if c["ok"] else "!! "
                log.write(f"      {flag}{c['name']}: 明細={c['detail']:,.0f} "
                          f"報表={c['total']:,.0f}\n")
            if not check["ok"]:
                log.write("      注意：恆等式未通過，該列已標記 verified=0\n")

            financials.append({**build_financial_row(inst_id, fy, acc),
                               "verified": 1 if check["ok"] else 0})

            # 逐項科目寫入 ledger（供末兩位數／班佛檢定使用）
            for name, val in acc.items():
                if name in (REVENUE_TOTAL, EXPENSE_TOTAL):
                    kind = "total"
                elif name in SURPLUS_ACCOUNTS:
                    kind = "surplus"
                elif name in REVENUE_ACCOUNTS:
                    kind = "revenue"
                else:
                    kind = "expense"
                ledger.append({
                    "inst_id": inst_id, "fiscal_year": fy,
                    "account": name, "kind": kind, "amount": round(val, 2),
                })

            # 招收人數：只在該機構最新學年度的報告上擷取（見上方說明）
            if fy == latest_fy.get(inst_id):
                try:
                    enr = extract_enrollment(doc)
                    for k, v in enr.items():
                        if v is not None:
                            institutions[inst_id][k] = v
                    log.write(f"      招收：{enr}\n")
                except Exception as exc:  # noqa: BLE001
                    log.write(f"      WARN 招收人數擷取失敗：{exc}\n")

            print(f"[{n}/{total}] {path.name}: {len(acc)} 科目, "
                  f"恆等式={'OK' if check['ok'] else 'FAIL'}", flush=True)
        finally:
            doc.close()
            log.flush()

    # ------------------------------------------------------------ 輸出
    inst_cols = ["inst_id", "name", "city", "org_type", "approved_capacity",
                 "enrolled", "staff_count", "teacher_count"]
    with (OUT_DIR / "institutions_nonprofit.csv").open(
            "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=inst_cols, extrasaction="ignore")
        w.writeheader()
        for r in institutions.values():
            w.writerow(r)

    fin_cols = ["inst_id", "fiscal_year", "revenue_tuition", "revenue_subsidy",
                "revenue_other", "total_revenue", "expense_personnel",
                "expense_teaching", "expense_facility", "expense_rent",
                "expense_admin", "expense_other", "total_expense", "surplus",
                "verified"]
    with (OUT_DIR / "financials_nonprofit.csv").open(
            "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fin_cols, extrasaction="ignore")
        w.writeheader()
        for r in financials:
            w.writerow(r)

    with (OUT_DIR / "ledger_nonprofit.csv").open(
            "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["inst_id", "fiscal_year", "account",
                                          "kind", "amount"])
        w.writeheader()
        for r in ledger:
            w.writerow(r)

    verified = sum(1 for r in financials if r.get("verified"))
    log.write(f"\n{'=' * 60}\n")
    log.write(f"PDF {total} 份；決算列 {len(financials)} 筆"
              f"（恆等式通過 {verified}）；科目明細 {len(ledger)} 筆；"
              f"機構 {len(institutions)} 所；失敗 {len(failures)} 筆\n")
    if unmatched_all:
        log.write("\n未能對應的標籤（次數 >= 2 者可考慮加入科目清單）：\n")
        for k, v in sorted(unmatched_all.items(), key=lambda x: -x[1]):
            if v >= 2:
                log.write(f"  {v:>3}x  {k}\n")
    for x in failures:
        log.write("FAILED: " + x + "\n")
    log.close()

    print(f"\n完成：決算 {len(financials)} 筆（恆等式通過 {verified}）、"
          f"科目明細 {len(ledger)} 筆、機構 {len(institutions)} 所、"
          f"失敗 {len(failures)} 筆")
    print(f"日誌：{LOG_PATH}")


if __name__ == "__main__":
    main()
