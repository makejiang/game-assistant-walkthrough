#!/usr/bin/env python3
"""GUI 弹窗验证：倒计时自动选默认项、按钮选择、GitHub 警示变体。
CONFIRM_COUNTDOWN_SECONDS 调短为 2s。全部通过输出 ALL_OK。"""
import sys, io, time, logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)
import deploy
deploy.CONFIRM_COUNTDOWN_SECONDS = 2

import tkinter as tk

# 捕获弹窗选择日志（deploy logger）
class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []
    def emit(self, record):
        self.lines.append(record.getMessage())

_cap = _Capture()
_cap.setLevel(logging.INFO)
_logger = logging.getLogger("deploy")
_logger.setLevel(logging.INFO)   # 默认 NOTSET 会继承 root 的 WARNING，INFO 不到 handler
_logger.addHandler(_cap)
def choice_lines():
    return [ln for ln in _cap.lines if ln.startswith("[弹窗选择]")]
def clear_cap():
    _cap.lines.clear()

results = []
def check(name, cond):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name)

def pump_until(root, cond, timeout=6.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        root.update()
        if cond():
            return True
        time.sleep(0.02)
    return cond()

SUMMARY = {
    "service": {"label": "v1.1.1", "sha8": "abcd1234", "size_s": "381 MB",
                "transport": "direct"},
    "old_service_sha256": "00000000",
    "models": [{"name": "LLM", "repo_id": "FakeOrg/llm", "size_s": "7 GB",
                "reason": "仓库内容有更新"}],
    "service_running": True,
    "inflight_prev": False,
}

root = tk.Tk()
root.withdraw()

# 1) 更新确认窗：倒计时超时 → 默认「暂不更新」
dlg = deploy.UpdateConfirmDialog(root, SUMMARY)
check("更新窗默认按钮初始文案含倒计时",
      "2s" in str(dlg._btn_secondary["text"]))
gone = pump_until(root, lambda: not bool(dlg.win.winfo_exists()))
check("更新窗倒计时超时自动选「暂不更新」", gone and dlg.result is False)
check("日志区分：倒计时超时自动选择", choice_lines() == [
      "[弹窗选择] 倒计时超时，自动选择默认项：暂不更新"])

# 2) 更新确认窗：点「立即更新」→ True，倒计时停表
clear_cap()
dlg2 = deploy.UpdateConfirmDialog(root, SUMMARY)
root.update()
dlg2._btn_primary.invoke()
root.update()
check("更新窗点「立即更新」→ True", dlg2.result is True
      and not dlg2.win.winfo_exists())
check("倒计时已停表", dlg2._cd_stopped)
check("日志区分：用户主动点击「立即更新」", choice_lines() == [
      "[弹窗选择] 用户主动点击：立即更新"])

# 3) GitHub 变体：警示文案 + 默认仍为「暂不更新」
clear_cap()
GHSUM = dict(SUMMARY, github={"tag": "v1.1.1", "size": 381 * 1024 * 1024})
dlg3 = deploy.UpdateConfirmDialog(root, GHSUM)
texts = " ".join(str(c["text"]) for c in dlg3.inner.winfo_children()
                 if str(c.winfo_class()) == "Label")
check("GitHub 变体含慢速警示文案", "速度有较大概率很慢" in texts)
check("GitHub 变体按钮文案", "GitHub" in str(dlg3._btn_primary["text"]))
dlg3._btn_primary.invoke()   # 点「仍要用 GitHub 下载」
root.update()
check("GitHub 变体主动点击→True", dlg3.result is True)
check("日志区分：用户主动点击「仍要用 GitHub 下载」", choice_lines() == [
      "[弹窗选择] 用户主动点击：仍要用 GitHub 下载"])

# 3b) GitHub 变体：关闭窗口 = 按默认项处理
clear_cap()
dlg3b = deploy.UpdateConfirmDialog(root, GHSUM)
root.update()
dlg3b._on_window_close()
root.update()
check("GitHub 变体关闭窗口→False", dlg3b.result is False)
check("日志区分：关闭窗口按默认项处理", choice_lines() == [
      "[弹窗选择] 用户关闭窗口，按默认项处理：暂不更新"])

# 4) 安装方式选择窗：倒计时超时 → 默认「自动下载并安装」
clear_cap()
dlg4 = deploy.InstallChoiceDialog(root, r"D:\MyDir")
check("安装窗默认按钮初始文案含倒计时",
      "2s" in str(dlg4._btn_auto["text"]))
gone4 = pump_until(root, lambda: not bool(dlg4.win.winfo_exists()))
check("安装窗倒计时超时自动选「自动下载」", gone4 and dlg4.result == "auto")
check("日志区分：倒计时超时自动选择安装方式", choice_lines() == [
      "[弹窗选择] 倒计时超时，自动选择默认项：自动下载并安装"])

# 5) 安装方式选择窗：点「取消」→ cancel
clear_cap()
dlg5 = deploy.InstallChoiceDialog(root, r"D:\MyDir")
root.update()
dlg5._btn_cancel.invoke()
root.update()
check("安装窗点「取消」→ cancel", dlg5.result == "cancel")
check("日志区分：用户主动点击「取消部署」", choice_lines() == [
      "[弹窗选择] 用户主动点击：取消部署"])

# 6) 更新窗「暂不更新」手动点击
clear_cap()
dlg6 = deploy.UpdateConfirmDialog(root, SUMMARY)
root.update()
dlg6._btn_secondary.invoke()
root.update()
check("更新窗手动点「暂不更新」→ False", dlg6.result is False)
check("日志区分：用户主动点击「暂不更新」", choice_lines() == [
      "[弹窗选择] 用户主动点击：暂不更新"])

root.destroy()
passed = sum(results)
print(f"\n{passed}/{len(results)} 通过")
print("ALL_OK" if passed == len(results) else "HAS_FAILURES")
sys.exit(0 if passed == len(results) else 1)
