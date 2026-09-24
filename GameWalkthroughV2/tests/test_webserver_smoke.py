"""Offline smoke/integration test for the push-only webserver.

The webserver no longer parses guide pages; it stores/pushes navigation targets
{game, url, image_src, title} and serves a same-origin pass-through proxy so
the viewer frame can be scrolled to the pushed content position.

The proxy's HTTP session is stubbed with canned HTML, so the whole flow runs
without any network access.

Run:  python tests/test_webserver_smoke.py
"""

from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):  # Windows 控制台默认 cp1252
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.config import AppConfig
from app.download_progress import STATUS_FILE as _REAL_DL_STATUS_FILE
from app import download_progress
from app.state import StateStore
from app.webserver import server as server_module

PAGE_URL = "https://www.gamersky.com/handbook/202303/1575258.shtml"
NEXT_PAGE_URL = "https://www.gamersky.com/handbook/202303/1575258_2.shtml"
THIRD_PAGE_URL = "https://www.gamersky.com/handbook/202303/1575258_3.shtml"
IMAGE_URL = "https://img1.gamersky.com/image/gs/image_0001.jpg"
ROADBOOK_URL = (
    "https://www.gamersky.com/tools/guide-map/roadbooks/47"
    "?appNavigationBarStyle=kNoneBarr&gsAppOpenWithNewWindow=true"
)

SAMPLE_HTML = """<!DOCTYPE html>
<html>
<head>
<title>《测试游戏》图文攻略_游民星空</title>
<link rel="stylesheet" href="/css/style.css">
</head>
<body>
<div class="TopHead">站点头部/导航</div>
<div class="Mid2L_con">
  <p>第一段攻略文字</p>
  <p class="GsImageLabel"><a href="showimage/id_gamersky.shtml?https%3A%2F%2Fimg1.gamersky.com%2Fimage%2Fgs%2Fimage_0001.jpg"><img data-src="https://img1.gamersky.com/image/gs/image_0001.jpg"></a></p>
</div>
<div class="pagecss"><ul>
  <li><a href="1575258_2.shtml">下一页</a></li>
  <li><a href="#comment">评论</a></li>
  <li><a href="javascript:swap(1)">切换</a></li>
  <li><a href="https://www.baidu.com/s?wd=x">外部链接</a></li>
</ul></div>
<div class="post_ding"><div class="bd"><span class="pagecss"><ul>
<li><b>第1页：起始</b></li>
<li><a href="https://www.gamersky.com/handbook/202303/1575258_2.shtml">第2页：寺院</a></li>
<li><a href="https://www.gamersky.com/handbook/202303/1575258_3.shtml">第3页：河边</a></li>
</ul></span></div></div>
</body>
</html>
"""

# 新版“攻略路书”页（Nuxt SSR 单页）：节点卡内联全部内容，无 post_ding/翻页器
ROADBOOK_HTML = """<!DOCTYPE html>
<html class="is-guide-roadbook-document">
<head><title>《测试游戏》全探索图文流程攻略</title></head>
<body>
<main class="guide-content-main">
<section class="guide-content-header"><h1 class="guide-content-header__title">《测试游戏》全探索图文流程攻略</h1></section>
<section class="reader-node-stream reader-node-stream--guide">
<article id="reader-node-268625960" class="reader-node-card is-active is-expanded">
  <div class="reader-node-card__header"><span class="reader-node-card__node-id">A1</span><h3 class="reader-node-card__title">序章及概要</h3></div>
  <div class="reader-node-card__content"><div class="reader-rich-content">
    <p>路书第一节点正文</p>
    <p class="GsImageLabel" align="center"><a target="_blank" href="https://www.gamersky.com/showimage/id_gamersky.shtml?https://img1.gamersky.com/image2026/09/20260902_fxy_625_1/11.jpg" class="n1"><img class="picact reader-content-image" src="https://img1.gamersky.com/image2026/09/20260902_fxy_625_1/11_S.jpg" width="600"></a></p>
  </div></div>
</article>
<article id="reader-node-317911517" class="reader-node-card is-active is-expanded">
  <div class="reader-node-card__header"><span class="reader-node-card__node-id">A2</span><h3 class="reader-node-card__title">参道</h3></div>
  <div class="reader-node-card__content"><div class="reader-rich-content">
    <p>路书第二节点正文</p>
    <p class="GsImageLabel" align="center"><a target="_blank" href="https://www.gamersky.com/showimage/id_gamersky.shtml?https://img1.gamersky.com/image2026/09/20260902_fxy_625_1/18.jpg" class="n1"><img class="picact reader-content-image" src="https://img1.gamersky.com/image2026/09/20260902_fxy_625_1/18_S.jpg"></a></p>
  </div></div>
</article>
</section>
</main>
</body>
</html>
"""


class FakeResponse:
    def __init__(self, body: bytes | str, content_type: str = "text/html"):
        self.content = body.encode("utf-8") if isinstance(body, str) else body
        self.headers = {"Content-Type": content_type}
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"
        self.status_code = 200

    def raise_for_status(self) -> None:
        pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_json(url: str, method: str = "GET", body: dict | None = None,
              timeout: float = 10) -> tuple[int, dict]:
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def make_server(tmp: Path) -> server_module.WalkthroughWebServer:
    cfg = AppConfig()
    cfg.walkthrough_dir = tmp / "walkthrough"
    cfg.data_dir = tmp / "data"
    cfg.state_file = tmp / "data" / "state.json"
    cfg.webserver_host = "127.0.0.1"
    cfg.webserver_port = free_port()
    cfg.ensure_dirs()
    # 下载进度状态文件指向测试目录（轮询线程按调用时的模块常量取路径）
    download_progress.STATUS_FILE = tmp / "data" / "download_status.json"
    return server_module.WalkthroughWebServer(cfg)


def test_server() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="gwa-test-"))
    server = make_server(tmp)
    state_file = tmp / "data" / "state.json"
    base = f"http://127.0.0.1:{server.config.webserver_port}"

    # 用假响应替换代理的 HTTP session（不做真实网络请求）
    upstream_calls: list[dict] = []

    def fake_request(method: str = "GET", url: str = "", data: bytes | None = None,
                     headers: dict | None = None, timeout: float | None = None) -> FakeResponse:
        upstream_calls.append({"method": method, "url": url, "data": data, "headers": headers or {}})
        if url == "http://router.gamersky.com/@/gsComment/comment/list/6.0.0/0/PC":
            raise requests.ConnectionError("connection reset by peer")  # router. 80 端口已下线的场景
        if url.endswith("/gsComment/comment/list/6.0.0/0/PC"):
            return FakeResponse('{"list": [], "total": 0}', "application/json")
        # 上游无视 Accept-Encoding 强制返回 brotli 压缩（本机无解码器）的场景
        if url.startswith("https://router6.gamersky.com/@display/pc/comment"):
            if (headers or {}).get("Accept-Encoding") != "identity":
                raise requests.exceptions.ContentDecodingError("brotli: decoder failed")
            return FakeResponse('{"hot": 1}', "application/json")
        if "router" in url:
            return FakeResponse('{"commentCount": 3}', "application/json")
        if url == ROADBOOK_URL:
            return FakeResponse(ROADBOOK_HTML)
        return FakeResponse(SAMPLE_HTML)

    server.proxy._session.request = fake_request  # type: ignore[assignment]
    try:
        server.start()
        time.sleep(0.2)

        # 1) fresh server: no game yet -> hint
        status, view0 = http_json(f"{base}/api/view")
        assert status == 200 and view0.get("hint") == "进入游戏后开始展示攻略", view0
        print("PASS 1: 未检测到游戏时显示提示")

        # 2) SSE subscription (game-detected + navigate both arrive)
        sse_events: list[dict] = []

        def _read_sse() -> None:
            req = urllib.request.Request(f"{base}/api/events")
            with urllib.request.urlopen(req, timeout=20) as resp:
                buf = b""
                while len(sse_events) < 2:
                    chunk = resp.read(1)
                    if not chunk:
                        break
                    buf += chunk
                    if buf.endswith(b"\n\n"):
                        frame = buf.decode("utf-8")
                        buf = b""
                        for line in frame.splitlines():
                            if line.startswith("data:"):
                                sse_events.append(json.loads(line[5:].strip()))

        t = threading.Thread(target=_read_sse, daemon=True)
        t.start()
        time.sleep(0.3)

        # 2) game detected with first_url -> active game + initial push
        status, det = http_json(
            f"{base}/api/game-detected", "POST",
            {"game": "测试游戏", "first_url": PAGE_URL},
        )
        assert status == 200 and det.get("ok") is True and det.get("active_game") == "测试游戏", det
        assert server.state.get_active_game() == "测试游戏"
        print("PASS 2: game-detected 设置 active_game 并推送攻略首页")

        # 3) navigate push: stored + published verbatim (no parsing)
        status, nav = http_json(
            f"{base}/api/navigate", "POST",
            {"game": "测试游戏", "url": NEXT_PAGE_URL, "image_src": IMAGE_URL,
             "image_index": 0, "title": "第2页：第1章-初遇电锯男"},
        )
        assert status == 200 and nav.get("ok") is True, nav
        assert nav.get("url") == NEXT_PAGE_URL and nav.get("image_src") == IMAGE_URL, nav
        assert nav.get("title") == "第2页：第1章-初遇电锯男", nav
        assert nav.get("image_index") == 0, nav
        target = server.state.get_target("测试游戏")
        assert target and target["url"] == NEXT_PAGE_URL and target["image_src"] == IMAGE_URL, target
        assert target.get("image_index") == 0, target
        print("PASS 3: navigate 仅存储并转发 url/图片定位，不再解析章节页码")

        # 3b) missing fields -> 400
        status, _ = http_json(f"{base}/api/navigate", "POST", {"game": "测试游戏"})
        assert status == 400
        print("PASS 3b: navigate 缺少 game/url 返回 400")

        # 4) view returns the current target for a freshly opened browser
        status, view = http_json(f"{base}/api/view")
        assert view["game"] == "测试游戏", view
        assert view["url"] == NEXT_PAGE_URL and view["image_src"] == IMAGE_URL, view
        assert view["title"] == "第2页：第1章-初遇电锯男", view
        assert view.get("image_index") == 0, view
        print("PASS 4: 新打开的浏览器通过 /api/view 拿到当前目标")

        # 4b) 重启/再识别已浏览过的游戏：恢复上次浏览位置（含图片定位），
        #     而不是被识别推送的“攻略首页”拉回第一章
        sse_events2: list[dict] = []

        def _read_sse2() -> None:
            req = urllib.request.Request(f"{base}/api/events")
            with urllib.request.urlopen(req, timeout=20) as resp:
                buf = b""
                while len(sse_events2) < 1:
                    chunk = resp.read(1)
                    if not chunk:
                        break
                    buf += chunk
                    if buf.endswith(b"\n\n"):
                        frame = buf.decode("utf-8")
                        buf = b""
                        for line in frame.splitlines():
                            if line.startswith("data:"):
                                sse_events2.append(json.loads(line[5:].strip()))

        t2 = threading.Thread(target=_read_sse2, daemon=True)
        t2.start()
        time.sleep(0.3)
        status, det2 = http_json(
            f"{base}/api/game-detected", "POST",
            {"game": "测试游戏", "first_url": PAGE_URL},
        )
        assert status == 200 and det2.get("ok") is True and det2.get("restored") is True, det2
        t2.join(timeout=15)
        assert len(sse_events2) == 1, sse_events2
        restored = sse_events2[0]
        assert restored["url"] == NEXT_PAGE_URL and restored["image_src"] == IMAGE_URL, restored
        assert restored.get("image_index") == 0 and restored.get("title") == "第2页：第1章-初遇电锯男", restored
        print("PASS 4b: 重启/再识别恢复上次浏览位置（不被首页拉回第一章）")

        # 4c) 切换游戏：识别游戏 B（无历史）落到 B 的首页（含首页图片定位）；
        #     此后上一款游戏 A 的迟到推送必须被忽略（否则页面被拽回 A）
        sse_events3: list[dict] = []

        def _read_sse3() -> None:
            req = urllib.request.Request(f"{base}/api/events")
            with urllib.request.urlopen(req, timeout=20) as resp:
                buf = b""
                while len(sse_events3) < 2:
                    chunk = resp.read(1)
                    if not chunk:
                        break
                    buf += chunk
                    if buf.endswith(b"\n\n"):
                        frame = buf.decode("utf-8")
                        buf = b""
                        for line in frame.splitlines():
                            if line.startswith("data:"):
                                sse_events3.append(json.loads(line[5:].strip()))

        t3 = threading.Thread(target=_read_sse3, daemon=True)
        t3.start()
        time.sleep(0.3)
        status, det3 = http_json(
            f"{base}/api/game-detected", "POST",
            {"game": "游戏B", "first_url": THIRD_PAGE_URL,
             "first_image_src": IMAGE_URL, "first_image_index": 2, "first_title": "B-第3页"},
        )
        assert status == 200 and det3.get("ok") is True, det3
        time.sleep(0.3)
        # 上一款游戏 A 的迟到推送（模拟切换瞬间在途的识别查询返回）
        status, stale = http_json(
            f"{base}/api/navigate", "POST",
            {"game": "测试游戏", "url": NEXT_PAGE_URL, "image_src": IMAGE_URL,
             "image_index": 1, "title": "A-第2页"},
        )
        assert status == 200 and stale.get("ok") is True and stale.get("ignored") is True, stale
        t3.join(timeout=15)
        assert len(sse_events3) == 1, sse_events3
        b_event = sse_events3[0]
        assert b_event["game"] == "游戏B" and b_event["url"] == THIRD_PAGE_URL, b_event
        assert b_event["image_index"] == 2 and b_event["image_src"] == IMAGE_URL, b_event
        # 当前目标仍是游戏 B（未被 A 的迟到推送改写）
        status, view_b = http_json(f"{base}/api/view")
        assert view_b["game"] == "游戏B" and view_b["url"] == THIRD_PAGE_URL, view_b
        print("PASS 4c: 切换游戏落到新游戏首页；旧游戏的迟到推送被忽略")

        # 5) SSE got both events (game-detected first_url + navigate)
        t.join(timeout=15)
        assert len(sse_events) >= 2, sse_events
        first, second = sse_events[0], sse_events[1]
        assert first["type"] == "navigate" and first["url"] == PAGE_URL, first
        assert second["url"] == NEXT_PAGE_URL and second["image_src"] == IMAGE_URL, second
        assert "chapter" not in second and "page" not in second, second
        print("PASS 5: SSE 推送 {type:navigate, url, image_src} 且不再含章节/页码")

        # 6) proxy: fetch + transform (base/no-referrer injection, <a> rewrite)
        proxy_src = f"{base}/proxy?url=" + urllib.parse.quote(PAGE_URL, safe="")
        with urllib.request.urlopen(proxy_src, timeout=10) as resp:
            html = resp.read().decode("utf-8")
        html_fetches = [c for c in upstream_calls if c["url"] == PAGE_URL]
        assert len(html_fetches) == 1 and html_fetches[0]["method"] == "GET", upstream_calls
        assert f'<base href="{PAGE_URL}">' in html, html[:200]
        assert 'name="referrer" content="no-referrer"' in html
        # 环境归一脚本在 <head> 内、站点脚本之前（阻止手机上 frame 被跳去 wap 版）
        assert "freeze(navigator" in html
        assert html.index("freeze(navigator") < html.lower().index("</head>")
        # XHR/fetch 改写补丁也在站点脚本之前（评论区接口同源化）
        assert "toProxyUrl" in html and "XMLHttpRequest.prototype.open" in html
        assert html.index("toProxyUrl") < html.lower().index("</head>")
        # 动态插入链接的点击兜底
        assert 'document.addEventListener("click"' in html
        # PC 版注入“评论区默认排序=最新”（推荐接口在代理环境加载不出）
        assert 'DEFAULT_COMMENT_SORT = "最新"' in html
        assert 'WAP_EXPAND_FULL = ""' in html  # PC 版没有“展开全文”元素
        # 请求里的本代理页面地址（contentUrl 等）还原成真实页面地址：
        # 游民星空服务端无法访问我们的局域网地址，“推荐”接口会失败
        assert "unwrapSelfProxy" in html and "XMLHttpRequest.prototype.send" in html
        # 相对链接被改写为「绝对」路径式代理链接：注入的 <base> 指向游民星空，
        # 根相对链接会被解析到游民星空域名导致 404，必须带访问者自己的源
        rewritten = f"{base}/proxy/{NEXT_PAGE_URL}"
        assert rewritten in html, html
        # 锚点/javascript/外站链接保持原样
        assert 'href="#comment"' in html and 'href="javascript:swap(1)"' in html
        assert 'href="https://www.baidu.com/s?wd=x"' in html
        # 非 <a> 的相对资源交给 <base>，不改写
        assert 'href="/css/style.css"' in html
        print("PASS 6: /proxy 透传 HTML（注入 base/no-referrer，仅改写站内 <a> 链接）")

        # 6b) proxy caches a single fetch for repeated requests
        with urllib.request.urlopen(proxy_src, timeout=10) as resp:
            resp.read()
        assert len([c for c in upstream_calls if c["url"] == PAGE_URL]) == 1, upstream_calls
        print("PASS 6b: /proxy 同页重复请求命中缓存")

        # 6c) POST passthrough: 页面内 gamersky 域 XHR（评论区等）经 /proxy 转发
        api_url = "https://router.gamersky.com/@/gsComment/news/batchGetNewsInteractionCount/pc"
        status, payload = http_json(
            f"{base}/proxy?url=" + urllib.parse.quote(api_url, safe=""),
            "POST", {"newsList": [1961684]},
        )
        assert status == 200 and payload == {"commentCount": 3}, payload
        api_calls = [c for c in upstream_calls if c["url"] == api_url]
        assert api_calls and api_calls[0]["method"] == "POST", api_calls
        assert b"newsList" in (api_calls[0]["data"] or b""), api_calls[0]
        assert "application/json" in api_calls[0]["headers"].get("Content-Type", ""), api_calls[0]
        # 补齐来源头（部分接口按 Origin/Referer 校验）
        assert api_calls[0]["headers"].get("Origin") == "https://www.gamersky.com", api_calls[0]
        assert api_calls[0]["headers"].get("Referer") == "https://www.gamersky.com/", api_calls[0]
        print("PASS 6c: /proxy 透传 POST（评论区/互动接口恢复同源语义 + 来源头）")

        # 6d) 路径式路由：/proxy/<完整URL>（含目标自身的查询参数）与折叠斜杠还原
        page_with_q = PAGE_URL + "?from=test"
        with urllib.request.urlopen(
            f"{base}/proxy/https://www.gamersky.com/handbook/202303/1575258.shtml?from=test",
            timeout=10,
        ) as resp:
            html2 = resp.read().decode("utf-8")
        assert f'<base href="{page_with_q}">' in html2, html2[:200]
        assert any(c["url"] == page_with_q for c in upstream_calls), upstream_calls
        with urllib.request.urlopen(
            f"{base}/proxy/https:/www.gamersky.com/handbook/202303/1575258.shtml", timeout=10
        ) as resp:
            resp.read()  # 折叠斜杠也能还原
        assert any(c["url"] == PAGE_URL for c in upstream_calls), upstream_calls
        # 缓存命中后换一个 Host 访问（手机用局域网 IP、电脑用 127.0.0.1）：
        # 链接必须按当次请求的 Host 重新生成，而不是沿用缓存里的旧源
        req_alt_host = urllib.request.Request(
            f"{base}/proxy/https://www.gamersky.com/handbook/202303/1575258.shtml",
            headers={"Host": "192.168.9.9:8180"},
        )
        with urllib.request.urlopen(req_alt_host, timeout=10) as resp:
            html_alt = resp.read().decode("utf-8")
        assert f"http://192.168.9.9:8180/proxy/{NEXT_PAGE_URL}" in html_alt, html_alt[-300:]
        assert f"{base}/proxy/{NEXT_PAGE_URL}" not in html_alt
        print("PASS 6d: 路径式代理路由（保留查询参数、容忍斜杠折叠、按 Host 生成链接）")

        # 6d-2) 新版路书页：代理注入裁剪样式——只保留中间图文内容
        # （隐藏顶部导航/左侧目录/右侧地图/页脚），手机上以自适应排版展示
        with urllib.request.urlopen(f"{base}/proxy/{ROADBOOK_URL}", timeout=10) as resp:
            roadbook_html = resp.read().decode("utf-8")
        assert 'id="gs-frame-strip"' in roadbook_html, roadbook_html[:300]
        assert "aside.roadbook-nav" in roadbook_html and ".reader-progress-wrapper" in roadbook_html, \
            "裁剪样式应覆盖左侧目录与右侧地图"
        # 旧版页面不注入
        with urllib.request.urlopen(f"{base}/proxy/{PAGE_URL}", timeout=10) as resp:
            plain_html = resp.read().decode("utf-8")
        assert 'id="gs-frame-strip"' not in plain_html, "旧版页面不应注入路书裁剪样式"
        print("PASS 6d-2: 路书页注入裁剪样式（仅留中间图文内容），旧版页面不受影响")

        # 6e) 站点 JS 用绝对路径跳页时的兜底：/gl/*、/handbook/* 重定向回代理
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        opener = urllib.request.build_opener(NoRedirect)
        try:
            opener.open(f"{base}/gl/Content-1961684_7.html", timeout=10)
            code, location = None, None
        except urllib.error.HTTPError as exc:
            code, location = exc.code, exc.headers.get("Location")
        assert code == 302, f"应返回 302，实际: {code}"
        assert location == "/proxy/https://wap.gamersky.com/gl/Content-1961684_7.html", location
        print("PASS 6e: 绝对路径跳页兜底重定向（/gl/ -> wap、/handbook/ -> www）")

        # 6f) http 端点连接失败时自动退回 https（router. 老域名 80 端口已下线的场景）
        list_url = "http://router.gamersky.com/@/gsComment/comment/list/6.0.0/0/PC"
        status, payload = http_json(
            f"{base}/proxy?url=" + urllib.parse.quote(list_url, safe=""),
            "POST", {"contentUrl": PAGE_URL, "sortType": 1},
        )
        assert status == 200 and payload == {"list": [], "total": 0}, payload
        attempted = [c["url"] for c in upstream_calls
                     if c["url"].endswith("/gsComment/comment/list/6.0.0/0/PC")]
        assert attempted and attempted[0].startswith("http://") and attempted[-1].startswith("https://"), attempted
        print("PASS 6f: http 端点失败自动退回 https（router. 老域名场景）")

        # 6g) 上游强制 brotli 压缩（本机未装解码器）时，退回 identity 不压缩重试
        br_url = "https://router6.gamersky.com/@display/pc/comment?contentUrl=" + urllib.parse.quote(PAGE_URL, safe="")
        status, payload = http_json(f"{base}/proxy?url=" + urllib.parse.quote(br_url, safe=""))
        assert status == 200 and payload == {"hot": 1}, payload
        br_calls = [c for c in upstream_calls if c["url"] == br_url]
        assert len(br_calls) >= 2, br_calls
        assert br_calls[0]["headers"].get("Accept-Encoding") == "gzip", br_calls[0]
        assert br_calls[-1]["headers"].get("Accept-Encoding") == "identity", br_calls[-1]
        print("PASS 6g: 上游强制 brotli 压缩时退回 identity 重试")

        # 7) proxy host allow-list blocks non-gamersky hosts (SSRF guard)
        status, _ = http_json(f"{base}/proxy?url=" + urllib.parse.quote("http://evil.example.com/x", safe=""))
        assert status == 400, status
        status, _ = http_json(f"{base}/proxy?url=" + urllib.parse.quote("file:///etc/passwd", safe=""))
        assert status == 400, status
        print("PASS 7: /proxy 域名白名单拦截非游民星空地址")

        # 8) removed endpoints stay removed
        status, _ = http_json(f"{base}/api/chapter?game=x&chapter=1")
        assert status == 404
        status, _ = http_json(f"{base}/images/whatever.jpg")
        assert status == 404
        print("PASS 8: 旧的 /api/chapter 与 /images 接口已移除")

        # 8b) 浮窗以普通窗口直接加载查看器页（无独立壳页面、无图钉接口）
        with urllib.request.urlopen(f"{base}/?overlay=1", timeout=10) as resp:
            assert resp.status == 200
            resp.read()
        print("PASS 8b: 浮窗直接加载查看器页（/?overlay=1）")

        # 8c) 章节目录：抓取攻略页解析“文章内容导航”（页面内容命中代理缓存）
        page_fetches_before = len([c for c in upstream_calls if c["url"] == PAGE_URL])
        _, data = http_json(f"{base}/api/toc?url=" + urllib.parse.quote(PAGE_URL, safe=""))
        assert data.get("ok") is True and len(data["toc"]) == 3, data
        assert data["toc"][0] == {"name": "第1页：起始", "url": PAGE_URL}, data["toc"][0]
        assert data["toc"][1]["url"] == NEXT_PAGE_URL, data["toc"][1]
        assert data["toc"][2]["url"] == THIRD_PAGE_URL, data["toc"][2]
        http_json(f"{base}/api/toc?url=" + urllib.parse.quote(PAGE_URL, safe=""))
        page_fetches_after = len([c for c in upstream_calls if c["url"] == PAGE_URL])
        assert page_fetches_after == page_fetches_before, "目录重复请求应命中页面缓存"
        print("PASS 8c: /api/toc 解析文章内容导航并缓存")

        # 8c-2) 新版路书页：目录来自 reader-node-card 节点卡，链接带 #reader-node-<id> 片段
        _, data = http_json(f"{base}/api/toc?url=" + urllib.parse.quote(ROADBOOK_URL, safe=""))
        assert data.get("ok") is True and len(data["toc"]) == 2, data
        assert data["toc"][0] == {
            "name": "A1 序章及概要",
            "url": ROADBOOK_URL + "#reader-node-268625960",
        }, data["toc"][0]
        assert data["toc"][1] == {
            "name": "A2 参道",
            "url": ROADBOOK_URL + "#reader-node-317911517",
        }, data["toc"][1]
        # 请求地址带 #片段时（推送地址的常态）同样返回干净目录——
        # 否则会拼出“双片段”目录项：当前章节匹配不上、点选也无法定位
        _, data = http_json(f"{base}/api/toc?url=" + urllib.parse.quote(ROADBOOK_URL + "#reader-node-268625960", safe=""))
        assert data.get("ok") is True and len(data["toc"]) == 2, data
        for entry in data["toc"]:
            assert entry["url"].count("#") == 1, f"目录项不应携带双片段: {entry}"
        assert data["toc"][0]["url"] == ROADBOOK_URL + "#reader-node-268625960", data["toc"][0]
        print("PASS 8c-2: /api/toc 解析新版路书节点卡目录（带片段请求也返回干净目录）")

        # 8d) 下载进度：/api/download-status 直读共享状态，变化经 SSE 广播给页面
        status, dl = http_json(f"{base}/api/download-status")
        assert status == 200 and dl.get("status") == "idle" and dl.get("active") is False, dl

        # 8d-2) 下载重试：页面“重试”按钮 -> /api/download-retry -> 客户端 hook
        # 未接 hook（独立 webserver）时返回 503，页面提示无法自动重试
        status, data = http_json(f"{base}/api/download-retry", method="POST", body={"game": "测试游戏"})
        assert status == 503 and data.get("ok") is False, (status, data)
        # 接上客户端 hook：正常受理（转发给客户端的下载/导入线程）
        retry_calls: list[str] = []

        def _fake_retry_hook(game: str) -> tuple[bool, str]:
            retry_calls.append(game)
            return True, "已开始重新下载（从失败处继续）"

        server.on_download_retry = _fake_retry_hook
        try:
            status, data = http_json(f"{base}/api/download-retry", method="POST", body={"game": "测试游戏"})
            assert status == 200 and data.get("ok") is True and data.get("game") == "测试游戏", (status, data)
            assert retry_calls == ["测试游戏"], retry_calls
            # hook 拒绝（同游戏已在下载中）时返回 409
            server.on_download_retry = lambda game: (False, "该游戏的下载/导入已在进行中")
            status, data = http_json(f"{base}/api/download-retry", method="POST", body={"game": "测试游戏"})
            assert status == 409 and data.get("ok") is False, (status, data)
        finally:
            server.on_download_retry = None
        print("PASS 8d-2: /api/download-retry 受理/拒绝/未接 hook 三种响应")

        sse_dl: list[dict] = []

        def _read_sse_dl() -> None:
            req = urllib.request.Request(f"{base}/api/events")
            with urllib.request.urlopen(req, timeout=20) as resp:
                buf = b""
                while not sse_dl:
                    chunk = resp.read(1)
                    if not chunk:
                        break
                    buf += chunk
                    if buf.endswith(b"\n\n"):
                        frame = buf.decode("utf-8")
                        buf = b""
                        for line in frame.splitlines():
                            if line.startswith("data:"):
                                ev = json.loads(line[5:].strip())
                                if ev.get("type") == "download_progress" and ev.get("status") == "running":
                                    sse_dl.append(ev)

        t_dl = threading.Thread(target=_read_sse_dl, daemon=True)
        t_dl.start()
        time.sleep(0.3)
        assert download_progress.write_status(
            {"status": "running", "game": "测试游戏", "stage": "下载攻略页面",
             "progress": 30.0, "detail": "处理第5/12页", "updated_at": time.time()},
        )
        t_dl.join(timeout=10)
        assert sse_dl, "SSE 未收到 download_progress 事件"
        assert sse_dl[0]["game"] == "测试游戏" and sse_dl[0]["stage"] == "下载攻略页面", sse_dl[0]
        assert sse_dl[0]["progress"] == 30.0 and "处理第5/12页" in sse_dl[0]["detail"], sse_dl[0]
        status, dl = http_json(f"{base}/api/download-status")
        assert dl["status"] == "running" and dl["active"] is True and dl["progress"] == 30.0, dl
        download_progress.write_status(
            {"status": "done", "game": "测试游戏", "stage": "全部完成",
             "progress": 100.0, "updated_at": time.time()},
        )
        status, dl = http_json(f"{base}/api/download-status")
        assert dl["status"] == "done" and dl["active"] is False and dl["stage"] == "全部完成", dl
        print("PASS 8d: /api/download-status 直读共享进度，变化经 SSE download_progress 广播")

        # 9) state persists; a fresh StateStore (server restart) still serves the target
        server.stop()
        state = StateStore(state_file)
        target = state.get_target("测试游戏")
        assert target and target["url"] == NEXT_PAGE_URL, target
        print("PASS 9: 目标持久化到 state.json，重启后仍可恢复")

        print("\nALL WEBSERVER SMOKE TESTS PASSED")
    finally:
        server.stop()
        download_progress.STATUS_FILE = _REAL_DL_STATUS_FILE  # 不污染真实 data 目录


def test_transform_unit() -> None:
    """transform() 的纯函数行为：head 注入、链接改写、非站内链接不动。"""
    from app.webserver.server import PageProxy

    html = '<html><head><title>t</title></head><body><a href="a.shtml">x</a><a href="/abs">y</a></body></html>'
    out = PageProxy.transform(html, PAGE_URL)
    assert out.index("<base") == out.index("<head>") + len("<head>")
    assert "freeze(navigator" in out  # 环境归一脚本随 head 注入
    assert "/proxy/https://www.gamersky.com/handbook/202303/a.shtml" in out
    assert "/proxy/https://www.gamersky.com/abs" in out
    # 带源前缀：绝对链接不被 <base> 劫持
    out_prefixed = PageProxy.transform(html, PAGE_URL, "http://192.168.1.5:8180")
    assert "http://192.168.1.5:8180/proxy/https://www.gamersky.com/handbook/202303/a.shtml" in out_prefixed


def test_unwrap_self_proxy() -> None:
    """转发请求里的本代理页面地址还原成真实地址（评论接口 contentUrl 场景）。"""
    from app.webserver.server import _unwrap_self_proxy

    real = "https://www.gamersky.com/handbook/202507/1961684_6.shtml"
    # 表单编码体（jQuery 默认形态，日志里实际出现的形态）
    body = "contentUrl=http%3A%2F%2F127.0.0.1%3A8180%2Fproxy%2Fhttps%3A%2F%2Fwww.gamersky.com%2Fhandbook%2F202507%2F1961684_6.shtml&pageSize=20"
    out = _unwrap_self_proxy(body)
    assert out == f"contentUrl={urllib.parse.quote(real, safe='')}&pageSize=20", out
    # 未编码形态
    raw = f'{{"contentUrl":"http://127.0.0.1:8180/proxy/{real}","sort":1}}'
    out2 = _unwrap_self_proxy(raw)
    assert out2 == f'{{"contentUrl":"{real}","sort":1}}', out2
    # 不含本代理地址时原样返回
    assert _unwrap_self_proxy("a=1&b=2") == "a=1&b=2"
    print("PASS: 请求参数/体里的本代理页面地址还原（编码与未编码形态）")


def test_transform_unit_more() -> None:
    from app.webserver.server import PageProxy

    # 无 <head> 的页面也能注入
    out2 = PageProxy.transform("<html><body>hi</body></html>", PAGE_URL)
    assert out2.startswith("<base")

    # 同页锚点不动
    assert PageProxy._rewrite_href("#top", PAGE_URL) is None
    assert PageProxy._rewrite_href(f"{PAGE_URL}#p2", PAGE_URL) is None
    assert PageProxy._rewrite_href("javascript:void(0)", PAGE_URL) is None

    # wap 版页面注入移动 UA（wap 站 JS 检测到桌面 UA 会跳回 PC 版），PC 版注入桌面 UA
    wap_url = "https://wap.gamersky.com/gl/Content-1961684_6.html"
    html = "<html><head><title>t</title></head><body>x</body></html>"
    out_wap = PageProxy.transform(html, wap_url)
    assert "iPhone; CPU iPhone OS" in out_wap and 'platform", "iPhone"' in out_wap
    assert 'DEFAULT_COMMENT_SORT = ""' in out_wap  # wap 版没有评论排序标签，不注入
    assert 'WAP_EXPAND_FULL = "1"' in out_wap  # wap 版启用“展开全文”自动处理
    out_pc = PageProxy.transform(html, PAGE_URL)
    assert "Windows NT 10.0" in out_pc and "iPhone" not in out_pc
    print("PASS: transform 单元行为补充（无 head 注入/锚点/按版本注入 UA）")


def test_config_game_thresholds() -> None:
    """每游戏阈值：文件指定优先，缺省 0.80/0.01，命令行显式指定优先级最高。"""
    import tempfile

    from app.config import AppConfig

    tmp = Path(tempfile.mkdtemp(prefix="gwa-th-"))
    cfg_file = tmp / "game_thresholds.json"
    cfg_file.write_text(
        json.dumps(
            {
                "明末：渊虚之羽": {"threshold": 0.80, "threshold_2": 0.01},
                "识质存在": {"threshold": 0.85, "threshold_2": 0.10},
                "只有阈值的游戏": {"threshold": 0.90},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cfg = AppConfig()
    cfg.game_thresholds_file = cfg_file

    # 配置文件里有 -> 用文件值
    assert cfg.thresholds_for("明末：渊虚之羽") == (0.80, 0.01)
    assert cfg.thresholds_for("识质存在") == (0.85, 0.10)
    # 只写 threshold 的条目 -> threshold_2 用默认
    assert cfg.thresholds_for("只有阈值的游戏") == (0.90, 0.01)
    # 没配置的游戏 -> 内置默认 0.80/0.01
    assert cfg.thresholds_for("未配置的游戏") == (0.80, 0.01)
    # 文件不存在/损坏 -> 默认
    cfg.game_thresholds_file = tmp / "missing.json"
    assert cfg.thresholds_for("任意游戏") == (0.80, 0.01)

    # 命令行显式指定 -> 优先于每游戏配置
    cfg.game_thresholds_file = cfg_file
    cfg.query_threshold, cfg.query_threshold_2 = 0.95, 0.20
    cfg.cli_thresholds_given = True
    assert cfg.thresholds_for("识质存在") == (0.95, 0.20)

    # mtime/size 变化后重新加载（热更新）
    cfg.cli_thresholds_given = False
    cfg_file.write_text(
        json.dumps({"识质存在": {"threshold": 0.70, "threshold_2": 0.05}}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert cfg.thresholds_for("识质存在") == (0.70, 0.05)

    # 加载失败（非法 JSON）-> 什么都不做，沿用上次成功的配置
    cfg_file.write_text("{invalid json", encoding="utf-8")
    assert cfg.thresholds_for("识质存在") == (0.70, 0.05)
    # 修复文件后立即恢复加载
    cfg_file.write_text(
        json.dumps({"识质存在": {"threshold": 0.60, "threshold_2": 0.02}}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert cfg.thresholds_for("识质存在") == (0.60, 0.02)
    print("PASS: 每游戏阈值（文件指定/缺省 0.80+0.01/命令行优先/热更新/失败沿用旧值）")


def test_overlay_config() -> None:
    """浮窗配置热加载：默认值/修改生效/非法文件沿用上次。"""
    import tempfile

    from app.overlay_window import OverlayConfig

    tmp = Path(tempfile.mkdtemp(prefix="gwa-ov-"))
    cfg_file = tmp / "overlay.json"
    cfg = OverlayConfig(cfg_file)
    assert cfg.get() == {"enabled": True, "width": 320, "height": 640}  # 文件不存在 -> 默认

    cfg_file.write_text(json.dumps({"enabled": True, "width": 360, "height": 700}), encoding="utf-8")
    assert cfg.get() == {"enabled": True, "width": 360, "height": 700}  # 修改生效

    cfg_file.write_text("{bad json", encoding="utf-8")
    assert cfg.get() == {"enabled": True, "width": 360, "height": 700}  # 失败沿用上次

    # 越界值收敛到下限
    cfg_file.write_text(json.dumps({"width": 10, "height": -5}), encoding="utf-8")
    got = cfg.get()
    assert got["width"] >= 200 and got["height"] >= 300, got

    print("PASS: 浮窗配置热加载（默认/修改生效/失败沿用上次/越界收敛）")


if __name__ == "__main__":
    test_server()
    test_transform_unit()
    test_transform_unit_more()
    test_unwrap_self_proxy()
    test_config_game_thresholds()
    test_overlay_config()
