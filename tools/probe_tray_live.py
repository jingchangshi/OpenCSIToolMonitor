"""Prove the tray really starts on Windows, then exit.

`opencsi tray --check` proves the icon and menu can be *built*. This goes one
step further and proves the icon actually reaches the notification area: it runs
the real pystray message loop on a timer, asserts the icon reports itself
visible, and exits on its own so the probe can be run unattended.

Uses the offline stub client, so it touches no network and does not depend on
the browser being up.
"""
#: labels: LIVE, GET_ONLY

from __future__ import annotations

import sys
import threading
import time

sys.path.insert(0, "src")

from opencsi.auth.session import CredentialStatus, RenewalResult, RenewalStatus
from opencsi.models import MyToolsSnapshot
from opencsi.monitor import MonitorConfig, MonitorService
from opencsi.tray.app import TrayApp


class _Client:
    """A client that returns a fixed snapshot without any network."""

    def get_my_tools(self, *args, **kwargs) -> MyToolsSnapshot:
        return MyToolsSnapshot(
            total_tokens=3_634_063_175,
            total_request_count=26_566,
        )


class _Session:
    credentials = object()

    def status(self) -> CredentialStatus:
        return CredentialStatus(True, "cdp", None, 3600.0)

    def needs_renewal(self, margin=None) -> bool:
        return False

    def renew(self, **kwargs) -> RenewalResult:
        return RenewalResult(RenewalStatus.ALREADY_VALID)


def main() -> int:
    service = MonitorService(_Client(), session=_Session(), config=MonitorConfig())
    app = TrayApp(service)

    observations: dict[str, object] = {}

    def inspect_then_stop() -> None:
        """Wait for the icon to become visible, record it, then stop."""
        # ``app._icon`` is assigned inside run(), so it must be re-read every
        # time -- capturing it once races the message loop and observes None.
        deadline = time.monotonic() + 10.0
        icon = None
        while time.monotonic() < deadline:
            icon = app._icon  # noqa: SLF001 - this probe exists to look inside
            if icon is not None and getattr(icon, "visible", False):
                break
            time.sleep(0.25)
        icon = app._icon  # noqa: SLF001
        observations["visible"] = bool(icon is not None and getattr(icon, "visible", False))
        observations["title"] = getattr(icon, "title", None)
        try:
            observations["menu_items"] = len(list(icon.menu)) if icon is not None else 0
        except Exception as exc:  # noqa: BLE001
            observations["menu_items"] = f"error: {type(exc).__name__}"
        # Let the monitor publish its first snapshot before reading the tooltip.
        time.sleep(1.5)
        observations["state"] = service.snapshot.state.value
        observations["tooltip"] = app.tooltip(service.snapshot)
        if icon is not None:
            icon.stop()

    threading.Thread(target=inspect_then_stop, daemon=True).start()

    started = time.monotonic()
    try:
        app.run()
    except Exception as exc:  # noqa: BLE001
        print(f"tray run raised {type(exc).__name__}: {exc}")
        return 1
    elapsed = time.monotonic() - started

    print(f"message loop ran for : {elapsed:.1f}s")
    print(f"icon visible         : {observations.get('visible')}")
    print(f"tooltip title        : {str(observations.get('title'))[:100]}")
    print(f"menu items           : {observations.get('menu_items')}")
    print(f"monitor state        : {observations.get('state')}")
    print(f"tooltip after fetch  : {str(observations.get('tooltip')).replace(chr(10), ' / ')}")

    if observations.get("visible") and observations.get("state") == "OK":
        print("\nPASS: the tray entered the Windows message loop, showed an icon,")
        print("      and rendered a live snapshot.")
        return 0
    print("\nFAIL: the icon never became visible or no snapshot was rendered.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
