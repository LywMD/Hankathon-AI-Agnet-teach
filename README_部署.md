# 部署方式

本系統提供兩種交付形式，可同時使用：

| 形式 | 評審端操作 | 適用情境 | 功能完整度 |
|---|---|---|---|
| **A. 線上網址**（GitHub Pages） | 點網址即可 | 遠端評審、不想下載檔案 | 唯讀分析結果 |
| **B. 單一執行檔**（.exe） | 雙擊 exe | 現場示範、無網路環境 | 完整（含權重調整、AI 偵查） |

建議兩種都準備：網址方便評審隨時查看，執行檔用於現場展示互動功能。

---

## A. 產生線上網址（GitHub Pages）

### 為什麼需要額外處理

GitHub Pages 只能提供靜態檔案，無法執行 Python。本系統的分析是批次計算，
因此可以先在本機算完、把結果匯出成 JSON，再由前端直接讀取——分析結果與
本機版完全相同。

### 步驟

**1. 在本機建置資料集並匯出靜態網站**

```powershell
python scripts/collect_ece.py        # 採集機構、裁罰、評鑑（約 40 分鐘）
python scripts/collect_sentiment.py  # 採集輿情（約 1 小時）
python scripts/build_dataset.py      # 整合成分析用資料集
python scripts/export_static.py      # 匯出到 docs/
```

**2. 本機預覽確認**

```powershell
python -m http.server 8000 --directory docs
```

開啟 <http://127.0.0.1:8000/> 確認儀表板正常。**這一步不要跳過**：
發布後才發現空白的成本很高。

**3. 推送到 GitHub**

```powershell
git init
git add .
git commit -m "教保機構風險預警系統"
git branch -M main
git remote add origin https://github.com/<你的帳號>/<專案名>.git
git push -u origin main
```

**4. 啟用 Pages**

在 GitHub 專案頁面：`Settings` → `Pages` → `Build and deployment` →
Source 選 **GitHub Actions**。

推送後 `.github/workflows/pages.yml` 會自動執行，完成後在
`Settings` → `Pages` 即可看到網址，格式為：

```
https://<你的帳號>.github.io/<專案名>/
```

之後只要修改 `app/`、`web/` 或 `data/` 並推送，網站會自動重新發布。

### 線上版的功能界線

以下功能需要執行 Python 或呼叫外部 API，靜態網站無法提供，
前端會明確提示改用執行檔，而不是按了沒反應：

- 調整構面權重後重新計算分數
- 重新採集／重新載入資料
- AI 代理人即時偵查（需 API 金鑰與網路）
- 匯出 Excel、下載預警單

唯讀部分（風險總覽、機構清單與詳情、鑑識分析、輿情分析、模型驗證、
稽查排程建議）皆與本機版一致。

### 注意事項

- **儲存庫必須包含 `data/*.csv`**，CI 不會重跑採集（避免對政府網站產生
  大量請求，也不可能在 CI 內執行 OCR）。
- 公開儲存庫等於公開這份資料集。資料本身來自政府公開網站，但仍請確認
  符合競賽規定；若需私有，可改用私有儲存庫搭配 GitHub Pages
  （需 GitHub Pro）或改交付執行檔。

---

## B. 產生單一執行檔（.exe）

```powershell
python scripts/build_exe.py
```

流程會自動完成四件事：

1. 檢查 `data/` 內資料表是否齊備（缺哪張表會導致對應風險構面沒有分數）
2. 以 PyInstaller 打包
3. **實際啟動 exe 並查詢其分析結果** — 打包最常見的失敗不是建置失敗，
   而是建置成功但執行期缺資源（資料沒進去、hiddenimports 漏宣告）。
   這類問題只有真的跑起來才會現形。
4. 產出 `release/` 資料夾：exe + 外置 `data/` + 使用說明.txt

把 `release/` 整個交付即可，或只給其中的 exe。

### 評審端需求

- Windows 10 或以上
- **不需要** Python、不需要網路、不需要 Tesseract

### 兩件要提醒評審的事

**未經數位簽章**：首次執行時 Windows 會顯示「已保護您的電腦」，
需點「其他資訊」→「仍要執行」。建議現場先示範一次。

**服務僅綁 127.0.0.1**：不對外開放連線，因此不會觸發防火牆詢問視窗。

### 資料更新不必重新打包

程式讀取資料的順序是「exe 同層的 `data/` 優先，其次為打包在內部的版本」。
要更新資料時，把新的 `data/` 資料夾放到 exe 旁邊即可。

---

## 資料建置階段的環境需求

僅在**你自己的電腦**建置資料時需要，評審端完全不需要：

| 需求 | 用途 | 取得方式 |
|---|---|---|
| Python 3.10+ | 執行腳本 | <https://www.python.org/> |
| PyMuPDF | 讀取決算 PDF | `pip install PyMuPDF` |
| Tesseract OCR 5.x | 決算 PDF 文字層損毀，須 OCR | <https://github.com/UB-Mannheim/tesseract/wiki> |
| chi_tra.traineddata | 繁體中文辨識 | 已放於 `resources/tessdata/` |
| PyInstaller | 打包 exe | `pip install pyinstaller` |

可用環境變數覆蓋預設路徑：

```powershell
$env:DATA_SOURCE_PATH = "D:\我的資料\w"      # 決算 PDF 來源目錄
$env:TESSERACT_PATH   = "D:\Tesseract\tesseract.exe"
```

---

## 完整建置流程

```powershell
# 1. 採集公開資料（需網路）
python scripts/collect_ece.py              # 機構主檔、裁罰、評鑑
python scripts/collect_sentiment.py        # 新聞與社群輿情

# 2. 從決算 PDF 擷取財務資料（需 Tesseract）
python scripts/extract_nonprofit_financials.py   # 非營利園財報
python scripts/extract_public_schools.py         # 市立幼兒園決算

# 3. 整合成分析用資料集
python scripts/build_dataset.py

# 4. 交付
python scripts/export_static.py     # → docs/（GitHub Pages）
python scripts/build_exe.py         # → release/（執行檔）
```
