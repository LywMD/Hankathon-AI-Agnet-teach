"""中文輿情分析（NLP）模組 — 純標準函式庫實作。

流程：
1. 文本正規化（全形轉半形、去除連結與雜訊）
2. 詞庫比對式情感分析：情緒詞 × 程度副詞 × 否定詞轉向
3. 風險主題分類：以主題詞庫命中權重歸類，並帶入主題嚴重度
4. 來源可信度加權（新聞／陳情 > 匿名社群）
5. 爆量偵測（burst detection）：近 30 日聲量對比歷史基線，Poisson 尾機率
6. 關鍵詞抽取：字元 n-gram 統計 + 停用詞過濾
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from datetime import date, timedelta

# ---------------------------------------------------------------- 詞庫
NEG_WORDS = {
    "虐": 3.0, "體罰": 3.0, "打小孩": 3.0, "吼": 2.0, "嚇": 1.6, "瘀青": 2.6,
    "受傷": 2.2, "不當管教": 3.0, "關禁": 2.8, "關在": 2.4, "推倒": 2.4,
    "餵藥": 3.0, "餵食藥": 3.0, "疑似": 1.4, "疏忽": 2.0, "沒人顧": 2.2,
    "超收": 2.4, "師生比": 1.6, "人力不足": 2.0, "代課": 1.2, "離職": 1.4,
    "換老師": 1.6, "流動": 1.3, "拖延": 2.0, "欠薪": 2.8, "薪水": 0.8,
    "不新鮮": 2.0, "過期": 2.6, "拉肚子": 2.2, "腸胃炎": 2.2, "食物中毒": 3.0,
    "不衛生": 2.2, "髒": 1.8, "蟑螂": 2.4, "沒消毒": 2.0, "異味": 1.5,
    "退費": 1.6, "不退": 2.2, "亂收費": 2.6, "加收": 2.0, "收據": 1.2,
    "保證金": 1.8, "不透明": 2.0, "跟公告不一樣": 2.4, "違法": 2.6,
    "生鏽": 1.8, "鬆動": 1.8, "沒修": 1.6, "逃生": 2.2, "消防": 1.8,
    "門禁": 1.8, "陌生人": 2.0, "危險": 2.2, "堆滿": 1.5,
    "混亂": 1.6, "臨時改": 1.2, "查不到": 1.2, "沒有更新": 1.0, "推託": 2.0,
    "投訴": 2.0, "陳情": 2.2, "檢舉": 2.4, "告": 1.8, "教育局": 1.2,
    "擔心": 1.4, "失望": 1.6, "後悔": 1.8, "不推薦": 2.0, "地雷": 2.2,
    "沒有正面說明": 1.8, "敷衍": 1.8, "情緒不穩": 1.6, "適應不良": 1.4,
}
POS_WORDS = {
    "用心": 2.0, "開心": 1.8, "推薦": 2.0, "很棒": 2.2, "感謝": 1.8,
    "乾淨": 1.6, "營養": 1.4, "均衡": 1.4, "進步": 1.6, "耐心": 1.8,
    "透明": 1.6, "充足": 1.4, "採光好": 1.4, "即時": 1.2, "值得": 1.6,
    "安心": 1.8, "貼心": 1.6, "專業": 1.6, "細心": 1.8, "溫暖": 1.5,
}
DEGREE_WORDS = {"非常": 1.6, "超": 1.5, "很": 1.3, "太": 1.4, "極": 1.7,
                "有點": 0.7, "稍微": 0.6, "還算": 0.6, "特別": 1.4, "真的": 1.3,
                "完全": 1.5, "根本": 1.5, "一直": 1.4, "常常": 1.3, "又": 1.2}
NEGATION = {"不", "沒", "沒有", "未", "別", "非", "無", "毫無", "並未", "不會"}

TOPICS: dict[str, dict] = {
    "兒少保護": {
        "severity": 3.0,
        "words": ["虐", "體罰", "打小孩", "不當管教", "瘀青", "受傷", "餵藥", "關在",
                  "吼", "推倒", "疏忽", "情緒不穩", "身上有傷"],
    },
    "餐飲衛生": {
        "severity": 2.2,
        "words": ["餐點", "食材", "過期", "不新鮮", "拉肚子", "腸胃炎", "食物中毒",
                  "廚房", "衛生", "消毒", "蟑螂", "餐具", "優酪乳", "午餐"],
    },
    "設施安全": {
        "severity": 2.2,
        "words": ["遊具", "生鏽", "鬆動", "樓梯", "防護", "逃生", "消防", "門禁",
                  "陌生人", "接送", "危險", "堆滿", "設備"],
    },
    "收退費爭議": {
        "severity": 1.9,
        "words": ["退費", "收費", "加收", "才藝費", "保證金", "收據", "繳費",
                  "明細", "月費", "註冊費", "亂收"],
    },
    "人員異動": {
        "severity": 1.8,
        "words": ["換老師", "離職", "流動", "代課", "師資", "教保員", "欠薪",
                  "薪水", "人力不足", "導師"],
    },
    "超收與師生比": {
        "severity": 2.3,
        "words": ["超收", "師生比", "人數太多", "塞", "一位老師", "混班", "招生"],
    },
    "行政管理": {
        "severity": 1.2,
        "words": ["行政", "通知", "公告", "家長委員會", "混亂", "查不到", "更新"],
    },
}

SOURCE_CREDIBILITY = {
    "地方新聞": 1.35, "1999陳情": 1.30, "Google評論": 1.10,
    "Facebook社團": 1.0, "家長line群回報": 0.95, "Dcard": 0.9, "PTT": 0.9,
}

STOPWORDS = set("的了是在有和就都而及與著或一個我們你們他們這那很也還把被讓對於因為所以但是如果可以真的好像"
                "感覺覺得聽說其實什麼怎麼哪裡自己一直已經比較應該可能需要該園幼兒園非營利私立區立市立"
                "新北臺北桃園臺中高雄臺南") | \
    {"孩子", "小孩", "老師", "園長", "家長", "幼兒", "該園", "園方", "教室", "沒有", "不會",
     "這樣", "真的", "聽說", "希望", "還是", "一個", "兩個", "上週", "回家", "反映", "說會"}

_URL_RE = re.compile(r"https?://\S+")
_NONCJK_RE = re.compile(r"[^\u4e00-\u9fffA-Za-z0-9]+")


def normalize_text(t: str) -> str:
    t = unicodedata.normalize("NFKC", str(t or ""))
    t = _URL_RE.sub(" ", t)
    return t.strip()


def analyze_post(content: str, source: str = "") -> dict:
    """單篇貼文分析：情感極性、風險主題、風險強度。"""
    text = normalize_text(content)
    neg_score = 0.0
    pos_score = 0.0
    hits: list[str] = []

    for word, w in NEG_WORDS.items():
        idx = 0
        while True:
            i = text.find(word, idx)
            if i < 0:
                break
            idx = i + len(word)
            ctx = text[max(0, i - 3):i]
            mult = 1.0
            for dw, dv in DEGREE_WORDS.items():
                if dw in ctx:
                    mult = max(mult, dv)
            flipped = any(ng in ctx for ng in NEGATION)
            if flipped:
                pos_score += w * 0.6
            else:
                neg_score += w * mult
                hits.append(word)
    for word, w in POS_WORDS.items():
        if word in text:
            ctx_i = text.find(word)
            ctx = text[max(0, ctx_i - 3):ctx_i]
            if any(ng in ctx for ng in NEGATION):
                neg_score += w * 0.8
                hits.append("不" + word)
            else:
                mult = 1.0
                for dw, dv in DEGREE_WORDS.items():
                    if dw in ctx:
                        mult = max(mult, dv)
                pos_score += w * mult

    total = neg_score + pos_score
    polarity = 0.0 if total == 0 else (pos_score - neg_score) / total

    topic_scores: dict[str, float] = {}
    for topic, spec in TOPICS.items():
        s = sum(1.0 for w in spec["words"] if w in text)
        if s:
            topic_scores[topic] = s * spec["severity"]
    topic = max(topic_scores, key=topic_scores.get) if topic_scores else "其他"
    severity = TOPICS.get(topic, {}).get("severity", 1.0)

    cred = SOURCE_CREDIBILITY.get(source, 1.0)
    # 風險強度 0~1：負面程度 × 主題嚴重度 × 來源可信度
    intensity = 0.0
    if polarity < 0:
        intensity = min(1.0, (-polarity) * (0.45 + 0.25 * severity) * cred)
    return {
        "polarity": round(polarity, 4),
        "negative": polarity < -0.15,
        "topic": topic if polarity < 0 else ("正面回饋" if polarity > 0.15 else "中性"),
        "severity": severity,
        "intensity": round(intensity, 4),
        "hits": hits[:8],
    }


def burst_detection(dates: list[str], as_of: date, window: int = 30,
                    baseline_days: int = 365) -> dict:
    """近期聲量爆量偵測：以歷史日均為基線，計算 Poisson 右尾機率。"""
    ds = []
    for s in dates:
        try:
            ds.append(date.fromisoformat(s))
        except (ValueError, TypeError):
            continue
    if not ds:
        return {"recent": 0, "baseline_daily": 0.0, "expected": 0.0,
                "p_value": 1.0, "burst": False, "score": 0.0}
    recent_start = as_of - timedelta(days=window)
    base_start = as_of - timedelta(days=baseline_days + window)
    recent = sum(1 for d in ds if recent_start < d <= as_of)
    base = sum(1 for d in ds if base_start < d <= recent_start)
    baseline_daily = base / max(1, baseline_days)
    expected = max(0.15, baseline_daily * window)
    # Poisson P(X >= recent)
    p = 1.0
    if recent > 0:
        cum = 0.0
        term = math.exp(-expected)
        for k in range(recent):
            cum += term
            term *= expected / (k + 1)
        p = max(0.0, min(1.0, 1 - cum))
    burst = recent >= 3 and p < 0.05
    score = 0.0
    if recent >= 2:
        score = max(0.0, min(100.0, (1 - p) * 100 * min(1.0, recent / 6)))
    return {"recent": recent, "baseline_daily": round(baseline_daily, 4),
            "expected": round(expected, 2), "p_value": round(p, 6),
            "burst": burst, "score": round(score, 2)}


def _build_vocab() -> dict[str, dict]:
    """關鍵詞詞彙表：以風險詞庫與主題詞庫為基礎，附帶主題與權重。"""
    vocab: dict[str, dict] = {}
    for topic, spec in TOPICS.items():
        for w in spec["words"]:
            if len(w) >= 2:
                vocab.setdefault(w, {"topic": topic, "weight": spec["severity"]})
    for w, wt in NEG_WORDS.items():
        if len(w) >= 2:
            vocab.setdefault(w, {"topic": "負面情緒", "weight": wt})
    return vocab


VOCAB = _build_vocab()


def keywords(texts: list[str], top: int = 30) -> list[dict]:
    """關鍵詞抽取。

    採「領域詞庫優先 + 新詞發現補充」兩階段：
    1. 先以風險／主題詞庫統計命中次數，確保詞彙有明確監理意義。
    2. 再以字元 n-gram 找出詞庫未涵蓋且高頻的候選新詞（會過濾與既有詞重疊者），
       讓詞庫能隨輿情演變持續擴充。
    """
    cnt: Counter[str] = Counter()
    for t in texts:
        text = normalize_text(t)
        for w in VOCAB:
            c = text.count(w)
            if c:
                cnt[w] += c

    out = [{"word": w, "count": c, "topic": VOCAB[w]["topic"],
            "weight": VOCAB[w]["weight"], "source": "詞庫"}
           for w, c in cnt.most_common(top)]

    if len(out) < top:
        ng: Counter[str] = Counter()
        for t in texts:
            seg_text = _NONCJK_RE.sub(" ", normalize_text(t))
            for seg in seg_text.split():
                for n in (3, 4):
                    for i in range(len(seg) - n + 1):
                        g = seg[i:i + n]
                        if any(ch in STOPWORDS for ch in g) or g in STOPWORDS:
                            continue
                        if any(v in g or g in v for v in VOCAB):
                            continue
                        ng[g] += 1
        known = {o["word"] for o in out}
        for g, c in ng.most_common(top * 8):
            if len(out) >= top:
                break
            if c < 3 or g in known:
                continue
            if any(g in k or k in g for k in known):
                continue
            known.add(g)
            out.append({"word": g, "count": c, "topic": "新詞候選",
                        "weight": 1.0, "source": "新詞發現"})
    return out


def summarize_institution(posts: list[dict], as_of: date) -> dict:
    """機構層級輿情彙總。"""
    if not posts:
        return {
            "n_posts": 0, "n_negative": 0, "neg_ratio": 0.0, "avg_polarity": 0.0,
            "weighted_negative": 0.0, "topics": [], "burst": burst_detection([], as_of),
            "score": 0.0, "recent_examples": [], "timeline": [],
        }
    analyzed = []
    for p in posts:
        a = analyze_post(p.get("content", ""), p.get("source", ""))
        a["post_date"] = p.get("post_date")
        a["source"] = p.get("source")
        a["content"] = p.get("content")
        a["engagement"] = p.get("engagement") or 0
        analyzed.append(a)

    n = len(analyzed)
    negs = [a for a in analyzed if a["negative"]]
    neg_ratio = len(negs) / n
    avg_pol = sum(a["polarity"] for a in analyzed) / n

    # 時間衰減加權負面強度（半衰期 180 天）
    wsum = 0.0
    for a in negs:
        try:
            d = date.fromisoformat(a["post_date"])
            age = max(0, (as_of - d).days)
        except (ValueError, TypeError):
            age = 365
        decay = 0.5 ** (age / 180)
        eng = 1 + math.log10(1 + (a["engagement"] or 0)) / 3
        wsum += a["intensity"] * decay * eng
    burst = burst_detection([a["post_date"] for a in analyzed if a["post_date"]], as_of)

    topic_cnt: Counter[str] = Counter(a["topic"] for a in negs)
    topics = [{"topic": t, "count": c,
               "severity": TOPICS.get(t, {}).get("severity", 1.0)}
              for t, c in topic_cnt.most_common()]

    # 綜合輿情分數 0~100
    volume_term = min(1.0, wsum / 6.0) * 62
    ratio_term = neg_ratio * 20
    burst_term = burst["score"] * 0.18
    score = max(0.0, min(100.0, volume_term + ratio_term + burst_term))

    negs_sorted = sorted(negs, key=lambda a: (a["post_date"] or "", a["intensity"]),
                         reverse=True)
    timeline: Counter[str] = Counter()
    for a in analyzed:
        if a["post_date"]:
            timeline[a["post_date"][:7]] += 1
    neg_timeline: Counter[str] = Counter()
    for a in negs:
        if a["post_date"]:
            neg_timeline[a["post_date"][:7]] += 1

    return {
        "n_posts": n,
        "n_negative": len(negs),
        "neg_ratio": round(neg_ratio, 4),
        "avg_polarity": round(avg_pol, 4),
        "weighted_negative": round(wsum, 3),
        "topics": topics,
        "burst": burst,
        "score": round(score, 2),
        "recent_examples": [
            {"post_date": a["post_date"], "source": a["source"], "topic": a["topic"],
             "intensity": a["intensity"], "engagement": a["engagement"],
             "content": a["content"]}
            for a in negs_sorted[:6]
        ],
        "negative_texts": [a["content"] for a in negs],
        "negative_dates": [a["post_date"] for a in negs],
        "negative_sources": [a["source"] for a in negs],
        "timeline": [{"month": m, "total": timeline[m], "negative": neg_timeline.get(m, 0)}
                     for m in sorted(timeline)],
    }
