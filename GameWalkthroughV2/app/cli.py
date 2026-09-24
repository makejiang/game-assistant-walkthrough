"""Shared command line argument helpers for the entry scripts."""

from __future__ import annotations

import argparse

from app.config import AppConfig, config_from_args


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    """Register options shared by the client and the webserver entry points."""
    parser.add_argument("--service-host", dest="service_host", default=None,
                        help="游戏助手服务端地址，默认 127.0.0.1:22919"
                             "（未指定时 22919 不通会自动回退旧端口 9190；显式指定则不回退）")
    parser.add_argument("--web-host", dest="webserver_host", default=None,
                        help="webserver 监听地址，默认 0.0.0.0")
    parser.add_argument("--web-port", dest="webserver_port", type=int, default=None,
                        help="webserver 端口，默认 22818")
    parser.add_argument("--no-web", action="store_true",
                        help="不启动内嵌 webserver（仅 webserver 模式时忽略）")
    parser.add_argument("--three-d-threshold", type=float, default=None,
                        help="GPU 3D 利用率阈值（>=，默认 20）")
    parser.add_argument("--decoder-threshold", type=float, default=None,
                        help="GPU 解码利用率上限（<=，默认 10）")
    parser.add_argument("--encoder-threshold", type=float, default=None,
                        help="GPU 编码利用率上限（<=，默认 10）")
    parser.add_argument("--phys-index", type=int, default=None,
                        help="可选：指定物理 GPU 索引")
    parser.add_argument("--detection-interval", dest="detection_interval_seconds",
                        type=float, default=None, help="游戏检测轮询间隔秒，默认 1")
    parser.add_argument("--game-exit-grace-seconds", type=float, default=None,
                        help="游戏进程消失后持续多久才判定退出，默认 5")
    parser.add_argument("--screenshot-fps", type=int, default=None,
                        help="截图帧率，默认 10")
    parser.add_argument("--query-topk", type=int, default=None, help="vision 查询 topk，默认 1")
    parser.add_argument("--query-threshold", type=float, default=None, help="vision 查询阈值")
    parser.add_argument("--query-threshold-2", type=float, default=None, help="vision 查询阈值2")
    parser.add_argument("--match-score-threshold", type=float, default=None,
                        help="匹配分数阈值（低于则忽略）")
    parser.add_argument("--confirm-hit-count", type=int, default=None,
                        help="连续命中次数确认，默认 2")
    parser.add_argument("--toast-hold-seconds", type=float, default=None,
                        help="弹窗停留秒数，默认 10")
    parser.add_argument("--no-qr-panel", action="store_true",
                        help="不启用右上角扫码访问面板（鼠标移到屏幕右上角弹出二维码）")
    parser.add_argument("--no-overlay", action="store_true",
                        help="不启用左上角攻略浮窗（即使鼠标移到左上角也不弹出）")
    parser.add_argument("--qr-hotzone", dest="qr_hotzone_px", type=int, default=None,
                        help="触发扫码面板的屏幕右上角热区大小（像素），默认 16")
    parser.add_argument("--no-auto-bootstrap", action="store_true",
                        help="检测到游戏但未导入攻略时不自动下载/导入")


def make_config(args: argparse.Namespace) -> AppConfig:
    """Build AppConfig from parsed args and apply the no-* toggles."""
    config = config_from_args(args)
    if getattr(args, "no_web", False):
        config.embedded_webserver = False
    if getattr(args, "no_qr_panel", False):
        config.qr_panel_enabled = False
    if getattr(args, "no_overlay", False):
        config.overlay_enabled = False
    if getattr(args, "no_auto_bootstrap", False):
        config.auto_bootstrap = False
    return config
