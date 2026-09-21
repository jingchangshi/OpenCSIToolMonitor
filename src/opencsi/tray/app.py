"""Windows 11 notification-area tray for openCsiTool usage.

This module is the *view*. It owns a pystray icon, a menu and a clipboard
action, and it renders a :class:`~opencsi.monitor.MonitorSnapshot`. It contains
no polling policy, no renewal logic and no HTTP: all of that is in
:class:`~opencsi.monitor.MonitorService`, which is why that layer is testable on
any platform and this one is not.

Consequences of that split, made explicit because they are the point:

* the tray never shells out to ``opencsi usage --json``. It imports
  ``OpenCsiToolClient`` through the service, so there is no subprocess to spawn,
  no JSON to parse and no second copy of the auth logic;
* pystray is imported lazily, inside :meth:`TrayApp.run`. Importing it at module
  scope would make ``opencsi tray --help`` fail on a machine without the extra,
  and would break every test that merely imports the package.

Threading: pystray owns the main thread's message loop (a Windows requirement --
``Shell_NotifyIcon`` needs a message pump). The monitor's worker thread does all
blocking work, and menu callbacks only enqueue. A menu callback that fetched
would freeze the icon until the network answered.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from ..auth.session import SessionManager
from ..errors import OpenCsiError
from ..monitor import MonitorService, MonitorSnapshot, MonitorState
from .icons import make_icon
from .presenter import Action, actions_for, status_text, tooltip_for
from .single_instance import SingleInstance

log = logging.getLogger("opencsi.tray")

#: Where "Open openCsiTool in browser" points.
OPENCSITOOL_URL = "https://opencsitool.com/myTools"


class TrayUnavailableError(OpenCsiError):
    """pystray or Pillow is missing, so there is no tray to show."""

    code = "TRAY_UNAVAILABLE"
    exit_code = 2
    hint = 'Install the tray extra: pip install "opencsi[tray]"'


def tray_available() -> tuple[bool, str | None]:
    """Whether a tray can be shown, and why not when it cannot.

    Returned as a reason rather than a bare bool so ``opencsi doctor`` can tell
    the user *what* to install instead of "not available".
    """
    missing: list[str] = []
    try:
        import pystray  # noqa: F401
    except Exception:  # noqa: BLE001
        missing.append("pystray")
    try:
        import PIL.Image  # noqa: F401
    except Exception:  # noqa: BLE001
        missing.append("Pillow")
    if missing:
        return False, "missing: " + ", ".join(missing)
    return True, None


class TrayApp:
    """The tray icon and its menu.

    Construct, then :meth:`run`. The service is started by ``run`` and stopped
    when the icon exits, so there is exactly one owner of the worker's lifetime.
    """

    def __init__(
        self,
        service: MonitorService,
        *,
        session: SessionManager | None = None,
        on_login: Callable[[], None] | None = None,
        on_open: Callable[[], None] | None = None,
        single_instance: SingleInstance | None = None,
    ) -> None:
        self._service = service
        self._session = session if session is not None else getattr(service, "_session", None)
        self._on_login = on_login
        self._on_open = on_open
        self._single = single_instance or SingleInstance()

        self._icon: Any = None
        self._auto_refresh = True
        self._last_menu_signature: tuple[Any, ...] | None = None
        self._last_icon_state: str | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._lock = threading.RLock()

    # ── rendering ─────────────────────────────────────────────────────────
    def tooltip(self, snapshot: MonitorSnapshot) -> str:
        return tooltip_for(snapshot)

    def build_menu(self) -> list[Action]:
        return actions_for(self._service.snapshot, auto_refresh=self._auto_refresh)

    def _pystray_menu(self):
        """Build the pystray menu from the pure-data action list."""
        import pystray

        items = []
        for action in self.build_menu():
            if action.id.startswith("sep"):
                items.append(pystray.Menu.SEPARATOR)
                continue
            items.append(
                pystray.MenuItem(
                    action.label,
                    self._make_handler(action.id),
                    enabled=action.enabled,
                    default=action.default,
                    checked=(
                        (lambda item: self._auto_refresh)
                        if action.checked is not None
                        else None
                    ),
                )
            )
        return pystray.Menu(*items)

    def _make_handler(self, action_id: str):
        """Wrap an action id in a pystray callback.

        Every callback is defensive: an exception inside a Win32 message handler
        can take the whole message loop down, which would silently kill the tray.
        """

        def handler(icon=None, item=None) -> None:  # noqa: ANN001, ARG001
            try:
                self._dispatch(action_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("tray action %r failed: %s", action_id, type(exc).__name__)

        return handler

    def _dispatch(self, action_id: str) -> None:
        """Perform a menu action. Runs on the pystray message thread."""
        if action_id == "refresh":
            # Enqueue only: the worker does the blocking fetch, so the icon
            # stays responsive.
            self._service.refresh_now()
        elif action_id == "renew":
            self._service.renew_now()
        elif action_id == "autorefresh":
            with self._lock:
                self._auto_refresh = not self._auto_refresh
            if self._auto_refresh:
                self._service.refresh_now()
            self._refresh_view()
        elif action_id == "login":
            self._trigger_login()
        elif action_id == "open":
            self._open_browser()
        elif action_id == "copy":
            self._copy_status()
        elif action_id == "quit":
            self.quit()
        else:  # pragma: no cover - unknown ids are a programming error
            log.debug("ignoring unknown tray action %r", action_id)

    # ── actions ───────────────────────────────────────────────────────────
    def _trigger_login(self) -> None:
        """Start an interactive login without blocking the message loop.

        The login is a browser round trip measured in tens of seconds; running
        it inline would leave the icon frozen and Windows may draw it as "not
        responding".
        """
        if self._on_login is not None:
            threading.Thread(target=self._on_login, name="opencsi-login", daemon=True).start()
            return
        log.info("login requested from the tray; run 'opencsi login' to sign in")

    def _open_browser(self) -> None:
        if self._on_open is not None:
            self._on_open()
            return
        import webbrowser

        try:
            webbrowser.open(OPENCSITOOL_URL)
        except Exception as exc:  # noqa: BLE001
            log.debug("could not open a browser: %s", type(exc).__name__)

    def _copy_status(self) -> None:
        """Put a secret-free status report on the clipboard."""
        text = status_text(self._service.snapshot)
        copied = False
        try:
            import ctypes

            # CF_UNICODETEXT. Done with ctypes rather than tkinter so the tray
            # does not need a Tk dependency it otherwise never uses.
            CF_UNICODETEXT = 13
            GMEM_MOVEABLE = 0x0002
            kernel32 = ctypes.windll.kernel32
            user32 = ctypes.windll.user32
            if user32.OpenClipboard(None):
                try:
                    user32.EmptyClipboard()
                    buffer = ctypes.create_unicode_buffer(text)
                    size = ctypes.sizeof(buffer)
                    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
                    if handle:
                        pointer = kernel32.GlobalLock(handle)
                        ctypes.memmove(pointer, buffer, size)
                        kernel32.GlobalUnlock(handle)
                        user32.SetClipboardData(CF_UNICODETEXT, handle)
                        copied = True
                finally:
                    user32.CloseClipboard()
        except Exception as exc:  # noqa: BLE001 - clipboard is best-effort
            log.debug("clipboard copy failed: %s", type(exc).__name__)
        log.debug("status copied to clipboard: %s", copied)

    def quit(self) -> None:
        """Stop the worker and remove the icon."""
        try:
            self._service.stop()
        finally:
            if self._icon is not None:
                try:
                    self._icon.stop()
                except Exception:  # noqa: BLE001
                    pass

    # ── lifecycle ─────────────────────────────────────────────────────────
    def _refresh_view(self) -> None:
        """Redraw the icon and menu from the current snapshot.

        Called on the monitor's worker thread (via ``subscribe``), so pystray's
        own thread-safety is relied on; pystray serialises ``update`` through the
        message loop on Windows.
        """
        if self._icon is None:
            return
        snapshot = self._service.snapshot

        if self._last_icon_state != snapshot.state.value:
            try:
                self._icon.icon = make_icon(snapshot.state.value)
                self._last_icon_state = snapshot.state.value
            except Exception as exc:  # noqa: BLE001
                log.debug("could not draw the tray icon: %s", type(exc).__name__)

        try:
            self._icon.title = self.tooltip(snapshot)
        except Exception as exc:  # noqa: BLE001
            log.debug("could not set the tooltip: %s", type(exc).__name__)

        signature = tuple(
            (a.id, a.label, a.enabled, a.checked) for a in self.build_menu()
        )
        if signature != self._last_menu_signature:
            try:
                self._icon.menu = self._pystray_menu()
                self._last_menu_signature = signature
            except Exception as exc:  # noqa: BLE001
                log.debug("could not rebuild the menu: %s", type(exc).__name__)

    def run(self, *, blocking: bool = True, start_service: bool = True) -> int:
        """Start the tray. Returns the process exit code.

        ``blocking=False`` builds the icon and returns without entering the
        message loop, which is what makes this testable and what
        ``opencsi tray --check`` uses to prove the tray *can* start.

        ``start_service=False`` additionally skips starting the monitor worker,
        so the call touches neither the network nor a background thread. A
        health check that needed the API could not tell "the tray is broken"
        from "the network is down" -- the one question it exists to answer.
        """
        available, reason = tray_available()
        if not available:
            raise TrayUnavailableError(f"the tray cannot start: {reason}")

        if blocking and not self._single.acquire():
            log.error("another OpenCSI tray is already running")
            return 2

        import pystray

        self._icon = pystray.Icon(
            "opencsi",
            icon=make_icon(self._service.snapshot.state.value),
            title=self.tooltip(self._service.snapshot),
            menu=self._pystray_menu(),
        )
        self._last_menu_signature = tuple(
            (a.id, a.label, a.enabled, a.checked) for a in self.build_menu()
        )
        self._last_icon_state = self._service.snapshot.state.value

        if not start_service:
            if not blocking:
                return 0
        else:
            self._unsubscribe = self._service.subscribe(lambda _s: self._refresh_view())
            self._service.start()

        if not blocking:
            return 0

        try:
            self._icon.run()
        finally:
            self._unsubscribe = None
            self._service.stop()
            self._single.release()
        return 0

    def __repr__(self) -> str:
        state = self._service.snapshot.state.value
        return f"TrayApp(state={state}, auto_refresh={self._auto_refresh})"
