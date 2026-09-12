# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包設定：產出可在評審端電腦直接雙擊執行的單一 exe。

打包內容
--------
* web/   前端儀表板（HTML/CSS/JS）
* data/  已建置完成的分析資料集（CSV）

為什麼要把 data/ 打包進去
------------------------
評審端的電腦不會有 Tesseract OCR、原始決算 PDF，也不會有時間等待網路採集，
因此資料必須「事先建置好、隨程式一起帶走」。程式啟動時會先找 exe 同層的
data/ 資料夾（可用來更新資料），找不到才使用打包在內部的版本
（見 app/config.py 的 data_search_dirs）。

不打包的內容
-----------
scripts/ 之下的採集與 OCR 萃取腳本不納入 exe：它們依賴 PyMuPDF 與外部
Tesseract，屬於「資料建置階段」的工具，執行期用不到。主程式本身只使用
Python 標準函式庫，因此打包結果不需要任何額外執行環境。
"""

from pathlib import Path

# 只打包主程式實際會讀取的資料表，排除中間產物（*_official / *_nonprofit）
# 與體積龐大的 OCR 訓練資料，避免 exe 無謂膨脹。
_RUNTIME_TABLES = ("institutions.csv", "financials.csv", "ledger.csv",
                   "penalties.csv", "evaluations.csv", "posts.csv",
                   "fees.csv", "staff_changes.csv", "manifest.json")

_datas = [("web", "web")]
_data_dir = Path("data")
for _name in _RUNTIME_TABLES:
    if (_data_dir / _name).exists():
        _datas.append((str(_data_dir / _name), "data"))


a = Analysis(
    ['run.py'],
    pathex=[],
    binaries=[],
    datas=_datas,
    # tkinter 是控制視窗所需；app.collect 為 AI 代理人即時查證新聞所需，
    # 兩者都只在執行期間動態使用，需明確宣告以免被靜態分析漏掉。
    hiddenimports=[
        'tkinter', 'tkinter.ttk', 'tkinter.messagebox',
        'app.collect', 'app.collect.http_util', 'app.collect.social',
        'app.collect.ece',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 排除資料建置階段才需要的套件，縮小體積並避免打包失敗
    excludes=['pymupdf', 'fitz', 'PIL', 'numpy', 'pandas', 'matplotlib',
              'scipy', 'setuptools', 'pip'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='教保機構風險預警系統',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # 不使用 UPX 壓縮：UPX 未必已安裝，且壓縮後的執行檔較容易被防毒軟體
    # 誤判為可疑程式。競賽場合以「一定能開起來」優先於檔案大小。
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
