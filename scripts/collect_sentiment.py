"""採集教保機構輿情（新聞 + PTT 公開看板），產出 data/posts.csv。

資料來源
--------
* Google News RSS：以引號包住機構全名做精確查詢，涵蓋各大媒體，回傳含
  發布日期與來源媒體。這是「事件型」輿情（不當管教、食安、裁罰新聞）的主來源。
* PTT 公開看板搜尋（BabyMother 等）：家長討論密度高，可補足新聞未報導的
  日常抱怨與口碑，回傳含推文數作為互動熱度。

（Dcard 與新北市開放資料平台於實測分別回 403 / WAF 阻擋，故未納入。）

關於「查無輿情」的處理
--------------------
多數小型園所本來就沒有新聞或社群討論，抓不到資料是常態而非錯誤。本腳本
一律據實輸出（該機構就是 0 筆），不會為了讓分數好看而補任何內容。
下游 app/nlp.py 對 0 筆貼文回傳輿情分數 0，代表「無負面訊號」而非「安全」，
儀表板會另外標示資料覆蓋率，避免把「沒有資料」誤讀成「沒有問題」。

用法：
    python scripts/collect_sentiment.py                  # 全部機構
    python scripts/collect_sentiment.py --limit 50       # 只跑前 50 所（測試）
    python scripts/collect_sentiment.py --no-ptt         # 只抓新聞
    python scripts/collect_sentiment.py --city-only      # 只抓全市層級輿情
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app import textnorm  # noqa: E402
from app.collect import social  # noqa: E402
from app.collect.http_util import Session  # noqa: E402

OUT_DIR = BASE_DIR / "data"
LOG_PATH = BASE_DIR / ".tmp" / "collect_sentiment_log.txt"

POST_COLS = ["post_id", "inst_id", "name", "source", "post_date", "content",
             "engagement", "url"]


def load_institutions() -> list[dict]:
    """載入機構主檔（優先用官方採集結果）。"""
    for fname in ("institutions_official.csv", "institutions.csv"):
        path = OUT_DIR / fname
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            rows = [r for r in csv.DictReader(f) if r.get("name")]
        if rows:
            print(f"機構主檔：{fname}（{len(rows)} 所）")
            return rows
    return []


def load_priority_names() -> set[str]:
    """讀出「值得逐一深查」的機構名稱：有裁罰紀錄者。

    逐機構查詢會被新聞來源限速，因此不對全部機構做，而是集中在已知有
    法遵問題的園所——這些正是監理最關心、也最可能有後續報導的對象。
    """
    names: set[str] = set()
    path = OUT_DIR / "penalties.csv"
    if not path.exists():
        return names
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            nm = (r.get("name") or "").strip()
            if nm:
                names.add(nm)
    return names


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="逐機構查詢的上限筆數（0 = 不額外限制）")
    # 預設只深查「有裁罰紀錄」的機構：對全部 1100 餘所逐一查詢會被新聞
    # 來源限速到每分鐘一筆，需十餘小時，且多數小型園所本來就無報導。
    ap.add_argument("--all-institutions", action="store_true",
                    help="逐機構查詢全部機構（很慢，且容易被限速）")
    # 行政區掃描會讓查詢數從 21 增加到 108 筆。新聞來源在連續查詢下會
    # 收緊配額，查詢數愈多愈容易整批卡住，故預設不啟用。
    ap.add_argument("--with-districts", action="store_true",
                    help="加入行政區層級的議題掃描（查詢數增為約 5 倍）")
    ap.add_argument("--force", action="store_true",
                    help="即使本次採集筆數少於既有檔案，仍強制覆寫 posts.csv")
    ap.add_argument("--delay", type=float, default=0.7)
    # PTT 對持續性請求會限速，批次跑一千多所機構時容易被封鎖且耗時甚長，
    # 因此預設關閉；需要社群口碑資料時可用 --with-ptt 開啟（建議搭配 --limit）。
    ap.add_argument("--with-ptt", action="store_true",
                    help="同時抓取 PTT（會明顯變慢，且可能被限速）")
    ap.add_argument("--city-only", action="store_true",
                    help="只抓全市層級輿情，不逐機構查詢")
    ap.add_argument("--city", default="新北市")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log = LOG_PATH.open("w", encoding="utf-8")

    def prog(msg: str) -> None:
        log.write(msg + "\n")
        log.flush()

    sess = Session(delay=args.delay, verify_tls=False)
    posts: list[dict] = []
    t0 = time.time()

    institutions = load_institutions()
    districts = tuple(sorted({(r.get("district") or "").strip()
                              for r in institutions
                              if (r.get("district") or "").strip()})
                      ) if args.with_districts else ()

    # ------------------------------------------------- 議題掃描（主力路徑）
    print(f"\n[1/2] 風險議題掃描（{args.city}"
          + (f"，含 {len(districts)} 個行政區）" if districts else "）"))
    try:
        city_posts = social.collect_city_wide(
            sess, args.city, districts=districts, on_progress=prog)
    except Exception as exc:  # noqa: BLE001
        print(f"  失敗：{exc}")
        log.write(f"FAIL city_wide: {exc}\n")
        city_posts = []
    print(f"  => {len(city_posts)} 則")

    # 以機構名稱回填 inst_id：報導常直接點名園所，比對到就掛給該機構，
    # 比對不到則保留為市級背景輿情（inst_id 留空，不計入任何機構分數）。
    name_index = {textnorm.canonical_name(r["name"]): r["inst_id"]
                  for r in institutions if r.get("name")}
    id_to_name = {r["inst_id"]: r["name"] for r in institutions}

    matched_city = 0
    for p in city_posts:
        iid = ""
        for ck, cand_id in name_index.items():
            if ck and len(ck) >= 2 and ck in p["content"]:
                iid = cand_id
                matched_city += 1
                break
        p["inst_id"] = iid
        p["name"] = id_to_name.get(iid, "")
        posts.append(p)
    print(f"  其中 {matched_city} 則可對應到具體機構")

    # ---------------------------------------------------------- 逐機構輿情
    if not args.city_only:
        if args.all_institutions:
            targets = institutions
            scope = "全部機構"
        else:
            prio = load_priority_names()
            targets = [r for r in institutions if r["name"] in prio]
            scope = f"有裁罰紀錄者（{len(prio)} 個名稱比對到 {len(targets)} 所）"
            if not targets:
                targets = institutions
                scope = "全部機構（找不到 penalties.csv，退回全查）"
        if args.limit:
            targets = targets[:args.limit]
        print(f"\n[2/2] 逐機構深查：{scope} → {len(targets)} 所")
        if not targets:
            print("  機構主檔為空，請先執行 scripts/collect_ece.py")
        else:
            done = 0

            def on_prog(msg: str) -> None:
                nonlocal done
                prog(msg)
                done += 1
                if done % 20 == 0:
                    el = time.time() - t0
                    print(f"  {msg}｜已處理 {done}/{len(targets)}"
                          f"（{el / 60:.1f} 分，延遲 {sess.delay:.1f}s，"
                          f"限速 {sess.throttled} 次）", flush=True)

            try:
                inst_posts = social.collect_for_institutions(
                    sess, targets, include_ptt=args.with_ptt,
                    on_progress=on_prog)
            except Exception as exc:  # noqa: BLE001
                print(f"  失敗：{exc}")
                log.write(f"FAIL per-institution: {exc}\n")
                inst_posts = []
            for p in inst_posts:
                p["name"] = id_to_name.get(p["inst_id"], "")
                posts.append(p)
            print(f"  => {len(inst_posts)} 則")
    else:
        print("\n[2/2] 逐機構輿情（已略過）")

    # ---------------------------------------------------------- 去重與輸出
    seen: set[tuple[str, str]] = set()
    unique: list[dict] = []
    for p in posts:
        key = (p.get("inst_id", ""), p.get("url") or p.get("content", "")[:90])
        if key in seen:
            continue
        seen.add(key)
        p.setdefault("name", "")
        unique.append(p)

    for i, p in enumerate(unique, 1):
        p["post_id"] = f"P{i:06d}"

    # ---------------------------------------------------------- 寫檔防護
    # 新聞來源限速時，fetch_news 會捕捉例外並回傳空陣列，於是整趟採集可能
    # 「成功結束」但一則都沒抓到。若直接覆寫，先前辛苦採集的資料就沒了
    # （實測曾因此把 114 則輿情清成 0 則）。
    # 因此比新檔筆數少於既有檔時一律拒絕覆寫，除非明確加上 --force。
    out_path = OUT_DIR / "posts.csv"
    existing = 0
    if out_path.exists():
        with out_path.open("r", encoding="utf-8-sig", newline="") as f:
            existing = sum(1 for _ in csv.DictReader(f))

    if len(unique) < existing and not args.force:
        print(f"\n⚠ 本次僅取得 {len(unique)} 則，少於既有的 {existing} 則，"
              f"**不覆寫** {out_path.name}")
        print("  多半是新聞來源限速所致（fetch_news 失敗會回傳空結果）。")
        print(f"  被限速 {sess.throttled} 次，最終延遲 {sess.delay:.1f} 秒。")
        print("  建議隔數小時或改用其他網路後重試；")
        print("  確定要以本次結果取代舊檔時請加上 --force。")
        log.write(f"拒絕覆寫：新 {len(unique)} 筆 < 既有 {existing} 筆\n")
        log.close()
        return 1

    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=POST_COLS, extrasaction="ignore")
        w.writeheader()
        for p in unique:
            w.writerow(p)

    # ---------------------------------------------------------- 摘要
    from collections import Counter
    by_source = Counter(p["source"].split("：")[0] for p in unique)
    with_inst = sum(1 for p in unique if p.get("inst_id"))
    covered = len({p["inst_id"] for p in unique if p.get("inst_id")})
    dated = sum(1 for p in unique if p.get("post_date"))

    lines = [
        "", "=" * 60,
        f"貼文總數        {len(unique)} 則（去重後）",
        f"  來源分布      {dict(by_source)}",
        f"  已對應機構    {with_inst} 則，涵蓋 {covered} 所機構",
        f"  有日期        {dated} 則（爆量偵測需要日期）",
        f"耗時            {(time.time() - t0) / 60:.1f} 分"
        f"（被限速 {sess.throttled} 次，最終延遲 {sess.delay:.1f} 秒）",
        "=" * 60,
    ]
    for ln in lines:
        print(ln)
        log.write(str(ln) + "\n")
    log.close()
    print(f"\n輸出：{out_path}\n日誌：{LOG_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
