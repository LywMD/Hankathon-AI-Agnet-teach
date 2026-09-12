"""輿情採集器：新聞（Google News RSS）與公開社群看板（PTT）。

輸出統一為 posts 表結構，直接餵給 app/nlp.py 的情感／主題／爆量分析：
    post_id, inst_id, source, post_date, content, engagement, url

來源選擇理由：
* Google News RSS：無須金鑰、涵蓋各大媒體、可用引號做機構名精確查詢，
  且回傳含 pubDate 與來源媒體，適合作為「事件型」輿情主來源。
* PTT 公開看板搜尋：家長討論密度高（BabyMother 等），可補足新聞未報導的
  日常抱怨與口碑，回傳含推文數可作為互動熱度。
* Dcard 與新北市開放平台於實測中分別回 403 / WAF 阻擋，故不納入；
  若日後開放，可依相同介面新增採集器。
"""
from __future__ import annotations

import re
import urllib.parse
from datetime import datetime, timezone

from .http_util import Session, strip_tags, unescape

# ---------------------------------------------------------------- Google News
NEWS_RSS = "https://news.google.com/rss/search"

_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.S)


def _tag(block: str, name: str) -> str:
    m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", block, re.S)
    if not m:
        return ""
    val = m.group(1).strip()
    # RSS 常以 CDATA 包裹
    cd = re.match(r"<!\[CDATA\[(.*?)\]\]>", val, re.S)
    if cd:
        val = cd.group(1)
    return unescape(val).strip()


def _rss_date(s: str) -> str:
    """RFC-822（Tue, 21 Jan 2026 08:00:00 GMT）轉 ISO 日期。"""
    s = s.strip()
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z",
                "%d %b %Y %H:%M:%S %Z"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo:
                dt = dt.astimezone(timezone.utc)
            return dt.date().isoformat()
        except ValueError:
            continue
    m = re.search(r"(\d{1,2})\s+(\w{3})\s+(\d{4})", s)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}",
                                     "%d %b %Y").date().isoformat()
        except ValueError:
            pass
    return ""


def fetch_news(sess: Session, query: str, limit: int = 60) -> list[dict]:
    """以 Google News RSS 查詢單一關鍵字，回傳貼文結構清單。"""
    url = (f"{NEWS_RSS}?q={urllib.parse.quote(query)}"
           "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant")
    try:
        xml = sess.get(url, accept="application/xml,text/xml,*/*")
    except Exception:  # noqa: BLE001
        return []
    out: list[dict] = []
    for block in _ITEM_RE.findall(xml)[:limit]:
        title = _tag(block, "title")
        if not title:
            continue
        desc = strip_tags(_tag(block, "description"))
        src = _tag(block, "source")
        link = _tag(block, "link")
        # description 常只是來源媒體名的重複，去除以避免污染情感詞頻
        if desc and src and desc.replace(src, "").strip() in ("", "-"):
            desc = ""
        content = title if not desc else f"{title}。{desc}"
        out.append({
            "source": f"新聞：{src}" if src else "新聞",
            "post_date": _rss_date(_tag(block, "pubDate")),
            "content": content,
            "engagement": "",
            "url": link,
        })
    return out


# ---------------------------------------------------------------- PTT
PTT_BASE = "https://www.ptt.cc"
# 只保留實測存在的看板（Preschool／parenting／teach 皆為 404）。
# PTT 對持續性請求會限速，掃多個看板不但成本高（每所機構多數個請求），
# 還容易觸發封鎖，因此批次採集預設不啟用 PTT
# （見 collect_for_institutions 的 include_ptt），僅在針對單一機構的
# 即時偵查、或搭配 --limit 的小規模採集時使用。
PTT_BOARDS = ("BabyMother", "Kids", "Education")

_PTT_ROW_RE = re.compile(
    r'<div class="r-ent">(.*?)</div>\s*</div>\s*</div>', re.S)
_PTT_TITLE_RE = re.compile(r'<div class="title">\s*(?:<a href="([^"]+)">(.*?)</a>|(.*?))\s*</div>', re.S)
_PTT_NREC_RE = re.compile(r'<div class="nrec">(?:<span[^>]*>)?(.*?)(?:</span>)?</div>', re.S)
_PTT_DATE_RE = re.compile(r'<div class="date">\s*(.*?)\s*</div>', re.S)


def fetch_ptt(sess: Session, query: str, boards=PTT_BOARDS,
              limit_per_board: int = 25, fetch_content: bool = False) -> list[dict]:
    """搜尋 PTT 公開看板。

    預設只取標題（fetch_content=False），因為逐篇抓取內文的請求量大、
    且標題已足以觸發風險詞庫；需要更細緻分析時可開啟內文抓取。
    """
    out: list[dict] = []
    year_now = datetime.now().year
    for board in boards:
        url = (f"{PTT_BASE}/bbs/{board}/search?q="
               f"{urllib.parse.quote(query)}")
        try:
            html = sess.get(url)
        except Exception:  # noqa: BLE001
            continue
        if "r-ent" not in html:
            continue
        blocks = re.split(r'<div class="r-ent">', html)[1:]
        for blk in blocks[:limit_per_board]:
            tm = _PTT_TITLE_RE.search(blk)
            if not tm:
                continue
            href = tm.group(1) or ""
            title = strip_tags(tm.group(2) or tm.group(3) or "")
            if not title or "本文已被刪除" in title:
                continue
            nm = _PTT_NREC_RE.search(blk)
            nrec = strip_tags(nm.group(1)) if nm else ""
            dm = _PTT_DATE_RE.search(blk)
            dstr = strip_tags(dm.group(1)) if dm else ""
            post_date = ""
            md = re.match(r"(\d{1,2})/(\d{1,2})", dstr)
            if md:
                # PTT 搜尋結果只給月/日，年份需由文章連結的 timestamp 推回
                yr = year_now
                tsm = re.search(r"/M\.(\d{9,11})\.", href)
                if tsm:
                    try:
                        yr = datetime.fromtimestamp(
                            int(tsm.group(1)), tz=timezone.utc).year
                    except (ValueError, OSError):
                        yr = year_now
                try:
                    post_date = f"{yr:04d}-{int(md.group(1)):02d}-{int(md.group(2)):02d}"
                except ValueError:
                    post_date = ""

            content = title
            if fetch_content and href:
                try:
                    page = sess.get(PTT_BASE + href)
                    body = re.search(r'id="main-content"[^>]*>(.*?)<span class="f2">',
                                     page, re.S)
                    if body:
                        content = f"{title}。{strip_tags(body.group(1))[:1200]}"
                except Exception:  # noqa: BLE001
                    pass

            out.append({
                "source": "PTT",
                "post_date": post_date,
                "content": content,
                "engagement": nrec if nrec.isdigit() else ("100" if nrec == "爆" else ""),
                "url": PTT_BASE + href if href else "",
            })
    return out


# ---------------------------------------------------------------- 機構層級彙整
_NOISE_PREFIX_RE = re.compile(r"^(新北市|臺北市|台北市)")
_NOISE_SUFFIX_RE = re.compile(r"(非營利幼兒園|附設幼兒園|幼兒園|幼稚園)$")


def short_name(name: str) -> str:
    """把機構全名縮成適合搜尋的核心名稱。

    例：「新北市安溪非營利幼兒園」→「安溪」，用於組合查詢字串時提高召回率
    （媒體與網友多以簡稱指稱園所）。
    """
    s = _NOISE_PREFIX_RE.sub("", str(name or "").strip())
    s = re.sub(r"^(私立|市立|縣立|公立|非營利)", "", s)
    s = _NOISE_SUFFIX_RE.sub("", s)
    return s.strip()


def queries_for(name: str, district: str = "") -> list[str]:
    """為單一機構產生查詢字串組合（精確全名 + 簡稱加地區限定）。"""
    qs = [f'"{name}"']
    core = short_name(name)
    if core and len(core) >= 2:
        loc = district or "新北"
        qs.append(f'"{core}幼兒園" {loc}')
    return qs


def collect_for_institutions(sess: Session, institutions: list[dict],
                             include_ptt: bool = False,
                             on_progress=None) -> list[dict]:
    """逐機構抓取輿情，回傳含 inst_id 的 posts 清單。

    以「精確園名」為主要查詢條件，避免把同名或泛論性報導誤掛到特定機構；
    同一則報導在多個查詢中重複出現時以 (inst_id, url/content) 去重。
    """
    posts: list[dict] = []
    seen: set[tuple[str, str]] = set()
    total = len(institutions)

    for i, inst in enumerate(institutions, 1):
        iid = str(inst.get("inst_id") or "")
        name = str(inst.get("name") or "")
        if not iid or not name:
            continue
        if on_progress:
            on_progress(f"[{i}/{total}] 輿情蒐集：{name}")

        found: list[dict] = []
        for q in queries_for(name, inst.get("district") or ""):
            found.extend(fetch_news(sess, q))
        if include_ptt:
            core = short_name(name)
            if core and len(core) >= 2:
                found.extend(fetch_ptt(sess, f"{core}幼兒園"))

        for p in found:
            key = (iid, p.get("url") or p.get("content", "")[:80])
            if key in seen:
                continue
            seen.add(key)
            posts.append({
                "post_id": f"{iid}-{len(posts) + 1:05d}",
                "inst_id": iid,
                "source": p["source"],
                "post_date": p["post_date"],
                "content": p["content"],
                "engagement": p["engagement"],
                "url": p.get("url", ""),
            })
    return posts


# 風險議題關鍵詞：對應 nlp.TOPICS 的七大風險主題，確保抓到的輿情
# 與下游的主題分類體系一致。
RISK_TERMS = ("違規", "裁罰", "罰鍰", "不當管教", "虐童", "體罰", "超收",
              "師生比", "食安", "餐點", "退費", "收費爭議", "評鑑",
              "停招", "廢止", "教保員", "家長投訴", "申訴")


def collect_city_wide(sess: Session, city: str = "新北市",
                      extra_terms=RISK_TERMS,
                      districts: tuple[str, ...] = (),
                      on_progress=None) -> list[dict]:
    """抓取全市（可再細到行政區）層級的教保輿情。

    **這是輿情採集的主力路徑**，而非逐機構查詢。原因：

    1. 新聞會報導的是「出事的園所」，用風險議題關鍵詞掃一遍，就能撈到
       絕大多數值得注意的事件；而全市 1100 餘所園中的多數小型園所本來
       就沒有任何新聞，逐一查詢等於用上千次請求換取幾乎全空的結果。
    2. 新聞來源對持續查詢會逐步限速（實測逐機構查到第 100 所時，速率從
       每分鐘 26 筆掉到 1 筆，完成全部需十餘小時）。議題掃描只需數十次
       請求，可在幾分鐘內完成且不易觸發限速。

    抓到的報導會在 collect_sentiment.py 以機構名稱回填 inst_id；
    比對不到者保留為市級背景輿情（inst_id 留空），不計入任何機構分數。
    """
    posts: list[dict] = []
    seen: set[str] = set()
    queries = [f"{city} 幼兒園 {t}" for t in extra_terms]
    queries += [f"{city} 教保服務中心 {t}" for t in ("違規", "裁罰", "不當管教")]
    # 加上行政區可挖出地方媒體的在地報導（全市關鍵詞常被大型事件淹沒）
    queries += [f"{city}{d} 幼兒園 {t}"
                for d in districts for t in ("違規", "不當管教", "裁罰")]

    for i, q in enumerate(queries, 1):
        if on_progress:
            on_progress(f"議題掃描 [{i}/{len(queries)}]：{q}")
        for p in fetch_news(sess, q):
            key = p.get("url") or p["content"][:80]
            if key in seen:
                continue
            seen.add(key)
            posts.append({
                "post_id": f"CITY-{len(posts) + 1:05d}",
                "inst_id": "",
                "source": p["source"],
                "post_date": p["post_date"],
                "content": p["content"],
                "engagement": p["engagement"],
                "url": p.get("url", ""),
            })
    return posts
