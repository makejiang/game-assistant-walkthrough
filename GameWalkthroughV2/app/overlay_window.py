"""左上角攻略浮窗：本地代码实现的细标题栏 + pywebview 无边框网页窗口。

结构（独立子进程运行：pywebview 与 Tk 都要求独占主线程，各占一条线程）：
  - 主线程：pywebview 无边框窗口（frameless=True，完全没有系统标题栏，也不
    会像 Chromium 应用窗口那样在客户区自绘一条标题栏），加载本机 webserver
    的查看器页面，内容与浏览器访问完全一致，常驻置顶。
  - Tk 线程：本地代码（非 HTML）实现的细标题栏，高 28px，背景色与页面顶栏
    一致（#14181f）。左侧显示当前展示的攻略章节名：后台线程每秒优先用
    evaluate_js 读页面目录下拉的选中项（随翻页跟手），读不到再退回 webserver
    /api/view 的推送章节；右侧只有一个“图钉”按钮，在「已置顶 / 自动隐藏」
    之间切换。浮窗默认停靠屏幕左上角，按住标题栏可拖到任意位置（图钉按钮
    照常点击）。

窗口管理：pywebview 只负责渲染；位置/尺寸/显示/隐藏/置顶全部经 ctypes
user32 对两个窗口直接控制（跨线程安全），两条线程各自只读写自己的界面状态。
显示时序：hidden=True 创建的窗口必须等 WebView2 初始化完成（页面加载完成）
后才能显示，并在首次显示时补一次“隐藏→显示”循环——否则 Chromium 不创建
渲染子窗口（Chrome_RenderWidgetHostHWND），网页区永久白屏（部分机器实测）。

图钉按钮语义：
  - 已置顶（默认）：浮窗常驻显示，不自动隐藏。
  - 自动隐藏：鼠标离开浮窗约 0.6 秒后向左滑出隐藏；鼠标移到屏幕左上角热区
    （8px 见方，固定位置，与浮窗大小和拖动后的停靠点无关）重新滑入；鼠标
    停在浮窗上时保持显示。

配置: config/overlay.json {"enabled": bool, "width": int, "height": int}
按内容对比热加载：width/height 是浮窗整体 CSS 逻辑尺寸（与浏览器模拟宽度
同义，标题栏 28 逻辑像素含在内），修改保存即生效；enabled=false 时浮窗
隐藏；加载失败沿用上次成功配置。

依赖: pywebview（Windows 上用 Edge WebView2 渲染，Win10/11 默认自带运行时）。
未安装 pywebview 或 WebView2 运行时时打印原因退出，不影响其它功能。

Agent 沙盒兼容: 模块导入时设置 no_proxy 回环免代理与
WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS（关 Chromium 沙箱，防 Agent 注入的
DLL 令 WebView2 白屏崩溃），见 app/env_setup.py。
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import winreg
from ctypes import wintypes
from pathlib import Path

from app.env_setup import ensure_no_proxy, ensure_webview2_args

# 必须在 webview 创建 WebView2 环境之前生效，模块导入时设置正是为此
ensure_no_proxy()
ensure_webview2_args()

BAR_HEIGHT = 28   # 细标题栏高度（逻辑像素）
HOT_CORNER = 8    # 屏幕左上角呼出热区（逻辑像素见方，呼出只认这一小块）
HIDE_DELAY = 0.6  # 未置顶时鼠标离开浮窗多少秒后自动隐藏
BG = "#14181f"    # 标题栏背景色：与页面顶栏一致
WINDOW_TITLE = "攻略浮窗"
DEFAULT_TITLE = "等待攻略推送"  # webserver 尚无推送章节时标题栏显示的文字
BODY_FIND_TIMEOUT = 20.0  # 等待 webview 窗口出现的最长时间（秒）

_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010
_SWP_NOZORDER = 0x0004
_HWND_TOPMOST = -1
_SW_SHOW = 5
_SW_HIDE = 0
_WM_CLOSE = 0x0010
_GA_ROOT = 2

# Win11 默认给顶层窗口画圆角和 1px 边框，会让浮窗与标题栏看起来不连贯
_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWA_BORDER_COLOR = 34
_DWMWCP_DONOTROUND = 1
_DWMWA_COLOR_NONE = 0xFFFFFFFE

_WEBVIEW2_RUNTIME_KEY = (
    r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
)

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi
user32.GetDpiForSystem.restype = ctypes.c_uint
user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
user32.GetAncestor.restype = ctypes.c_void_p


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long), ("top", ctypes.c_long),
        ("right", ctypes.c_long), ("bottom", ctypes.c_long),
    ]


user32.SetWindowPos.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, ctypes.c_uint,
]
user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.IsWindow.argtypes = [ctypes.c_void_p]
user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
user32.FindWindowW.restype = ctypes.c_void_p
user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT)]


class OverlayConfig:
    """config/overlay.json 热加载：{"enabled": bool, "width": int, "height": int}。

    每次轮询读文件内容并与上次对比（文件极小，开销可忽略）：按内容判断变化，
    同一秒内、等长内容的修改也能感知（mtime+size 方案会漏掉这种修改）；
    加载失败沿用上次成功配置。
    """

    DEFAULTS = {"enabled": True, "width": 320, "height": 640}

    def __init__(self, path: Path | None) -> None:
        self._path = Path(path) if path else None
        self._raw: bytes = b""
        self._data: dict = dict(self.DEFAULTS)

    def get(self) -> dict:
        if self._path is None:
            return dict(self._data)
        try:
            raw = self._path.read_bytes()
        except OSError:
            return dict(self._data)
        if raw == self._raw:
            return dict(self._data)
        try:
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("top-level must be an object")
            merged = dict(self.DEFAULTS)
            merged.update({k: data[k] for k in self.DEFAULTS if k in data})
            merged["enabled"] = bool(merged["enabled"])
            merged["width"] = max(200, int(merged["width"]))
            merged["height"] = max(300, int(merged["height"]))
            self._data = merged
            self._raw = raw
        except Exception:
            pass  # 加载失败：什么都不做，沿用上次成功配置
        return dict(self._data)


def set_dpi_aware() -> None:
    """本进程改用物理像素坐标（Tk 与 WinForms 两边坐标必须一致）。"""
    try:
        # PER_MONITOR_AWARE_V2；失败（旧系统/已设置）退回旧 API
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    with contextlib.suppress(Exception):
        user32.SetProcessDPIAware()


def dpi_scale() -> float:
    """系统缩放比（96 DPI = 1.0）。浮窗固定在主屏左上角，用系统 DPI 即可。"""
    try:
        return user32.GetDpiForSystem() / 96.0
    except Exception:
        return 1.0


def square_window(hwnd: int) -> None:
    """去掉 Win11 的窗口圆角与 1px 边框，让标题栏与网页区连成一个整体。

    旧系统不支持这些属性时会失败，忽略即可（本来就没有圆角）。
    """
    if not hwnd:
        return
    try:
        pref = ctypes.c_uint(_DWMWCP_DONOTROUND)
        dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd), _DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(pref), 4
        )
        color = ctypes.c_uint(_DWMWA_COLOR_NONE)
        dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd), _DWMWA_BORDER_COLOR, ctypes.byref(color), 4
        )
    except Exception:
        pass


def webview2_runtime_installed() -> bool:
    """Evergreen WebView2 运行时是否已注册（pywebview 的渲染后端依赖它）。"""
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(root, _WEBVIEW2_RUNTIME_KEY) as key:
                return bool(winreg.QueryValueEx(key, "pv")[0])
        except OSError:
            continue
    return False


def cursor_pos() -> tuple[int, int] | None:
    try:
        pt = _POINT()
        if user32.GetCursorPos(ctypes.byref(pt)):
            return int(pt.x), int(pt.y)
    except Exception:
        pass
    return None


def window_alive(hwnd: int) -> bool:
    return bool(hwnd) and bool(user32.IsWindow(hwnd))


class OverlayApp:
    """子进程主体：主线程跑 pywebview 渲染，Tk 线程负责标题栏与全部界面状态。"""

    def __init__(self, port: int, config_path: str | None) -> None:
        self._port = int(port)
        self._overlay_cfg = OverlayConfig(Path(config_path) if config_path else None)
        # webview 窗口标题带上 pid 保证全局唯一，FindWindow 精确匹配不误认
        self._body_title = f"{WINDOW_TITLE}-内容-{os.getpid()}"

        # ---- 以下状态只在 Tk 线程读写 ----
        self._bar = None            # Tk 标题栏窗口
        self._title_label = None
        self._pin_btn = None
        self._body_hwnd = 0         # webview 窗口句柄（找到后设置）
        self._body_visible = False
        self._scale = 1.0           # 系统 DPI 缩放比（run() 里测定）
        self._cfg_w = 320           # 配置的逻辑尺寸（CSS 像素，与浏览器一致）
        self._cfg_h = 640
        self._w = 320               # 浮窗整体物理尺寸（标题栏 + 网页区）
        self._h = 640
        self._bar_h = BAR_HEIGHT    # 标题栏物理高度
        self._hot = HOT_CORNER      # 呼出热区物理尺寸
        self._x = 0                 # 当前位置 x（滑动动画的现值；隐藏时为屏幕外负值）
        self._y = 0                 # 当前位置 y（无垂直动画，跟随拖动）
        self._target_x = 0          # 滑动动画目标 x
        self._home = (0, 0)         # 停靠位置：拖动后的落点，显示/呼出回到这里
        self._drag = None           # 拖动中：鼠标按下点相对窗口左上角的偏移
        self._shown = True          # 是否处于“显示”状态（相对滑出到屏幕外）
        self._pinned = True         # 图钉状态：True=常驻显示，False=自动隐藏
        self._enabled = True        # config/overlay.json 的 enabled
        self._last_want = 0.0       # 最近一次“鼠标想看到浮窗”的时刻
        self._animating = False
        self._closing = False
        self._body_search_start = 0.0  # 开始找 webview 窗口的时刻（Tk 线程设置）

        # 标题线程写入 / Tk 线程读取
        self._title_lock = threading.Lock()
        self._latest_title = ""     # /api/view 的推送章节（兜底）
        self._display_title = ""    # 解析后的展示章节：页面目录选中项优先
        self._stop = threading.Event()

        self._window = None         # pywebview 窗口对象（run() 里创建）

        # 渲染子窗口监测（白屏修复，见 _find_body/_monitor_render_widget）：
        # hidden=True 创建 + 直接显示时，部分机器的 Chromium 不创建渲染子窗口
        # （Chrome_RenderWidgetHostHWND），网页区永久白屏。显示后监测该子窗口，
        # 缺失即做一次“隐藏→显示”循环强制其创建（测试机实测有效）。
        self._render_monitor_pending = False
        self._render_kick_count = 0

    # -------------------------------------------------------------------- 启动
    def _set_size(self, cfg_w: int, cfg_h: int) -> None:
        """配置宽高按 CSS 逻辑像素解释（与浏览器 DevTools 的模拟宽度同义），
        乘以系统缩放比得到物理像素——这样任意缩放下浮窗里的页面都和浏览器
        模拟同宽所见即所得，文字大小一致。"""
        self._cfg_w, self._cfg_h = int(cfg_w), int(cfg_h)
        self._w = round(self._cfg_w * self._scale)
        self._h = round(self._cfg_h * self._scale)
        self._bar_h = round(BAR_HEIGHT * self._scale)
        self._hot = round(HOT_CORNER * self._scale)

    def run(self) -> int:
        set_dpi_aware()
        self._scale = dpi_scale()

        try:
            import webview
        except ImportError:
            print("[overlay] 未安装 pywebview，浮窗不可用（pip install pywebview）", file=sys.stderr)
            return 1
        if not webview2_runtime_installed():
            print("[overlay] 未安装 WebView2 Runtime，浮窗不可用"
                  "（https://developer.microsoft.com/microsoft-edge/webview2）", file=sys.stderr)
            return 1

        cfg = self._overlay_cfg.get()
        self._set_size(cfg["width"], cfg["height"])
        self._enabled = bool(cfg["enabled"])
        self._shown = self._enabled  # 默认已置顶，跟 enabled 一致
        self._x = self._target_x = 0 if self._shown else self._parked_x()
        self._last_want = time.monotonic()

        threading.Thread(target=self._tk_main, name="overlay-titlebar", daemon=True).start()

        # webview：无边框（无任何系统标题栏）、常驻置顶、禁用网页区拖动窗口，
        # 先隐藏，由 Tk 线程定位后再显示，避免闪现在错误位置。
        # background_color 用标题栏同色：WebView2 就绪前网页区画的是该底色，
        # 白色默认值会让初始化期间看起来像“白屏”。
        self._window = webview.create_window(
            self._body_title,
            f"http://127.0.0.1:{self._port}/",
            width=self._w,
            height=max(1, self._h - self._bar_h),
            x=0,
            y=self._bar_h,
            frameless=True,
            easy_drag=False,
            on_top=True,
            hidden=True,
            background_color=BG,
        )
        try:
            webview.start(gui="edgechromium")
        except Exception as exc:
            print(f"[overlay] webview 启动失败: {exc}", file=sys.stderr)
            os._exit(1)  # Tk 线程已在运行，直接结束子进程（原因已打印）

        self._closing = True  # webview 窗口被关闭（如任务管理器结束）时收尾退出
        self._stop.set()
        return 0

    # ---------------------------------------------------------------- 标题栏 UI
    def _tk_main(self) -> None:
        import tkinter as tk

        bar = tk.Tk()
        bar.title(WINDOW_TITLE)
        bar.withdraw()                      # 先藏起来，摆好位置再显示，避免闪现在默认位置
        bar.overrideredirect(True)          # 无系统边框：细标题栏由本窗口渲染
        bar.attributes("-topmost", True)
        bar.configure(bg=BG)
        self._bar = bar
        self._build_bar(tk)

        threading.Thread(target=self._poll_title, name="overlay-title", daemon=True).start()
        bar.after(200, self._find_body)     # 等 webview 窗口出现后接管其位置
        bar.after(30, self._tick_anim)      # 滑动动画
        bar.after(120, self._tick_pointer)  # 自动隐藏 / 左上角呼出
        bar.after(400, self._tick_config)   # 配置热加载 + webview 存活检查
        bar.after(500, self._tick_title)    # 章节名刷新
        bar.protocol("WM_DELETE_WINDOW", self._shutdown)

        self._apply_layout()
        # 此前的 withdraw 只为防闪现；这里一次性显示。运行期不再 withdraw——
        # 隐藏态就是停在屏幕外（x 为负），避免 Tk 重建窗口句柄的坑。
        bar.deiconify()
        bar.update_idletasks()
        # 标题栏同样去掉 Win11 圆角/边框（失败忽略：旧系统本来就没有）
        with contextlib.suppress(Exception):
            square_window(user32.GetAncestor(bar.winfo_id(), _GA_ROOT) or bar.winfo_id())
        bar.mainloop()

    def _build_bar(self, tk) -> None:
        s = self._scale
        # 字体用负数像素值（物理像素），高缩放屏上不会因 Tk 的 pt 换算失真
        font = ("Microsoft YaHei UI", -round(12 * s))
        inner = tk.Frame(self._bar, bg=BG)
        inner.pack(fill="both", expand=True)

        # 最右边：图钉按钮（唯一按钮）；左侧其余空间显示章节名
        self._pin_btn = tk.Button(
            inner, text="📌 已置顶", bd=0, bg=BG, fg="#ff8c1f",
            activebackground="#27313f", activeforeground="#ff8c1f",
            font=font, cursor="hand2",
            command=self._toggle_pin,
        )
        self._pin_btn.pack(side="right", padx=(0, round(6 * s)))
        self._title_label = tk.Label(
            inner, text=DEFAULT_TITLE, bg=BG, fg="#cfe0f5",
            font=font, anchor="w",
        )
        self._title_label.pack(side="left", fill="both", expand=True, padx=(round(8 * s), round(4 * s)))

        # 按住标题栏（空白处/章节名）拖动整个浮窗；图钉按钮不在绑定列表里，
        # 点击只触发按钮本身，不会拖动窗口
        def press(event) -> None:
            # 按点相对窗口左上角的偏移（用屏幕坐标算，与命中的具体控件无关，
            # 否则按在带内边距的 Label 上会差出 padding）
            self._drag = (event.x_root - self._x, event.y_root - self._y)
            self._target_x = self._x          # 打断进行中的滑动动画
            self._animating = False

        def motion(event) -> None:
            if self._drag is None:
                return
            # x_root/y_root 是屏幕物理坐标（进程已 DPI aware），减去按点偏移
            # 即窗口应到的左上角位置；标题栏与网页区一起动
            nx = event.x_root - self._drag[0]
            ny = event.y_root - self._drag[1]
            self._x, self._y = nx, ny
            self._home = (nx, ny)
            self._apply_layout()

        def release(_event) -> None:
            self._drag = None

        bar = self._bar
        for widget in (bar, inner, self._title_label):
            widget.configure(cursor="fleur")
            widget.bind("<Button-1>", press)
            widget.bind("<B1-Motion>", motion)
            widget.bind("<ButtonRelease-1>", release)

    def _toggle_pin(self) -> None:
        self._pinned = not self._pinned
        if self._pin_btn is not None:
            self._pin_btn.configure(
                text="📌 已置顶" if self._pinned else "📌 自动隐藏",
                fg="#ff8c1f" if self._pinned else "#8fa3c0",
            )
        self._last_want = time.monotonic()
        if self._pinned and self._enabled and not self._shown:
            self._shown = True
            self._slide_to(self._home[0])
        # 取消置顶不立即隐藏：鼠标离开浮窗 HIDE_DELAY 秒后由 _tick_pointer 收起

    # ------------------------------------------------------------ webview 控制
    def _find_body(self) -> None:
        if self._closing or self._bar is None:
            return
        hwnd = user32.FindWindowW(None, self._body_title)
        if hwnd:
            self._body_hwnd = int(hwnd)
            square_window(hwnd)  # 去圆角/边框：与标题栏连成一个整体窗口
            self._apply_layout()
            self._set_body_visible(self._shown)  # 按当前状态显示或保持隐藏
            if self._shown:
                # 启动渲染子窗口监测（部分机器显示后 Chromium 不创建渲染窗口
                # -> 白屏，见 _monitor_render_widget）
                self._bar.after(1000, self._monitor_render_widget)
            return
        if not self._body_search_start:
            self._body_search_start = time.monotonic()
        elif time.monotonic() - self._body_search_start > BODY_FIND_TIMEOUT:
            print("[overlay] 等待 webview 窗口超时，浮窗退出", file=sys.stderr)
            self._shutdown()
            return
        self._bar.after(200, self._find_body)

    def _set_body_visible(self, visible: bool) -> None:
        hwnd = self._body_hwnd
        if not hwnd:
            return
        try:
            user32.ShowWindow(hwnd, _SW_SHOW if visible else _SW_HIDE)
            self._body_visible = visible
            if visible:
                # 重新插入 TOPMOST 带，保证盖在游戏画面上；不抢焦点
                user32.SetWindowPos(
                    hwnd, _HWND_TOPMOST, 0, 0, 0, 0,
                    _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE,
                )
        except Exception:
            pass

    def _render_widget_count(self) -> int:
        """网页区窗口下 Chromium 渲染子窗口（Chrome_RenderWidgetHostHWND）的
        数量：为 0 说明渲染宿主未创建，页面内容永远不上屏（表现为白屏）。"""
        if not self._body_hwnd:
            return 0
        count = [0]

        def on_child(hwnd, _lparam):
            buf = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, buf, 64)
            if buf.value == "Chrome_RenderWidgetHostHWND":
                count[0] += 1
            return True

        CFUNC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        with contextlib.suppress(Exception):
            user32.EnumChildWindows(wintypes.HWND(self._body_hwnd), CFUNC(on_child), None)
        return count[0]

    def _monitor_render_widget(self) -> None:
        """显示后检查渲染子窗口：hidden=True 创建 + 直接显示的组合会让部分
        机器的 Chromium 不创建渲染宿主（网页区白屏，测试机实测）。缺失时做
        一次“隐藏→显示”循环强制其创建，出现后停止监测。"""
        if self._closing or self._bar is None or not self._body_visible:
            return
        if self._render_widget_count() > 0:
            return   # 渲染窗口已就位：无需处理
        if self._render_kick_count >= 4:
            print("[overlay] 渲染子窗口多次补偿后仍未创建，停止监测", file=sys.stderr)
            return
        self._render_kick_count += 1
        print(f"[overlay] 网页区渲染窗口缺失，执行隐藏→显示补偿（第 {self._render_kick_count} 次）",
              file=sys.stderr)
        hwnd = self._body_hwnd
        with contextlib.suppress(Exception):
            user32.ShowWindow(hwnd, _SW_HIDE)
        self._bar.after(150, lambda: self._set_body_visible(True))
        self._bar.after(2500, self._monitor_render_widget)

    def _apply_layout(self) -> None:
        """把标题栏与 webview 摆到当前位置（只在 Tk 线程调用）。"""
        bar = self._bar
        if bar is not None:
            with contextlib.suppress(Exception):
                bar.geometry(f"{self._w}x{self._bar_h}+{self._x}+{self._y}")
        hwnd = self._body_hwnd
        if hwnd:
            try:
                user32.SetWindowPos(
                    hwnd, 0,
                    self._x, self._y + self._bar_h,
                    self._w, max(1, self._h - self._bar_h),
                    _SWP_NOZORDER | _SWP_NOACTIVATE,
                )
            except Exception:
                pass

    # -------------------------------------------------------------- 周期任务
    # 读页面当前展示章节：目录下拉的选中项（页面随翻页/跳章自动同步它）。
    # value 非空才是真实目录项（占位项“目录列表”的 value 是空串）。
    _PAGE_CHAPTER_JS = (
        "(function(){var s=document.getElementById('tocSelect');"
        "if(s&&s.selectedIndex>=0){var o=s.options[s.selectedIndex];"
        "if(o&&o.value)return o.text;}"
        "return '';})()"
    )

    def _fetch_title(self, requests, url: str) -> None:
        """拉取一次 /api/view 的推送章节名；失败时什么都不做，沿用上次标题。"""
        try:
            resp = requests.get(url, timeout=2)
            title = str(resp.json().get("title") or "")
        except Exception:
            return  # webserver 未就绪/重启中：沿用上次标题
        with self._title_lock:
            self._latest_title = title

    def _poll_title(self) -> None:
        """后台线程：解析标题栏应显示的章节，每秒一次。

        优先读页面目录下拉的选中项——它是页面自己维护的“当前展示章节”，
        用户翻页/跳章时跟手，且不依赖推送是否携带 section 字段；页面给不出
        （目录未加载/还在跳转）时退回 /api/view 的推送章节名。
        """
        try:
            import requests
        except ImportError:
            return
        url = f"http://127.0.0.1:{self._port}/api/view"
        while not self._stop.wait(1.0):
            self._fetch_title(requests, url)
            page_title = ""
            window = self._window
            if window is not None:
                try:
                    r = window.evaluate_js(self._PAGE_CHAPTER_JS)
                    if isinstance(r, str):
                        page_title = r.strip()
                except Exception:
                    pass  # 页面未就绪/正在导航：本轮用推送章节兜底
            with self._title_lock:
                self._display_title = page_title or self._latest_title

    def _tick_title(self) -> None:
        if self._closing or self._bar is None:
            return
        with self._title_lock:
            title = self._display_title
        text = title.strip() or DEFAULT_TITLE
        if text != self._title_label.cget("text"):
            self._title_label.configure(text=text)
        self._bar.after(500, self._tick_title)

    def _parked_x(self) -> int:
        # 隐藏位固定在屏幕左侧外（不管窗口被拖到哪里，都从停靠点向左滑出屏幕）
        return -(self._w + round(20 * self._scale))

    def _want_visible(self, cur: tuple[int, int] | None) -> bool:
        """置顶常驻；未置顶时：呼出（隐藏→显示）只认屏幕左上角热区这一小块，
        与浮窗大小、拖动后的停靠点无关（否则浮窗占半屏时鼠标扫过那半边就会
        把它唤出来）；已显示时鼠标停在浮窗上则保持显示，离开 HIDE_DELAY 秒
        后收起。"""
        if not self._enabled:
            return False
        if self._pinned:
            return True
        if cur is None:
            return False
        cx, cy = cur
        if cx <= self._hot and cy <= self._hot:
            return True
        if not self._shown:
            return False  # 隐藏中：只有热区能呼出
        pad = round(4 * self._scale)
        return (-pad <= cx <= self._x + self._w + pad
                and -pad <= cy <= self._y + self._h + pad)

    def _tick_pointer(self) -> None:
        if self._closing or self._bar is None:
            return
        now = time.monotonic()
        want = self._want_visible(cursor_pos())
        if want:
            self._last_want = now
        if want and not self._shown:
            self._shown = True
            self._slide_to(self._home[0])
        elif not want and self._shown and now - self._last_want > HIDE_DELAY:
            self._shown = False
            self._slide_to(self._parked_x())
        self._bar.after(120, self._tick_pointer)

    def _slide_to(self, target_x: int) -> None:
        self._target_x = target_x
        if self._x == target_x:
            self._finish_slide()
            return
        if not self._animating:
            self._animating = True
            if target_x == self._home[0]:
                self._set_body_visible(True)  # 先恢复显示再滑入
            self._tick_anim()

    def _tick_anim(self) -> None:
        if self._closing or self._bar is None:
            return
        if self._x == self._target_x:
            self._animating = False
            self._finish_slide()
            return
        step = max(24, self._w // 10)
        if abs(self._target_x - self._x) <= step:
            self._x = self._target_x
        else:
            self._x += step if self._target_x > self._x else -step
        self._apply_layout()
        self._bar.after(16, self._tick_anim)

    def _finish_slide(self) -> None:
        # 滑出完成后把 webview 真正藏起来（任务栏/截屏都不再出现）。
        # 标题栏不做 withdraw/deiconify：Tk 对 overrideredirect 窗口反复
        # 隐藏/重显会重建窗口句柄（topmost 等属性随之丢失），停到屏幕外即可。
        if not self._shown and self._x == self._parked_x() and self._body_visible:
            self._set_body_visible(False)

    def _tick_config(self) -> None:
        """config/overlay.json 热加载 + webview 存活检查；失败沿用上次配置。"""
        if self._closing or self._bar is None:
            return
        cfg = self._overlay_cfg.get()
        if bool(cfg["enabled"]) != self._enabled:
            self._enabled = bool(cfg["enabled"])
            self._last_want = time.monotonic()
            if not self._enabled and self._shown:
                self._shown = False
                self._slide_to(self._parked_x())
            # 重新启用由 _tick_pointer 按图钉/鼠标状态自然恢复
        size = (int(cfg["width"]), int(cfg["height"]))
        if size != (self._cfg_w, self._cfg_h):
            self._set_size(*size)
            if not self._shown:
                self._x = self._target_x = self._parked_x()
            self._apply_layout()
        if self._body_hwnd and not window_alive(self._body_hwnd):
            # webview 窗口被外部关闭（如 Alt+F4 / 任务管理器）：整个浮窗退出，
            # 主线程的 webview.start() 也会随之返回
            print("[overlay] webview 窗口已关闭，浮窗退出", file=sys.stderr)
            self._shutdown()
            return
        self._bar.after(400, self._tick_config)

    # -------------------------------------------------------------------- 退出
    def _shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._stop.set()
        if self._body_hwnd:
            with contextlib.suppress(Exception):
                user32.PostMessageW(self._body_hwnd, _WM_CLOSE, None, None)
        if self._bar is not None:
            with contextlib.suppress(Exception):
                self._bar.destroy()


def main() -> int:
    parser = argparse.ArgumentParser(prog="app.overlay_window", description="左上角攻略浮窗")
    parser.add_argument("--port", type=int, required=True, help="webserver 端口")
    parser.add_argument("--config", default="", help="overlay.json 路径")
    parser.add_argument("--parent-pid", type=int, default=0,
                        help="客户端主进程 PID：父进程退出时浮窗跟随退出")
    args = parser.parse_args()
    if args.parent_pid:
        # 看门狗线程：父进程消失即自行退出（Job Object 分配失败时的兜底）
        threading.Thread(
            target=watch_parent_exit, args=(args.parent_pid,),
            name="overlay-parent-watchdog", daemon=True,
        ).start()
    return OverlayApp(args.port, args.config or None).run()


# ── 子进程随父进程退出 ────────────────────────────────────────────────────────
# 浮窗是独立子进程：客户端被强杀（taskkill /F、崩溃、agent 直接按 PID 结束）时
# 优雅清理（OverlayWindow.stop）不会执行，浮窗会残留在桌面。两道机制保证
# "父进程死 → 浮窗死"：
#   1) Job Object（内核级，app/winprocess.py）：子进程放进 KILL_ON_JOB_CLOSE 的
#      Job，父进程退出时句柄被内核关闭，Job 内所有进程（含 WebView2 子进程）
#      随之终止；
#   2) 子进程内父进程看门狗：等父进程句柄，父进程消失即自行退出——兜底 Job
#      分配失败的场景（如旧系统不支持嵌套 Job）。

from app.winprocess import assign_kill_on_close, close_job_handle  # noqa: E402,F401

_SYNCHRONIZE = 0x00100000
_INFINITE = 0xFFFFFFFF


def watch_parent_exit(parent_pid: int) -> None:
    """子进程内看门狗：父进程消失（句柄被内核触发）就立即退出自己。"""
    if sys.platform != "win32" or int(parent_pid) <= 0:
        return
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    handle = kernel32.OpenProcess(_SYNCHRONIZE, False, int(parent_pid))
    if not handle:
        return  # 打不开（权限不足/父进程早已退出）→ 交由 Job Object 兜底
    try:
        kernel32.WaitForSingleObject(handle, _INFINITE)
    finally:
        kernel32.CloseHandle(handle)
    # 父进程已退出：强制结束自己（webview 主循环无法优雅收尾也没关系）
    print("[overlay] 客户端主进程已退出，浮窗跟随退出", file=sys.stderr, flush=True)
    os._exit(0)


class OverlayWindow:
    """父进程侧控制器：以子进程方式启动浮窗（pywebview 需要主线程）。"""

    def __init__(self, config, log=None) -> None:
        self._config = config
        self._log = log or (lambda m: print(m, file=sys.stderr))
        self._proc: subprocess.Popen | None = None
        self._job_handle: int | None = None

    def start(self) -> None:
        if not getattr(self._config, "overlay_enabled", True):
            self._log("[overlay] 已通过 --no-overlay 禁用")
            return
        args = [
            sys.executable, "-m", "app.overlay_window",
            "--port", str(self._config.webserver_port),
            "--parent-pid", str(os.getpid()),  # 看门狗：父进程退出则跟随退出
        ]
        cfg_path = getattr(self._config, "overlay_config_file", None)
        if cfg_path:
            args += ["--config", str(cfg_path)]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._proc = subprocess.Popen(
                args, cwd=str(self._config.project_root), creationflags=creationflags
            )
        except Exception as exc:
            self._log(f"[overlay] 浮窗子进程启动失败: {exc}")
            return
        # 内核级兜底：放进"父进程退出即全灭"的 Job（客户端被强杀时也生效）
        self._job_handle = assign_kill_on_close(self._proc)
        if self._job_handle is None:
            self._log("[overlay] Job Object 不可用，浮窗仅靠进程内看门狗跟随退出")
        # 子进程的 stdout/stderr 继承父进程控制台：缺 pywebview/WebView2 等错误可见
        self._log("[overlay] 浮窗子进程已启动（缺依赖时会打印原因并退出）")

    def stop(self) -> None:
        if self._proc is not None:
            with contextlib.suppress(Exception):
                self._proc.terminate()
            self._proc = None
        close_job_handle(self._job_handle)  # 正常收尾：子进程已终止，还掉句柄
        self._job_handle = None


if __name__ == "__main__":
    sys.exit(main())
