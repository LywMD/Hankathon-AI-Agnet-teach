"""把分析結果匯出成靜態網站，可直接由 GitHub Pages 提供網址存取。

產出結構（預設寫入 docs/，GitHub Pages 可設定為此目錄）
--------------------------------------------------
    docs/index.html          前端（複製自 web/）
    docs/css/ docs/js/
    docs/static-mode.js      告知前端目前為靜態模式
    docs/api/status.json     分析狀態（固定為已完成）
    docs/api/meta.json       設定、構面定義、資料來源
    docs/api/summary.json    儀表板彙總
    docs/api/institutions.json  完整機構清單（前端自行篩選排序）
    docs/api/institution/<id>.json  各機構詳情
    docs/api/forensics.json  鑑識分析
    docs/api/sentiment.json  輿情分析
    docs/api/metrics.json    模型驗證
    docs/api/schedule.json   稽查排程建議
    docs/api/dataset/*.csv   原始資料集（供檢視與下載）

為什麼要分成「完整清單」與「逐機構詳情」
------------------------------------
機構詳情含完整特徵與鑑識檢定結果，1000 餘所全部合併會是數十 MB 的單一
檔案，首次載入極慢。因此清單頁需要的欄位放在 institutions.json，
詳情則各機構一檔，點進去才載入。

靜態版的功能界線
--------------
唯讀分析結果完全一致；但「調整權重後重新計算」「重新採集資料」
「AI 代理人即時偵查」需要執行 Python 或呼叫外部 API，靜態網站無法提供，
前端會明確提示改用本機執行檔，而不是讓按鈕按下去沒反應。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app import config, schedule  # noqa: E402
from app.engine import ENGINE  # noqa: E402

WEB_SRC = BASE_DIR / "web"
DATA_SRC = BASE_DIR / "data"

# 清單頁實際會用到的欄位，避免把完整特徵塞進清單檔
LIST_FIELDS = ("inst_id", "name", "city", "district", "org_type", "inst_kind",
               "enrolled", "capacity", "teacher_count", "score", "rule_score",
               "model_score", "anomaly_score", "band", "band_label", "color",
               "rank", "percentile", "dims", "pen_count_4y", "pen_count_1y",
               "senti_negative", "burst", "top_reasons", "urgency", "mode")

DATASET_FILES = ("institutions.csv", "penalties.csv", "evaluations.csv",
                 "posts.csv", "financials.csv", "ledger.csv")


def write_json(path: Path, obj) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"),
                      default=str)
    path.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs", help="輸出目錄（預設 docs）")
    ap.add_argument("--base", default="api",
                    help="JSON 資料的相對路徑前綴")
    ap.add_argument("--no-dataset", action="store_true",
                    help="不附上原始 CSV 資料集")
    args = ap.parse_args()

    out = BASE_DIR / args.out
    api = out / args.base

    print("=" * 66)
    print("1. 執行分析")
    print("=" * 66)
    ENGINE.build(reload_data=True)
    if ENGINE.error:
        print(f"分析失敗：{ENGINE.error[:500]}")
        return 1
    n = len(ENGINE.rows)
    if not n:
        print("分析結果為空，請先確認 data/ 內有資料"
              "（python scripts/build_dataset.py）")
        return 1
    print(f"  完成：{n} 所機構，高風險 "
          f"{ENGINE.summary.get('n_high_risk')} 所")

    print()
    print("=" * 66)
    print("2. 複製前端")
    print("=" * 66)
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(WEB_SRC, out)
    # GitHub Pages 預設會用 Jekyll 處理，底線開頭的目錄會被忽略；
    # 加上 .nojekyll 可確保所有檔案原樣發布。
    (out / ".nojekyll").write_text("", encoding="utf-8")
    (out / "static-mode.js").write_text(
        "/* 由 scripts/export_static.py 產生：標記為靜態網站模式 */\n"
        "window.KREWS_STATIC = true;\n"
        f"window.KREWS_STATIC_BASE = {json.dumps(args.base)};\n",
        encoding="utf-8")
    print(f"  {out}")

    print()
    print("=" * 66)
    print("3. 匯出 JSON")
    print("=" * 66)
    total = 0

    total += write_json(api / "status.json", {
        "ready": True, "progress": 100, "status": "完成",
        "error": None, "built_at": ENGINE.built_at,
    })

    settings = dict(ENGINE.settings)
    settings.pop("anthropic_api_key", None)  # 不外流金鑰
    total += write_json(api / "meta.json", {
        "app": {"name": config.APP_NAME, "version": config.APP_VERSION,
                "subtitle": config.APP_SUBTITLE},
        "dimensions": [{"key": k, "label": lbl, "desc": d}
                       for k, lbl, d in config.DIMENSIONS],
        "bands": [{"key": k, "label": lbl, "color": c}
                  for k, lbl, _lo, _hi, c in config.RISK_BANDS],
        "settings": settings,
        "data": ENGINE.bundle.meta if ENGINE.bundle else {},
        "static_mode": True,
        "static_note": "線上展示版僅提供唯讀分析結果；"
                       "調整權重與 AI 深度偵查請使用本機執行檔。",
    })
    total += write_json(api / "summary.json", ENGINE.summary)
    total += write_json(api / "forensics.json", ENGINE.forensics_overview)
    total += write_json(api / "sentiment.json", ENGINE.sentiment_overview)
    total += write_json(api / "metrics.json", ENGINE.metrics)

    rows = ENGINE.rows
    total += write_json(api / "institutions.json", {
        "total": len(rows),
        "cities": sorted({r["city"] for r in rows if r.get("city")}),
        "org_types": sorted({r["org_type"] for r in rows if r.get("org_type")}),
        "rows": [{k: r.get(k) for k in LIST_FIELDS} for r in rows],
    })

    # 稽查排程：靜態版無法接受任意量能參數（那需要重新計算），
    # 因此以設定檔的稽查量能匯出一份結果；前端的量能滑桿在靜態模式下
    # 會固定顯示此結果並註明無法調整。
    cap = int(ENGINE.settings.get("inspection_capacity") or 40)
    try:
        total += write_json(api / "schedule.json",
                            schedule.build(ENGINE.rows, cap, ENGINE.metrics))
    except Exception as exc:  # noqa: BLE001
        print(f"  ! 排程匯出略過：{exc}")

    det_dir = api / "institution"
    for r in rows:
        iid = str(r["inst_id"])
        d = ENGINE.details.get(iid) or {}
        total += write_json(det_dir / f"{iid}.json", {
            "inst": d.get("inst"), "row": r, "explain": d.get("explain"),
            "action": d.get("action"), "features": d.get("features"),
            "detail": d.get("detail"), "dims": d.get("dims"),
            "ai_report": ENGINE.ai_reports.get(iid),
        })
    print(f"  機構詳情 {len(rows)} 檔")

    if not args.no_dataset:
        ds = api / "dataset"
        ds.mkdir(parents=True, exist_ok=True)
        for name in DATASET_FILES:
            src = DATA_SRC / name
            if src.exists():
                shutil.copy2(src, ds / name)
                total += src.stat().st_size
        print(f"  原始資料集 {len(list(ds.glob('*.csv')))} 檔")

    print(f"  合計約 {total / 1024 / 1024:.1f} MB")

    print()
    print("=" * 66)
    print("完成")
    print("=" * 66)
    print(f"輸出目錄：{out}")
    print("本機預覽：")
    print(f"  python -m http.server 8000 --directory {args.out}")
    print("  然後開啟 http://127.0.0.1:8000/")
    print("發布到 GitHub Pages：見 README_部署.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
