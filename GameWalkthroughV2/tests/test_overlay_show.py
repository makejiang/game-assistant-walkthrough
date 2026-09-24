"""Offline test: overlay 网页区渲染子窗口监测（白屏修复）。

hidden=True 创建 + 直接显示的组合会让部分机器的 Chromium 不创建渲染子窗口
（Chrome_RenderWidgetHostHWND），网页区永久白屏（测试机实测）。验证：
  1. 渲染子窗口正常出现 -> 不做任何补偿；
  2. 渲染子窗口缺失 -> 执行“隐藏→显示”循环补偿，补偿后出现即停止；
  3. 网页区隐藏（自动隐藏/禁用）时不做补偿。

Run:  python tests/test_overlay_show.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):  # Windows 控制台默认 cp1252
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app import overlay_window

SW_HIDE = 0
SW_SHOW = 5


class FakeBar:
    """Tk 标题栏替身：after 只登记不执行，用 run_pending() 手动推进。"""

    def __init__(self) -> None:
        self.pending: list[tuple[int, object]] = []

    def after(self, delay: int, fn) -> None:
        self.pending.append((delay, fn))

    def run_pending(self) -> None:
        todo, self.pending = self.pending, []
        for _delay, fn in todo:
            fn()


class FakeUser32:
    """user32 替身：记录 ShowWindow 序列并模拟渲染子窗口的存在。"""

    def __init__(self, render_children: int, kick_creates: bool = True) -> None:
        self.render_children = render_children   # 当前渲染子窗口数
        self.kick_creates = kick_creates         # 隐藏→显示后是否出现渲染子窗口
        self.calls: list[str] = []

    def FindWindowW(self, _cls, _title):
        return 4321

    def ShowWindow(self, hwnd, cmd):
        self.calls.append("show" if cmd == SW_SHOW else "hide")
        if cmd == SW_SHOW and self.kick_creates:
            self.render_children = 1   # 显示后渲染子窗口出现（健康路径）

    def SetWindowPos(self, hwnd, after, x, y, cx, cy, flags):
        pass

    def IsWindow(self, hwnd):
        return True

    def EnumChildWindows(self, hwnd, callback, _lp):
        n = self.render_children
        for i in range(n):
            if not callback(9000 + i, 0):
                return False
        return True

    def GetClassNameW(self, hwnd, buf, max_len):
        buf.value = "Chrome_RenderWidgetHostHWND"
        return 27

    def GetWindowThreadProcessId(self, hwnd, out):
        if out:
            out.value = 1
        return 1


def make_app(u32: FakeUser32) -> overlay_window.OverlayApp:
    app = overlay_window.OverlayApp(22818, None)
    overlay_window.user32 = u32
    app._bar = FakeBar()
    app._body_hwnd = 4321
    app._shown = True
    app._body_visible = True
    return app


def test_no_kick_when_render_widget_present() -> None:
    u32 = FakeUser32(render_children=1)   # 渲染子窗口正常出现
    app = make_app(u32)
    app._monitor_render_widget()
    app._bar.run_pending()
    assert "hide" not in u32.calls, "渲染窗口正常时不应做隐藏→显示补偿"
    print("PASS 1: 渲染子窗口正常时不做补偿（无闪烁）")


def test_kick_when_render_widget_missing() -> None:
    u32 = FakeUser32(render_children=0, kick_creates=True)   # 白屏机器：缺失，补偿后出现
    app = make_app(u32)
    app._monitor_render_widget()
    assert u32.calls == ["hide"], "缺失时应先隐藏"
    app._bar.run_pending()   # 150ms 后的重新显示
    assert "show" in u32.calls, "隐藏后应重新显示"
    assert app._body_visible is True
    # 补偿后渲染子窗口出现：再次监测不再补偿
    before = u32.calls.count("hide")
    app._monitor_render_widget()
    app._bar.run_pending()
    assert u32.calls.count("hide") == before, "渲染窗口出现后应停止补偿"
    print("PASS 2: 渲染子窗口缺失时补偿（隐藏→显示），出现后停止")


def test_no_monitor_when_body_hidden() -> None:
    u32 = FakeUser32(render_children=0)
    app = make_app(u32)
    app._body_visible = False   # 自动隐藏/禁用态
    app._monitor_render_widget()
    app._bar.run_pending()
    assert u32.calls == [], "网页区隐藏时不应做补偿"
    print("PASS 3: 网页区隐藏时不做补偿")


def main() -> int:
    test_no_kick_when_render_widget_present()
    test_kick_when_render_widget_missing()
    test_no_monitor_when_body_hidden()
    print("ALL OVERLAY SHOW TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
