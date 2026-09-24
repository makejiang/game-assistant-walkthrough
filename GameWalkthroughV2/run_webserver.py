"""游戏攻略助手 - 独立 webserver 入口。

当客户端以内嵌模式运行时无需单独启动本文件。如果希望 webserver 独立运行
（例如部署在另一台机器上，或只让用户访问网页），可以单独启动：

    python run_webserver.py --web-host 0.0.0.0 --web-port 22818

webserver 只负责接收推送（攻略页面 URL + 内容定位）并通过 SSE 通知浏览器，
浏览器在 frame 内经同源代理打开原始攻略页面并定位到内容位置。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.cli import add_common_arguments, make_config
from app.env_setup import ensure_no_proxy
from app.webserver.server import WalkthroughWebServer

# Agent 沙盒可能注入 http_proxy：本机回环请求一律免代理（详见 app/env_setup.py）
ensure_no_proxy()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="game-walkthrough-web",
        description="游戏攻略助手 webserver：解析攻略页面并提供网页浏览",
    )
    add_common_arguments(parser)
    return parser


def main() -> int:
    from app.file_logging import setup_logging

    setup_logging()
    parser = build_parser()
    args = parser.parse_args()
    config = make_config(args)
    config.embedded_webserver = True  # this IS the webserver

    server = WalkthroughWebServer(config)
    server.start()
    print("[webserver] 按 Ctrl+C 停止服务。")
    try:
        while not server.stop_event.is_set():
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n[webserver] 正在停止…")
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
