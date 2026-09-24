"""游戏攻略助手 - 桌面客户端入口（自动启动内嵌 webserver）。

用法:
    python run_app.py
    python run_app.py 艾尔登法环           # 指定游戏名：跳过检测，下载/导入并识别全屏
    python run_app.py --service-host 192.168.1.10:22919 --web-port 22818
    python run_app.py --no-web            # 只运行客户端，不启动 webserver
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make `app` and `scripts` importable no matter where the script is invoked from.
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.cli import add_common_arguments, make_config
from app.client import GameWalkthroughApp
from app.env_setup import ensure_no_proxy

# Agent 沙盒可能注入 http_proxy：本进程对 127.0.0.1 的请求（webserver、
# 游戏助手服务）一律免代理，入口处先设置（详见 app/env_setup.py）
ensure_no_proxy()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="game-walkthrough-app",
        description="游戏攻略助手：检测游戏 → 截图 → vision 匹配 → 推送网页跳转",
    )
    parser.add_argument("fixed_game", nargs="?", default=None, metavar="游戏名",
                        help="指定游戏名：跳过游戏检测，直接下载/导入攻略并识别全屏画面")
    add_common_arguments(parser)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    config = make_config(args)
    # 文件日志：下载/导入/推送/页面打开等运行事件（logs/game-assistant-YYYYMMDD.log）
    from app.file_logging import setup_logging

    setup_logging()
    app = GameWalkthroughApp(config)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
