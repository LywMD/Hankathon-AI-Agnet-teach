"""全域設定：路徑解析、常數、預設權重。

本專案的執行期只使用 Python 標準函式庫，確保 PyInstaller 打包後
在評審端電腦上可離線、免安裝直接執行。
"""
from __future__ import annotations

import os
import re
import sys
from datetime import date
from pathlib import Path

APP_NAME = "教保機構風險預警系統"
APP_SHORT = "KREWS"
APP_VERSION = "1.0.0"
APP_SUBTITLE = "AI × 鑑識會計 × 輿情分析 之高風險教保機構預警平台"

# ---------------------------------------------------------------- 路徑處理
def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def resource_dir() -> Path:
    """唯讀資源（web 前端、內建詞庫）所在目錄。"""
    if _is_frozen():
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parent.parent


def base_dir() -> Path:
    """可寫入的工作目錄（exe 所在資料夾 / 開發時的專案根目錄）。"""
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


WEB_DIR = resource_dir() / "web"
DATA_DIR = base_dir() / "data"           # 可更新的資料位置（exe 同層資料夾）
BUNDLED_DATA_DIR = resource_dir() / "data"  # 隨程式打包的資料（唯讀）
SAMPLE_DIR = base_dir() / "sample_data"  # 示範資料與欄位範本（不會被載入器掃描）
OUTPUT_DIR = base_dir() / "output"
LOG_FILE = base_dir() / "krews.log"
SETTINGS_FILE = base_dir() / "settings.json"


def data_search_dirs() -> list[Path]:
    """資料檔搜尋順序：exe 同層的 data/ 優先，其次為打包在程式內的 data/。

    這樣設計是為了讓同一份 exe 同時滿足兩種使用情境：

    * **直接執行**：評審端只拿到單一 exe，資料已隨程式打包，雙擊即可看到
      完整分析結果，不需另外準備任何檔案。
    * **更新資料**：若要換成更新的採集結果，只要把新的 data/ 資料夾放在
      exe 旁邊即可覆蓋內建版本，不必重新打包。

    兩個目錄都存在時以外部者為準，且不會混用同一張表的兩個版本
    （見 datastore._find_local 的實作）。
    """
    dirs: list[Path] = []
    for d in (DATA_DIR, BUNDLED_DATA_DIR):
        if d.exists() and d not in dirs:
            dirs.append(d)
    return dirs

# ---------------------------------------------------------------- 時間基準
# 資料截止日（as-of）：所有「當期」特徵僅使用此日期之前的資料，避免資料洩漏。
AS_OF = date(2026, 8, 31)
# 訓練用時間切分：以 TRAIN_ASOF 之前的資料為特徵，之後 12 個月是否被裁罰為標籤。
TRAIN_ASOF = date(2025, 6, 30)
LABEL_WINDOW_DAYS = 365

FISCAL_YEARS = [2021, 2022, 2023, 2024, 2025]

# ---------------------------------------------------------------- 風險構面
DIMENSIONS = [
    ("compliance", "法遵風險", "裁罰紀錄之頻率、金額、嚴重度與再犯情形"),
    ("financial", "財務風險", "決算與收費之比率異常、班佛定律、同儕偏離與交叉核對落差"),
    ("operation", "營運風險", "師生比、超收率、教師流動、規模與負責人關聯"),
    ("evaluation", "評鑑風險", "基礎評鑑等第、待改善項目與追蹤複評情形"),
    ("sentiment", "輿情風險", "社群負面聲量、風險主題命中與爆量預警"),
]

DEFAULT_WEIGHTS = {
    "compliance": 0.30,
    "financial": 0.25,
    "operation": 0.15,
    "evaluation": 0.10,
    "sentiment": 0.20,
}

# 規則分數與監督式模型分數的混合比例（0 = 全規則，1 = 全模型）
DEFAULT_MODEL_BLEND = 0.25

# 風險分級採「族群相對排序」而非固定分數門檻。
# 理由：綜合分數的絕對尺度會隨構面權重與模型混合比例變動，固定門檻在調整
# 權重後就失去意義；而監理實務關心的是「在有限稽查人力下該優先看誰」，
# 本質上是排序問題。各級距的百分位邊界依稽查量能規劃設定。
# 欄位：(key, label, 起始百分位, 結束百分位, 顏色)，百分位由風險最高端起算。
RISK_BANDS = [
    ("critical", "極高風險", 0.0, 2.0, "#b3261e"),
    ("high", "高風險", 2.0, 8.0, "#e8590c"),
    ("medium", "中風險", 8.0, 25.0, "#f0a020"),
    ("low", "低風險", 25.0, 60.0, "#2f9e44"),
    ("minimal", "極低風險", 60.0, 100.01, "#1971c2"),
]

PRIORITY_BANDS = ("critical", "high")


def band_by_rank(rank: int, total: int) -> tuple[str, str, str]:
    """依風險排名（1 為最高）決定風險等級。"""
    if total <= 0:
        return "minimal", "極低風險", "#1971c2"
    pct = (rank - 1) / total * 100
    for key, label, lo, hi, color in RISK_BANDS:
        if lo <= pct < hi:
            return key, label, color
    return "minimal", "極低風險", "#1971c2"


def band_label(key: str) -> str:
    for k, label, _lo, _hi, _c in RISK_BANDS:
        if k == key:
            return label
    return key


# ---------------------------------------------------------------- 師生比法定上限
# 幼兒教育及照顧法第16條第4項：教保服務人員配置採「逐班分級」規定，並非單純
# 比例——2歲以上未滿3歲之班級8人以下置1人、9人以上置2人；3歲以上至入國小前
# 15人以下置1人、16人以上置2人。可證明：設某年齡層幼兒N人、班級C班，
# 則最少應置人員 = max(C, ceil(N/t))，t 即為該年齡層之實質師生比分母
# （2-3歲為8、3歲以上為15）。兩年齡層須分別計算，不可以全園混合平均互相稀釋
# （新北市城鄉發展局內部會議亦重申此點）。
RATIO_LIMIT_AGE2 = 8
RATIO_LIMIT_AGE35 = 15

# 幼兒教育及照顧法第16條第1項：各年齡層班級人數上限（2-3歲不得與其他年齡混齡）。
CLASS_SIZE_LIMIT_AGE2 = 16
CLASS_SIZE_LIMIT_AGE35 = 30

# 幼兒教育及照顧法第16條第5項：公立學校附設幼兒園，除依規定配置教保服務人員外，
# 每園應再增置教保服務人員1人——比對生師比是否違規前，應先扣除此 1 名法定
# 加派人力，否則會把依法多配置的人力誤判為「餘裕」而低估其他班級的實際負荷。
PUBLIC_KINDERGARTEN_EXTRA_STAFF = 1

# ---------------------------------------------------------------- 違規類別
# 依兒童權益衝擊程度給定嚴重度權重（1.0 ~ 3.0）
PENALTY_CATEGORIES = {
    "兒少保護事件": 3.0,
    "不當管理行為": 2.7,
    "師生比不足": 2.0,
    "超收幼生": 1.9,
    "人員資格不符": 1.8,
    "設施安全缺失": 1.7,
    "餐飲衛生違規": 1.6,
    "收退費違規": 1.5,
    "未依法通報": 1.4,
    "行政管理缺失": 1.0,
}

# 教保服務機構的設立別（幼兒教育及照顧法所定四類）。
#
# 這裡刻意**不包含**「公立國小／公立國中／公立高中」：本系統的分析對象是
# 教保服務機構，各級學校本體不在範圍內。混入學校會造成兩個問題：
#   1. 師生比上限、超收率等指標對學校沒有相同的法規意義，放在同一份風險
#      排名中等於拿性質不同的機構互比。
#   2. 學校附設幼兒園沒有獨立決算，其收支併入學校整體預算；若以學校總收支
#      代替，金額會差好幾個數量級，財務比率與鑑識檢定將產生大量假警訊。
#
# 學校附設幼兒園本身仍在分析範圍內（設立別為「公立」），只是其財務構面
# 會標記為無資料，由 scoring 的構面可得性機制排除，不以推估值填補。
ORG_TYPES = ["公立", "非營利", "準公共", "私立"]


# ---------------------------------------------------------------- 評鑑結果判讀
def eval_result_level(result) -> str:
    """判讀基礎評鑑結果字串，回傳 'pass' / 'fail' / 'unknown'。

    全國教保資訊網的評鑑結果是**描述性字句**，不是「通過／不通過」二元值。
    實際出現的字樣只有三種：

        基礎評鑑－全數指標通過
        基礎評鑑－部分指標通過
        追蹤評鑑－全數指標通過

    原先的實作以 `{"不通過":100, "追蹤輔導":55, "通過":0}` 查表，這些鍵在真實
    資料中一次都不會命中，於是每一所有評鑑紀錄的機構都落到預設值 30 分，
    連「全數指標通過」的園所也被扣分——實測 95.5% 的機構因此帶有非零評鑑
    風險，是大量偽陽性的來源。

    判讀時特別注意「部分指標通過」：字面含「通過」，實質卻是未全數通過，
    必須判為 fail。若只比對「未通過」三個字會整批漏判。

    無法判讀時回傳 unknown，由上層以缺值處理，而不是預設當成有缺失。
    """
    t = re.sub(r"\s+", "", str(result or ""))
    if not t:
        return "unknown"
    if "全數指標通過" in t or "全部通過" in t:
        return "pass"
    if any(k in t for k in ("部分", "未通過", "不通過", "待改善", "限期改善")):
        return "fail"
    return "unknown"

DEFAULT_SETTINGS = {
    "weights": DEFAULT_WEIGHTS,
    "model_blend": DEFAULT_MODEL_BLEND,
    "inspection_capacity": 40,
    "data_source": "local",
    "aws_base_url": "",
    "port": 8760,
    # Claude AI agent（深度偵查用，選用功能）：需使用者自備 API 金鑰並連線網路，
    # 未設定時不影響其餘離線功能（快速初篩、儀表板、稽查排程皆正常運作）。
    "anthropic_api_key": "",
    "anthropic_model": "claude-sonnet-5",
}


def ensure_dirs() -> None:
    for d in (DATA_DIR, SAMPLE_DIR, OUTPUT_DIR):
        os.makedirs(d, exist_ok=True)
