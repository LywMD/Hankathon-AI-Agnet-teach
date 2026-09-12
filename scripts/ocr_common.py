"""決算 PDF 的共用 OCR 與表格解析工具。

背景
----
非營利園財報與公校決算書的 PDF 文字層都內嵌了缺少 ToUnicode 對照表的字型，
pdfplumber / PyMuPDF 直接取文字一律亂碼，因此必須「頁面轉點陣圖 + Tesseract
OCR（繁體中文）」。

OCR 的實際誤字情形（取自 113 學年度樣本，已逐頁比對）
--------------------------------------------------
    教保費收入   → 教係費收入
    業務費       → 緒務費 / 素務費
    修繕購置費   → 值繒購置費 / 倍繒購置費
    業務發展費   → 緒務發展費 / 素務發展費
    呆帳損失     → 紙帳損失
    延長照顧服務 → 廷長照顧服務

也就是說，用**精確字串**比對科目名稱一定會漏抓（原本 revenue_tuition 整欄
空白就是這個原因）。本模組因此改採：

1. **章節感知**：先辨識「收入」「支出」段落標記，把候選科目限制在該段落內，
   解決「延長照顧服務收入」與「延長照顧服務支出」只差一字、模糊比對無法
   區分的問題。
2. **模糊科目比對**：以編輯距離加上「尾字必須相符」的條件排序候選，
   避免收入誤判為支出。
3. **會計恆等式驗證**：以「明細加總 ≈ 合計」「收入合計 − 支出合計 ≈ 餘絀」
   交叉檢核。OCR 偶爾會多讀或少讀一位數（實測 支出合計差異欄
   1,357,797 被讀成 13,357,797），恆等式可以把這類錯誤攔下來。
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------- OCR 設定
def _resolve_tesseract() -> str:
    """找出 tesseract 執行檔（環境變數優先，再試常見安裝路徑）。"""
    env = os.getenv("TESSERACT_PATH")
    if env and Path(env).exists():
        return env
    for cand in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                 r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"):
        if Path(cand).exists():
            return cand
    return "tesseract"


def _resolve_tessdata() -> str:
    """找出含 chi_tra.traineddata 的 tessdata 目錄。

    優先使用專案內 resources/tessdata：原先指向 %LOCALAPPDATA%\\Temp\\tessdata，
    但 Temp 會被系統清理，導致重跑萃取時突然找不到繁中訓練資料。
    """
    env = os.getenv("TESSDATA_PREFIX")
    cands = [env] if env else []
    cands += [
        str(BASE_DIR / "resources" / "tessdata"),
        str(Path(os.getenv("LOCALAPPDATA", "")) / "Temp" / "tessdata"),
        r"C:\Program Files\Tesseract-OCR\tessdata",
    ]
    for c in cands:
        if c and (Path(c) / "chi_tra.traineddata").exists():
            return c
    return cands[0] or ""


TESSERACT = _resolve_tesseract()
TESSDATA = _resolve_tessdata()
if TESSDATA:
    os.environ["TESSDATA_PREFIX"] = TESSDATA


def check_ocr_ready() -> tuple[bool, str]:
    """檢查 OCR 環境是否就緒，回傳 (是否可用, 說明)。"""
    if not Path(TESSERACT).exists() and TESSERACT != "tesseract":
        return False, f"找不到 tesseract 執行檔：{TESSERACT}"
    if not TESSDATA or not (Path(TESSDATA) / "chi_tra.traineddata").exists():
        return False, (f"找不到繁體中文訓練資料 chi_tra.traineddata"
                       f"（已檢查 {TESSDATA}）")
    try:
        r = subprocess.run([TESSERACT, "--version"], capture_output=True,
                           timeout=15)
        ver = r.stdout.decode("utf-8", errors="replace").splitlines()
        return True, f"{ver[0] if ver else 'tesseract'}｜tessdata={TESSDATA}"
    except Exception as exc:  # noqa: BLE001
        return False, f"tesseract 無法執行：{exc}"


def ocr_page(doc, idx: int, dpi: int = 300, psm: int = 6) -> str:
    """把 PDF 單頁轉點陣圖後做 OCR，回傳文字。"""
    import pymupdf

    page = doc[idx]
    pix = page.get_pixmap(matrix=pymupdf.Matrix(dpi / 72, dpi / 72))
    tmp_img = tempfile.mktemp(suffix=".png")
    pix.save(tmp_img)
    try:
        out = subprocess.run(
            [TESSERACT, tmp_img, "stdout", "-l", "chi_tra", "--psm", str(psm)],
            capture_output=True, timeout=180,
        )
        return out.stdout.decode("utf-8", errors="replace")
    finally:
        try:
            os.remove(tmp_img)
        except OSError:
            pass


# ---------------------------------------------------------------- 數字解析
# Tesseract 對「單位：新臺幣元」表格中的千分位逗號／貨幣符號／括號，常辨識為
# 直式標點的異體字（U+FE52 小型句點、U+FE69 小型錢號…），而非標準 ASCII，
# 須全數納入字元集合，否則千分位會被當成數字邊界，把單一金額截成多個小數字。
_SEP = "\uFF0C\uFE52\u3001\u2027,.\u00B7\uFE50\uFE51"
_OPEN = "(\uFF08\uFE59\uFE35\uFE5D"
_CLOSE = ")\uFF09\uFE5A\uFE36\uFE5E"
_CUR = "$\uFFE5\uFE69\u00A5\uFF04"
NUM_CHARS = set("0123456789" + _SEP + _OPEN + _CLOSE + _CUR + "%\uFE6A\uFF05")


def parse_numbers(line: str) -> list[float]:
    """擷取一行中所有金額。

    先掃出「數字與其鄰接標點」的連續區段，區段內僅取數字字元組成整數，
    並以區段內是否出現括號判斷正負，避免千分位分隔符的 OCR 誤判
    （句點、頓號等異體字）把單一金額截成多個小數字。
    """
    out: list[float] = []
    run = ""
    for ch in str(line) + " ":
        if ch in NUM_CHARS:
            run += ch
        else:
            if any(c.isdigit() for c in run):
                digits = "".join(c for c in run if c.isdigit())
                if digits:
                    v = float(digits)
                    if any(c in _OPEN or c in _CLOSE for c in run):
                        v = -v
                    out.append(v)
            run = ""
    return out


def strip_spaces(s: str) -> str:
    """移除所有空白（OCR 會在每個中文字之間插入空格）。"""
    return "".join(str(s).split())


# ---------------------------------------------------------------- 模糊科目比對
def edit_distance(a: str, b: str) -> int:
    """Levenshtein 距離。"""
    if a == b:
        return 0
    if not a or not b:
        return max(len(a), len(b))
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def fuzzy_pick(label: str, candidates: list[str],
               max_ratio: float = 0.45) -> str | None:
    """從候選科目名稱中找出與 OCR 標籤最相符者。

    排序規則：
    1. 完全相同或候選為標籤的前綴 → 直接命中
    2. **候選科目的尾字必須出現在標籤中**（硬性條件）
    3. 否則以「編輯距離 / 候選長度」為分數，取最小者，且需 ≤ max_ratio

    第 2 條是必要的：「延長照顧服務收入」與「延長照顧服務支出」編輯距離
    只有 2（8 字中的 2 字），任何寬鬆的距離門檻都會讓兩者互相誤判，
    把收入記成支出會直接汙染所有財務比率。要求尾字（入／出／費／計／失）
    出現在標籤中即可區辨，且仍容許「延長照顧服務收入淨額」被 OCR 成
    「延長照顧服務收入淨顒」這類尾綴誤讀（因為「入」仍在字串中）。

    若 OCR 連尾字都讀錯，本函式會回傳 None（視為擷取失敗）。對鑑識用途而言，
    留空遠優於把金額掛到錯誤科目。

    max_ratio 預設 0.45：以「材料費」(3 字) 為例允許 1 字誤讀，
    「延長照顧服務收入」(8 字) 允許 3 字誤讀，符合實測 OCR 誤字率。
    """
    lab = strip_spaces(label)
    if not lab:
        return None
    best: tuple[float, str] | None = None
    for c in candidates:
        if lab == c or lab.startswith(c):
            return c
        if c and c[-1] not in lab:
            continue
        # 全字串比對，以及「取標籤前綴」比對兩者取較好的分數。
        # 前綴比對是必要的：科目名固定出現在行首，其後常黏著附註欄字元
        # （實測「業務費 五、同」被 OCR 成「緒務費丘同」、「修繕購置費 二、貳」
        # 成「值繒購置費二」）。只比全字串會因尾端雜字而距離爆增被誤判為不符。
        ratio = edit_distance(lab, c) / max(1, len(c))
        if len(lab) > len(c):
            ratio = min(ratio, edit_distance(lab[:len(c)], c) / max(1, len(c)))
        if ratio > max_ratio:
            continue
        key = (ratio, c)
        if best is None or key < best:
            best = key
    return best[1] if best else None
