"""公開資料採集器（collectors）。

本套件負責從政府公開網站與公開社群／新聞來源抓取本系統所需的真實資料，
補齊原本只有 PDF 決算報告（institutions / financials）而缺少的四張表：

* penalties.csv   — 裁罰紀錄（全國教保資訊網 裁罰紀錄查詢）
* evaluations.csv — 基礎評鑑結果（全國教保資訊網 評鑑結果查詢）
* posts.csv       — 輿情（Google News RSS + PTT 公開看板搜尋）
* institutions    — 機構基本資料（全國教保資訊網 基本資料查詢，擴充母體）

設計原則：
1. **僅用標準函式庫**（urllib / html.parser / csv），與主程式一致，
   確保 PyInstaller 打包後仍可執行、不需額外套件。
2. **只抓公開頁面**，遵守禮貌性延遲（預設每次請求間隔 ≥ 0.8 秒），
   並帶可識別的 User-Agent，不繞過任何登入或授權機制。
3. **抓不到就留空**，絕不以生成資料填補；每個採集器都會回報成功／失敗筆數，
   讓使用者清楚知道哪些構面有真實資料支撐。
"""

__all__ = ["ece", "social", "http_util"]
