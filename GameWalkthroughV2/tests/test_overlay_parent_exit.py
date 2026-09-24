"""Offline test: 客户端主进程退出/被强杀时，浮窗子进程跟随退出。

覆盖 app/overlay_window.py 的两道机制（均以真实子进程验证，不依赖 pywebview）：
  1) Job Object：父进程硬退出（os._exit，不走任何清理）后，放进 Job 的子进程
     被内核终止；
  2) 父进程看门狗：子进程内 watch_parent_exit 线程在"父进程"消失后让自己退出。

Run:  python tests/test_overlay_parent_exit.py
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

if sys.platform != "win32":
    print("SKIP: 仅 Windows 支持 Job Object / 父进程句柄等待")
    raise SystemExit(0)

# 中间进程脚本：模拟"客户端主进程"。A 验证 Job Object（硬退出后子进程全灭），
# B 验证看门狗（"父进程"被杀后自己退出）。均通过真实进程行为断言。
_MIDDLE_JOB = """\
import os, subprocess, sys, time
sys.path.insert(0, r"{root}")
from app.overlay_window import assign_kill_on_close
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
job = assign_kill_on_close(child)
print(child.pid, flush=True)
time.sleep(1.0)          # 让子进程确实跑起来
os._exit(1)              # 模拟强杀：不走 terminate/任务清理
"""

_MIDDLE_WATCHDOG = """\
import subprocess, sys, threading, time
sys.path.insert(0, r"{root}")
from app.overlay_window import watch_parent_exit
victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
print(victim.pid, flush=True)
threading.Thread(target=watch_parent_exit, args=(victim.pid,), daemon=True).start()
time.sleep(60)           # 看门狗应在 victim 死后让本进程退出
"""


def pid_alive(pid: int) -> bool:
    """SYNCHRONIZE 句柄上 Wait(0)：WAIT_TIMEOUT(0x102) 即仍存活。"""
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32]
    handle = kernel32.OpenProcess(0x00100000, False, int(pid))
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0x00000102
    finally:
        kernel32.CloseHandle(handle)


def wait_gone(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    return not pid_alive(pid)


def run_middle(script: str) -> tuple[subprocess.Popen, int]:
    tmp = Path(tempfile.mkdtemp(prefix="gwa-overlay-")) / "middle.py"
    tmp.write_text(script.format(root=str(PROJECT_ROOT)), encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(
        [sys.executable, str(tmp)],
        stdout=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace", env=env,
    )
    # 中间进程会先打印它创建的子进程 PID
    line = proc.stdout.readline().strip()
    if not line.isdigit():
        proc.kill()
        raise AssertionError(f"中间进程未按约定打印子进程 PID: {line!r}")
    return proc, int(line)


def main() -> int:
    # 1) Job Object：父进程硬退出（os._exit，无任何清理）→ 子进程被内核终止
    middle, child_pid = run_middle(_MIDDLE_JOB)
    try:
        time.sleep(0.5)
        assert pid_alive(child_pid), "子进程应随父进程存活一段时间"
        assert wait_gone(child_pid, timeout=8), (
            f"父进程硬退出后，Job 内子进程 (pid={child_pid}) 未被终止"
        )
        middle.wait(timeout=5)
        print(f"PASS 1: 父进程硬退出（os._exit）→ Job 内子进程 (pid={child_pid}) 被内核终止")
    finally:
        if pid_alive(child_pid):
            subprocess.run(["taskkill", "/F", "/PID", str(child_pid)],
                           capture_output=True, timeout=10)

    # 2) 父进程看门狗：Job 之外的兜底——"父进程"消失后子进程自行退出
    middle2, victim_pid = run_middle(_MIDDLE_WATCHDOG)
    try:
        time.sleep(0.5)
        assert pid_alive(victim_pid) and pid_alive(middle2.pid)
        subprocess.run(["taskkill", "/F", "/PID", str(victim_pid)],
                       capture_output=True, timeout=10)
        assert wait_gone(middle2.pid, timeout=8), (
            "看门狗未在父进程消失后让子进程退出"
        )
        print(f"PASS 2: 父进程被强杀 → 子进程内看门狗触发跟随退出 (middle pid={middle2.pid})")
    finally:
        for pid in (victim_pid, middle2.pid):
            if pid_alive(pid):
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=10)

    print("\nALL OVERLAY PARENT EXIT TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
