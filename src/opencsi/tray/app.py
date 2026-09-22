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
        on_launch_browser: Callable[[], None] | None = None,
        single_instance: SingleInstance | None = None,
    ) -> None:
        self._service = service
        self._session = session if session is not None else getattr(service, "_session", None)
        self._on_login = on_login
        self._on_open = on_open
        self._on_launch_browser = on_launch_browser
        self._single = single_instance or SingleInstance()

        self._icon: Any = None
        self._auto_refresh = True
        self._last_menu_signature: tuple[Any, ...] | None = None
        self._last_icon_state: str | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._unsubscribe_attention: Callable[[], None] | None = None
        self._lock = threading.RLock()

        #: Whether "start with Windows" is supported here, and if so whether it
        #: is currently on. ``None`` means not applicable, and the menu item is
        #: then omitted rather than shown doing nothing.
        self._startup = self._read_startup()

    # ── start with Windows ────────────────────────────────────────────────
    def _read_startup(self):
        """Read the startup registration, or ``None`` where it does not apply.

        Imported lazily and guarded: this runs on non-Windows hosts too, where
        the Run key does not exist, and a tray that refused to start because a
        Windows-only feature was unavailable would be a worse bug than the
        missing menu item.
        """
        try:
            from .startup import StartupManager

            status = StartupManager().status()
        except Exception as exc:  # noqa: BLE001 - never fatal
            log.debug("startup status unavailable: %s", type(exc).__name__)
            return None
        if not status.supported:
            return None
        return status.enabled

    def _toggle_startup(self) -> None:
        """Turn start-at-sign-in on or off, and reflect the *real* result.

        The state is re-read from the registry afterwards rather than assumed
        from the request. A toggle that failed would otherwise show itself as
        having succeeded, and the user would find out at the next reboot -- the
        worst possible moment to discover it.
        """
        try:
            from .startup import StartupManager

            manager = StartupManager()
            wanted = not bool(self._startup)
            status = manager.enable() if wanted else manager.disable()
        except Exception as exc:  # noqa: BLE001 - a failed toggle must not crash
            log.warning("could not change the startup setting: %s", type(exc).__name__)
            return

        with self._lock:
            self._startup = status.enabled if status.supported else None
        if status.supported and status.enabled != wanted:
            log.warning(
                "the startup setting did not take effect (asked for %s, got %s)",
                wanted,
                status.enabled,
            )
        self._refresh_view()

    # ── rendering ─────────────────────────────────────────────────────────
    def tooltip(self, snapshot: MonitorSnapshot) -> str:
        return tooltip_for(snapshot)

    def build_menu(self) -> list[Action]:
        return actions_for(
            self._service.snapshot,
            auto_refresh=self._auto_refresh,
            startup_enabled=self._startup,
        )

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
                        (lambda item, aid=action.id: self._checked_state(aid))
                        if action.checked is not None
                        else None
                    ),
                )
            )
        return pystray.Menu(*items)

    def _checked_state(self, action_id: str) -> bool:
        """Read a ticked menu item's state at render time, not build time.

        pystray calls this on every menu display, so it must reflect the current
        value -- a closure over the value at build time would freeze the tick
        and make the toggle look broken.
        """
        if action_id == "startup":
            return bool(self._startup)
        return bool(self._auto_refresh)

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
        elif action_id == "startup":
            self._toggle_startup()
        elif action_id == "login":
            self._trigger_login()
        elif action_id == "launch_browser":
            self._trigger_launch_browser()
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

        A callback, when the host wired one, runs on its own thread: the login is
        a browser round trip measured in tens of seconds, and running it inline
        would leave the icon frozen and Windows may draw it as "not responding".

        Without a callback this **opens the login page** rather than doing
        nothing. The menu offers "Sign in..." precisely when the session is gone,
        so a click that only wrote to a log file would be a dead menu item --
        which is what this was: the CLI never wired ``on_login``, so the one
        action a stuck user is most likely to try was silently inert.
        """
        if self._on_login is not None:
            threading.Thread(
                target=self._on_login, name="opencsi-login", daemon=True
            ).start()
            return
        self._open_browser()

    def _trigger_launch_browser(self) -> None:
        """Start a CDP-capable browser, then refresh once it is up.

        Runs on its own thread: starting a browser and waiting for its DevTools
        port is a multi-second operation, and doing it inline would freeze the
        icon and may make Windows draw the tray as "not responding".

        The refresh afterwards is what turns this from "a window appeared" into
        "the tray recovered" -- the user should not have to click Refresh once
        the browser they were told to start is actually running.
        """
        if self._on_launch_browser is not None:
            threading.Thread(
                target=self._on_launch_browser,
                name="opencsi-launch-browser",
                daemon=True,
            ).start()
            return
        self._open_browser()

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

    def _notify_attention(self, snapshot: MonitorSnapshot) -> None:
        """Show one notification when the session needs the user.

        Called on the *transition* into a state that needs action, never on
        every poll -- see ``MonitorService.subscribe_attention``. The tray polls
        every five minutes, so a level-triggered notification would nag twelve
        times an hour about something the user has already read.

        pystray exposes ``notify`` only on some backends, so a failure here is
        logged and swallowed: a missing balloon must not take down the tray, and
        the icon and tooltip already say "Login required".
        """
        message = {
            MonitorState.LOGIN_REQUIRED: "会话已过期，点击「登录」重新认证。",
            MonitorState.AUTH_ERROR: "会话被拒绝，点击「立即续期」或「登录」。",
            # The state a user lands in right after a reboot, which is exactly
            # when they have not yet noticed the tray is collecting nothing. The
            # message names the fix rather than the symptom, because "browser not
            # running" is not something a user knows how to act on.
            MonitorState.BROWSER_UNAVAILABLE: (
                "浏览器未运行，点击「启动浏览器并登录」即可恢复。"
            ),
        }.get(snapshot.state)
        if message is None:
            return

        icon = self._icon
        if icon is None:
            return
        try:
            icon.notify(message, title="OpenCSI Monitor")
        except Exception as exc:  # noqa: BLE001 - not every backend supports it
            log.debug("could not show a notification: %s", type(exc).__name__)

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
            self._unsubscribe_attention = self._service.subscribe_attention(
                self._notify_attention
            )
            self._service.start()

        if not blocking:
            return 0

        try:
            self._icon.run()
        finally:
            if self._unsubscribe_attention is not None:
                try:
                    self._unsubscribe_attention()
                except Exception:  # noqa: BLE001
                    pass
                self._unsubscribe_attention = None
            self._unsubscribe = None
            self._service.stop()
            self._single.release()
        return 0

    def __repr__(self) -> str:
        state = self._service.snapshot.state.value
        return f"TrayApp(state={state}, auto_refresh={self._auto_refresh})"


def main() -> int:
    """Console-script entry point: ``opencsi-monitor``.

    ``pyproject.toml`` has always declared ``opencsi-monitor =
    "opencsi.tray.app:main"``, but this function did not exist -- the real entry
    logic lived only in :mod:`opencsi.tray.__main__`, which the startup
    registration invokes as ``pythonw.exe -m opencsi.tray``. So the module form
    worked while the *declared console script* was broken: a fresh
    ``pip install`` created an ``opencsi-monitor`` that died on import with
    ``AttributeError: module 'opencsi.tray.app' has no attribute 'main'``.

    It went unnoticed because the machine's installed ``opencsi.exe`` predated
    the declaration, so nothing ever resolved the target. A declared entry point
    that does not exist is worse than a missing one: the failure happens after
    installation, in the user's shell, with a traceback about an attribute rather
    than a message about the tool.

    Delegates rather than duplicating, so there is exactly one place that decides
    how the tray starts.
    """
    from .__main__ import main as _main

    return _main()
