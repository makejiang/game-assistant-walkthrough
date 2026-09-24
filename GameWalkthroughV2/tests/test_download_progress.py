"""攻略下载进度共享状态（app/download_progress.py）的单元测试。

不依赖网络与 webserver，只验证：进度行解析、上报器生命周期（含单调进度、
幂等收尾）、共享文件的归一化读取（含心跳失联判中断）。

Run:  python tests/test_download_progress.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):  # Windows 控制台默认 cp1252
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app import download_progress
from app.download_progress import (
    DownloadProgressReporter,
    active_status,
    progress_from_line,
    read_status,
    write_status,
)


def approx(a: float, b: float, eps: float = 0.01) -> bool:
    return abs(a - b) <= eps


def test_progress_from_line() -> None:
    # 下载阶段：页码带总数 -> 10 + 72 * n/total
    stage, pct = progress_from_line("[walkthrough] 处理第3/12页(仅图片): https://x")
    assert stage == "下载攻略页面" and approx(pct, 10.0 + 72.0 * 3 / 12), (stage, pct)
    # 最后一页 -> 82
    stage, pct = progress_from_line("处理第12/12页: https://x")
    assert stage == "下载攻略页面" and approx(pct, 82.0), (stage, pct)
    # 页码超出总数按封顶处理
    _, pct = progress_from_line("处理第99/12页: https://x")
    assert approx(pct, 82.0), pct
    # 总数未知：有阶段无百分比（页面走 indeterminate 动画）
    stage, pct = progress_from_line("处理第5页: https://x")
    assert stage == "下载攻略页面" and pct is None, (stage, pct)
    # 阶段关键词
    assert progress_from_line("[walkthrough] 开始下载攻略图片(纯图片): G") == ("开始下载攻略", 2.0)
    assert progress_from_line("输出目录: d/walkthrough/G") == ("准备下载目录", 6.0)
    assert progress_from_line("[walkthrough] 下载结束(仅图片): 扫描12页") == ("攻略下载完成", 85.0)
    assert progress_from_line("[vision] inserting 120 scene(s)") == ("导入场景图片", 88.0)
    assert progress_from_line("[vision] no new scenes to insert") == ("导入场景图片", 88.0)
    assert progress_from_line("[vision] build started") == ("构建图片索引", 96.0)

    # \r 进度行被按 \n 读取时折叠成一行：页码/比例取行内最后一处（最新进度）
    collapsed = "\r[vision] insert ▓▓▓░░ 3/120 scene_id=a \r[vision] insert ▓▓▓▓░ 9/120 scene_id=b"
    stage, pct = progress_from_line(collapsed)
    assert stage == "导入场景图片" and approx(pct, 88.0 + 8.0 * 9 / 120), (stage, pct)
    # 构建进度条同理
    stage, pct = progress_from_line("[vision] build    ▓▓░░ 3/12 知识构建事件")
    assert stage == "构建图片索引" and approx(pct, 96.0 + 3.0 * 3 / 12), (stage, pct)
    # 无关行
    assert progress_from_line("本页完成(仅图片): 新下载5张") == (None, None)
    assert progress_from_line("") == (None, None)
    print("PASS: 进度行解析（页码/阶段/\\r折叠行取最新/无关行）")


def test_reporter_lifecycle() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="gwa-dl-"))
    status_file = tmp / "download_status.json"

    reporter = DownloadProgressReporter(
        "测试游戏", path=status_file, heartbeat_seconds=3600, min_write_interval=0
    )
    reporter.start("正在搜索攻略", 2.0)
    raw = read_status(status_file)
    assert raw["status"] == "running" and raw["game"] == "测试游戏", raw
    assert raw["stage"] == "正在搜索攻略" and raw["progress"] == 2.0, raw
    assert raw["updated_at"] > 0

    # 页面行更新：阶段 + 进度 + 详情一起落盘
    reporter.update_from_line("[walkthrough] 处理第3/12页(仅图片): https://x")
    raw = read_status(status_file)
    assert raw["stage"] == "下载攻略页面" and approx(raw["progress"], 28.0), raw
    assert "处理第3/12页" in raw["detail"], raw

    # 进度单调不回退（晚到的旧日志行不把进度条拉回去）
    reporter.update(progress=50.0)
    reporter.update(progress=20.0)
    assert read_status(status_file)["progress"] == 50.0

    # 收尾落终态；收尾后的更新一律忽略；重复 finish 幂等
    reporter.finish(True, "攻略下载并导入完成")
    raw = read_status(status_file)
    assert raw["status"] == "done" and raw["progress"] == 100.0, raw
    assert raw["stage"] == "攻略下载并导入完成"
    reporter.update(progress=1.0, stage="倒退")
    reporter.finish(False, "不该覆盖终态")
    assert read_status(status_file) == raw

    # 失败收尾保留最后进度，便于页面上看出卡在哪一步
    failed = DownloadProgressReporter(
        "失败游戏", path=status_file, heartbeat_seconds=3600, min_write_interval=0
    )
    failed.start("下载攻略页面", 40.0)
    failed.finish(False, "下载/导入失败: 网络超时")
    raw = read_status(status_file)
    assert raw["status"] == "error" and raw["progress"] == 40.0, raw
    assert raw["stage"] == "下载/导入失败: 网络超时"

    # 频控：纯详情更新被节流（等心跳兜底）；阶段切换绕过频控立即落盘
    throttled = DownloadProgressReporter(
        "节流游戏", path=status_file, heartbeat_seconds=3600, min_write_interval=60
    )
    throttled.start("正在搜索攻略", 2.0)
    throttled.update(detail="只是详情变化")
    assert read_status(status_file)["detail"] != "只是详情变化", "纯详情更新不应绕过频控"
    throttled.update(stage="下载攻略页面", progress=30.0, detail="处理第5页")
    raw = read_status(status_file)
    assert raw["stage"] == "下载攻略页面" and raw["detail"] == "处理第5页", raw
    assert raw["progress"] == 30.0
    print("PASS: 上报器生命周期（running -> 更新 -> done/error，单调进度，幂等收尾，频控与阶段直写）")


def test_active_status() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="gwa-dl-"))
    status_file = tmp / "download_status.json"

    # 无记录 -> idle
    st = active_status(status_file)
    assert st["status"] == "idle" and st["active"] is False, st

    # running（心跳新鲜）-> active
    write_status({"status": "running", "game": "G", "stage": "下载攻略页面",
                  "progress": 42.0, "detail": "处理第5/12页", "updated_at": time.time()},
                 status_file)
    st = active_status(status_file)
    assert st["active"] is True and st["status"] == "running" and st["progress"] == 42.0, st

    # running 但 updated_at 长时间未刷新（写入进程已死）-> interrupted，页面不挂假进度条
    st = active_status(status_file, now=time.time() + download_progress.STALE_AFTER_SECONDS + 5)
    assert st["status"] == "interrupted" and st["active"] is False, st
    assert st["stage"] == "下载已中断" and st["game"] == "G", st

    # 终态：active=False，保留阶段/进度供页面做一次性提示
    write_status({"status": "done", "game": "G", "stage": "全部完成",
                  "progress": 100.0, "updated_at": time.time() - 3600}, status_file)
    st = active_status(status_file)
    assert st["status"] == "done" and st["active"] is False and st["stage"] == "全部完成", st

    # 损坏文件/未知状态 -> idle
    status_file.write_text("{not json", encoding="utf-8")
    assert active_status(status_file)["status"] == "idle"
    write_status({"hello": "world"}, status_file)
    assert active_status(status_file)["status"] == "idle"
    print("PASS: 归一化读取（idle/running/心跳失联判中断/终态/损坏文件兜底）")


def test_incremental_reporter() -> None:
    """增量模式（边下边导入）：百分比按已完成页数单调推进，
    页内插入/构建细节只进详情行，不把百分比顶到 99% 后卡住。"""
    with tempfile.TemporaryDirectory(prefix="gwa-incr-") as tmp:
        status_file = Path(tmp) / "status.json"
        reporter = DownloadProgressReporter(
            "测试游戏", path=status_file, heartbeat_seconds=3600,
            min_write_interval=0, incremental=True,
        )
        reporter.start("正在搜索攻略", 2.0)

        reporter.update_from_line("[walkthrough] 攻略共约 4 页")
        reporter.update_from_line("[walkthrough] 处理第1/4页(仅图片): http://x/1")
        st = active_status(status_file)
        assert st["stage"] == "下载并导入攻略页面", st
        assert approx(st["progress"], 5.0 + 85.0 * 1 / 4), st

        reporter.update_from_line("[vision] inserting 1 scene(s)")
        reporter.update_from_line("[vision] build ▓▓▓ 1/1")
        st = active_status(status_file)
        # 页内插入/构建只进详情行：百分比不被顶到 99% 后卡住
        assert approx(st["progress"], 5.0 + 85.0 * 1 / 4), st
        assert "insert" in st["detail"] or "build" in st["detail"], st

        reporter.update_from_line("[walkthrough] 处理第4/4页(仅图片): http://x/4")
        st = active_status(status_file)
        assert approx(st["progress"], 5.0 + 85.0), st
        reporter.finish(True, "完成")
        st = active_status(status_file)
        assert st["status"] == "done" and st["progress"] == 100.0, st
    print("PASS: 增量模式进度按页数单调推进（页内构建不卡 99%）")


def test_default_path_roundtrip() -> None:
    """默认路径读写走同一份文件（monkeypatch 模块常量，不污染真实 data 目录）。"""
    tmp = Path(tempfile.mkdtemp(prefix="gwa-dl-"))
    original = download_progress.STATUS_FILE
    download_progress.STATUS_FILE = tmp / "download_status.json"
    try:
        reporter = DownloadProgressReporter("默认路径游戏", min_write_interval=0)
        reporter.start("下载攻略页面", 30.0)
        assert active_status()["game"] == "默认路径游戏", active_status()
        reporter.finish(True, "全部完成")
        assert active_status()["status"] == "done"
        # 文件内容是合法 JSON 且带 updated_at
        raw = json.loads((tmp / "download_status.json").read_text(encoding="utf-8"))
        assert raw["updated_at"] > 0
    finally:
        download_progress.STATUS_FILE = original
    print("PASS: 默认状态文件路径读写一致")


if __name__ == "__main__":
    test_progress_from_line()
    test_reporter_lifecycle()
    test_incremental_reporter()
    test_active_status()
    test_default_path_roundtrip()
    print("\nALL DOWNLOAD PROGRESS TESTS PASSED")
