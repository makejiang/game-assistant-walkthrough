#!/usr/bin/env python3
"""
攻略下载进度弹窗包装器
  包装 download_and_import_walkthrough.py，提供 tkinter 进度窗口。

用法:
    python download_with_progress.py <游戏名>  [--output-dir ...] [--download-timeout ...]

架构（参考 deploy.py）:
    主线程 = tkinter mainloop + 进度弹窗
    工作线程 = Popen 运行原始脚本，逐行读 stdout → queue → 更新 UI
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk


_SCRIPT_DIR = Path(__file__).resolve().parent
# 与 v2 客户端的 walkthrough_dir 保持一致（GameWalkthroughV2/walkthrough），
# 客户端检测到游戏后可直接复用已下载的攻略，不会重复下载。
_DEFAULT_WALKTHROUGH_DIR = _SCRIPT_DIR.parent / "GameWalkthroughV2" / "walkthrough"


# ══════════════════════════════════════════════════════════════════════════════
# Font helpers (same as deploy.py)
# ══════════════════════════════════════════════════════════════════════════════

def _pick_font(families: tuple, size: int, root) -> tuple:
    import tkinter.font as tkfont
    available = set(tkfont.families(root))
    for f in families:
        if f in available:
            return f, size
    return families[-1], size


_MONO_FONTS = ("Cascadia Code", "Consolas", "Courier New", "SimSun")
_UI_FONTS   = ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "SimSun")


# ══════════════════════════════════════════════════════════════════════════════
# Progress Dialog (same style as deploy.py ProgressDialog)
# ══════════════════════════════════════════════════════════════════════════════

class ProgressDialog:
    """攻略下载进度窗口 — 参考 deploy.py ProgressDialog 风格。"""

    def __init__(self, root: tk.Tk, game_name: str = ""):
        self.root = root

        ui_font   = _pick_font(_UI_FONTS, 10, root)
        mono_font = _pick_font(_MONO_FONTS, 10, root)

        title = f"下载攻略: {game_name}" if game_name else "攻略下载进度"
        root.title(title)
        root.geometry("860x560")
        root.minsize(640, 400)
        root.configure(bg="#fafafa")
        root.protocol("WM_DELETE_WINDOW", self._on_user_close)

        # ── 状态标签 ──
        self._status_var = tk.StringVar(value="准备中...")
        status = tk.Label(
            root, textvariable=self._status_var,
            font=ui_font, anchor="w", justify="left",
            wraplength=820, bg="#fafafa", fg="#333333",
        )
        status.place(x=12, y=12, width=820, height=44)

        # ── 进度条 ──
        self._bar = ttk.Progressbar(root, mode="indeterminate", length=820)
        self._bar.place(x=12, y=62, width=820, height=20)
        self._bar.start(30)

        # ── 日志文本框 ──
        log_frame = tk.Frame(root, bg="#fafafa")
        log_frame.place(x=12, y=94, width=820, height=368)

        self._log_box = tk.Text(
            log_frame, font=mono_font, wrap="word",
            state="disabled", relief="sunken", borderwidth=1,
            bg="#ffffff", fg="#333333",
        )
        v_scroll = tk.Scrollbar(
            log_frame, orient="vertical", command=self._log_box.yview,
        )
        self._log_box.configure(yscrollcommand=v_scroll.set)
        v_scroll.pack(side="right", fill="y")
        self._log_box.pack(side="left", fill="both", expand=True)

        self._close_at: float | None = None
        self._close_base: str = ""

        self._center(860, 560)

    def _center(self, w, h):
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

    def update(self, *, stage: str = None, progress: float = None, detail: str = ""):
        if stage is not None:
            self._status_var.set(stage)
        if progress is not None:
            self._bar.configure(mode="determinate")
            self._bar["value"] = min(100, max(0, progress))
        if detail:
            self._log_box.configure(state="normal")
            self._log_box.insert("end", detail.rstrip() + "\n")
            self._log_box.see("end")
            self._log_box.configure(state="disabled")

    def schedule_close(self, seconds: int = 8):
        self._close_at = time.monotonic() + seconds
        self._close_base = self._status_var.get()

    def tick_close(self) -> bool:
        if self._close_at is None:
            return False
        remaining = max(0, int(self._close_at - time.monotonic()))
        if remaining > 0:
            self._status_var.set(
                f"{self._close_base}  —  {remaining} 秒后自动关闭"
            )
            return False
        self.root.quit()
        return True

    def _on_user_close(self):
        _write_download_task_status("error", "用户取消了下载")
        _terminate_child()
        os._exit(0)


# ══════════════════════════════════════════════════════════════════════════════
# Progress parsing
# ══════════════════════════════════════════════════════════════════════════════

_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_PAGE_RE = re.compile(r"处理第(\d+)页")

_DOWNLOAD_PHASES = [
    ("开始下载攻略", "搜索攻略", 2),
    ("搜索", "正在搜索攻略", 5),
    ("输出目录", "准备下载目录", 8),
    ("处理第", "下载攻略页面", 10),
    ("下载结束", "攻略下载完成", 85),
    ("knowledge", "导入文本知识库", 88),
    ("vision", "导入场景图片", 92),
    ("导出完成", "全部完成", 100),
    ("导入完成", "全部完成", 100),
]


def _parse_line(line: str) -> tuple[str, float]:
    """从一行 stdout 提取 (stage, progress_pct)，-1 表示无更新。"""
    stage = ""
    pct = -1.0
    for keyword, label, base_pct in _DOWNLOAD_PHASES:
        if keyword in line:
            stage = label
            if pct < 0:
                pct = float(base_pct)
            break

    m = _PCT_RE.search(line)
    if m:
        pct = float(m.group(1))

    m2 = _PAGE_RE.search(line)
    if m2:
        page = int(m2.group(1))
        pct = 8.0 + 72.0 * min(page, 20) / 20.0

    return stage, pct


# ══════════════════════════════════════════════════════════════════════════════
# Worker thread
# ══════════════════════════════════════════════════════════════════════════════

_child_proc: subprocess.Popen | None = None


def _terminate_child() -> None:
    """用户关掉弹窗时，把下载/导入子进程一起停掉，避免留下看不见的孤儿任务。"""
    proc = _child_proc
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
    except Exception:
        pass


def _worker(gui_q: queue.Queue, cmd: list[str], cwd: Path):
    """后台线程：运行原始脚本，逐行读 stdout，推送到 GUI。"""
    global _child_proc

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"       # 子进程链无缓冲
    env["PYTHONIOENCODING"] = "utf-8"    # 子进程强制 UTF-8 输出，跨系统不乱码

    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",                # 与 PYTHONIOENCODING 对齐
        errors="replace",
        env=env,
    )
    _child_proc = proc

    last_error_line = ""
    for raw in proc.stdout:
        line = raw.rstrip("\n\r")
        if line:
            print(line, flush=True)
            # 记住真正的失败原因（如"未找到包含关键词的攻略链接"），
            # 否则用户只会看到"返回码: 1"，完全无从判断该怎么办
            if line.startswith(("下载失败:", "error:", "导入失败:")):
                last_error_line = line.strip()[:200]
        stage, pct = _parse_line(line)
        # 只有真解析出东西才更新对应字段：无关行给的 pct=-1 会被 update() 夹成 0，
        # 进度条每来一行就被清零。
        payload: dict = {"detail": line.strip()[:120]}
        if stage:
            payload["stage"] = stage
        if pct >= 0:
            payload["value"] = pct
        gui_q.put(("progress", payload))

    proc.wait()
    if proc.returncode == 0:
        gui_q.put(("done", {"message": "攻略下载并导入完成"}))
    else:
        reason = last_error_line or f"返回码: {proc.returncode}"
        gui_q.put(("error", {"message": f"下载/导入失败 — {reason}"}))


# ══════════════════════════════════════════════════════════════════════════════
# Task status update
# ══════════════════════════════════════════════════════════════════════════════

_DEFAULT_STATUS_DIR = Path(os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))) / "GameAssistant"
_STATUS_DIR = _DEFAULT_STATUS_DIR
_STATUS_FILE = _STATUS_DIR / "task_status.json"


def _resolve_status_dir(install_dir: str) -> Path:
    """状态文件目录：install_dir 非空且可写→用它；否则回落默认（与 service_manager 对齐）。"""
    install_dir = (install_dir or "").strip()
    if not install_dir:
        return _DEFAULT_STATUS_DIR
    p = Path(install_dir)
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".write_probe"
        probe.write_text("ok")
        probe.unlink()
        return p
    except OSError:
        return _DEFAULT_STATUS_DIR


def _write_download_task_status(status: str, message: str):
    """更新 task_status.json，标记下载任务完成/失败。"""
    try:
        _STATUS_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "task": "download",
            "status": status,
            "stage": message,
            "progress": 100 if status == "done" else 0,
        }
        tmp = _STATUS_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        tmp.replace(_STATUS_FILE)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="攻略下载进度弹窗 — 包装 download_and_import_walkthrough.py"
    )
    parser.add_argument("game_name", help="要下载攻略的游戏名")
    parser.add_argument("--output-dir", default=_DEFAULT_WALKTHROUGH_DIR.as_posix())
    parser.add_argument("--host", default=None,
                        help="服务端地址，默认自动探测（22919，不通回退 9190）")
    parser.add_argument("--install-dir", default="")
    parser.add_argument("--download-timeout", type=int, default=20)
    parser.add_argument("--import-timeout", type=float, default=30.0)
    parser.add_argument("--force-reimport", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    global _STATUS_DIR, _STATUS_FILE
    # 支持 %LOCALAPPDATA% 这类环境变量路径（与 service_manager / deploy 对齐）
    args.install_dir = os.path.expandvars((args.install_dir or "").strip())
    _STATUS_DIR = _resolve_status_dir(args.install_dir)
    _STATUS_FILE = _STATUS_DIR / "task_status.json"

    game_name = str(args.game_name).strip()
    if not game_name:
        print("error: game_name cannot be empty", file=sys.stderr)
        return 2

    base_script = _SCRIPT_DIR / "download_and_import_walkthrough.py"
    cmd = [
        sys.executable, str(base_script),
        game_name,
        "--output-dir", str(args.output_dir),
        # --host 未指定时不下发，由子脚本自动探测（22919，不通回退 9190）
        *(["--host", str(args.host)] if args.host else []),
        "--download-timeout", str(args.download_timeout),
        "--import-timeout", str(args.import_timeout),
    ]
    if args.force_reimport:
        cmd.append("--force-reimport")
    if args.verbose:
        cmd.append("--verbose")

    # ── GUI 主循环（参考 deploy.py main()） ──
    root = tk.Tk()
    dialog = ProgressDialog(root, game_name=game_name)

    gui_q: queue.Queue = queue.Queue()
    final_result = {"success": False, "message": "", "finished": False}
    countdown_started = False

    def process_messages():
        nonlocal countdown_started
        try:
            while True:
                msg_type, payload = gui_q.get_nowait()

                if msg_type == "progress":
                    dialog.update(
                        stage=payload.get("stage"),
                        progress=payload.get("value"),
                        detail=payload.get("detail", ""),
                    )
                elif msg_type == "done":
                    final_result["success"] = True
                    final_result["message"] = payload.get("message", "")
                    final_result["finished"] = True
                    dialog.update(stage=payload.get("message", "完成"), progress=100)
                    _write_download_task_status("done", payload.get("message", ""))
                    if not countdown_started:
                        dialog.schedule_close(seconds=8)
                        countdown_started = True
                elif msg_type == "error":
                    final_result["success"] = False
                    final_result["message"] = payload.get("message", "")
                    final_result["finished"] = True
                    dialog.update(
                        stage=f"错误: {payload.get('message', '')}", progress=0
                    )
                    _write_download_task_status("error", payload.get("message", ""))
                    if not countdown_started:
                        dialog.schedule_close(seconds=10)
                        countdown_started = True
        except queue.Empty:
            pass

        if countdown_started and dialog.tick_close():
            return

        root.after(500, process_messages)

    worker = threading.Thread(
        target=_worker,
        args=(gui_q, cmd, _SCRIPT_DIR),
        daemon=True,
    )
    worker.start()

    root.after(100, process_messages)
    root.mainloop()
    worker.join(timeout=5)

    if final_result["success"]:
        msg = final_result["message"]
        print(f"\n  [OK] {msg}\n")
        return 0
    else:
        msg = final_result["message"] or "未知错误"
        print(f"\n  [FAIL] {msg}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
