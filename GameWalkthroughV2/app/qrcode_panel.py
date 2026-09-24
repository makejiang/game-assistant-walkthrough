"""扫码访问面板：鼠标移到屏幕右上角时弹出二维码 + 局域网访问地址。

设计:
  - 默认完全隐藏。后台线程轮询全局光标位置（Windows 用 GetCursorPos，其它平台
    退回 Tk 查询），光标进入主屏右上角热区时面板从右侧滑入；光标离开热区且
    不在面板上时自动收回（滑出后窗口隐藏）。
  - 二维码内容为 http://<局域网IP>:<端口>。这是标准 URL 二维码，安卓 / iOS /
    鸿蒙的系统相机均可直接识别并打开浏览器，无需按平台额外适配。
  - 二维码由可选依赖 `qrcode` 生成（pip install qrcode）；未安装时仍显示
    地址文本与安装提示，不影响其它功能。
"""

from __future__ import annotations

import contextlib
import io
import sys
import threading
import time
import tkinter as tk
from typing import Callable

from app.netinfo import lan_adapters, lan_ipv4_addresses, lan_url

MAX_QR_CODES = 2  # 面板上最多同时展示的二维码数量（双网卡场景）

# 全屏展示配色：纯黑底铺满整屏 + 白色文字；二维码本身用标准白底黑码
# （兼容性最好，任何手机/扫码 App 都能识别），像一张白卡嵌在黑屏上。
FS_BG = "#000000"
FS_FG = "#FFFFFF"
FS_QR_FG = "#000000"  # 二维码模块颜色（标准黑）
FS_QR_BG = "#FFFFFF"  # 二维码底色（标准白，含静区）
FS_EDGE_PX = 16  # 全屏二维码离屏幕边的距离（等比例放大到该极限）


def build_qr_png(
    text: str,
    *,
    box_size: int = 6,
    border: int = 2,
    fill_color: str = "black",
    back_color: str = "white",
) -> bytes | None:
    """Encode `text` as a QR PNG; returns None when qrcode/Pillow are unusable."""
    try:
        import qrcode

        qr = qrcode.QRCode(
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=max(2, int(box_size)),
            border=max(1, int(border)),
        )
        qr.add_data(text)
        qr.make(fit=True)
        image = qr.make_image(fill_color=fill_color, back_color=back_color)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception:
        return None


def in_hot_corner(pos: tuple[int, int], screen_width: int, zone_px: int) -> bool:
    """Whether `pos` sits inside the top-right hot corner of the primary screen."""
    x, y = pos
    zone = max(1, int(zone_px))
    return screen_width - zone <= x <= screen_width + zone and y <= zone


def cursor_position(root: tk.Misc | None = None) -> tuple[int, int] | None:
    """Global cursor position. Windows uses GetCursorPos; elsewhere fall back to Tk."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            point = wintypes.POINT()
            if ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
                return int(point.x), int(point.y)
        except Exception:
            pass
    if root is None:
        return None
    try:
        return int(root.winfo_pointerx()), int(root.winfo_pointery())
    except Exception:
        return None


class QrcodePanel:
    """Borderless top-right panel showing the QR code and the LAN access URL.

    UI work happens on the Tk thread (via `after` marshalling, same as
    ToastNotifier); only plain ints/bytes are shared with the watcher thread.
    """

    FONT_TITLE = ("Microsoft YaHei UI", 11, "bold")
    FONT_URL = ("Consolas", 10, "bold")
    FONT_HINT = ("Microsoft YaHei UI", 8)

    WIDTH = 236
    SCREEN_REFRESH_SECONDS = 5.0  # 分辨率变化后热区位置也能跟进
    ACCESS_REFRESH_SECONDS = 2.0  # 展示期间复查访问地址的间隔（SSH 隧道上线/断开时就地切换）

    def __init__(
        self,
        root: tk.Tk,
        *,
        port: int,
        margin: int = 16,
        hotzone_px: int = 16,
        poll_interval_ms: int = 80,
        hide_delay_ms: int = 500,
        y_provider: Callable[[], int] | None = None,
        public_url_provider: Callable[[], str | None] | None = None,
    ) -> None:
        self._root = root
        self._port = int(port)
        self._margin = max(4, int(margin))
        self._hotzone = max(4, int(hotzone_px))
        self._poll_ms = max(30, int(poll_interval_ms))
        self._hide_delay_ms = max(100, int(hide_delay_ms))
        self._y_provider = y_provider
        # SSH 隧道公网地址提供方（app.ssh_tunnel）：返回非空 URL 时二维码优先
        # 展示公网地址（外网可访问），返回空/None 时回退局域网地址
        self._public_url_provider = public_url_provider

        self._bg = "#0f141d"
        self._fg = "#e8f1ff"
        self._body_fg = "#c9d8ee"
        self._border = "#3f4f6a"

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        self._win: tk.Toplevel | None = None
        self._body: tk.Frame | None = None
        self._photo: object | None = None  # keep the PhotoImage alive
        self._after_ids: list[str] = []

        # watcher-thread state
        self._visible = False
        self._leave_since: float | None = None

        # shared state (guarded by _lock where two threads touch it)
        # _info_key：watcher 线程已算好的地址缓存（避免周期复查重复生成二维码）；
        # _built_key：窗口当前真正显示的内容。两者必须分开——缓存先行更新时，
        # 不能让 UI 侧误以为"界面已经是新内容"而跳过重建。
        self._info_key: str | None = None
        self._built_key: str | None = None
        self._entries: list[tuple[str, str, bytes | None]] = []  # (完整URL, 标签, png)
        self._extras: list[str] = []

        # panel geometry for the watcher's "cursor over the panel" check
        self._rect: tuple[int, int, int, int] | None = None
        self._screen_width = int(root.winfo_screenwidth())

        # 全屏展示（电视/大荧幕远距离扫码）：独立 Toplevel，热角自动呼出/自动
        # 隐藏在其间暂停；退出后面板恢复并给一小段宽免自动收回
        self._fs_win: tk.Toplevel | None = None
        self._fs_photo: object | None = None  # keep the PhotoImage alive
        self._fullscreen = False
        self._suppress_hide_until = 0.0
        # 多网卡轮播：全屏时保存全部条目与当前页，左右切换重绘
        self._fs_entries: list[tuple[str, str, bytes | None]] = []
        self._fs_index = 0
        self._fs_qr_label: tk.Label | None = None
        self._fs_text_panel: tk.Frame | None = None

    # ------------------------------------------------------------------ public
    def start(self) -> None:
        """Start polling the cursor for the hot corner. Called once."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._watch, name="qr-hotcorner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._cancel_animations()
        win = self._win
        self._win = None
        if win is not None:
            with contextlib.suppress(Exception):
                if win.winfo_exists():
                    win.destroy()
        fs_win = self._fs_win
        self._fs_win = None
        self._fullscreen = False
        if fs_win is not None:
            with contextlib.suppress(Exception):
                if fs_win.winfo_exists():
                    fs_win.destroy()
        self._rect = None

    # ---------------------------------------------------------------- watcher
    def _watch(self) -> None:
        last_screen_refresh = 0.0
        last_access_refresh = 0.0
        while not self._stop_event.wait(self._poll_ms / 1000):
            now = time.monotonic()
            if now - last_screen_refresh >= self.SCREEN_REFRESH_SECONDS:
                last_screen_refresh = now
                self._refresh_screen_size_from_watcher()
            try:
                pos = cursor_position(self._root)
            except Exception:
                continue
            if pos is None:
                continue
            corner = in_hot_corner(pos, self._screen_width, self._hotzone)
            over = self._over_panel(pos)
            visible = self._visible
            if corner and not visible and not self._fullscreen:
                # claim visibility here so the fast poll loop shows it only once
                self._visible = True
                self._leave_since = None
                last_access_refresh = now
                entries, extras = self._access_info()
                self._after_ui(self._show_ui, entries, extras)
            elif visible and not corner and not over:
                if now < self._suppress_hide_until:
                    self._leave_since = None  # 全屏退出后的宽限期：不立即自动收回
                elif self._leave_since is None:
                    self._leave_since = time.monotonic()
                elif time.monotonic() - self._leave_since >= self._hide_delay_ms / 1000:
                    self._leave_since = None
                    self._visible = False
                    self._after_ui(self._hide_ui)
            else:
                self._leave_since = None

            # 展示期间定期复查访问地址：SSH 隧道在面板开着时才上线/断开也能切过来
            if self._visible and now - last_access_refresh >= self.ACCESS_REFRESH_SECONDS:
                last_access_refresh = now
                entries, extras = self._access_info()
                self._after_ui(self._refresh_ui, entries, extras)

    def _access_info(self) -> tuple[list[tuple[str, str, bytes | None]], list[str]]:
        """Resolve access URLs (and their QRs) on the watcher thread; cached by content.

        条目为 (完整URL, 标签, 二维码png)。SSH 隧道公网地址可用时优先展示（手机
        不在同一局域网也能访问）；未生效时回退局域网地址。双网卡（如有线+无线）
        时返回前 MAX_QR_CODES 个地址的二维码（标注网卡名），其余以文本列出。
        内容没变化时直接返回缓存（周期复查不重复生成二维码）。
        """
        public_url = ""
        provider = self._public_url_provider
        if provider is not None:
            with contextlib.suppress(Exception):
                public_url = str(provider() or "").strip()

        adapters = lan_adapters()
        multi = len(adapters) > 1
        if public_url:
            urls: list[tuple[str, str]] = [(public_url, "公网访问 · SSH穿透")]
            extras: list[str] = []
            box_size = 6
        else:
            urls = [
                (f"http://{ip}:{self._port}", label)
                for ip, label in adapters[:MAX_QR_CODES]
            ]
            extras = [ip for ip, _ in adapters[MAX_QR_CODES:]]
            box_size = 4 if multi else 6  # 并排时二维码缩小（box_size 4），单个时用大码

        key = repr(urls)
        with self._lock:
            if key == self._info_key:
                return self._entries, self._extras

        entries: list[tuple[str, str, bytes | None]] = [
            (url, label, build_qr_png(url, box_size=box_size)) for url, label in urls
        ]
        with self._lock:
            self._info_key = key
            self._entries = entries
            self._extras = extras
        if not entries:
            entries = [("", "", None)]  # 无局域网地址：提示本机兜底
        return entries, extras

    def _over_panel(self, pos: tuple[int, int]) -> bool:
        rect = self._rect
        if rect is None:
            return False
        pad = 8
        x, y = pos
        rx, ry, rw, rh = rect
        return rx - pad <= x <= rx + rw + pad and ry - pad <= y <= ry + rh + pad

    def _after_ui(self, fn: Callable[..., None], *args) -> None:
        def runner() -> None:
            if not self._stop_event.is_set():
                with contextlib.suppress(Exception):
                    fn(*args)

        with contextlib.suppress(Exception):
            self._root.after(0, runner)

    # --------------------------------------------------------------------- UI
    def _show_ui(self, entries: list, extras: list[str]) -> None:
        if self._stop_event.is_set():
            return
        self._refresh_screen_size()  # running on the UI thread -> query directly
        key = repr([(ip, label) for ip, label, _ in entries])
        if self._win is None or not self._win.winfo_exists():
            self._win = self._build_window()
            with self._lock:
                self._built_key = None  # fresh window always needs its content
        if self._body is None or self._built_key != key:
            self._fill_content(entries, extras)
        win = self._win
        win.update_idletasks()
        width = max(self.WIDTH, win.winfo_reqwidth())
        height = win.winfo_reqheight()
        y = self._panel_y(height)
        target_x = self._screen_width - width - self._margin
        self._rect = (target_x, y, width, height)
        win.geometry(f"{width}x{height}+{self._screen_width + 8}+{y}")
        win.deiconify()
        win.lift()
        self._animate_x(win, width, height, self._screen_width + 8, target_x)

    def _refresh_ui(self, entries: list, extras: list[str]) -> None:
        """面板已显示时访问地址变化（如隧道上线：局域网 -> 公网）的就地刷新。

        与 _show_ui 的区别：不把窗口挪回屏外重播滑入动画，只重建内容并按新
        尺寸原位调整位置，避免面板开着时肉眼可见的闪动。
        """
        if self._stop_event.is_set():
            return
        win = self._win
        if win is None or not win.winfo_exists():
            return
        key = repr([(url, label) for url, label, _ in entries])
        if self._body is not None and self._built_key != key:
            self._fill_content(entries, extras)
            self._refresh_screen_size()  # UI 线程：直接查
            win.update_idletasks()
            width = max(self.WIDTH, win.winfo_reqwidth())
            height = win.winfo_reqheight()
            y = self._panel_y(height)
            target_x = self._screen_width - width - self._margin
            self._rect = (target_x, y, width, height)
            with contextlib.suppress(Exception):
                win.geometry(f"{width}x{height}+{target_x}+{y}")

    def _hide_ui(self) -> None:
        win = self._win
        if win is None or not win.winfo_exists():
            return
        try:
            width = max(self.WIDTH, win.winfo_width())
            height = max(1, win.winfo_height())
            start_x = win.winfo_x()
        except Exception:
            win.withdraw()
            return
        self._animate_x(
            win, width, height, start_x, self._screen_width + width + 8,
            on_done=self._finish_hide,
        )

    def _finish_hide(self) -> None:
        win = self._win
        if win is None:
            return
        with contextlib.suppress(Exception):
            if win.winfo_exists():
                win.withdraw()
        self._rect = None

    def _build_window(self) -> tk.Toplevel:
        win = tk.Toplevel(self._root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.withdraw()  # shown only once positioned off-screen -> no flash at 0,0
        win.configure(bg=self._bg)
        frame = tk.Frame(win, bg=self._bg, highlightbackground=self._border, highlightthickness=1)
        frame.pack(fill="both", expand=True, padx=1, pady=1)
        self._body = tk.Frame(frame, bg=self._bg)
        self._body.pack(fill="both", expand=True)
        return win

    def _fill_content(self, entries: list, extras: list[str]) -> None:
        body = self._body
        if body is None:
            return
        for child in body.winfo_children():
            with contextlib.suppress(Exception):
                child.destroy()

        multi = len(entries) > 1
        wraplength = self.WIDTH - 28

        header = tk.Frame(body, bg=self._bg)
        header.pack(fill="x", padx=14, pady=(10, 2))
        tk.Label(
            header,
            text="手机扫码查看攻略",
            bg=self._bg,
            fg=self._fg,
            font=self.FONT_TITLE,
        ).pack(side="left")
        tk.Button(
            header,
            text="⛶ 全屏",
            command=self._enter_fullscreen,
            bg=self._bg,
            fg=self._body_fg,
            activebackground=self._bg,
            activeforeground=self._fg,
            relief="flat",
            bd=0,
            highlightthickness=0,
            font=self.FONT_HINT,
            cursor="hand2",
        ).pack(side="right")

        try:
            from PIL import ImageTk
        except ImportError:
            ImageTk = None  # type: ignore[assignment]

        def qr_label(parent: tk.Misc, png: bytes | None) -> None:
            if png is not None and ImageTk is not None:
                try:
                    photo = ImageTk.PhotoImage(data=png)
                    photos.append(photo)
                    tk.Label(parent, image=photo, bg=self._bg, bd=0).pack(pady=(2, 2))
                    return
                except Exception:
                    pass
            photos.append(None)
            text = "二维码不可用" if multi else "二维码不可用\n执行 pip install qrcode 启用"
            tk.Label(
                parent, text=text, bg=self._bg, fg=self._body_fg,
                font=self.FONT_HINT, justify="center",
            ).pack(pady=(2, 2))

        photos = []
        if multi:
            # 并排布局：每个单元 = 网卡名 + 二维码 + 访问地址
            row = tk.Frame(body, bg=self._bg)
            row.pack(pady=(2, 0))
            for url, label, png in entries:
                cell = tk.Frame(row, bg=self._bg)
                cell.pack(side="left", padx=5, expand=True)
                if label:
                    tk.Label(
                        cell, text=label, bg=self._bg, fg=self._fg,
                        font=self.FONT_HINT,
                    ).pack()
                qr_label(cell, png)
                tk.Label(
                    cell,
                    text=url.replace("http://", "") if url else "本机",
                    bg=self._bg,
                    fg=self._body_fg,
                    font=self.FONT_URL,
                ).pack()
        else:
            url, label, png = entries[0]
            if not url:
                url = lan_url(self._port)
            if label:
                tk.Label(
                    body, text=label, bg=self._bg, fg=self._body_fg,
                    font=self.FONT_HINT,
                ).pack(pady=(4, 0))
            tk.Label(body, text=url, bg=self._bg, fg=self._fg, font=self.FONT_URL).pack(
                fill="x", padx=10, pady=(2, 0)
            )
            qr_label(body, png)

        if extras:
            tk.Label(
                body,
                text="其它网卡: " + "  ".join(extras[:2]),
                bg=self._bg,
                fg=self._body_fg,
                font=self.FONT_HINT,
                wraplength=wraplength,
                justify="left",
            ).pack(fill="x", padx=10, pady=(2, 0))

        # 公网模式（SSH 隧道）下手机不在同一局域网也能访问，提示语随场景切换
        public_mode = bool(entries) and entries[0][1] == "公网访问 · SSH穿透"
        tk.Label(
            body,
            text=("外网访问 · 经云服务器SSH穿透" if public_mode else "手机/平板需与电脑同一网络")
                 + " · 鼠标移开自动隐藏",
            bg=self._bg,
            fg=self._body_fg,
            font=self.FONT_HINT,
            wraplength=wraplength,
            justify="left",
        ).pack(fill="x", padx=10, pady=(4, 10))

        self._photo = photos  # keep every PhotoImage alive

        with self._lock:
            self._built_key = repr([(url, label) for url, label, _ in entries])

    # ------------------------------------------------------------- fullscreen
    def _enter_fullscreen(self) -> None:
        """二维码全屏展示：黑底铺满整屏；二维码锚定屏幕左侧，左/上/下边距各
        16px（高度撑满整屏、等比例正方形），访问地址与来源文本在二维码右侧
        垂直居中。多网卡时右侧提供 ◀/▶ 翻页（键盘 ←/→ 同效）轮播各网卡二维码。
        再次点击"还原"、点击二维码或按 Esc 恢复正常面板。全屏期间热角自动
        呼出与自动隐藏都暂停。"""
        if self._fs_win is not None:
            return  # 已在全屏
        entries, _extras = self._access_info()
        if not entries or not entries[0][0]:
            return  # 没有可用地址

        self._fullscreen = True
        self._visible = False  # 收起小面板；全屏期间热角逻辑整体暂停
        self._leave_since = None
        with contextlib.suppress(Exception):
            if self._win is not None and self._win.winfo_exists():
                self._win.withdraw()

        root = self._root
        screen_w = int(root.winfo_screenwidth())
        screen_h = int(root.winfo_screenheight())
        win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=FS_BG)
        self._fs_win = win
        self._fs_entries = list(entries)
        self._fs_index = 0
        win.geometry(f"{screen_w}x{screen_h}+0+0")  # 铺满整个屏幕

        tk.Button(
            win, text="⛶ 还原（Esc）", command=self._exit_fullscreen,
            font=("Microsoft YaHei UI", 12), cursor="hand2",
            relief="flat", bd=0, highlightthickness=0,
            bg="#1A1A1A", fg=FS_FG, activebackground="#2E2E2E",
        ).place(relx=1.0, x=-14, y=14, anchor="ne")
        win.bind("<Escape>", lambda _e: self._exit_fullscreen())
        win.bind("<Left>", lambda _e: self._fs_switch(-1))   # 演示翻页笔/键盘左右键
        win.bind("<Right>", lambda _e: self._fs_switch(1))

        self._render_fs_page()
        with contextlib.suppress(Exception):
            win.focus_force()  # 让 Esc/方向键生效

    def _render_fs_page(self) -> None:
        """渲染全屏当前页：左侧大二维码 + 右侧垂直居中的地址文本（含翻页器）。"""
        win = self._fs_win
        if win is None:
            return
        entries = self._fs_entries
        if not entries:
            return
        self._fs_index = self._fs_index % len(entries)
        url, label_text, _png = entries[self._fs_index]

        root = self._root
        screen_w = int(root.winfo_screenwidth())
        screen_h = int(root.winfo_screenheight())

        # 清掉上一页的控件
        for old in (self._fs_qr_label, self._fs_text_panel):
            if old is not None:
                with contextlib.suppress(Exception):
                    old.destroy()
        self._fs_qr_label = None
        self._fs_text_panel = None

        # 二维码等比例正方形：高度撑满整屏（上/下边距各 16px）；竖屏时退而
        # 受宽度约束。整屏同色底就是静区。
        qr_target = max(200, min(screen_w, screen_h) - 2 * FS_EDGE_PX)

        # 按模块整数倍放大重绘（每个模块都是完整像素，锐利、远距离可扫）。
        # 二维码本体用标准白底黑码（兼容性最好），嵌在黑色整屏上如同一张白卡。
        # 注意探测图的像素宽 = 模块格数 × 探测 box_size，必须先除回去，
        # 否则算出的放大倍数只有一半（之前"二维码只占半屏"就是这个原因）。
        png = None
        qr_size = 0
        try:
            import io

            from PIL import Image
            probe_box = 8
            probe = build_qr_png(url, box_size=probe_box, border=2, fill_color=FS_QR_FG, back_color=FS_QR_BG)
            if probe is not None:
                unit = max(1, Image.open(io.BytesIO(probe)).size[0] // probe_box)  # 模块格总数
                box = max(2, qr_target // unit)
                png = build_qr_png(
                    url, box_size=box, border=2, fill_color=FS_QR_FG, back_color=FS_QR_BG,
                )
                if png is not None:
                    qr_size = Image.open(io.BytesIO(png)).size[0]
        except Exception:
            png = None

        try:
            from PIL import ImageTk
            if png is not None:
                self._fs_photo = ImageTk.PhotoImage(data=png)
                qr = tk.Label(win, image=self._fs_photo, bg=FS_QR_BG, bd=0, cursor="hand2")
                qr.place(x=FS_EDGE_PX, y=FS_EDGE_PX)  # 锚定屏幕左侧，上边距 16px
                qr.bind("<Button-1>", lambda _e: self._exit_fullscreen())  # 点二维码也可还原
                self._fs_qr_label = qr
        except Exception:
            pass
        if png is None:
            qr_size = min(400, screen_h - 2 * FS_EDGE_PX)
            fallback = tk.Label(
                win, text="二维码不可用\n（pip install qrcode 启用）",
                bg=FS_BG, fg="#CCCCCC", font=("Microsoft YaHei UI", 24),
                justify="center",
            )
            fallback.place(x=FS_EDGE_PX, y=FS_EDGE_PX, width=qr_size, height=qr_size)
            self._fs_qr_label = fallback

        # 文本区：二维码右侧 16px 起，垂直居中；字号按剩余宽度自适应收缩，
        # 二维码尺寸是先定的，永远不被文本挤压。
        text_x = FS_EDGE_PX + qr_size + FS_EDGE_PX
        text_panel = tk.Frame(win, bg=FS_BG)
        text_panel.place(x=text_x, rely=0.5, anchor="w")
        self._fs_text_panel = text_panel
        big_font = max(20, screen_h // 18)
        try:
            import tkinter.font as tkfont

            avail = max(120, screen_w - text_x - FS_EDGE_PX)
            font_obj = tkfont.Font(family="Consolas", size=big_font, weight="bold")
            while big_font > 10 and font_obj.measure(url) > avail:
                big_font -= 1
                font_obj.configure(size=big_font)
        except Exception:
            pass  # 字体测量不可用时用初始字号
        if label_text:
            tk.Label(
                text_panel, text=label_text, bg=FS_BG, fg=FS_FG,
                font=("Microsoft YaHei UI", max(14, big_font // 2)),
            ).pack(pady=(0, max(8, big_font // 3)))
        tk.Label(
            text_panel, text=url, bg=FS_BG, fg=FS_FG,
            font=("Consolas", big_font, "bold"),
        ).pack()

        # 多网卡：大号 ◀/▶ 翻页器（左侧被二维码占据，箭头放右侧文本区；
        # 键盘 ←/→ 与演示翻页笔同样可切）
        if len(entries) > 1:
            pager = tk.Frame(text_panel, bg=FS_BG)
            pager.pack(pady=(max(16, big_font // 2), 0))
            arrow_font = max(16, screen_h // 28)

            def arrow(text: str, delta: int) -> tk.Button:
                return tk.Button(
                    pager, text=text, command=lambda: self._fs_switch(delta),
                    font=("Microsoft YaHei UI", arrow_font), cursor="hand2",
                    relief="flat", bd=0, highlightthickness=0,
                    bg="#1A1A1A", fg=FS_FG, activebackground="#2E2E2E",
                    activeforeground=FS_FG, padx=max(10, arrow_font // 2),
                )

            arrow("◀", -1).pack(side="left", padx=(0, max(10, arrow_font // 2)))
            tk.Label(
                pager, text=f"{self._fs_index + 1}/{len(entries)}",
                bg=FS_BG, fg="#9A9A9A", font=("Microsoft YaHei UI", max(14, screen_h // 36)),
            ).pack(side="left")
            arrow("▶", 1).pack(side="left", padx=(max(10, arrow_font // 2), 0))

    def _fs_switch(self, delta: int) -> None:
        """全屏下切换上一张/下一张网卡二维码（循环轮播）。"""
        if self._fs_win is None or len(self._fs_entries) < 2:
            return
        self._fs_index = (self._fs_index + delta) % len(self._fs_entries)
        self._render_fs_page()

    def _exit_fullscreen(self) -> None:
        """退出全屏并恢复 normal 面板（3 秒宽免期内不自动收回，防止闪没）。"""
        win = self._fs_win
        self._fs_win = None
        self._fs_photo = None
        self._fs_entries = []
        self._fs_index = 0
        self._fs_qr_label = None
        self._fs_text_panel = None
        self._fullscreen = False
        if win is not None:
            with contextlib.suppress(Exception):
                if win.winfo_exists():
                    win.destroy()
        entries, extras = self._access_info()
        self._visible = True
        self._leave_since = None
        self._suppress_hide_until = time.monotonic() + 3.0
        self._show_ui(entries, extras)

    def _panel_y(self, height: int) -> int:
        y = self._margin
        if self._y_provider is not None:
            with contextlib.suppress(Exception):
                y = max(y, int(self._y_provider()))
        screen_height = 0
        with contextlib.suppress(Exception):
            screen_height = int(self._root.winfo_screenheight())
        if screen_height and y + height > screen_height - self._margin:
            y = max(0, screen_height - height - self._margin)
        return y

    def _refresh_screen_size(self) -> None:
        """Update the cached screen width. UI thread: direct; watcher: marshalled."""
        with contextlib.suppress(Exception):
            self._screen_width = int(self._root.winfo_screenwidth())

    def _refresh_screen_size_from_watcher(self) -> None:
        self._after_ui(self._refresh_screen_size)

    # ------------------------------------------------------------- animation
    def _animate_x(
        self,
        win: tk.Misc,
        width: int,
        height: int,
        start_x: int,
        end_x: int,
        *,
        on_done: Callable[[], None] | None = None,
        duration_ms: int = 220,
    ) -> None:
        self._cancel_animations()
        steps = max(1, int(duration_ms / 15))
        delta = (end_x - start_x) / float(steps)
        y = self._rect[1] if self._rect else 0
        state = {"i": 0}

        def tick() -> None:
            try:
                if not win.winfo_exists():
                    return
            except Exception:
                return
            state["i"] += 1
            next_x = end_x if state["i"] >= steps else int(start_x + delta * state["i"])
            with contextlib.suppress(Exception):
                win.geometry(f"{width}x{height}+{next_x}+{y}")
            if state["i"] >= steps:
                if on_done is not None:
                    on_done()
                return
            after_id = self._root.after(15, tick)
            self._after_ids.append(after_id)

        tick()

    def _cancel_animations(self) -> None:
        for after_id in self._after_ids:
            with contextlib.suppress(Exception):
                self._root.after_cancel(after_id)
        self._after_ids = []
