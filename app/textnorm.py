"""文字正規化與資料校驗工具（純標準函式庫）。

三個用途：

1. **機構名稱比對**：PDF 決算報告與全國教保資訊網對同一所園的寫法不同
   （前者「新北市安溪非營利幼兒園」，後者可能帶法人前綴如
   「○○文教股份有限公司附設新北市私立○○幼兒園」）。本模組提供
   canonical key 與模糊比對，讓財務資料能正確掛到官方機構主檔上。

2. **OCR 文字清理**：決算 PDF 以 OCR 讀取，地址等欄位常帶入直式標點的
   異體字雜訊（﹍﹒﹔﹀等）；行政區也可能出現誤字（新苗區／鷺歌區）。
   注意：機構的行政區與地址一律以官方主檔為準，本模組的行政區校正
   僅在查無官方資料時作為退路。

3. **金額合理性校驗**：OCR 可能把「7,099,629」截斷成「7」。這類錯誤若
   直接寫入決算表，會讓人事費率之類的比率完全失真，且殘差會被推到
   其他科目造成連鎖錯誤。因此提供以「佔總額比例」為基礎的合理性檢核，
   不合理者一律回傳 None（視為擷取失敗），而非留下錯誤數字。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

# ---------------------------------------------------------------- 行政區
# 新北市 29 個行政區
NEW_TAIPEI_DISTRICTS = (
    "板橋區", "三重區", "中和區", "永和區", "新莊區", "新店區", "樹林區",
    "鶯歌區", "三峽區", "淡水區", "汐止區", "瑞芳區", "土城區", "蘆洲區",
    "五股區", "泰山區", "林口區", "深坑區", "石碇區", "坪林區", "三芝區",
    "石門區", "八里區", "平溪區", "雙溪區", "貢寮區", "金山區", "萬里區",
    "烏來區",
)

# 已實際觀察到的 OCR 誤判（明確列舉，便於審核與追溯）。
# 之所以採用白名單映射而非純編輯距離：像「新苗」到「新莊」與「新店」的
# 編輯距離都是 1，純距離無法判斷，硬猜會產生錯誤的行政區統計。
DISTRICT_OCR_FIXES = {
    "新苗區": "新莊區",
    "新菜區": "新莊區",
    "鷺歌區": "鶯歌區",
    "驚歌區": "鶯歌區",
    "蘆州區": "蘆洲區",
    "汐上區": "汐止區",
    "士城區": "土城區",
    "五穀區": "五股區",
    "泰川區": "泰山區",
    "淡小區": "淡水區",
    "永合區": "永和區",
    "中合區": "中和區",
    "板檐區": "板橋區",
    "三崚區": "三峽區",
    "萬裡區": "萬里區",
}

# OCR 常見的直式／小型標點異體字，出現在中文欄位開頭或中間都屬雜訊
_OCR_NOISE_CHARS = (
    "\uFE52"  # ﹒ 小型句號
    "\uFE55"  # ﹕
    "\uFE54"  # ﹔
    "\uFE51"  # ﹑
    "\uFE50"  # ﹐
    "\uFE4F"  # ﹏
    "\uFE4D"  # ﹍
    "\uFE4E"  # ﹎
    "\uFE31"  # ︱
    "\uFE30"  # ︰
    "\uFF00"
    "\u2027"  # ‧
    "\u00B7"  # ·
    "\u02D9"  # ˙
    "\uFF64"  # ､
    "\u3002"  # 。
    "\uFE35\uFE36"  # ︵ ︶
    "\u2570\u256D\u2572\u2571"  # 框線字元
    "\uFF40\u0060\u00A8\u02C7\u02CB"  # ` ¨ ˇ ˋ
)
_NOISE_EDGE_RE = re.compile(
    rf"^[\s{re.escape(_OCR_NOISE_CHARS)}]+|[\s{re.escape(_OCR_NOISE_CHARS)}]+$")
# NFKC 正規化會把部分相容字元轉為 ASCII（例如 ﹍ U+FE4D → _、︲ → -），
# 因此正規化之後必須再清一次，且清理集合要含這些 ASCII 對應字元。
_NOISE_EDGE_ASCII_RE = re.compile(r"^[\s_\-~`^'\".,;:·・]+|[\s_\-~`^'\"]+$")


def clean_ocr_text(s: str) -> str:
    """去除 OCR 產生的邊緣雜訊字元並正規化全形字。

    清理需在 NFKC 之前與之後各做一次：NFKC 會把直式標點的相容字元
    （如 ﹍ U+FE4D）轉成 ASCII 底線，若只在正規化前清理，殘留的 `_`
    會留在欄位開頭。
    """
    if not s:
        return ""
    t = _NOISE_EDGE_RE.sub("", str(s))
    t = unicodedata.normalize("NFKC", t)
    t = _NOISE_EDGE_RE.sub("", t)
    t = _NOISE_EDGE_ASCII_RE.sub("", t)
    return re.sub(r"\s+", " ", t).strip()


def clean_address(s: str) -> str:
    """清理地址欄位。

    除了去除雜訊字元外，也把 OCR 常見的破折號變體統一為連字號，
    並移除官方主檔常見的郵遞區號前綴（如「[207]」）以便與 PDF 版本比對。
    """
    t = clean_ocr_text(s)
    if not t:
        return ""
    t = re.sub(r"^\[\d{3,5}\]", "", t).strip()
    t = t.replace("\uFF0D", "-").replace("\u2212", "-").replace("\uFE63", "-")
    # 修正行政區誤字（僅在地址中出現已知誤字時）
    for bad, good in DISTRICT_OCR_FIXES.items():
        if bad in t:
            t = t.replace(bad, good)
    return t.strip()


def normalize_district(s: str, address: str = "") -> str:
    """把行政區正規化為新北市法定區名。

    比對順序：合法區名 → 已知 OCR 誤字表 → 從地址反推 → 編輯距離為 1
    且候選唯一。全部失敗則回傳空字串（寧可留空，不猜錯）。
    """
    t = clean_ocr_text(s)
    if not t:
        return _district_from_address(address)
    if not t.endswith("區"):
        t += "區"
    if t in NEW_TAIPEI_DISTRICTS:
        return t
    if t in DISTRICT_OCR_FIXES:
        return DISTRICT_OCR_FIXES[t]

    from_addr = _district_from_address(address)
    if from_addr:
        return from_addr

    # 編輯距離 1 且唯一候選才接受
    cands = [d for d in NEW_TAIPEI_DISTRICTS if _edit_distance(t, d) == 1]
    return cands[0] if len(cands) == 1 else ""


def _district_from_address(address: str) -> str:
    """從地址字串中直接找出法定區名。"""
    if not address:
        return ""
    a = clean_ocr_text(address)
    for d in NEW_TAIPEI_DISTRICTS:
        if d in a:
            return d
    for bad, good in DISTRICT_OCR_FIXES.items():
        if bad in a:
            return good
    return ""


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein 距離（字元級）。"""
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


# ---------------------------------------------------------------- 機構名稱
# 需剝除的設立別／組織型態修飾語，剝除後才能跨來源比對同一所園
_ORG_WORDS = ("非營利", "私立", "市立", "縣立", "公立", "準公共")
_ENTITY_SUFFIX = ("幼兒園", "幼稚園", "托兒所", "教保服務中心")
# 官方機構名稱常附註受託單位，例如
#   「新北市安溪非營利幼兒園(委託社團法人桃園市教保服務人員協會辦理)」
# 決算報告與新聞報導則只寫「新北市安溪非營利幼兒園」。這段附註屬於
# 委辦關係的說明，不是機構識別的一部分，若不剝除，兩邊的正規化名稱
# 會完全不同而無法對應（實測 38 所非營利園只有 4 所掛上財務資料）。
_DELEGATION_RE = re.compile(
    r"[（(][^（()）]*(?:委託|受託|辦理)[^（()）]*[）)]")
# 縣市前綴：需連同「市立／縣立」的「立」一起處理，否則
# 「新北市立萬里幼兒園」去掉「新北市」後會殘留「立萬里」。
_CITY_PREFIX_RE = re.compile(r"^(新北|臺北|台北|桃園|基隆|新竹|苗栗|臺中|台中)"
                             r"(市|縣)?(立)?")
# 法人／公司附設型名稱：真正的園名在「附設」之後
_ATTACHED_RE = re.compile(r".*?附設")


def canonical_name(name: str) -> str:
    """產生機構名稱的比對鍵。

    「附設」有兩種完全相反的命名模式，必須分開處理：

    * **法人／公司附設**：真正的園名在「附設」之**後**
      「陽光森林文教股份有限公司臺北縣分公司附設新北市私立陽光小子幼兒園」
      →「陽光小子」
    * **學校附設**：真正的辨識名稱在「附設」之**前**（後段只剩「幼兒園」）
      「新北市萬里區萬里國民小學附設幼兒園」→「萬里國民小學」

    若不分開處理而一律取後段，所有「○○國民小學附設幼兒園」都會被縮成
    空字串而互相碰撞成同一個機構——公立園多為國小附設，這會讓整份資料集
    的公立部分全部併成一筆。因此改為：先取後段，後段為空才回頭取前段。

    其餘步驟：去縣市前綴（含市立／縣立的「立」）、去行政區、去設立別字樣、
    去「幼兒園」等類別後綴，最後只保留中英數字元。
    「分班／分校／分園」為獨立立案單位，會被保留以免與本園混為一談。
    """
    t = clean_ocr_text(name)
    if not t:
        return ""

    # 先剝除「(委託○○辦理)」等委辦註記，再做其餘正規化
    t = _DELEGATION_RE.sub("", t).strip()

    if "附設" in t:
        head, _, tail = t.partition("附設")
        cand = _core_name(tail)
        t = tail if cand else head

    return _core_name(t)


def _core_name(t: str) -> str:
    """剝除縣市／行政區／設立別／機構類別後綴，取出辨識用核心名稱。"""
    t = _CITY_PREFIX_RE.sub("", t)
    t = re.sub(r"^[\u4e00-\u9fff]{2,3}區", "", t)
    for w in _ORG_WORDS:
        t = t.replace(w, "")
    for s in _ENTITY_SUFFIX:
        t = t.replace(s, "")
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", t)


def stable_inst_id(name: str, city: str = "新北市") -> str:
    """由機構名稱產生穩定且可重現的機構代碼。

    需求：多個採集來源（基本資料／裁罰／評鑑／輿情）各自獨立抓取，必須能在
    不依賴執行順序的情況下對到同一個代碼；重跑採集也不能讓代碼改變，否則
    歷史 AI 報告與稽查排程會全部對不上。因此以名稱雜湊而非流水號。

    **以「完整名稱」而非 canonical_name 為雜湊來源**：canonical_name 會刻意
    剝除設立別（市立／私立／非營利），這對跨來源模糊比對是必要的，但會讓
    「新北市立三芝幼兒園」與「財團法人…附設新北市私立三芝幼兒園」這兩所
    不同機構產生相同代碼。代碼的職責是唯一識別，比對則交由
    canonical_name／match_institution 處理，兩者不可混用。

    取 SHA-1 前 10 個十六進位字元（約 1.1 兆組合），對數千所機構而言
    碰撞機率可忽略。
    """
    key = f"{city}|{clean_ocr_text(name)}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
    return f"K{digest.upper()}"


def match_institution(name: str, candidates: dict[str, str]) -> str | None:
    """把單一名稱比對到候選機構。

    candidates: {canonical_name: inst_id}
    比對策略為「完全相同 → 互為子字串（且長度差不大）」；不做低相似度的
    模糊猜測，避免把不同園所錯併成同一所（財務資料錯掛的代價很高）。
    """
    key = canonical_name(name)
    if not key:
        return None
    if key in candidates:
        return candidates[key]
    hits = [
        iid for ck, iid in candidates.items()
        if ck and (
            (key in ck or ck in key)
            and abs(len(ck) - len(key)) <= 2
            and min(len(ck), len(key)) >= 2
        )
    ]
    return hits[0] if len(hits) == 1 else None


# ---------------------------------------------------------------- 金額校驗
def sane_amount(value, total=None, min_share: float = 0.0005,
                max_share: float = 1.5, min_abs: float = 1000.0):
    """檢核單一科目金額是否合理，不合理回傳 None。

    OCR 常見的失誤是把「7,099,629」讀成「7」（千分位被當成欄位邊界）。
    這種值若照收，人事費率會從 50% 掉到 0.00005%，並使殘差全部堆到
    其他科目，形成連鎖性的錯誤資料。

    判準：
    * 決算金額以元為單位，單一科目低於 min_abs（預設 1,000 元）即可疑
    * 有總額可比時，科目佔總額比例需落在 [min_share, max_share] 之間
      （上限放寬到 1.5 是為了容許「支出合計」本身或跨年度合併欄位）

    負數（如餘絀、減項）以絕對值判斷，並保留原始正負號。
    """
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    mag = abs(v)
    if mag == 0:
        return v
    if mag < min_abs:
        return None
    if total:
        try:
            t = abs(float(total))
        except (TypeError, ValueError):
            return v
        if t > 0:
            share = mag / t
            if share < min_share or share > max_share:
                return None
    return v


def sane_headcount(value, cap=None, lo: int = 1, hi: int = 2000):
    """檢核人數欄位是否合理，不合理回傳 None。

    另外檢查「實際招收數遠低於核定數」的情形：OCR 把 102 讀成 10 之類
    的錯誤會讓招收率變成 9%，進而讓超收率、生師比等特徵全部失真。
    低於核定數 15% 者視為擷取失敗（真正嚴重招生不足的園所仍會由
    官方主檔的核定人數與實際幼生數呈現，不需靠可疑的 OCR 值支撐）。
    """
    if value is None:
        return None
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    if not (lo <= n <= hi):
        return None
    if cap:
        try:
            c = int(round(float(cap)))
        except (TypeError, ValueError):
            return n
        if c > 0 and n < c * 0.15:
            return None
    return n
