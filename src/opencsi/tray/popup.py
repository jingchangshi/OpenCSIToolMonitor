"""The Windows 11 usage panel: a light Tk popup behind the tray icon.

Why Tkinter, and why this module is shaped the way it is
--------------------------------------------------------
The native ``pystray.Menu`` is a Win32 popup menu: text rows, no colour, no
layout. It cannot carry a dashboard, so the panel is a real window. Tkinter is
the only GUI toolkit in the standard library, needs no new dependency, and --
unlike Electron/WebView/Qt -- adds nothing to install or freeze beyond the
tcl/tk runtime PyInstaller already knows how to bundle.

All *decisions* in this module are pure functions tested without a display:
positioning, the toggle policy that stops a focus-out/close and a tray
re-click from fighting each other, and the view-model mapping (in
:mod:`opencsi.tray.popup_presenter`). Only the widget tree itself needs a
display, so only that part is skipped in headless tests.

Threading model
---------------
Three threads, each owning exactly one thing:

* **main thread** owns Tk: the root is created here, every widget is touched
  here, and ``mainloop()`` runs here;
* **the pystray thread** (``icon.run_detached()``) owns the tray message loop.
  Left-click (verified against pystray's win32 backend: ``WM_LBUTTONUP``
  invokes the first ``default=True`` menu item) and menu callbacks land here;
  they only *enqueue* commands;
* **the monitor worker thread** owns fetching and renewal. Its snapshot
  callbacks enqueue too.

The Tk thread drains one queue on a short ``after`` tick. Nothing outside the
Tk thread ever touches a widget, so a slow network call can never freeze the
panel and a fast click sequence can never race a redraw.
"""

from __future__ import annotations

import ctypes
import os
import queue
import sys
import time
from ctypes import wintypes
from typing import Any, Callable

from ..monitor import MonitorSnapshot

#: Re-clicking the tray icon within this window after a focus-out hide is
#: *ignored*. Clicking the tray takes focus from the panel, whose FocusOut
#: handler hides it; the same click's toggle command then arrives. Without the
#: suppression the panel would hide and immediately reopen -- a flicker that
#: reads as "the panel ignores me".
TOGGLE_SUPPRESS_SECONDS = 0.4

#: How often the Tk thread drains the command queue.
POLL_MS = 80

#: Panel geometry in DIPs (device-independent pixels; scaled at build time).
PANEL_WIDTH_DIP = 420
PANEL_MAX_HEIGHT_DIP = 560
PANEL_MARGIN_DIP = 12


def panel_supported() -> bool:
    """Whether the panel can run here: Tk present, Windows, a desktop session.

    Checked before the tray commits to the dual-loop architecture, so a machine
    without tcl/tk keeps the legacy pure-pystray tray instead of losing the
    tray entirely.
    """
    if sys.platform != "win32":
        return False
    try:
        import tkinter  # noqa: F401

        return True
    except Exception:  # noqa: BLE001 - a missing tk is a fallback, not a crash
        return False


# ── geometry (pure; tested without a display) ──────────────────────────────
def compute_popup_position(
    anchor_x: int,
    anchor_y: int,
    work: tuple[int, int, int, int],
    width: int,
    height: int,
    *,
    margin: int = PANEL_MARGIN_DIP,
) -> tuple[int, int]:
    """Place the panel near ``anchor``, clamped inside the work area.

    ``anchor`` is a point on the tray icon: the panel floats above it when the
    taskbar is at the bottom, beside it when the taskbar is on the left or
    right, and below it when the taskbar is at the top. Clamping is what keeps
    the panel on-screen on any monitor and any taskbar position -- the anchor
    only chooses *which edge* to hug, never a coordinate outside ``work``.
    """
    left, top, right, bottom = work
    x = anchor_x - width // 2
    y = anchor_y - height - margin

    if y < top:
        # No room above (taskbar at top, or anchor near the top): drop below.
        y = anchor_y + margin
    if y + height > bottom:
        y = bottom - height
    if y < top:
        y = top

    if x < left:
        x = left
    if x + width > right:
        x = right - width
    if x < left:  # panel wider than the work area; prefer the left edge
        x = left
    return x, y


def should_toggle(
    *, visible: bool, last_hidden_at: float | None, now: float,
    suppress_window: float = TOGGLE_SUPPRESS_SECONDS,
) -> bool:
    """Whether a tray toggle command should act, given the flicker guard.

    The rule: a toggle always acts while hidden; while visible it acts unless
    the panel was just hidden by losing focus -- that hide already consumed
    the click the user is mid-way through.
    """
    if not visible:
        return True
    if last_hidden_at is None:
        return True
    return (now - last_hidden_at) >= suppress_window


# ── win32 anchor discovery (guarded; returns None off-Windows) ─────────────
def tray_anchor_rect() -> tuple[int, int, int, int] | None:
    """The notification area's rectangle, in physical screen pixels.

    Walks ``Shell_TrayWnd`` (the taskbar) to ``TrayNotifyWnd`` (the clock and
    hidden-icons region the tray icon lives in). ``None`` means "could not ask
    Windows" -- explorer restarting, an exotic shell -- and the caller falls
    back to the monitor's work area corner.
    """
    if sys.platform != "win32":
        return None
    try:
        user32 = ctypes.windll.user32
        tray = user32.FindWindowW("Shell_TrayWnd", None)
        if not tray:
            return None
        notify = user32.FindWindowExW(tray, None, "TrayNotifyWnd", None)
        if not notify:
            return None
        rect = wintypes.RECT()
        if not user32.GetWindowRect(notify, ctypes.byref(rect)):
            return None
        return rect.left, rect.top, rect.right, rect.bottom
    except Exception:  # noqa: BLE001 - a probe must never break the tray
        return None


def work_area_containing(x: int, y: int) -> tuple[int, int, int, int] | None:
    """The work area of the monitor containing the point, excluding taskbars."""
    if sys.platform != "win32":
        return None
    try:
        user32 = ctypes.windll.user32
        MONITOR_DEFAULTTONEAREST = 2
        monitor = user32.MonitorFromPoint(
            wintypes.POINT(x, y), MONITOR_DEFAULTTONEAREST
        )
        if not monitor:
            return None

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        rc = info.rcWork
        return rc.left, rc.top, rc.right, rc.bottom
    except Exception:  # noqa: BLE001 - a probe must never break the tray
        return None


def _dpi_aware() -> None:
    """Opt the process into per-monitor DPI awareness, best-effort.

    Without this, Windows bitmap-scales the panel and it blurs at 125%/150%.
    Failure is non-fatal: the panel still works, just scaled by the OS.
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001 - already set, or old Windows
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


def _apply_round_corners(hwnd: int) -> None:
    """Win11 rounded corners + shadow for the borderless panel. Best-effort."""
    try:
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWCP_ROUND = 2
        value = ctypes.c_int(DWMWCP_ROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(value), ctypes.sizeof(value),
        )
    except Exception:  # noqa: BLE001 - pre-Win11 simply keeps square corners
        pass


class _Tooltip:
    """A minimal hover tooltip for full-precision numbers."""

    def __init__(self, widget: Any, text_provider: Callable[[], str]) -> None:
        self._widget = widget
        self._text = text_provider
        self._tip: Any = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _event: Any = None) -> None:
        text = self._text()
        if not text or self._tip is not None:
            return
        import tkinter as tk

        x = self._widget.winfo_rootx() + 8
        y = self._widget.winfo_rooty() - 28
        self._tip = tw = tk.Toplevel(self._widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        label = tk.Label(
            tw, text=text, justify="left",
            background="#1f2937", foreground="#f9fafb",
            font=("Microsoft YaHei UI", -12), padx=6, pady=3,
        )
        label.pack()

    def _hide(self, _event: Any = None) -> None:
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:  # noqa: BLE001 - the tip dying is harmless
                pass
            self._tip = None


class PopupController:
    """Own the panel window and the command queue that feeds it.

    Created and driven on the main thread (which runs the Tk mainloop).
    Everything other threads may do goes through :meth:`enqueue`, which only
    puts into a :class:`queue.Queue`.
    """

    def __init__(
        self,
        service: Any,
        *,
        startup_manager: Any | None = None,
        poll_ms: int = POLL_MS,
        clock: Callable[[], float] = time.monotonic,
        today_provider: Callable[[], str] | None = None,
    ) -> None:
        self._service = service
        self._startup_manager = startup_manager
        self._poll_ms = poll_ms
        self._clock = clock
        # The statistic day the panel labels its data with. Injectable so a
        # test can pin it: the real clock rolled over midnight once already and
        # re-classified every fixed-date fixture as 跨日.
        self._today_provider = today_provider or self._real_today
        self._commands: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self._root: Any = None
        self._panel: Any = None
        self._widgets: dict[str, Any] = {}
        self._visible = False
        self._expanded = False
        self._last_hidden_at: float | None = None
        self._view: Any = None
        self._scale = 1.0
        self._quit_requested = False
        self._subscription: Any = None

    # ── cross-thread surface ───────────────────────────────────────────────
    def enqueue(self, command: str, payload: Any = None) -> None:
        """Thread-safe request. The only method other threads may call."""
        self._commands.put((command, payload))

    # ── lifecycle (main thread only) ───────────────────────────────────────
    def run(self) -> None:
        """Build the hidden root, pump the queue, run the Tk mainloop.

        Returns when ``quit`` is drained. The tray icon is *not* this object's
        responsibility: the caller stops it after this returns.
        """
        import tkinter as tk

        _dpi_aware()
        self._root = tk.Tk()
        self._root.withdraw()
        self._scale = max(1.0, self._root.winfo_fpixels("1i") / 96.0)
        # A documented debug hook: OPENCSI_PANEL_DPI_SCALE emulates a display
        # scale factor (1.5, 2.0, ...) without changing the user's system
        # setting, so higher-DPI layouts can be checked on one machine. The
        # real DPI still governs the default.
        try:
            override = float(os.environ.get("OPENCSI_PANEL_DPI_SCALE", "") or 0)
        except ValueError:
            override = 0.0
        if override >= 1.0:
            self._scale = override
        self._build_panel()

        # The monitor publishes on its own thread; route to the queue.
        self._subscription = self._service.subscribe(
            lambda snapshot: self.enqueue("snapshot", snapshot)
        )
        # Seed with whatever the service already holds: if the monitor fetched
        # between its start and this subscription, waiting for the next publish
        # would leave the panel on its placeholder for up to a whole interval.
        self._apply_snapshot(self._service.snapshot)
        self._root.after(self._poll_ms, self._drain)
        self._root.mainloop()
        self._teardown()

    def request_quit(self) -> None:
        """Ask the mainloop to end. Thread-safe."""
        self.enqueue("quit")

    def _teardown(self) -> None:
        if self._subscription is not None:
            try:
                self._subscription()
            except Exception:  # noqa: BLE001 - teardown must not raise
                pass
            self._subscription = None

    # ── the queue pump ─────────────────────────────────────────────────────
    def _drain(self) -> None:
        while True:
            try:
                command, payload = self._commands.get_nowait()
            except queue.Empty:
                break
            if command == "quit":
                self._quit_requested = True
                break
            try:
                if command == "toggle":
                    self._toggle()
                elif command == "hide":
                    self._hide()
                elif command == "snapshot":
                    self._apply_snapshot(payload)
            except Exception:  # noqa: BLE001 - one bad frame must not kill the loop
                pass
        if self._quit_requested:
            self._root.destroy()
            return
        self._root.after(self._poll_ms, self._drain)

    # ── visibility ─────────────────────────────────────────────────────────
    def _toggle(self) -> None:
        now = self._clock()
        if not should_toggle(
            visible=self._visible,
            last_hidden_at=self._last_hidden_at,
            now=now,
        ):
            return
        if self._visible:
            self._hide()
        else:
            self._show()

    def _show(self) -> None:
        if self._panel is None:
            return
        self._place()
        self._panel.deiconify()
        self._panel.lift()
        try:
            self._panel.focus_force()
        except Exception:  # noqa: BLE001 - focus is best-effort
            pass
        self._visible = True
        self._last_hidden_at = None

    def _hide(self) -> None:
        if self._panel is None or not self._visible:
            return
        self._last_hidden_at = self._clock()
        self._panel.withdraw()
        self._visible = False

    def _on_focus_out(self, _event: Any = None) -> None:
        self._hide()

    def _place(self) -> None:
        """Position the panel at the tray, on the tray's own monitor."""
        anchor = tray_anchor_rect()
        if anchor is None:
            # No tray rectangle: bottom-right of the primary work area.
            self._panel.update_idletasks()
            w, h = self._panel.winfo_reqwidth(), self._panel.winfo_reqheight()
            area = work_area_containing(0, 0) or (0, 0, 1920, 1040)
            x, y = compute_popup_position(
                area[2] - 60, area[3], area, w, h, margin=int(12 * self._scale)
            )
            self._panel.wm_geometry(f"+{x}+{y}")
            _ = tk
            return

        left, top, right, bottom = anchor
        anchor_x = (left + right) // 2
        anchor_y = bottom
        area = work_area_containing(anchor_x, anchor_y) or (0, 0, 1920, 1040)
        self._panel.update_idletasks()
        # The width is the design's, not the content's: without it the widgets'
        # natural widths would squeeze the two headline numbers together.
        w = max(self._width, self._panel.winfo_reqwidth())
        h = self._panel.winfo_reqheight()
        x, y = compute_popup_position(
            anchor_x, anchor_y, area, w, h, margin=int(12 * self._scale)
        )
        self._panel.wm_geometry(f"{w}x{h}+{x}+{y}")

    # ── the widget tree (built once) ───────────────────────────────────────
    def _px(self, dip: float) -> int:
        return max(1, int(round(dip * self._scale)))

    def _font(self, size: int, weight: str = "normal") -> tuple:
        return ("Microsoft YaHei UI", -self._px(size), weight)

    def _build_panel(self) -> None:
        import tkinter as tk

        width = self._px(PANEL_WIDTH_DIP)
        panel = tk.Toplevel(self._root)
        panel.overrideredirect(True)
        panel.withdraw()
        panel.configure(bg="#f9fafb")
        self._width = width
        panel.after(10, lambda: _apply_round_corners(
            ctypes.windll.user32.GetParent(panel.winfo_id()) or panel.winfo_id()
        ))
        self._panel = panel
        # A click elsewhere takes focus away, and that is how Tk reports it.
        # Bound once: binding per-show with add="+" would stack handlers.
        panel.bind("<FocusOut>", self._on_focus_out, add="+")
        panel.bind("<Escape>", lambda _e: self._hide(), add="+")

        self._scale = max(1.0, self._root.winfo_fpixels("1i") / 96.0)
        try:
            override = float(os.environ.get("OPENCSI_PANEL_DPI_SCALE", "") or 0)
        except ValueError:
            override = 0.0
        if override >= 1.0:
            self._scale = override

        outer = tk.Frame(panel, bg="#f9fafb", padx=self._px(2), pady=self._px(2))
        outer.pack(fill="both", expand=True)
        card = tk.Frame(outer, bg="#ffffff", highlightbackground="#e5e7eb",
                        highlightthickness=self._px(1))
        card.pack(fill="both", expand=True)

        self._build_header(card)
        self._build_today(card)
        self._build_models(card)
        self._build_cumulative(card)
        self._build_actions(card)
        self._build_settings(card)
        self._build_footer(card)

    def _build_header(self, parent: Any) -> None:
        import tkinter as tk

        frame = tk.Frame(parent, bg="#ffffff", padx=self._px(14), pady=self._px(10))
        frame.pack(fill="x")
        row = tk.Frame(frame, bg="#ffffff")
        row.pack(fill="x")
        title = tk.Label(
            row, text="OpenCSIToolMonitor", bg="#ffffff", fg="#0f172a",
            font=self._font(11, "bold"), anchor="w",
        )
        title.pack(side="left")
        dot = tk.Canvas(
            row, width=self._px(10), height=self._px(10), bg="#ffffff",
            highlightthickness=0,
        )
        dot.pack(side="left", padx=(self._px(10), self._px(4)), pady=self._px(3))
        status = tk.Label(
            row, text="启动中", bg="#ffffff", fg="#334155",
            font=self._font(9, "bold"), anchor="w",
        )
        status.pack(side="left")
        reason = tk.Label(
            frame, text="", bg="#ffffff", fg="#64748b",
            font=self._font(8), anchor="w", wraplength=self._px(PANEL_WIDTH_DIP - 40),
            justify="left",
        )
        reason.pack(fill="x", pady=(self._px(2), 0))
        self._widgets["dot"] = dot
        self._widgets["status"] = status
        self._widgets["reason"] = reason

    def _build_today(self, parent: Any) -> None:
        import tkinter as tk

        frame = tk.Frame(parent, bg="#eff6ff", padx=self._px(14), pady=self._px(10))
        frame.pack(fill="x", padx=self._px(10), pady=(self._px(8), 0))
        caption = tk.Label(
            frame, text="今日用量", bg="#eff6ff", fg="#1e40af",
            font=self._font(9, "bold"), anchor="w",
        )
        caption.pack(fill="x")

        numbers = tk.Frame(frame, bg="#eff6ff")
        numbers.pack(fill="x", pady=self._px(4))
        tokens = tk.Label(
            numbers, text="--", bg="#eff6ff", fg="#1d4ed8",
            font=self._font(21, "bold"), anchor="e",
        )
        tokens.grid(row=0, column=0, sticky="e", padx=(0, self._px(6)))
        tokens_suffix = tk.Label(
            numbers, text="tokens", bg="#eff6ff", fg="#3b82f6",
            font=self._font(9), anchor="w",
        )
        tokens_suffix.grid(row=0, column=1, sticky="w")
        cost = tk.Label(
            numbers, text="--", bg="#eff6ff", fg="#0f172a",
            font=self._font(21, "bold"), anchor="e",
        )
        cost.grid(row=0, column=2, sticky="e", padx=(self._px(16), self._px(6)))
        cost_suffix = tk.Label(
            numbers, text="估算费用", bg="#eff6ff", fg="#64748b",
            font=self._font(9), anchor="w",
        )
        cost_suffix.grid(row=0, column=3, sticky="w")
        numbers.grid_columnconfigure(0, weight=1)
        numbers.grid_columnconfigure(2, weight=1)

        sub = tk.Label(
            frame, text="最近更新：--:--　数据来源：openCsiTool",
            bg="#eff6ff", fg="#64748b", font=self._font(8), anchor="w",
        )
        sub.pack(fill="x")
        note = tk.Label(
            frame, text="", bg="#eff6ff", fg="#b45309",
            font=self._font(8), anchor="w", justify="left",
            wraplength=self._px(PANEL_WIDTH_DIP - 56),
        )
        note.pack(fill="x")

        self._widgets["today_caption"] = caption
        self._widgets["today_tokens"] = tokens
        self._widgets["today_cost"] = cost
        self._widgets["today_sub"] = sub
        self._widgets["today_note"] = note
        _Tooltip(
            tokens,
            lambda: (self._view.today.tokens_full + " tokens")
            if self._view is not None and self._view.today.tokens_full
            else "完整数值暂不可用",
        )
        _Tooltip(
            cost,
            lambda: (
                "估算费用：tokens / 1e6 × 单价，非实际账单"
                if self._view is not None else ""
            ),
        )

    def _build_models(self, parent: Any) -> None:
        import tkinter as tk

        frame = tk.Frame(parent, bg="#ffffff", padx=self._px(14), pady=self._px(8))
        frame.pack(fill="x")
        head = tk.Frame(frame, bg="#ffffff")
        head.pack(fill="x")
        title = tk.Label(
            head, text="今日各模型用量", bg="#ffffff", fg="#334155",
            font=self._font(9, "bold"), anchor="w",
        )
        title.pack(side="left")
        toggle = tk.Label(
            head, text="展开 ▾", bg="#ffffff", fg="#2563eb",
            font=self._font(9), cursor="hand2",
        )
        toggle.pack(side="right")
        toggle.bind("<Button-1>", lambda _e: self._toggle_models())

        list_host = tk.Frame(frame, bg="#ffffff")
        # Not packed: the list exists only while expanded.

        self._widgets["models_frame"] = frame
        self._widgets["models_toggle"] = toggle
        self._widgets["models_list_host"] = list_host

    def _toggle_models(self) -> None:
        self._expanded = not self._expanded
        self._render_models()
        self._place()

    def _render_models(self) -> None:
        """Rebuild the model rows from the current view. No network, no fetch."""
        import tkinter as tk

        toggle: Any = self._widgets["models_toggle"]
        host: Any = self._widgets["models_list_host"]
        for child in host.winfo_children():
            child.destroy()
        view = self._view
        if not self._expanded or view is None:
            toggle.configure(text="展开 ▾")
            host.pack_forget()
            return
        toggle.configure(text="收起 ▴")
        if not view.models:
            tk.Label(
                host, text="今日暂无模型用量数据", bg="#ffffff", fg="#94a3b8",
                font=self._font(8), anchor="w",
            ).pack(fill="x", pady=self._px(2))
        rows = view.models
        # Enough room for the rows; beyond that the list scrolls internally so
        # the panel never outgrows the work area.
        row_h = self._px(22)
        visible_rows = min(len(rows), 8)
        canvas_h = row_h * visible_rows + self._px(6)
        canvas = tk.Canvas(
            host, bg="#ffffff", highlightthickness=0, height=canvas_h,
            width=self._px(PANEL_WIDTH_DIP - 52),
        )
        scrollbar = tk.Scrollbar(host, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg="#ffffff")
        canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind(
            "<Configure>",
            lambda _e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.configure(yscrollcommand=scrollbar.set)

        def _wheel(event: Any) -> None:
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        # Hover-scoped wheel binding: bind_all would survive the canvas being
        # destroyed on the next collapse and stack one handler per rebuild.
        canvas.bind("<MouseWheel>", _wheel)
        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _wheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        header = tk.Frame(inner, bg="#f1f5f9")
        header.pack(fill="x")
        tk.Label(
            header, text="模型", bg="#f1f5f9", fg="#64748b",
            font=self._font(8), anchor="w",
        ).pack(side="left", padx=self._px(6), fill="x", expand=True)
        tk.Label(
            header, text="Token", bg="#f1f5f9", fg="#64748b",
            font=self._font(8), anchor="e", width=12,
        ).pack(side="right", padx=self._px(6))
        tk.Label(
            header, text="估算费用", bg="#f1f5f9", fg="#64748b",
            font=self._font(8), anchor="e", width=10,
        ).pack(side="right")

        for row in rows:
            line = tk.Frame(inner, bg="#ffffff" if not row.is_total else "#f8fafc")
            line.pack(fill="x")
            name = tk.Label(
                line, text=row.name, bg=line["bg"],
                fg="#0f172a" if not row.is_total else "#334155",
                font=self._font(9, "bold" if row.is_total else "normal"),
                anchor="w",
            )
            name.pack(side="left", fill="x", expand=True)
            tk.Label(
                line, text=row.tokens_display, bg=line["bg"], fg="#334155",
                font=self._font(9), anchor="e", width=11,
            ).pack(side="right")
            tk.Label(
                line, text=row.cost_display, bg=line["bg"], fg="#334155",
                font=self._font(9), anchor="e", width=10,
            ).pack(side="right")

        canvas.pack(side="left", fill="both", expand=True)
        if len(rows) > 8:
            scrollbar.pack(side="right", fill="y")
        host.pack(fill="x", pady=(self._px(4), 0))

    def _build_cumulative(self, parent: Any) -> None:
        import tkinter as tk

        frame = tk.Frame(parent, bg="#f8fafc", padx=self._px(14), pady=self._px(6))
        frame.pack(fill="x", padx=self._px(10), pady=self._px(8))
        text = tk.Label(
            frame, text="累计总览", bg="#f8fafc", fg="#475569",
            font=self._font(8), anchor="w",
        )
        text.pack(side="left")
        copy = tk.Label(
            frame, text="复制", bg="#f8fafc", fg="#2563eb",
            font=self._font(8), cursor="hand2",
        )
        copy.pack(side="right")
        copy.bind("<Button-1>", lambda _e: self._copy_cumulative())
        self._widgets["cumulative"] = text
        self._widgets["cumulative_frame"] = frame

    def _build_actions(self, parent: Any) -> None:
        import tkinter as tk

        frame = tk.Frame(parent, bg="#ffffff", padx=self._px(14), pady=self._px(6))
        frame.pack(fill="x")

        def button(text: str, command: Any) -> tk.Label:
            label = tk.Label(
                frame, text=text, bg="#f1f5f9", fg="#0f172a",
                font=self._font(9), padx=self._px(10), pady=self._px(4),
                cursor="hand2",
            )
            label.bind("<Button-1>", lambda _e: command())
            label.bind(
                "<Enter>", lambda _e: label.configure(bg="#e2e8f0")
            )
            label.bind("<Leave>", lambda _e: label.configure(bg="#f1f5f9"))
            return label

        row = tk.Frame(frame, bg="#ffffff")
        row.pack(fill="x")
        self._widgets["btn_refresh"] = button("立即刷新", self._refresh)
        self._widgets["btn_refresh"].pack(side="left")
        self._widgets["btn_renew"] = button("立即续期", self._renew)
        self._widgets["btn_open"] = button("打开 openCsiTool 网站", self._open_site)
        self._widgets["btn_open"].pack(side="left", padx=(self._px(8), 0))

    def _build_settings(self, parent: Any) -> None:
        import tkinter as tk

        frame = tk.Frame(parent, bg="#ffffff", padx=self._px(14), pady=self._px(4))
        frame.pack(fill="x")
        style = {
            "bg": "#ffffff", "fg": "#334155", "activebackground": "#ffffff",
            "activeforeground": "#0f172a", "font": self._font(9),
            "highlightthickness": 0,
        }
        self._widgets["auto_refresh_var"] = tk.BooleanVar(value=True)
        auto = tk.Checkbutton(
            frame, text="自动刷新", variable=self._widgets["auto_refresh_var"],
            command=self._on_auto_refresh, **style,
        )
        auto.pack(side="left")
        self._widgets["auto_check"] = auto
        self._widgets["startup_var"] = tk.BooleanVar(value=False)
        startup = tk.Checkbutton(
            frame, text="登录 Windows 后自启动",
            variable=self._widgets["startup_var"], command=self._on_startup,
            **style,
        )
        startup.pack(side="left", padx=(self._px(12), 0))
        self._widgets["startup_check"] = startup

    def _build_footer(self, parent: Any) -> None:
        import tkinter as tk

        frame = tk.Frame(parent, bg="#f9fafb", padx=self._px(14), pady=self._px(8))
        frame.pack(fill="x", side="bottom")

        def link(text: str, command: Any) -> tk.Label:
            label = tk.Label(
                frame, text=text, bg="#f9fafb", fg="#64748b",
                font=self._font(9), cursor="hand2",
            )
            label.bind("<Button-1>", lambda _e: command())
            label.bind("<Enter>", lambda _e: label.configure(fg="#0f172a"))
            label.bind("<Leave>", lambda _e: label.configure(fg="#64748b"))
            return label

        link("复制状态", self._copy_status).pack(side="left")
        link("退出", self._quit).pack(side="right")

    # ── actions ────────────────────────────────────────────────────────────
    def _refresh(self) -> None:
        self._service.refresh_now()  # async; the worker fetches, the panel stays live

    def _renew(self) -> None:
        self._service.renew_now()

    def _open_site(self) -> None:
        import webbrowser

        try:
            webbrowser.open("https://opencsitool.com/myTools")
        except Exception:  # noqa: BLE001 - no browser is not a crash
            pass

    def _copy_cumulative(self) -> None:
        view = self._view
        if view is None or view.cumulative is None:
            return
        text = (
            f"{view.cumulative.tokens_full} tokens / "
            f"{view.cumulative.requests_display} requests"
        )
        self._clipboard(text)

    def _copy_status(self) -> None:
        from .presenter import status_text

        self._clipboard(status_text(self._service.snapshot))

    def _clipboard(self, text: str) -> None:
        try:
            self._root.clipboard_clear()
            self._root.clipboard_append(text)
        except Exception:  # noqa: BLE001 - clipboard is best-effort
            pass

    def _quit(self) -> None:
        # Stop the monitor first so the worker joins while the GUI is alive,
        # then end the mainloop; the tray icon is stopped by the caller.
        try:
            self._service.stop()
        finally:
            self.enqueue("quit")

    def _on_auto_refresh(self) -> None:
        # The checkbox drives the service's real schedule -- the same switch the
        # tray menu's 自动刷新 flips -- never a UI-only flag.
        enabled = bool(self._widgets["auto_refresh_var"].get())
        setter = getattr(self._service, "set_auto_refresh", None)
        if callable(setter):
            setter(enabled)

    def _on_startup(self) -> None:
        wanted = bool(self._widgets["startup_var"].get())
        manager = self._startup_manager
        if manager is None:
            from .startup import StartupManager

            manager = StartupManager()
        try:
            manager.enable() if wanted else manager.disable()
        except Exception:  # noqa: BLE001 - a registry fault must not kill the panel
            pass
        # Re-read the *registry*, not the request: the tick must show what is
        # actually on disk.
        try:
            actual = bool(manager.status().enabled)
        except Exception:  # noqa: BLE001
            actual = wanted
        self._widgets["startup_var"].set(actual)

    # ── rendering ──────────────────────────────────────────────────────────
    @staticmethod
    def _real_today() -> str:
        from datetime import date

        return date.today().isoformat()

    def _apply_snapshot(self, snapshot: MonitorSnapshot) -> None:
        from . import popup_presenter as pp

        startup: bool | None = None
        try:
            from .startup import StartupManager

            status = StartupManager().status()
            if status.supported:
                startup = bool(status.enabled)
        except Exception:  # noqa: BLE001 - a registry probe must not break rendering
            startup = None
        auto = bool(getattr(self._service, "auto_refresh_enabled", True))
        self._view = pp.build_panel_view(
            snapshot,
            today=self._today_provider(),
            auto_refresh=auto,
            startup=startup,
        )
        self._render()

    def _render(self) -> None:
        """Patch the widgets in place. Rebuilding the tree would flicker."""
        view = self._view
        if view is None or self._panel is None:
            return
        status = view.status
        self._widgets["status"].configure(text=status.label, fg=status.color)
        dot: Any = self._widgets["dot"]
        dot.delete("all")
        dot.create_oval(
            2, 2, self._px(8), self._px(8), fill=status.color, outline=""
        )
        reason = status.reason or ""
        if status.hint:
            reason = f"{reason}　·　{status.hint}" if reason else status.hint
        self._widgets["reason"].configure(text=reason)

        today = view.today
        self._widgets["today_caption"].configure(
            text=(
                f"今日用量 · {today.date}"
                if today.date
                else "今日用量"
            )
        )
        self._widgets["today_tokens"].configure(text=today.tokens_display)
        self._widgets["today_cost"].configure(text=today.cost_display)
        self._widgets["today_sub"].configure(text=today.caption)
        self._widgets["today_note"].configure(text=today.note)

        self._render_models()

        cumulative = view.cumulative
        if cumulative is None:
            self._widgets["cumulative"].configure(
                text="累计数据暂不可用", fg="#94a3b8"
            )
        else:
            self._widgets["cumulative"].configure(
                text=(
                    f"累计 {cumulative.tokens_display} tokens"
                    f"　·　{cumulative.requests_display} 次请求"
                ),
                fg="#475569",
            )

        renew: Any = self._widgets["btn_renew"]
        if view.renew_available:
            renew.pack(side="left", padx=(self._px(8), 0))
            renew.configure(text="立即续期")
        else:
            renew.pack_forget()

        self._widgets["auto_refresh_var"].set(view.auto_refresh)
        startup_check: Any = self._widgets["startup_check"]
        if view.startup is None:
            startup_check.pack_forget()
        else:
            startup_check.pack(side="left", padx=(self._px(12), 0))
            self._widgets["startup_var"].set(view.startup)

        if self._visible:
            self._place()
