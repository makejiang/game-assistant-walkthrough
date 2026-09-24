"""Webserver for the walkthrough assistant (push-only + pass-through proxy).

The webserver no longer downloads/filters/paginates guide pages. It only:

  - Receives navigation targets from the desktop app (POST /api/navigate and
    /api/game-detected): {game, url, image_src, title}.
  - Publishes those targets to open browsers over SSE (GET /api/events).
  - Serves the viewer page (GET /) which renders the pushed URL inside a frame
    and scrolls to the pushed content position.
  - Serves GET /proxy?url=... — a *pass-through* fetch of gamersky pages so the
    frame is same-origin with the viewer. Browsers forbid scrolling a
    cross-origin frame, so without the proxy the "locate content position"
    requirement is technically impossible. The proxy does no filtering: it
    injects <base> + no-referrer and rewrites page links to stay proxied.

Serving is deliberately stdlib-only (http.server); the proxy fetch uses
requests (already required by the desktop client).
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import re
import sys
import threading
import time
from collections import OrderedDict
from html import escape as html_escape
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

from app import download_progress
from app.config import AppConfig
from app.netinfo import lan_adapters, lan_url
from app.state import StateStore

_logger = logging.getLogger("webserver")
_proxy_log = logging.getLogger("webserver.proxy")

STATIC_DIR = Path(__file__).resolve().parent / "static"

# /proxy 只允许访问游民星空域名，防止被当成任意代理(SSRF)
PROXY_ALLOWED_SUFFIX = ".gamersky.com"
PROXY_ALLOWED_HOSTS = {"gamersky.com"}
PROXY_TIMEOUT = 15.0
PROXY_CACHE_TTL = 600.0
PROXY_CACHE_MAX = 32
TOC_CACHE_TTL = 600.0

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)

_ANCHOR_HREF_RE = re.compile(
    r"(<a\b[^>]*?\bhref\s*=\s*)(['\"])(.*?)\2", re.IGNORECASE | re.DOTALL
)

# 转发请求的参数/请求体里若出现"本代理页面地址"（如评论接口的 contentUrl），
# 还原成真实页面地址：游民星空服务端无法访问我们的局域网地址。
_UNWRAP_RAW_RE = re.compile(r"https?://[^/\"'\\s&]+/proxy/", re.IGNORECASE)
_UNWRAP_ENC_RE = re.compile(r"https?%3a%2f%2f[^&\s]*?%2fproxy%2f", re.IGNORECASE)


def _unwrap_self_proxy(text: str) -> str:
    if "/proxy/" not in text and "%2fproxy%2f" not in text.lower():
        return text
    text = _UNWRAP_ENC_RE.sub("", text)
    return _UNWRAP_RAW_RE.sub("", text)


def _extract_toc_entries(html: str, page_url: str) -> list[dict[str, str]]:
    """从攻略页底部的“文章内容导航”（div.post_ding）提取章节目录。

    返回 [{name, url}]（url 为绝对地址），按页码排序；目录里未加链接的高亮项
    （当前页）用当前页地址补齐。解析失败或无目录时返回空列表。

    新版“攻略路书”页（tools/guide-map/roadbooks/<id>）没有 post_ding/翻页器，
    目录即内联的 reader-node-card 节点卡标题，链接用 #reader-node-<id> 片段定位。
    """
    from urllib.parse import urljoin

    from bs4 import BeautifulSoup

    # page_url 可能自带 #片段（推送地址常态）：目录项地址必须基于去片段的
    # 页面地址，否则会拼出“双片段”目录项，页面无法高亮当前章节、点选无法定位
    page_url = str(page_url or "").split("#", 1)[0]

    soup = BeautifulSoup(html, "html.parser")
    found: list[tuple[int, dict[str, str]]] = []
    seen_urls: set[str] = set()

    node_articles = soup.find_all("article", class_="reader-node-card")
    if node_articles:
        for article in node_articles:
            name_parts = []
            for class_name in ("reader-node-card__node-id", "reader-node-card__title"):
                tag = article.find(class_=class_name)
                if tag is not None:
                    text = tag.get_text(" ", strip=True)
                    if text:
                        name_parts.append(text)
            name = " ".join(name_parts)
            if not name:
                continue
            anchor_id = str(article.get("id") or "").strip()
            url = f"{page_url}#{anchor_id}" if anchor_id else page_url
            if url in seen_urls:
                continue
            seen_urls.add(url)
            found.append((len(found) + 1, {"name": name, "url": url}))
        return [entry for _, entry in found]

    containers = []
    nav = soup.find("div", class_="post_ding")
    if nav is not None:
        containers.append(nav)
    else:
        # 无“文章内容导航”时才回退翻页器（其中含评论/外链等杂项，仅保留站内链接）
        containers.extend(soup.find_all(["span", "div"], class_=re.compile(r"page_?css", re.IGNORECASE)))

    for container in containers:
        for li in container.find_all("li"):
            name = li.get_text(" ", strip=True)
            if not name:
                continue
            anchor = li.find("a", href=True)
            if anchor is not None:
                url = urljoin(page_url, anchor["href"])
            elif li.find(["b", "strong"]) is not None:
                url = page_url  # 当前页：目录中高亮但未加链接的项
            else:
                continue
            parts = urlparse(url)
            if parts.scheme not in ("http", "https"):
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)
            match = re.search(r"第(\d+)页", name)
            page_no = int(match.group(1)) if match else len(found) + 1
            found.append((page_no, {"name": name, "url": url}))

    found.sort(key=lambda item: item[0])
    return [entry for _, entry in found]

# 注入到 <head> 最前：代理抓的是 PC 版页面，但手机浏览器里的 frame 内 JS 读到的
# navigator.userAgent 是手机的，游民星空据此把 frame 重定向到 wap.gamersky.com
# （跨域后无法定位/提示）。这里在站点脚本执行前把环境归一成桌面，阻止跳转。
def _is_wap_url(url: str) -> bool:
    return (urlparse(url).hostname or "").lower().startswith("wap.")


_ENV_NORMALIZE_TEMPLATE = """
<script>
(function () {
  var UA = "__UA__";
  function freeze(obj, prop, value) {
    try { Object.defineProperty(obj, prop, { get: function () { return value; }, configurable: true }); } catch (e) {}
  }
  freeze(navigator, "userAgent", UA);
  freeze(navigator, "appVersion", UA.replace(/^Mozilla\\//, ""));
  freeze(navigator, "platform", "__PLATFORM__");

  // 页面里的 gamersky 域 XHR/fetch 从我们的源发出会被 CORS 拦截（评论区/互动数据
  // 因此加载失败）。把它们转回本源 /proxy 透传，恢复同源语义。
  // 注意必须拼上 location.origin：页面注入了指向游民星空的 <base>，
  // 根相对地址会被解析到游民星空域名。
  // 另外：站点会把当前页面地址（我们的代理地址）作为 contentUrl 之类的参数/字段
  // 上报——游民星空服务端无法访问我们的局域网地址，"推荐"评论等需要服务端解析
  // 页面的接口会因此失败。凡出现"本代理页面地址"的地方一律还原成真实页面地址。
  var SELF_MARK = location.origin + "/proxy/";
  var SELF_MARK_ENC = encodeURIComponent(SELF_MARK);
  function unwrapSelfProxy(s) {
    if (SELF_MARK_ENC && s.indexOf(SELF_MARK_ENC) >= 0) s = s.split(SELF_MARK_ENC).join("");
    if (s.indexOf(SELF_MARK) >= 0) s = s.split(SELF_MARK).join("");
    return s;
  }
  function toProxyUrl(u) {
    try {
      var abs = new URL(String(u), document.baseURI || location.href);
      if (abs.origin === location.origin) return u;
      var h = abs.hostname.toLowerCase();
      if (!(h === "gamersky.com" || h.endsWith(".gamersky.com"))) return u;
      return unwrapSelfProxy(location.origin + "/proxy?url=" + encodeURIComponent(abs.href));
    } catch (e) { return u; }
  }
  var _open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url) {
    var rest = Array.prototype.slice.call(arguments, 2);
    var u = String(url || "");
    var pu = toProxyUrl(u);
    if (pu !== u) {
      try { console.info("[gs-proxy]", method, u); } catch (e) {}
    }
    return _open.apply(this, [method, pu].concat(rest));
  };
  var _send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function (body) {
    try {
      // 请求体可能是 URL 编码形态（%2Fproxy%2F），无条件做还原（无前缀时为空操作）
      if (typeof body === "string") body = unwrapSelfProxy(body);
    } catch (e) {}
    return _send.call(this, body);
  };
  if (window.fetch) {
    var _fetch = window.fetch;
    window.fetch = function (input, init) {
      try {
        if (typeof input === "string") input = toProxyUrl(input);
        else if (input && input.url) input = new Request(toProxyUrl(input.url), input);
        if (init && typeof init.body === "string") {
          init = Object.assign({}, init, { body: unwrapSelfProxy(init.body) });
        }
      } catch (e) {}
      return _fetch.call(window, input, init);
    };
  }

  // 兜底：站点 JS 动态插入的根相对代理链接（/proxy/...）会被 <base> 解析到
  // 游民星空域名，点击时补回本源。
  document.addEventListener("click", function (e) {
    var t = e.target;
    while (t && t !== document && !(t.tagName === "A")) t = t.parentNode;
    if (!t || t.tagName !== "A") return;
    var href = t.getAttribute("href") || "";
    if (href.indexOf("/proxy") === 0) {
      e.preventDefault();
      location.href = location.origin + href;
    }
  }, true);

  // 评论区默认排序切到“最新”（“推荐”接口在代理环境下加载不出来，一直转圈）：
  // 等评论组件渲染出排序标签后，模拟点击一次“最新”。wap 版没有该标签栏，不启用。
  var DEFAULT_COMMENT_SORT = "__DEFAULT_COMMENT_SORT__";
  if (DEFAULT_COMMENT_SORT) {
    var sortTries = 0;
    var sortTimer = setInterval(function () {
      sortTries += 1;
      if (sortTries > 60) { clearInterval(sortTimer); return; }  // ~30s 后放弃
      var nodes = document.querySelectorAll("a,span,li,div,button,em,p,b,label,i");
      for (var i = 0; i < nodes.length; i++) {
        var el = nodes[i];
        if ((el.textContent || "").trim() !== DEFAULT_COMMENT_SORT) continue;
        // 确认是排序标签组：其祖先容器里应同时存在“推荐”
        var p = el.parentElement, group = null;
        for (var d = 0; d < 4 && p; d++, p = p.parentElement) {
          if ((p.textContent || "").indexOf("推荐") >= 0) { group = p; break; }
        }
        if (!group) continue;
        clearInterval(sortTimer);
        try { el.click(); } catch (e) {}
        return;
      }
    }, 500);
  }

  // wap 版“展开全文”折叠提示：查看器里全文通常已直接展示，但该元素仍会悬浮
  // 挡住文字/图片。检测到后先点击一次（内容确实折叠时走站点自己的展开逻辑），
  // 随后将其移除。PC 版没有此元素，不启用。
  var WAP_EXPAND_FULL = "__WAP_EXPAND_FULL__";
  if (WAP_EXPAND_FULL) {
    var expandTries = 0;
    var expandTimer = setInterval(function () {
      expandTries += 1;
      if (expandTries > 120) { clearInterval(expandTimer); return; }  // ~60s 后放弃
      var nodes = document.querySelectorAll("a,span,div,button,em,p,b,label,section,i");
      for (var i = 0; i < nodes.length; i++) {
        var el = nodes[i];
        var t = (el.textContent || "").trim();
        if (t.indexOf("展开全文") < 0 || t.length > 20) continue;  // 只处理按钮/提示类小元素
        clearInterval(expandTimer);
        try { el.click(); } catch (e) {}
        var node = el;
        setTimeout(function () {
          try { if (node && node.parentNode) node.parentNode.removeChild(node); } catch (e) {}
        }, 600);
        return;
      }
    }, 500);
  }
})();
</script>
"""


def _env_normalize_script(page_url: str) -> str:
    """UA 冻结值与页面版本匹配：www(PC 版)配桌面 UA，wap(手机版)配移动 UA，
    防止站点 JS 把 frame 反向跳转到另一个版本。评论区默认排序仅 PC 版注入。"""
    wap = _is_wap_url(page_url)
    ua = MOBILE_UA if wap else USER_AGENT
    platform = "iPhone" if wap else "Win32"
    return (
        _ENV_NORMALIZE_TEMPLATE
        .replace("__UA__", ua)
        .replace("__PLATFORM__", platform)
        .replace("__DEFAULT_COMMENT_SORT__", "" if wap else "最新")
        .replace("__WAP_EXPAND_FULL__", "1" if wap else "")
    )


def _is_allowed_host(host: str) -> bool:
    host = str(host or "").lower()
    return host in PROXY_ALLOWED_HOSTS or host.endswith(PROXY_ALLOWED_SUFFIX)


# 新版路书页（guide-map/roadbooks/<id>）是桌面版排版：固定 1000px 布局，四周
# 站点导航在手机上完全没法看。frame 内只保留中间图文内容（guide-content-main）：
# 隐藏顶部导航栏/游戏链接条/广告、左侧目录导航（aside.roadbook-nav）、
# 右侧地图导航（.reader-progress-wrapper）、tab 栏与页脚；正文列从固定
# 1000/660px 放宽为自适应宽度，手机上按 fluid 模式铺满即可读。
# 目录跳转由查看器自己的“目录列表”下拉承担（选中后滚到对应节点/图片位置）。
_ROADBOOK_STRIP_CSS = """
.gamersky-nav-wrapper, .Top, .gamersky-game-links, .roadbook-ads,
aside.roadbook-nav, header.guide-tab-nav,
.reader-progress-wrapper, .Bot,
button[aria-label="收起目录"], button[aria-label="展开目录"] { display: none !important; }
html.is-guide-roadbook-document, body.is-guide-roadbook-document { min-width: 0 !important; }
.reader-page-shell, .guide-with-nav-layout, .guide-mode-layout,
.guide-main-wrapper, .guide-main, main.guide-content-main {
  width: auto !important; max-width: 100% !important; min-width: 0 !important;
  margin: 0 auto !important; float: none !important;
}
"""
_ROADBOOK_STRIP_ID = "gs-frame-strip"
_ROADBOOK_STRIP_HTML = (
    f'<style id="{_ROADBOOK_STRIP_ID}">{_ROADBOOK_STRIP_CSS}</style>'
    # 防御：站点 Nuxt 应用接管 <head> 时可能移除外来节点，加载后校验几次，
    # 被移除就补回（保住引用即可重新插入）。
    "<script>(function(){var s=document.getElementById('" + _ROADBOOK_STRIP_ID + "');"
    "if(!s)return;var keep=function(){if(!document.documentElement.contains(s))"
    "{document.head.appendChild(s)}};setTimeout(keep,800);setTimeout(keep,2500);"
    "document.addEventListener('DOMContentLoaded',keep);})();</script>"
)


def _is_roadbook_html(html: str) -> bool:
    """SSR 输出里是否含新版路书节点卡（用于决定是否注入裁剪样式）。"""
    return "reader-node-card" in str(html or "")


class SSEBroker:
    """Very small broadcast hub for Server-Sent Events."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: list[queue.SimpleQueue] = []

    def subscribe(self) -> queue.SimpleQueue:
        q: queue.SimpleQueue = queue.SimpleQueue()
        with self._lock:
            self._clients.append(q)
        return q

    def unsubscribe(self, q: queue.SimpleQueue) -> None:
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            clients = list(self._clients)
        for q in clients:
            q.put(dict(event))


class PageProxy:
    """Same-origin pass-through for gamersky pages. No parsing, no filtering.

    Only transformations (needed to keep the frame working):
      - <base href="原始页面URL"> — relative assets/links resolve against gamersky.
      - <meta name="referrer" content="no-referrer"> — avoids hotlink blocks on
        gamerky images when they are loaded from our origin.
      - <a href> pointing at gamersky pages is rewritten to /proxy?url=... so
        in-frame pagination stays same-origin (and thus locatable).
    """

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT})
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, tuple[float, bytes, str]] = OrderedDict()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._session.close()

    # ------------------------------------------------------------------ fetch
    def fetch(
        self,
        url: str,
        method: str = "GET",
        data: bytes | None = None,
        content_type: str | None = None,
        origin_prefix: str = "",
        transform: bool = True,
    ) -> tuple[int, bytes, str]:
        """带文件日志的透传抓取（耗时/结果/失败都记录到 logs/ 日志文件）。"""
        started = time.monotonic()
        try:
            status, body, ctype = self._fetch(
                url, method=method, data=data, content_type=content_type,
                origin_prefix=origin_prefix, transform=transform,
            )
        except Exception as exc:
            _proxy_log.warning(
                f"{method.upper()} {url} -> 失败 ({time.monotonic() - started:.2f}s): {exc}"
            )
            raise
        _proxy_log.info(
            f"{method.upper()} {url} -> {status} ({len(body)}B, {time.monotonic() - started:.2f}s)"
        )
        return status, body, ctype

    def _fetch(
        self,
        url: str,
        method: str = "GET",
        data: bytes | None = None,
        content_type: str | None = None,
        origin_prefix: str = "",
        transform: bool = True,
    ) -> tuple[int, bytes, str]:
        """Pass-through fetch -> (status, body, content_type); small GET cache.

        缓存里存的是未注入的原始 HTML：链接需要按每次请求的 Host 生成绝对地址
        （电脑用 127.0.0.1 打开、手机用局域网 IP 打开，各自的链接不能串）。
        transform=False 时不做任何注入/改写（供 /api/toc 等纯解析场景）。
        """
        cacheable = method.upper() == "GET" and data is None
        raw_text: str | None = None
        status = 200
        if cacheable:
            raw_text = self._cache_get(url)
        if raw_text is None:
            headers = {"Content-Type": content_type} if content_type else {}
            # wap 版按移动 UA 抓取（站点按 UA 出内容），PC 版按桌面 UA
            headers["User-Agent"] = MOBILE_UA if _is_wap_url(url) else USER_AGENT
            # 显式声明 gzip：本机未装 brotli 解码器，部分接口（router.）无视声明
            # 强制返回 br 会导致解码失败（502），失败时再用 identity（不压缩）重试
            headers["Accept-Encoding"] = "gzip"
            # 站点接口按来源校验（部分 WAF 对无 Origin/Referer 的请求直接断连）
            if _is_allowed_host(urlparse(url).hostname or ""):
                site = "https://wap.gamersky.com" if _is_wap_url(url) else "https://www.gamersky.com"
                headers.setdefault("Origin", site)
                headers.setdefault("Referer", site + "/")

            def _request(target_url: str, accept_encoding: str):
                last: Exception | None = None
                resp = None
                for _attempt in range(2):  # 上游瞬时抖动重试一次
                    try:
                        h = dict(headers)
                        h["Accept-Encoding"] = accept_encoding
                        resp = self._session.request(method=method.upper(), url=target_url, data=data,
                                                     headers=h, timeout=PROXY_TIMEOUT)
                        return resp
                    except requests.exceptions.ContentDecodingError:
                        raise  # 压缩档问题，交给上层换档重试
                    except requests.RequestException as exc:
                        last = exc
                        time.sleep(0.4)
                raise last if last else RuntimeError("proxy request failed")

            response = None
            last_exc: Exception | None = None
            attempts: list[tuple[str, str]] = [(url, "gzip")]
            if url.startswith("http://"):
                attempts.append(("https://" + url[7:], "gzip"))  # http 端点失效时退回 https
            for target_url, accept_encoding in attempts:
                try:
                    response = _request(target_url, accept_encoding)
                except requests.exceptions.ContentDecodingError:
                    # 上游强制 brotli 且本机无解码器 -> 用 identity（不压缩）重取
                    try:
                        response = _request(target_url, "identity")
                    except requests.RequestException as exc:
                        last_exc = exc
                        continue
                except requests.RequestException as exc:
                    last_exc = exc
                    continue
                if response.status_code < 400:
                    break
            if response is None:
                raise last_exc if last_exc else RuntimeError("proxy request failed")
            if "charset" not in (response.headers.get("Content-Type") or "").lower():
                response.encoding = response.apparent_encoding or "utf-8"
            body = response.content
            ctype = response.headers.get("Content-Type") or "text/html; charset=utf-8"
            if not (method.upper() == "GET" and "text/html" in ctype.lower()):
                return response.status_code, body, ctype
            raw_text = body.decode(response.encoding or "utf-8", "replace")
            status = response.status_code
            if status == 200 and cacheable:
                self._cache_put(url, raw_text, ctype)
        # 注入/链接改写按每次请求执行：链接里的源取自当次请求的 Host
        if transform:
            body = self.transform(raw_text, url, origin_prefix).encode("utf-8")
        else:
            body = raw_text.encode("utf-8")
        return status, body, "text/html; charset=utf-8"

    # -------------------------------------------------------------- transform
    @staticmethod
    def transform(html: str, page_url: str, origin_prefix: str = "") -> str:
        base_tag = (
            # html_escape: URL 里的 & 等字符在属性值中必须转义
            f'<base href="{html_escape(page_url, quote=True)}">'
            '<meta name="referrer" content="no-referrer">'
            f"{_env_normalize_script(page_url)}"
        )
        if _is_roadbook_html(html):
            # 新版路书页：只保留中间图文内容（详见 _ROADBOOK_STRIP_CSS 注释）
            base_tag += _ROADBOOK_STRIP_HTML
        if "<head" in html.lower():
            match = re.search(r"<head\b[^>]*>", html, re.IGNORECASE)
            if match:  # 注入到 <head> 开标签之后
                html = html[: match.end()] + base_tag + html[match.end() :]
            else:
                html = base_tag + html
        else:
            html = base_tag + html

        def _rewrite(match: re.Match[str]) -> str:
            prefix, quote_char, href = match.group(1), match.group(2), match.group(3)
            rewritten = PageProxy._rewrite_href(href, page_url, origin_prefix)
            if rewritten is None:
                return match.group(0)
            return f'{prefix}{quote_char}{rewritten}{quote_char}'

        return _ANCHOR_HREF_RE.sub(_rewrite, html)

    @staticmethod
    def _rewrite_href(href: str, page_url: str, origin_prefix: str = "") -> str | None:
        """Rewrite an <a href> to /proxy/<url> ; None = leave untouched.

        路径式代理（而非 ?url= 查询参数）让站点 JS 解析 location.pathname 时
        仍能拿到真实文章路径/页码——翻页按钮靠它计算上一页/下一页。
        origin_prefix（http://电脑IP:端口）必须有：页面注入了指向游民星空的
        <base>，根相对链接会被解析到游民星空域名导致 404，绝对链接不受影响。
        """
        href = str(href or "").strip()
        if not href or href.startswith(("#", "javascript:", "data:", "mailto:", "tel:")):
            return None
        resolved = urljoin(page_url, href)
        parts = urlparse(resolved)
        if parts.scheme not in ("http", "https") or not _is_allowed_host(parts.hostname or ""):
            return None
        # 纯锚点(同页)或已经是代理链接则不动
        if resolved.split("#", 1)[0] == page_url.split("#", 1)[0]:
            return None
        # 保留 URL 结构只编码空格/非 ASCII 等，% 不重复编码
        encoded = quote(resolved, safe=":/?#[]@!$&'()*+,;=-._~%")
        return html_escape(f"{origin_prefix}/proxy/{encoded}", quote=True)

    # ------------------------------------------------------------------ cache
    def _cache_get(self, url: str) -> str | None:
        with self._lock:
            item = self._cache.get(url)
            if item is None:
                return None
            ts, text, _ = item
            if time.monotonic() - ts > PROXY_CACHE_TTL:
                self._cache.pop(url, None)
                return None
            self._cache.move_to_end(url)
            return text

    def _cache_put(self, url: str, text: str, content_type: str) -> None:
        with self._lock:
            self._cache[url] = (time.monotonic(), text, content_type)
            self._cache.move_to_end(url)
            while len(self._cache) > PROXY_CACHE_MAX:
                self._cache.popitem(last=False)


class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that does not print tracebacks for client disconnects."""

    # 多台手机同时打开页面时（每台一次开多条连接），默认 listen backlog=5 会溢出丢
    # SYN，客户端要等 ~500ms 的倍数重传才连上——并发压测中 512ms/1s 毛刺的来源。
    request_queue_size = 128

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Browsers abort SSE/asset connections at any time; that is not an error
        # worth printing to the console.
        pass


class _ViewerHandler(BaseHTTPRequestHandler):
    server_version = "WalkthroughViewer/2.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # keep the console quiet
        pass

    @property
    def app(self) -> "WalkthroughWebServer":
        return self.server.app

    # ------------------------------------------------------------------ HTTP
    def _send_bytes(self, data: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, obj: dict[str, Any], status: int = 200) -> None:
        self._send_bytes(
            json.dumps(obj, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
        )

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path == "/" or path == "/index.html":
                self._serve_index()
            elif path == "/api/view":
                self._serve_view(query)
            elif path == "/api/events":
                self._serve_events(query)
            elif path == "/api/download-status":
                self._serve_download_status()
            elif path == "/api/toc":
                self._serve_toc(query)
            elif path == "/proxy":
                self._serve_proxy((query.get("url") or [""])[0], None, self.headers)
            elif path.startswith("/proxy/"):
                raw_url = unquote(path[len("/proxy/"):])
                if parsed.query:
                    raw_url += "?" + parsed.query
                # 部分链路会把 "https://" 折叠成 "https:/"，还原一次
                raw_url = re.sub(r"^(https?:/)(?!/)", r"\1/", raw_url)
                self._serve_proxy(raw_url, None, self.headers)
            elif path.startswith("/gl/") or path.startswith("/handbook/"):
                # 站点 JS 用绝对路径跳页时落到这里：按路径前缀映射回对应站点
                host = "wap.gamersky.com" if path.startswith("/gl/") else "www.gamersky.com"
                self.send_response(302)
                self.send_header("Location", f"/proxy/https://{host}{path}" + (f"?{parsed.query}" if parsed.query else ""))
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._send_json({"error": "not found"}, 404)
        except (ConnectionError, OSError):
            pass

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body: dict[str, Any] = {}
        if raw:
            try:
                body = json.loads(raw.decode("utf-8"))
            except ValueError:
                body = {}
        try:
            if path == "/api/navigate":
                self._handle_navigate(body)
            elif path == "/api/game-detected":
                self._handle_game_detected(body)
            elif path == "/api/download-retry":
                self._handle_download_retry(body)
            elif path == "/proxy":
                # 页面内被改写的 gamersky 域 XHR（评论区/互动数据）经此透传
                self._serve_proxy((parse_qs(parsed.query).get("url") or [""])[0], raw, self.headers)
            elif path.startswith("/proxy/"):
                raw_url = unquote(path[len("/proxy/"):])
                if parsed.query:
                    raw_url += "?" + parsed.query
                raw_url = re.sub(r"^(https?:/)(?!/)", r"\1/", raw_url)
                self._serve_proxy(raw_url, raw, self.headers)
            else:
                self._send_json({"error": "not found"}, 404)
        except (ConnectionError, OSError):
            pass

    # ------------------------------------------------------------ handlers
    def _serve_index(self) -> None:
        try:
            client_ip = self.client_address[0]
        except Exception:
            client_ip = "?"
        _logger.info(f"打开查看器页面: {client_ip}")
        self._serve_static("index.html")

    def _serve_static(self, filename: str) -> None:
        try:
            data = (STATIC_DIR / filename).read_bytes()
        except OSError:
            self._send_bytes(b"viewer missing", "text/plain", 500)
            return
        self._send_bytes(data, "text/html; charset=utf-8")

    def _serve_toc(self, query: dict[str, list[str]]) -> None:
        """章节目录：抓取攻略页并解析“文章内容导航”，供浮窗目录列表使用。"""
        url = (query.get("url") or [""])[0].strip()
        # 推送地址可能带 #片段（路书页 #reader-node-<id>）：目录抓取与目录项
        # 地址都必须用去片段的页面地址，否则会拼出“双片段”目录项，页面既
        # 无法高亮当前章节，点选也无法定位。
        url = url.split("#", 1)[0]
        parts = urlparse(url)
        if parts.scheme not in ("http", "https") or not _is_allowed_host(parts.hostname or ""):
            self._send_json({"error": "host not allowed"}, 400)
            return
        now = time.monotonic()
        cached = self.app.toc_cache.get(url)
        if cached is not None and now - cached[0] < TOC_CACHE_TTL:
            self._send_json({"ok": True, "toc": cached[1]})
            return
        try:
            _status, body, _ctype = self.app.proxy.fetch(url, transform=False)
        except requests.RequestException as exc:
            self._send_json({"error": "fetch failed", "message": str(exc)}, 502)
            return
        entries = _extract_toc_entries(body.decode("utf-8", "replace"), url)
        self.app.toc_cache[url] = (now, entries)
        self._send_json({"ok": True, "toc": entries})

    def _serve_view(self, query: dict[str, list[str]]) -> None:
        state = self.app.state
        requested = (query.get("game") or [None])[0]
        active_game = state.get_active_game()
        known_games = state.list_games()
        game = requested or active_game or (known_games[-1] if known_games else None)
        if not game:
            self._send_json(
                {
                    "game": None,
                    "active_game": None,
                    "games": [],
                    "url": "",
                    "image_src": "",
                    "title": "",
                    "hint": "进入游戏后开始展示攻略",
                }
            )
            return
        target = state.get_target(game) or {}
        self._send_json(
            {
                "game": game,
                "active_game": active_game,
                "games": known_games,
                "url": str(target.get("url") or ""),
                "image_src": str(target.get("image_src") or ""),
                "image_index": int(target.get("image_index") if target.get("image_index") is not None else -1),
                "title": str(target.get("title") or ""),
            }
        )

    def _serve_download_status(self) -> None:
        """当前攻略下载进度（页面首次加载时拉一次，之后靠 SSE 推送更新）。"""
        self._send_json(download_progress.active_status())

    def _serve_proxy(self, raw_url: str, body: bytes | None = None,
                     headers: Any = None) -> None:
        raw_url = (raw_url or "").strip()
        parts = urlparse(raw_url)
        if parts.scheme not in ("http", "https") or not _is_allowed_host(parts.hostname or ""):
            self._send_json({"error": "host not allowed"}, 400)
            return
        upstream_ct = None
        if body and headers is not None:
            upstream_ct = headers.get("Content-Type")
        # 请求体/查询串里的本代理页面地址还原成真实地址（覆盖任何编码变体）
        if body:
            body = _unwrap_self_proxy(body.decode("utf-8", "replace")).encode("utf-8")
        head, sep, q = raw_url.partition("?")
        raw_url = head + sep + _unwrap_self_proxy(q)
        # 用访问者请求里的 Host 生成绝对代理链接（注入的 <base> 指向游民星空，
        # 根相对链接会被解析到游民星空域名导致 404）
        host = headers.get("Host") if headers is not None else None
        origin_prefix = f"http://{host}" if host else ""
        try:
            status, payload, ctype = self.app.proxy.fetch(
                raw_url,
                method="POST" if body else "GET",
                data=body,
                content_type=upstream_ct,
                origin_prefix=origin_prefix,
            )
        except requests.RequestException as exc:
            print(
                f"[webserver] proxy fetch failed: {exc} | url={raw_url[:200]} "
                f"| body={'' if not body else body[:300]!r}",
                file=sys.stderr,
            )
            self._send_json({"error": "fetch failed", "message": str(exc)}, 502)
            return
        self._send_bytes(payload, ctype, status)

    def _serve_events(self, query: dict[str, list[str]]) -> None:
        """Server-Sent Events stream. Client disconnects are handled silently."""
        game = (query.get("game") or [None])[0]
        broker = self.app.broker
        stop_event = self.app.stop_event
        q = broker.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
        except Exception:
            broker.unsubscribe(q)
            return
        try:
            self.wfile.write(b"retry: 5000\n\n")
            self.wfile.flush()
        except Exception:
            broker.unsubscribe(q)
            return
        try:
            while not stop_event.is_set():
                try:
                    event = q.get(timeout=15)
                except queue.Empty:
                    if not self._safe_sse_write(b": ping\n\n"):
                        break
                    continue
                if game and event.get("game") not in (None, game):
                    continue
                payload = json.dumps(event, ensure_ascii=False)
                if not self._safe_sse_write(f"data: {payload}\n\n".encode("utf-8")):
                    break
        finally:
            broker.unsubscribe(q)

    def _safe_sse_write(self, data: bytes) -> bool:
        """Write to an SSE stream, returning False when the client is gone."""
        try:
            self.wfile.write(data)
            self.wfile.flush()
            return True
        except Exception:
            return False

    def _handle_navigate(self, body: dict[str, Any]) -> None:
        game = str(body.get("game") or "").strip()
        url = str(body.get("url") or "").strip()
        if not game or not url:
            self._send_json({"ok": False, "error": "game and url are required"}, 400)
            return
        active = self.app.state.get_active_game()
        if active and game != active:
            # 上一款游戏的迟到推送（切换瞬间在途的识别查询返回、残留的自动
            # 导航等）：不发布、不改写当前目标，否则攻略页面会被拽回旧游戏
            _logger.info(f"忽略非当前游戏的推送: game={game} (当前 {active}) url={url}")
            self._send_json({"ok": True, "ignored": True, "reason": "inactive game", "game": game})
            return
        image_src = str(body.get("image_src") or "").strip()
        title = str(body.get("title") or "").strip()
        try:
            image_index = int(body.get("image_index"))
        except (TypeError, ValueError):
            image_index = -1
        message = (
            f"navigate: game={game} url={url} image_src={image_src or '(空)'} "
            f"image_index={image_index} title={title or '(空)'}"
        )
        print(f"[webserver] {message}", file=sys.stderr)
        _logger.info(f"收到推送 {message}")  # 文件日志
        self.app.state.set_active_game(game)
        self.app.state.set_target(
            game, url, image_src=image_src, title=title, image_index=image_index
        )
        event = {
            "type": "navigate",
            "game": game,
            "url": url,
            "image_src": image_src,
            "image_index": image_index,
            "title": title,
        }
        self.app.broker.publish(event)
        self._send_json({"ok": True, **event})

    def _handle_download_retry(self, body: dict[str, Any]) -> None:
        """页面“重试”按钮：交回客户端从失败处重新发起下载/导入。"""
        game = str(body.get("game") or "").strip()
        if not game:
            # 未带游戏名时回退：最近一次下载的游戏 -> 当前激活游戏
            game = str(download_progress.active_status().get("game") or "").strip()
        if not game:
            game = str(self.app.state.get_active_game() or "").strip()
        hook = self.app.on_download_retry
        if hook is None:
            _logger.warning(f"收到下载重试请求，但客户端未在运行，无法处理: game={game or '(未知)'}")
            self._send_json({"ok": False, "error": "客户端不在运行，无法自动重试"}, 503)
            return
        _logger.info(f"收到下载重试请求: game={game or '(未知)'}")
        try:
            accepted, message = hook(game)
        except Exception as exc:
            _logger.error(f"重试请求处理失败: game={game or '(未知)'} 错误={exc}")
            self._send_json({"ok": False, "error": str(exc)}, 500)
            return
        if accepted:
            self._send_json({"ok": True, "message": message, "game": game})
        else:
            self._send_json({"ok": False, "error": message, "game": game}, 409)

    def _handle_game_detected(self, body: dict[str, Any]) -> None:
        game = str(body.get("game") or "").strip()
        if not game:
            self._send_json({"ok": False, "error": "game required"}, 400)
            return
        _logger.info(f"收到游戏识别推送: game={game} first_url={str(body.get('first_url') or '').strip() or '(空)'}")
        self.app.state.set_active_game(game)
        # 有历史浏览记录（重启 app / 游戏再次进入）时恢复上次位置，而不是被
        # 识别推送的“攻略首页”拉回第一章；全新游戏才落到 first_url
        stored = self.app.state.get_target(game)
        if stored and str(stored.get("url") or "").strip():
            event = {
                "type": "navigate",
                "game": game,
                "url": str(stored.get("url") or ""),
                "image_src": str(stored.get("image_src") or ""),
                "image_index": int(stored.get("image_index") if stored.get("image_index") is not None else -1),
                "title": str(stored.get("title") or ""),
            }
            self.app.broker.publish(event)
            _logger.info(f"游戏识别恢复上次浏览位置: game={game} url={event['url']}")
            self._send_json({"ok": True, "active_game": game, "restored": True, "url": event["url"]})
            return
        first_url = str(body.get("first_url") or "").strip()
        if first_url:
            # 首次游玩的游戏没有历史位置：落到攻略首页并按首页第一张图定位
            image_src = str(body.get("first_image_src") or "").strip()
            title = str(body.get("first_title") or "").strip()
            try:
                image_index = int(body.get("first_image_index"))
            except (TypeError, ValueError):
                image_index = -1
            self.app.state.set_target(
                game, first_url, image_src=image_src, title=title, image_index=image_index
            )
            self.app.broker.publish(
                {
                    "type": "navigate",
                    "game": game,
                    "url": first_url,
                    "image_src": image_src,
                    "image_index": image_index,
                    "title": title,
                }
            )
        self._send_json({"ok": True, "active_game": game})


class WalkthroughWebServer:
    def __init__(self, config: AppConfig, state: StateStore | None = None) -> None:
        config.ensure_dirs()
        self.config = config
        self.state = state or StateStore(config.state_file)
        self.broker = SSEBroker()
        self.proxy = PageProxy()
        self.toc_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}
        self.stop_event = threading.Event()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        # 攻略下载进度轮询（download_status.json 变化 -> SSE 广播给所有页面）
        self._download_status_thread: threading.Thread | None = None
        self._last_download_sig: tuple | None = None
        # 页面“重试”按钮的回调：(game) -> (是否受理, 说明)。
        # 内嵌模式下由客户端注入（真正的下载/导入也发生在客户端）；
        # 独立 webserver 未注入时重试请求返回 503。
        self.on_download_retry: Callable[[str], tuple[bool, str]] | None = None

    def start(self) -> None:
        if self._httpd is not None:
            return
        httpd = _QuietThreadingHTTPServer((self.config.webserver_host, self.config.webserver_port), _ViewerHandler)
        httpd.app = self  # type: ignore[attr-defined]
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, name="walkthrough-web", daemon=True)
        self._thread.start()
        if self._download_status_thread is None:
            self._download_status_thread = threading.Thread(
                target=self._download_status_loop, name="download-status", daemon=True
            )
            self._download_status_thread.start()
        host = self.config.webserver_host
        display_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
        print(f"[webserver] listening on http://{display_host}:{self.config.webserver_port}")
        if host in ("0.0.0.0", ""):
            # 双网卡/多网段：列出每个本机地址的访问入口（都在监听，
            # 某个网段设备连不上时优先排查 Windows 防火墙）
            try:
                adapters = lan_adapters()
            except Exception:
                adapters = []
            if not adapters:
                try:
                    print(f"[webserver] 局域网访问(手机/平板): {lan_url(self.config.webserver_port)}")
                except OSError:
                    pass
            for ip, label in adapters:
                site = label or "网卡"
                print(f"[webserver] 局域网访问({site}): http://{ip}:{self.config.webserver_port}")

    def stop(self) -> None:
        self.stop_event.set()
        if self._download_status_thread is not None:
            self._download_status_thread.join(timeout=2.0)
            self._download_status_thread = None
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception:
                pass
            try:
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None
        self.proxy.close()

    @property
    def is_running(self) -> bool:
        return self._httpd is not None

    def publish(self, event: dict[str, Any]) -> None:
        self.broker.publish(event)

    def _download_status_loop(self) -> None:
        """每秒看一眼共享的下载状态文件，变化就经 SSE 广播给所有打开的页面。

        下载进度由独立进程（skill 的 download 链路）或客户端自己的 bootstrap
        线程写进 download_status.json，webserver 只读不写。签名不含 updated_at：
        心跳只刷新时间戳时不必打扰页面。
        """
        while not self.stop_event.is_set():
            payload = download_progress.active_status()
            sig = (payload.get("status"), payload.get("game"), payload.get("stage"),
                   payload.get("progress"), payload.get("detail"))
            if sig != self._last_download_sig:
                self._last_download_sig = sig
                _logger.info(
                    f"广播下载状态: status={payload.get('status')} game={payload.get('game')} "
                    f"stage={payload.get('stage')} progress={payload.get('progress')}"
                    + (f" detail={payload.get('detail')}" if payload.get("detail") else "")
                )
                self.broker.publish({"type": "download_progress", **payload})
            self.stop_event.wait(1.0)
