"""採集器共用的 HTTP 工具：Session（cookie）、解壓、重試、禮貌延遲、ASP.NET postback。

僅使用標準函式庫，確保與主程式的離線打包需求一致。
"""
from __future__ import annotations

import gzip
import http.cookiejar
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

DEFAULT_DELAY = 0.8   # 禮貌性延遲（秒）
DEFAULT_RETRY = 3
DEFAULT_TIMEOUT = 40


class Session:
    """帶 cookie 與重試機制的簡易 HTTP session。"""

    def __init__(self, delay: float = DEFAULT_DELAY, timeout: int = DEFAULT_TIMEOUT,
                 retry: int = DEFAULT_RETRY, verify_tls: bool = True):
        self.delay = delay
        self.timeout = timeout
        self.retry = retry
        self.jar = http.cookiejar.CookieJar()
        ctx = ssl.create_default_context()
        if not verify_tls:
            # 部分政府網站憑證鏈設定不全，允許呼叫端在必要時關閉驗證。
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=ctx),
        )
        self._last_at = 0.0
        self.throttled = 0   # 被限速的次數，供呼叫端判斷是否該縮小採集範圍

    # ------------------------------------------------------------ 低階請求
    def _sleep(self) -> None:
        wait = self.delay - (time.time() - self._last_at)
        if wait > 0:
            time.sleep(wait)

    def request(self, url: str, data: dict | None = None, referer: str = "",
                accept: str = "text/html,application/xhtml+xml,application/xml,*/*",
                extra_headers: dict | None = None) -> str:
        """發出請求並回傳解碼後的文字；失敗時重試，最終失敗拋出例外。"""
        headers = {
            "User-Agent": UA,
            "Accept": accept,
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        }
        if referer:
            headers["Referer"] = referer
        if extra_headers:
            headers.update(extra_headers)

        body = None
        if data is not None:
            body = urllib.parse.urlencode(data, encoding="utf-8").encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        last_exc: Exception | None = None
        for attempt in range(self.retry):
            self._sleep()
            try:
                req = urllib.request.Request(url, data=body, headers=headers)
                with self.opener.open(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    enc = (resp.headers.get("Content-Encoding") or "").lower()
                    if enc == "gzip":
                        raw = gzip.decompress(raw)
                    elif enc == "deflate":
                        try:
                            raw = zlib.decompress(raw)
                        except zlib.error:
                            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                    charset = "utf-8"
                    ct = resp.headers.get("Content-Type") or ""
                    m = re.search(r"charset=([\w-]+)", ct, re.I)
                    if m:
                        charset = m.group(1)
                    return raw.decode(charset, errors="replace")
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
                    ssl.SSLError, ConnectionError) as exc:
                last_exc = exc
                # 4xx（除 429）通常重試無用，直接放棄
                if isinstance(exc, urllib.error.HTTPError) and \
                        400 <= exc.code < 500 and exc.code != 429:
                    raise
                # 遇到限速（429）時採指數退避，並把延遲永久調高：
                # 新聞來源對持續查詢會逐步收緊配額，若只退避單次請求，
                # 後續每一筆都會再撞一次限速，整體吞吐反而更差
                # （實測從每分鐘 26 筆掉到 1 筆）。
                if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                    self.delay = min(self.delay * 1.6 + 0.5, 20.0)
                    self.throttled += 1
                    time.sleep(min(60.0, 4.0 * (2 ** attempt)))
                else:
                    time.sleep(1.2 * (attempt + 1))
            finally:
                self._last_at = time.time()
        raise last_exc if last_exc else RuntimeError(f"請求失敗：{url}")

    def get(self, url: str, **kw) -> str:
        return self.request(url, data=None, **kw)

    def post(self, url: str, data: dict, **kw) -> str:
        return self.request(url, data=data, **kw)


# ---------------------------------------------------------------- HTML 工具
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)

_ENTITIES = {
    "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
    "&#39;": "'", "&apos;": "'", "&#160;": " ",
}


def unescape(s: str) -> str:
    for k, v in _ENTITIES.items():
        s = s.replace(k, v)
    # 數值型實體
    s = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), s)
    s = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), s)
    return s


def strip_tags(html: str) -> str:
    """移除標籤與 script/style，回傳壓縮空白後的純文字。"""
    html = _SCRIPT_RE.sub(" ", html)
    return _WS_RE.sub(" ", unescape(_TAG_RE.sub(" ", html))).strip()


def text_lines(html: str) -> list[str]:
    """把 HTML 轉成逐行純文字（標籤換行），保留區塊順序。"""
    html = _SCRIPT_RE.sub(" ", html)
    raw = _TAG_RE.sub("\n", unescape(html))
    out = []
    for line in raw.split("\n"):
        t = _WS_RE.sub(" ", line).strip()
        if t and t != "\xa0":
            out.append(t)
    return out


# ---------------------------------------------------------------- ASP.NET
_HIDDEN_RE_A = re.compile(
    r'<input[^>]+type="hidden"[^>]*name="([^"]+)"[^>]*value="([^"]*)"', re.I)
_HIDDEN_RE_B = re.compile(
    r'<input[^>]+name="([^"]+)"[^>]*type="hidden"[^>]*value="([^"]*)"', re.I)


def hidden_fields(html: str) -> dict[str, str]:
    """擷取 ASP.NET WebForms 的隱藏欄位（__VIEWSTATE 等）。"""
    out: dict[str, str] = {}
    for rx in (_HIDDEN_RE_A, _HIDDEN_RE_B):
        for m in rx.finditer(html):
            out.setdefault(m.group(1), unescape(m.group(2)))
    return out


def postback_payload(html: str, event_target: str, event_argument: str = "",
                     extra: dict[str, str] | None = None) -> dict[str, str]:
    """組出 __doPostBack 所需的表單資料。"""
    data = hidden_fields(html)
    data["__EVENTTARGET"] = event_target
    data["__EVENTARGUMENT"] = event_argument
    if extra:
        data.update(extra)
    return data


def has_postback(html: str, target: str) -> bool:
    """檢查頁面是否存在指定的 postback 目標（用於判斷是否還有下一頁）。"""
    needle = target.replace("$", r"\$")
    return re.search(r"__doPostBack\(&#39;" + needle + r"&#39;", html) is not None or \
        re.search(r"__doPostBack\('" + needle + r"'", html) is not None


def select_options(html: str, name: str) -> list[tuple[str, str]]:
    """取出指定 <select> 的所有 (value, label)。"""
    m = re.search(r'<select[^>]+name="' + re.escape(name) + r'"[^>]*>(.*?)</select>',
                  html, re.S | re.I)
    if not m:
        return []
    return [(v, _WS_RE.sub(" ", unescape(_TAG_RE.sub("", lbl))).strip())
            for v, lbl in re.findall(
                r'<option[^>]*value="([^"]*)"[^>]*>(.*?)</option>', m.group(1), re.S)]
