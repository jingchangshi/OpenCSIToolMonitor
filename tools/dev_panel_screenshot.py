"""Render the usage panel against a fixed, credential-free fixture snapshot
and capture real screenshots to ``docs/assets/``.

This is a *developer tool*, not a probe: it touches no network, no browser, no
credential store. The snapshots are the fixed fixtures from the panel's design,
so the screenshots are deterministic and repeatable -- which is what makes them
usable as visual baselines after a layout change.

Run it on the Windows machine with a desktop session:

    python tools/dev_panel_screenshot.py

It writes five PNGs (collapsed, expanded, 150% expanded, 200% collapsed, and a
login-required state) and prints the window placement of each.

The capture uses BitBlt from the panel's own window DC. Two more common paths
were measured and rejected on this machine: a full-screen grab composites only
the wallpaper (windows excluded), and ``PrintWindow`` renders Tk's text but
black backgrounds because Tk does not implement ``WM_PRINTCLIENT``.
"""
import ctypes
import os
import sys

sys.path.insert(0, r"D:\workspace\OpenCSIToolMonitor\src")

from ctypes import wintypes
from datetime import datetime, timezone

from opencsi.monitor import (
    DailyModelUsage,
    DailyUsage,
    MonitorSnapshot,
    MonitorState,
)
from opencsi.tray.popup import PopupController


class _Client:
    def get_my_tools(self, *a, **k):
        return None


def make_snapshot(state=MonitorState.OK):
    models = (
        DailyModelUsage("code_completion", "GLM-5.3-Flash", 41_000_000, 9.20),
        DailyModelUsage("code_completion", "DeepSeek-V4-Flash", 11_852_000, 5.60),
        DailyModelUsage("code_completion", "GLM-4.7-Air", 8_240_000, 3.10),
        DailyModelUsage("translate", "Qwen3-Max", 2_180_000, 0.62),
        DailyModelUsage("embedding", "Embedding-V4", 96_400, 0.04),
        DailyModelUsage("flat_plan", "包月模型(Trae)", 340_000, None),
    )
    daily = DailyUsage(
        date="2026-10-24",
        total_tokens=sum(m.tokens for m in models),
        total_cost=17.96,
        currency="CNY",
        models=models,
    )
    return MonitorSnapshot(
        state=state,
        total_tokens=6_563_731_577,
        requests=62_808,
        prs=258,
        added_lines=41088,
        generated_lines=3150,
        adopted_lines=120,
        adoption_rate=0.0381,
        data_fresh_time="2026-10-24T08:05:12+08:00",
        fetched_at=datetime(2026, 10, 24, 0, 5, 12, tzinfo=timezone.utc),
        credential_expires_in=3480.0,
        daily_usage=daily,
    )


class _Svc:
    def __init__(self, snapshot):
        self._snapshot = snapshot
        self.auto_refresh_enabled = True

    @property
    def snapshot(self):
        return self._snapshot

    def subscribe(self, cb):
        def unsub():
            pass
        return unsub

    def refresh_now(self, *a, **k):
        pass

    def renew_now(self, *a, **k):
        pass

    def set_auto_refresh(self, v):
        self.auto_refresh_enabled = v

    def stop(self):
        pass


def grab_window(hwnd: int, path: str) -> tuple[int, tuple[int, int]]:
    """Capture a window's own rendering via PrintWindow(PW_RENDERFULLCONTENT)."""
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top

    SRCCOPY = 0x00CC0020
    DIB_RGB_COLORS = 0
    BI_RGB = 0

    hdc_window = user32.GetWindowDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_window)
    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = BI_RGB
    bits = ctypes.c_void_p()
    bitmap = gdi32.CreateDIBSection(
        hdc_mem, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits), None, 0
    )
    gdi32.SelectObject(hdc_mem, bitmap)
    # BitBlt from the window's own DC, not PrintWindow: Tk does not implement
    # WM_PRINTCLIENT, so PrintWindow renders Tk's text but black backgrounds,
    # and the DWM full-content path renders all-black. A BitBlt off the window
    # DC returns exactly what Tk painted.
    ok = gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_window, 0, 0, SRCCOPY)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(hwnd, hdc_window)

    from PIL import Image

    img = Image.frombuffer("RGB", (w, h), ctypes.string_at(bits, w * h * 4),
                           "raw", "BGRX", 0, 1)
    img.save(path)
    gdi32.DeleteObject(bitmap)
    return ok, (w, h)


def shoot(out_path, *, scale=None, expanded=False, state=MonitorState.OK):
    if scale:
        os.environ["OPENCSI_PANEL_DPI_SCALE"] = str(scale)
    else:
        os.environ.pop("OPENCSI_PANEL_DPI_SCALE", None)
    import tkinter as tk

    from opencsi.tray.popup import PopupController, _dpi_aware, tray_anchor_rect

    _dpi_aware()
    svc = _Svc(make_snapshot(state))
    controller = PopupController(svc)
    controller._root = tk.Tk()
    controller._root.withdraw()
    controller._build_panel()
    controller._apply_snapshot(svc.snapshot)
    if expanded:
        controller._toggle_models()
    controller._show()
    controller._root.update_idletasks()
    controller._root.update()
    panel = controller._panel
    hwnd = panel.winfo_id()
    ok, (w, h) = grab_window(hwnd, out_path)
    print(f"{os.path.basename(out_path)}: printed={ok} {w}x{h} at "
          f"({panel.winfo_rootx()},{panel.winfo_rooty()}) anchor={tray_anchor_rect()}")
    controller._panel.destroy()
    controller._root.destroy()
    os.environ.pop("OPENCSI_PANEL_DPI_SCALE", None)


if __name__ == "__main__":
    outdir = r"D:\workspace\OpenCSIToolMonitor\docs\assets"
    os.makedirs(outdir, exist_ok=True)
    shoot(os.path.join(outdir, "panel-100-collapsed.png"))
    shoot(os.path.join(outdir, "panel-100-expanded.png"), expanded=True)
    shoot(os.path.join(outdir, "panel-150-expanded.png"), scale=1.5, expanded=True)
    shoot(os.path.join(outdir, "panel-200-collapsed.png"), scale=2.0)
    shoot(
        os.path.join(outdir, "panel-100-login.png"),
        state=MonitorState.LOGIN_REQUIRED,
    )
    print("done")
