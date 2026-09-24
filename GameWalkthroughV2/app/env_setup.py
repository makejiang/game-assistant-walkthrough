"""进程级环境兜底：本机回环免代理 + WebView2 兼容 Agent 沙盒的 DLL 注入。

客户端（run_app.py / run_webserver.py）与浮窗子进程（app.overlay_window）
在入口处调用本模块：

- ensure_no_proxy():      Agent 沙盒/企业网络常注入 http_proxy 等代理变量，会把
                          发往 127.0.0.1 的本机请求（浮窗/浏览器访问 webserver、
                          客户端连本机游戏助手服务）也送进代理而失败。把回环地址
                          合并进 no_proxy/NO_PROXY（保留已有条目；只影响回环，
                          外网下载照常走代理）。
- ensure_webview2_args(): 会往进程注入 DLL 的沙盒环境会让 WebView2(Chromium)
                          白屏崩溃。禁用 Chromium 自身沙箱后浏览器进程不再与
                          注入冲突（实测仅加 ThirdPartyDllBlocking feature 参数
                          无效，--no-sandbox 是关键；页面只来自本机 webserver，
                          风险可控）。必须在 webview 创建 WebView2 环境之前设置
                          ——浮窗在模块导入时调用。
"""

from __future__ import annotations

import os

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost")
_WEBVIEW2_ENV = "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"
# 顺序即语义：--no-sandbox 必须有，feature 参数只是配合
_WEBVIEW2_FLAGS = ("--disable-features=ThirdPartyDllBlocking", "--no-sandbox")


def _append_unique(entries: list[str], extra) -> list[str]:
    lowered = {e.lower() for e in entries}
    entries.extend(e for e in extra if e.lower() not in lowered)
    return entries


def ensure_no_proxy() -> None:
    """把 127.0.0.1 / localhost 合并进 no_proxy 与 NO_PROXY（幂等）。

    两个键取并集后写成同一份：Windows 的 os.environ 键不区分大小写，不同库
    读取的键也不同（requests 先小写、urllib 变体各异），两个键值不一致时
    会有进程漏掉回环免代理。已有条目原样保留。
    """
    entries: list[str] = []
    for key in ("no_proxy", "NO_PROXY"):
        entries = _append_unique(
            [e.strip() for e in os.environ.get(key, "").split(",") if e.strip()],
            entries,
        )
    merged = ",".join(_append_unique(entries, _LOOPBACK_HOSTS))
    os.environ["no_proxy"] = merged
    os.environ["NO_PROXY"] = merged


def ensure_webview2_args() -> None:
    """确保 WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS 带上防 DLL 注入白屏的参数。

    保留环境里已有的其它参数，只补缺的 flag（幂等）。注意 Chromium 对同名
    开关只认最后一个：若已有 --disable-features=X，本函数追加后以本模块的
    feature 列表为准——防白屏的参数不能丢。
    """
    flags = os.environ.get(_WEBVIEW2_ENV, "").split()
    os.environ[_WEBVIEW2_ENV] = " ".join(_append_unique(flags, _WEBVIEW2_FLAGS))
