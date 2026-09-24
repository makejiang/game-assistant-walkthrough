"""Tkinter toast notifications for the walkthrough assistant.

Animation (per requirement):
  - A new toast starts off-screen above the top-right corner, slides smoothly
    down to just below the top edge.
  - It stays for a hold period (default 10s), then slides out to the right
    until it is fully off-screen.
  - When multiple toasts are visible, a newly added toast takes the top slot
    and pushes the existing ones down (they are never covered).
"""

from __future__ import annotations

import contextlib
import threading
from dataclasses import dataclass, field
from typing import Any

import tkinter as tk


@dataclass
class _Toast:
    win: tk.Toplevel
    width: int
    height: int
    x: int = 0
    y: int = 0
    slot: int = 0
    after_ids: list[str] = field(default_factory=list)
    closing: bool = field(default=False)


class ToastNotifier:
    FONT_TITLE = ("Microsoft YaHei UI", 11, "bold")
    FONT_BODY = ("Microsoft YaHei UI", 9)

    def __init__(
        self,
        root: tk.Tk,
        *,
        margin: int = 16,
        gap: int = 10,
        hold_seconds: float = 10.0,
        slide_in_ms: int = 220,
        slide_out_ms: int = 240,
        min_width: int = 260,
        max_width: int = 460,
        background: str = "#0f141d",
        foreground: str = "#e8f1ff",
        body_foreground: str = "#c9d8ee",
        border: str = "#3f4f6a",
    ) -> None:
        self._root = root
        self._margin = max(8, int(margin))
        self._gap = max(4, int(gap))
        self._hold_ms = max(1000, int(hold_seconds * 1000))
        self._slide_in_ms = max(60, int(slide_in_ms))
        self._slide_out_ms = max(60, int(slide_out_ms))
        self._min_width = max(200, int(min_width))
        self._max_width = max(240, int(max_width))
        self._bg = background
        self._fg = foreground
        self._body_fg = body_foreground
        self._border = border
        self._lock = threading.Lock()
        self._toasts: list[_Toast] = []
        self._screen_width = root.winfo_screenwidth()
        self._screen_height = root.winfo_screenheight()

    # ------------------------------------------------------------------ public
    def notify(self, title: str, message: str, *, duration_ms: int | None = None) -> None:
        """Show a toast. Called from any thread; UI work is marshalled to the Tk loop."""
        hold_ms = self._hold_ms if duration_ms is None else max(1000, int(duration_ms))
        with self._lock:
            self._root.after(0, lambda: self._notify_ui(title, message, hold_ms))

    def dismiss_all(self) -> None:
        with self._lock:
            self._root.after(0, self._dismiss_all_ui)

    def occupied_bottom_y(self) -> int:
        """Bottom y below the lowest visible toast, so other panels can avoid overlap."""
        bottom = self._margin
        for toast in tuple(self._toasts):
            if toast.closing:
                continue
            bottom = max(bottom, toast.y + toast.height + self._gap)
        return bottom

    # ------------------------------------------------------------- internals
    def _notify_ui(self, title: str, message: str, hold_ms: int) -> None:
        try:
            toast = self._build_toast(title, message)
        except Exception:
            return
        self._refresh_screen_size()
        self._shift_existing_down()
        self._toasts.insert(0, toast)
        toast.slot = 0
        start_y = -toast.height - 8
        target_y = self._target_y(0, toast.height)
        self._animate_y(toast, start_y, target_y, self._slide_in_ms, on_done=None)
        self._schedule_close(toast, hold_ms)

    def _build_toast(self, title: str, message: str) -> _Toast:
        root = self._root
        win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.97)
        win.configure(bg=self._bg)

        frame = tk.Frame(win, bg=self._bg, highlightbackground=self._border, highlightthickness=1)
        frame.pack(fill="both", expand=True)

        title_label = tk.Label(
            frame,
            text=title or "",
            anchor="w",
            bg=self._bg,
            fg=self._fg,
            padx=14,
            pady=8,
            font=self.FONT_TITLE,
        )
        title_label.pack(fill="x")

        message_label = tk.Label(
            frame,
            text=message or "",
            justify="left",
            anchor="w",
            bg=self._bg,
            fg=self._body_fg,
            padx=14,
            pady=0,
            font=self.FONT_BODY,
            wraplength=self._max_width - 40,
        )
        message_label.pack(fill="x", pady=(0, 12))

        win.update_idletasks()
        width = max(self._min_width, min(self._max_width, win.winfo_reqwidth()))
        height = max(88, win.winfo_reqheight())
        x = max(0, self._screen_width - width - self._margin)
        y = -height - 8
        win.geometry(f"{width}x{height}+{x}+{y}")
        win.lift()
        return _Toast(win=win, width=width, height=height, x=x, y=y)

    def _target_y(self, slot: int, height: int) -> int:
        return self._margin + slot * (height + self._gap)

    def _shift_existing_down(self) -> None:
        for toast in list(self._toasts):
            if toast.closing:
                continue
            toast.slot += 1
            self._animate_y(
                toast,
                toast.y,
                self._target_y(toast.slot, toast.height),
                self._slide_out_ms,
                on_done=None,
            )

    def _schedule_close(self, toast: _Toast, hold_ms: int) -> None:
        after_id = self._root.after(hold_ms, lambda: self._start_close(toast))
        toast.after_ids.append(after_id)

    def _start_close(self, toast: _Toast) -> None:
        if toast.closing or not self._toast_alive(toast):
            return
        toast.closing = True
        start_x = toast.x
        end_x = self._screen_width + toast.width + 8
        self._animate_x(toast, start_x, end_x, self._slide_out_ms, on_done=self._finish_close)

    def _finish_close(self, toast: _Toast) -> None:
        if toast in self._toasts:
            self._toasts.remove(toast)
        with contextlib.suppress(Exception):
            if toast.win.winfo_exists():
                toast.win.destroy()
        self._reflow_remaining()

    def _reflow_remaining(self) -> None:
        for index, toast in enumerate(self._toasts):
            if toast.closing:
                continue
            toast.slot = index
            self._animate_y(
                toast,
                toast.y,
                self._target_y(index, toast.height),
                self._slide_out_ms,
                on_done=None,
            )

    def _dismiss_all_ui(self) -> None:
        for toast in list(self._toasts):
            self._start_close(toast)

    # ------------------------------------------------------------ animation
    def _animate_y(self, toast: _Toast, start_y: int, end_y: int, duration: int, on_done) -> None:
        steps = max(1, int(duration / 15))
        delta = (end_y - start_y) / float(steps)
        state = {"i": 0}

        def tick() -> None:
            if not self._toast_alive(toast):
                return
            state["i"] += 1
            if state["i"] >= steps:
                next_y = end_y
            else:
                next_y = int(start_y + delta * state["i"])
            toast.y = next_y
            toast.win.geometry(f"{toast.width}x{toast.height}+{toast.x}+{toast.y}")
            if state["i"] >= steps:
                if on_done is not None:
                    on_done(toast)
                return
            after_id = self._root.after(15, tick)
            toast.after_ids.append(after_id)

        tick()

    def _animate_x(self, toast: _Toast, start_x: int, end_x: int, duration: int, on_done) -> None:
        steps = max(1, int(duration / 15))
        delta = (end_x - start_x) / float(steps)
        state = {"i": 0}

        def tick() -> None:
            if not self._toast_alive(toast):
                return
            state["i"] += 1
            if state["i"] >= steps:
                next_x = end_x
            else:
                next_x = int(start_x + delta * state["i"])
            toast.x = next_x
            toast.win.geometry(f"{toast.width}x{toast.height}+{toast.x}+{toast.y}")
            if state["i"] >= steps:
                if on_done is not None:
                    on_done(toast)
                return
            after_id = self._root.after(15, tick)
            toast.after_ids.append(after_id)

        tick()

    def _toast_alive(self, toast: _Toast) -> bool:
        try:
            return bool(toast.win.winfo_exists())
        except Exception:
            return False

    def _refresh_screen_size(self) -> None:
        try:
            self._screen_width = self._root.winfo_screenwidth()
            self._screen_height = self._root.winfo_screenheight()
        except Exception:
            pass
