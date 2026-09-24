"""攻略下载进度共享状态（跨进程）。

谁在写：
  - scripts/download_and_import_walkthrough.py（skill 的 download 链路，独立进程）
  - 客户端自动下载/导入（app/client.py 的 _bootstrap_job，内嵌 webserver 同进程）

谁在读：
  - 内嵌 webserver（app/webserver/server.py）：每秒轮询状态文件，变化时经 SSE
    广播给打开的攻略页面；另提供 GET /api/download-status 供页面首次加载拉取。

状态文件固定在 <项目根>/data/download_status.json：三条链路都运行在同一个
GameWalkthroughV2 工程下（客户端由 service_manager 以该工程路径拉起，下载脚本
的输出目录默认也在这里），用固定路径保证跨进程读写始终指向同一份文件。

设计要点：
  - 写入方只增不减（progress 单调不回退），出错/收尾必须调用 finish() 落终态；
  - 心跳线程定期刷新 updated_at：写入方进程意外退出后，读取方据 updated_at
    判定"已中断"，页面不会永远挂着一个假进度条；
  - 所有 IO 异常一律吞掉——进度上报永远不能影响下载本身。
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATUS_FILE = _PROJECT_ROOT / "data" / "download_status.json"

HEARTBEAT_SECONDS = 10.0     # running 期间心跳刷新 updated_at 的间隔
STALE_AFTER_SECONDS = 45.0   # updated_at 超过该时长未刷新的 running 视为已中断
MIN_WRITE_INTERVAL = 0.5     # 两次落盘的最小间隔（频控；心跳兜底兜住静默期）

_DETAIL_MAX = 160


def read_status(path: str | Path | None = None) -> dict[str, Any]:
    """读取原始状态；文件缺失/损坏/非对象时返回空 dict。"""
    target = Path(path) if path is not None else STATUS_FILE
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def write_status(payload: dict[str, Any], path: str | Path | None = None) -> bool:
    """原子写入状态（临时文件 + replace，Windows 读并发冲突时重试）。"""
    target = Path(path) if path is not None else STATUS_FILE
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        for attempt in range(3):
            try:
                tmp.replace(target)
                return True
            except OSError:
                if attempt == 2:
                    return False
                time.sleep(0.05)
    except Exception:
        return False
    return False


def active_status(path: str | Path | None = None, *, now: float | None = None) -> dict[str, Any]:
    """归一化后的下载状态（webserver 直接把它发给页面）。

    - 无记录 -> idle；
    - running 且 updated_at 长时间未刷新 -> interrupted（写入进程已死/失联）；
    - done / error 为终态，页面据此做一次性提示，不常驻展示。
    """
    current = time.time() if now is None else now
    base: dict[str, Any] = {
        "active": False,
        "status": "idle",
        "game": "",
        "stage": "",
        "progress": 0.0,
        "detail": "",
        "updated_at": 0.0,
    }
    raw = read_status(path)
    if not raw:
        return base
    state = str(raw.get("status") or "")
    game = str(raw.get("game") or "")
    stage = str(raw.get("stage") or "")
    detail = str(raw.get("detail") or "")
    try:
        progress = float(raw.get("progress") or 0.0)
    except (TypeError, ValueError):
        progress = 0.0
    try:
        updated_at = float(raw.get("updated_at") or 0.0)
    except (TypeError, ValueError):
        updated_at = 0.0
    if state not in ("running", "done", "error"):
        return base
    if state == "running" and current - updated_at > STALE_AFTER_SECONDS:
        return {
            "active": False,
            "status": "interrupted",
            "game": game,
            "stage": "下载已中断",
            "progress": progress,
            "detail": "下载进程失去响应，请重新发起下载",
            "updated_at": updated_at,
        }
    return {
        "active": state == "running",
        "status": state,
        "game": game,
        "stage": stage,
        "progress": progress,
        "detail": detail,
        "updated_at": updated_at,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 进度行解析（下载器/导入器的人类可读输出行 -> (阶段, 百分比)）
# ══════════════════════════════════════════════════════════════════════════════

# 处理第3/12页 或 处理第3页（总数未知）
_PAGE_RE = re.compile(r"处理第(\d+)(?:/(\d+))?页")
# importer 的 "insert ▓▓ 5/120" / "build ▓▓ 12/34" 进度
_RATIO_RE = re.compile(r"(\d+)\s*/\s*(\d+)")

# 关键词必须只在真正进入该阶段时出现（与服务 Manager/弹窗脚本的关键词表同一思路）
_PHASE_KEYWORDS: list[tuple[str, str, float]] = [
    ("开始下载攻略", "开始下载攻略", 2.0),
    ("输出目录", "准备下载目录", 6.0),
    ("下载结束", "攻略下载完成", 85.0),
    ("[knowledge] inserting", "导入知识文本", 87.0),
    ("[vision] inserting", "导入场景图片", 88.0),
    ("[vision] no new scenes", "导入场景图片", 88.0),
    ("[vision] build started", "构建图片索引", 96.0),
]

_PAGE_BASE = 10.0    # 页面下载阶段的起始百分比
_PAGE_SPAN = 72.0    # 页面下载阶段跨度（10 -> 82）
_INSERT_BASE = 88.0  # 场景图片插入阶段
_INSERT_SPAN = 8.0   # (88 -> 96)
_BUILD_BASE = 96.0   # 索引构建阶段
_BUILD_SPAN = 3.0    # (96 -> 99)，最后 1% 留给收尾

_INCREMENTAL_STAGE = "下载并导入攻略页面"   # 增量模式（边下边导入）的进度阶段
_INCREMENTAL_SPAN = 85.0                    # 增量模式百分比跨度（5 -> 90）


def progress_from_line(line: str) -> tuple[str | None, float | None]:
    """从一行输出提取 (阶段, 百分比)；解析不出时对应项为 None。

    importer 的 \r 进度行在被按 \n 读取时会折叠成一行里的多段更新，因此
    页码/比例都取行内最后一次匹配（最新进度），而不是第一处。
    """
    text = str(line or "")
    stage: str | None = None
    pct: float | None = None

    for keyword, label, base in _PHASE_KEYWORDS:
        if keyword in text:
            stage, pct = label, base
            break

    match: re.Match[str] | None = None
    for match in _PAGE_RE.finditer(text):
        pass
    if match is not None:
        page = int(match.group(1))
        total = int(match.group(2)) if match.group(2) else 0
        stage = "下载攻略页面"
        if total > 0:
            pct = _PAGE_BASE + _PAGE_SPAN * min(page, total) / total

    head = text.lstrip("\r")
    inserting = head.startswith("[vision] insert ")
    building = head.startswith("[vision] build") or head.startswith("[knowledge] build")
    if inserting or building:
        ratio: re.Match[str] | None = None
        for ratio in _RATIO_RE.finditer(text):
            pass
        if ratio is not None:
            current, total = int(ratio.group(1)), int(ratio.group(2))
            if total > 0:
                if building:
                    stage = "构建图片索引" if head.startswith("[vision]") else "构建知识索引"
                    pct = _BUILD_BASE + _BUILD_SPAN * min(current, total) / total
                else:
                    stage = "导入场景图片"
                    pct = _INSERT_BASE + _INSERT_SPAN * min(current, total) / total
        elif inserting:
            stage = "导入场景图片"

    return stage, pct


# ══════════════════════════════════════════════════════════════════════════════
# 进度上报器（一条下载任务一个实例，负责整条"下载 -> 导入"生命周期）
# ══════════════════════════════════════════════════════════════════════════════

class DownloadProgressReporter:
    """把一条攻略下载任务的进度落到共享状态文件。

    用法：
        reporter = DownloadProgressReporter(game_name)
        reporter.start("正在搜索攻略", 2.0)
        ...
        reporter.update(stage="下载攻略页面", progress=42.0, detail="处理第5/12页")
        ...
        reporter.finish(True, "攻略下载并导入完成")

    start 后心跳线程每 HEARTBEAT_SECONDS 刷新一次 updated_at；finish 终止心跳。
    update_from_line 可直接作为 downloader/importer 的 progress_callback 使用。
    """

    def __init__(
        self,
        game_name: str,
        *,
        path: str | Path | None = None,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
        min_write_interval: float = MIN_WRITE_INTERVAL,
        incremental: bool = False,
    ) -> None:
        self._game = str(game_name or "")
        self._path = Path(path) if path is not None else STATUS_FILE
        self._heartbeat_seconds = max(1.0, float(heartbeat_seconds))
        self._min_write_interval = max(0.0, float(min_write_interval))
        # 增量模式（边下边导入）：进度按“已完成的页数”单调推进，
        # 每页内部的插入/构建细节只进详情行，不参与百分比
        self._incremental = bool(incremental)
        self._lock = threading.Lock()
        self._payload: dict[str, Any] = {}
        self._last_write = 0.0
        self._stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None

    # ------------------------------------------------------------------ 生命周期
    def start(self, stage: str = "正在准备下载", progress: float = 0.0, detail: str = "") -> None:
        with self._lock:
            self._payload = {
                "game": self._game,
                "status": "running",
                "stage": str(stage),
                "progress": self._clamp(progress, 0.0),
                "detail": str(detail)[:_DETAIL_MAX],
            }
            self._stop.clear()
        self._write()
        if self._heartbeat_thread is None or not self._heartbeat_thread.is_alive():
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop, name="download-progress-heartbeat", daemon=True
            )
            self._heartbeat_thread.start()

    def update(
        self,
        *,
        stage: str | None = None,
        progress: float | None = None,
        detail: str | None = None,
    ) -> None:
        with self._lock:
            payload = self._payload
            if not payload or payload.get("status") != "running":
                return  # 已收尾的任务不再接受更新
            previous_stage = payload.get("stage")
            if stage:
                payload["stage"] = str(stage)
            if progress is not None:
                # 单调不回退：晚到的旧日志行不会把进度条拉回去
                payload["progress"] = self._clamp(progress, float(payload.get("progress") or 0.0))
            if detail is not None:
                payload["detail"] = str(detail)[:_DETAIL_MAX]
            # 阶段切换立即落盘（页面上最显眼的变化），其余按频控节流
            stage_changed = bool(stage) and str(stage) != previous_stage
            due = stage_changed or time.monotonic() - self._last_write >= self._min_write_interval
        if due:
            self._write()   # 没到频控点也没关系：心跳会带着内存里的最新值落盘

    def finish(self, success: bool, message: str = "", detail: str = "") -> None:
        """落终态。失败时 message 进横幅标题、detail 携带具体错误信息（页面
        错误提示与“重试”按钮会展示它）。"""
        with self._lock:
            if self._payload and self._payload.get("status") != "running":
                return  # 幂等：重复 finish 不覆盖终态
            self._payload = {
                "game": self._game,
                "status": "done" if success else "error",
                "stage": str(message or ("攻略已就绪" if success else "下载失败")),
                "progress": 100.0 if success else float(self._payload.get("progress") or 0.0),
                "detail": str(detail or "")[:_DETAIL_MAX],
            }
            self._stop.set()
        self._write()

    # ------------------------------------------------------------------ 行回调
    def update_from_line(self, line: str) -> None:
        """progress_callback 形态的更新入口（downloader / importer 直接可用）。"""
        if self._incremental:
            self._update_from_line_incremental(line)
            return
        stage, pct = progress_from_line(line)
        if stage is None and pct is None:
            # 没解析出阶段也把原文带上：页面详情行能跟着滚动
            text = str(line or "").strip()
            if text:
                self.update(detail=text)
            return
        self.update(stage=stage or None, progress=pct, detail=str(line or "").strip())

    def _update_from_line_incremental(self, line: str) -> None:
        """增量模式（边下边导入）的进度解析。

        每一“页”是一个 下载→插入→构建 周期：百分比按已完成的页数单调推进
        （5%→90%），页内的插入/构建细节只进详情行——否则每页的构建进度会把
        百分比顶到 99% 后卡住不动。
        """
        text = str(line or "")
        match: re.Match[str] | None = None
        for match in _PAGE_RE.finditer(text):
            pass
        if match is not None:
            page = int(match.group(1))
            total = int(match.group(2)) if match.group(2) else 0
            detail = text.strip()[:_DETAIL_MAX]
            if total > 0:
                progress = 5.0 + _INCREMENTAL_SPAN * min(page, total) / total
                self.update(stage=_INCREMENTAL_STAGE, progress=progress, detail=detail)
            else:
                self.update(stage=_INCREMENTAL_STAGE, detail=detail)
            return
        head = text.lstrip("\r")
        if head.startswith("[vision]") or head.startswith("[knowledge]"):
            self.update(detail=head[:_DETAIL_MAX])
            return
        stage, pct = progress_from_line(text)
        if stage is None and pct is None:
            stripped = text.strip()
            if stripped:
                self.update(detail=stripped)
            return
        self.update(stage=stage, progress=pct, detail=text.strip())

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _clamp(value: float, floor: float) -> float:
        return max(floor, min(100.0, float(value)))

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self._heartbeat_seconds):
            with self._lock:
                running = self._payload.get("status") == "running"
            if not running:
                return
            self._write()

    def _write(self) -> None:
        with self._lock:
            payload = dict(self._payload)
            self._last_write = time.monotonic()
        if not payload:
            return
        payload["updated_at"] = time.time()
        write_status(payload, self._path)
