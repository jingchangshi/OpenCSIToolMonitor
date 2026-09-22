"""A hidden Chromium that exists only to complete authentication.

The distinction this module is built on
---------------------------------------
Objective §74 names three things that must not be conflated, and this is where
two of them come apart:

.. code-block::

    browser engine required   ≠   user must interact with a browser window

The openCsiTool OAuth leg genuinely needs a browser *engine*: GitCode's
``/oauth/authorize`` is a client-rendered SPA, so something has to execute its
JavaScript and follow the redirect chain to the callback. What was never
required is a browser *window* -- nothing in the flow needs a human to look at or
click anything, as long as the profile it runs in is already signed in to
GitCode.

So this module runs the engine with no window at all. It is the difference
between "open Chrome and go and sign in" and a background process that renews
the session while the user is doing something else.

What was measured before this was written
-----------------------------------------
* A headless Chrome (``--headless=new``) serves the DevTools protocol normally
  and answers ``/json/version``; verified on Chrome 153 on this machine.
* The GitCode bridge verification -- a real ``fetch`` to a *known authenticated*
  endpoint, from a page inside that headless browser -- returned ``200`` where
  the same request signed out returned ``401``
  (``tools/probe_gitcode_sso_bridge2.py``). The engine is therefore not merely
  running, it is completing authenticated work.

That second measurement is why ``--headless=new`` is the default here rather
than an experiment.

Honesty about the fallback
--------------------------
Not every Chromium build supports ``--headless=new``; older ones want the
legacy ``--headless``, and a build that supports neither must not be reported as
headless. The launch result records which mode actually started, and a caller
that asked for headless and got a visible window is told so rather than
reassured.

Process lifetime
----------------
The host is **detached**, like :func:`~opencsi.auth.browser_launch.launch_debug_browser`.
A tray that starts it at sign-in and then exits must not take the browser with
it: the whole point is that it keeps the session alive across restarts of the
monitor. ``stop()`` is therefore the only thing that ends it.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.request import urlopen

from ..errors import OpenCsiError  # noqa: F401 - documents the module's error contract
from ..ws import CdpConnection
from .browser_launch import (
    BrowserLaunchStatus,
    _endpoint_answers,
    dedicated_profile_dir,
    find_browser,
    launch_debug_browser,
)
from .cdp import discover_cdp_endpoint
log = logging.getLogger("opencsi.auth.host")

#: The auth host gets its **own** profile directory, deliberately not the one
#: interactive sign-in uses. The host is started and stopped by the tool, and a
#: profile the user may also have open in a visible window cannot be managed that
#: way -- a second launch against a profile already in use is silently treated by
#: Chromium as "open a tab over there", and no debugging endpoint is ever
#: created. Two profiles, two owners, no ambiguity.
AUTH_PROFILE_DIRNAME = "opencsi-auth-profile"

#: The port the auth host listens on. Deliberately *not* 9222: that is the port
#: interactive sign-in and the user's own debugging browser use, and the host
#: must not be mistaken for either.
DEFAULT_AUTH_PORT = 9224

#: How long to wait for a freshly started headless browser to answer.
DEFAULT_START_TIMEOUT = 25.0

_POLL_INTERVAL = 0.25

#: Flags that make a Chromium a quiet background service rather than a browser
#: someone is using. Each is here for a specific reason:
#:
#: ``--headless=new``
#:     no window at all. The whole point of the module.
#: ``--no-first-run`` / ``--no-default-browser-check``
#:     without them a fresh profile opens a welcome flow, which in a headless
#:     build is an invisible page that never finishes loading.
#: ``--disable-gpu``
#:     a headless build has no use for a GPU process, and on some drivers it
#:     fails to start rather than falling back.
#: ``--no-service-autorun`` / ``--disable-background-networking``
#:     the host is not the user's browser; it should not phone home for
#:     component updates or run background services on their behalf.
_QUIET_FLAGS = (
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-gpu",
    "--no-service-autorun",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-extensions",
    "--disable-default-apps",
)


class AuthHostMode(str, Enum):
    """How the engine is running. Reported, never assumed."""

    #: No window at all (``--headless=new``).
    HEADLESS = "HEADLESS"
    #: A real window exists. Either the platform/build could not go headless, or
    #: the caller explicitly asked for a visible browser.
    VISIBLE = "VISIBLE"
    #: Nothing is running.
    STOPPED = "STOPPED"


class AuthHostStatus(str, Enum):
    """Outcome of an :meth:`AuthBrowserHost.ensure_running` call."""

    #: Started by this call and answering on its port.
    STARTED = "STARTED"
    #: An endpoint was already answering; nothing was started.
    ALREADY_RUNNING = "ALREADY_RUNNING"
    #: No Chromium-family browser is installed.
    NO_BROWSER_FOUND = "NO_BROWSER_FOUND"
    #: The process started but never exposed a DevTools endpoint.
    FAILED = "FAILED"
    #: This platform has no implementation.
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class AuthHostResult:
    """What the host is doing now (secret-free)."""

    status: AuthHostStatus
    mode: AuthHostMode = AuthHostMode.STOPPED
    port: int = DEFAULT_AUTH_PORT
    browser: str | None = None
    executable: str | None = None
    profile: str | None = None
    detail: str | None = None
    #: Whether ``--headless=new`` was accepted. ``False`` on a visible fallback,
    #: so a caller can tell "hidden as asked" from "a window is on your desktop".
    headless: bool = False

    @property
    def ok(self) -> bool:
        return self.status in (AuthHostStatus.STARTED, AuthHostStatus.ALREADY_RUNNING)

    @property
    def visible(self) -> bool:
        """Whether this result means a window the user can see."""
        return self.mode is AuthHostMode.VISIBLE

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "status": self.status.value,
            "mode": self.mode.value,
            "headless": self.headless,
            "port": self.port,
            "ok": self.ok,
        }
        for key in ("browser", "executable", "profile", "detail"):
            value = getattr(self, key)
            if value:
                out[key] = value
        return out


def auth_profile_dir() -> Path:
    """Where the auth host's profile lives.

    Derived from :func:`~opencsi.auth.browser_launch.dedicated_profile_dir`'s
    parent so the two profiles sit side by side and a user who goes looking finds
    them together, rather than in two unrelated places.
    """
    return dedicated_profile_dir().parent / AUTH_PROFILE_DIRNAME


def _headless_supported(executable: Path) -> bool:
    """Whether ``executable`` can run headless. Answers from evidence.

    This asks the browser to *do* something headless rather than asking it to
    describe itself. The previous implementation ran
    ``chrome --headless=new --version`` and returned False on a build where
    headless provably works, which made the unattended monitor refuse to start
    any engine at all.

    What that probe actually did
    ----------------------------
    Measured five times on Chrome 153, with the user's browser running:

    * it returned ``rc=0`` in 0.12-0.41s -- it did **not** hang, and the 15s
      timeout never fired;
    * it printed **nothing at all**, so the ``b"Chrom" in blob`` check could
      only ever fail. That is the whole reason it answered False;
    * it started a real headless browser and left it running: eleven orphaned
      processes per call, holding a ``HeadlessChrome*`` profile in ``%TEMP%``.

    So ``--headless=new`` combined with ``--version`` starts a browser and
    ignores the request to print a version. The probe was not a capability check
    that failed; it was an accidental browser launch that leaked, read as a
    capability result.

    An earlier revision of this docstring, and of the closure report, described
    this as a *hang* in which a fifteen-second timeout fired. Five fresh
    measurements do not reproduce a hang. The likeliest explanation for the
    original observation is machine load: at that point in the investigation
    roughly a hundred orphaned browsers from this very probe were running, which
    is exactly the condition that would make a launch slow enough to trip the
    timeout. The correction is recorded rather than quietly dropped, because
    "it hangs" and "it starts a browser and prints nothing" imply different
    fixes -- and only the second one explains the leak.

    Why ``--screenshot`` is the replacement
    ---------------------------------------
    It asks the browser to do the thing under test and leaves an artifact that
    can be checked. Measured here, three runs each:

    ====================  ==========  ================  ==========
    probe                 artifact    visible windows   wall time
    ====================  ==========  ================  ==========
    ``--version``         never       0 (leaked 11)     0.12s, False
    ``--dump-dom``        0/3         0                 0.12s
    ``--screenshot``      3/3         0                 0.12s
    ``--print-to-pdf``    3/3         0                 0.12s
    ====================  ==========  ================  ==========

    A real PNG begins with a known eight-byte signature, so the check is exact
    rather than a substring search.

    The exit code is deliberately **not** the evidence. Chromium's launcher
    hands off to a child and exits immediately with status 0 -- measured at 0.1s
    -- so an exit code proves only that the launcher ran, not that a browser did.
    Every candidate above exited 0, including ``--dump-dom``, which produced
    nothing at all. The artifact is the evidence.

    That hand-off is also what made an earlier investigation conclude that
    launching a browser from Python was broken on this machine: the launcher's
    fast zero exit was read as the browser failing, when the browser was in fact
    starting normally and answering its debug port 0.5s later.

    A failure means "unknown", and unknown is treated as unsupported, so the
    caller falls back to a mode known to work rather than starting a browser that
    immediately exits.
    """
    import tempfile

    # The profile directory name carries this process's PID so that a sweep can
    # find leftovers by path. Chromium puts --user-data-dir verbatim on every
    # child's command line, so the path is a reliable handle on the whole tree
    # even after the launcher exits and the children are reparented.
    token = f"opencsi-headless-probe-{os.getpid()}"
    profile = Path(tempfile.gettempdir()) / token
    shot = Path(tempfile.gettempdir()) / f"{token}.png"
    try:
        _sweep_probe(token)
        profile.mkdir(parents=True, exist_ok=True)
        argv = [
            str(executable),
            "--headless=new",
            f"--screenshot={shot}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu",
            "about:blank",
        ]
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        # Wait for the *artifact*, not for the launcher. Chromium's launcher
        # hands off to a child and exits in ~0.1s, while the browser needs about
        # 0.6s to write the screenshot -- measured on this machine. Waiting on
        # the launcher therefore returns before any work has happened, and the
        # first version of this function then killed the tree at 0.1s and
        # destroyed the browser before it could produce the very evidence it was
        # being asked for. That is why it answered False on a build where
        # headless provably works.
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if _is_png(shot):
                return True
            if process.poll() is not None and not _probe_still_alive(token):
                # The launcher is gone and nothing is left to write the file.
                # One last look covers a browser that finished just as it exited.
                return _is_png(shot)
            time.sleep(0.1)
        return False
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        # Runs on every path, including the timeout. The timeout is the *normal*
        # path on a build that ignores the flag, so cleanup placed after the try
        # would skip exactly the case that leaves browsers behind.
        _sweep_probe(token)
        _remove_quietly(profile)
        _remove_quietly(shot)


_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _is_png(path: Path) -> bool:
    """Whether ``path`` is a real PNG, judged by its signature.

    Size alone is not enough: a truncated or empty file would pass a "did
    something appear" check, and the whole point of this probe is to distinguish
    a browser that ran from a launcher that merely exited.
    """
    try:
        with path.open("rb") as handle:
            return handle.read(8) == _PNG_MAGIC
    except OSError:
        return False


def _probe_still_alive(token: str) -> bool:
    """Whether any browser from the probe tagged ``token`` is still running.

    Needed because the launcher exits immediately while the browser it started
    keeps working, so ``poll()`` on the launcher says nothing about whether the
    probe is still in progress. Returns True on any uncertainty, so a slow or
    unreadable process list makes the caller wait rather than give up early.
    """
    if os.name != "nt":
        # Elsewhere the launcher is the browser, so poll() is authoritative.
        return False
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "(Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\""
                f" | Where-Object {{ $_.CommandLine -like '*{token}*' }}).Count",
            ],
            capture_output=True,
            timeout=20.0,
            check=False,
        )
        text = (completed.stdout or b"").decode("utf-8", "replace").strip()
        return int(text or "0") > 0
    except (OSError, subprocess.SubprocessError, ValueError):
        return True


def _remove_quietly(path: Path) -> None:
    """Delete a file or directory, ignoring every failure.

    A probe's scratch files are not worth an exception: on Windows a browser
    that has not fully exited still holds its profile open, and failing the
    capability check because cleanup could not finish would turn a cosmetic
    problem into a functional one.
    """
    import shutil

    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _sweep_probe(token: str) -> None:
    """Kill any browser left over from a probe tagged ``token``. Never raises.

    Two mechanisms, because neither is sufficient alone:

    * ``taskkill /T`` cannot be used here -- the launcher has usually exited
      already, having handed the real work to a child that is now orphaned, and
      a dead PID has no tree to walk.
    * every child still carries ``--user-data-dir=<...token...>`` on its command
      line, so matching that token finds the orphans the parent link cannot.

    The token contains this process's PID, so the sweep cannot reach a browser
    the user is running or a concurrent probe's browser.

    It shells out to ``powershell.exe`` rather than ``wmic`` because ``wmic`` was
    removed in Windows 11: calling it raises ``FileNotFoundError``, which a bare
    ``except OSError`` swallows, so the sweep reported success while killing
    nothing. ``powershell.exe`` is used rather than ``pwsh`` deliberately --
    Windows PowerShell ships with every supported Windows release, while
    PowerShell 7 is an optional install, and this is a cleanup path where a
    missing interpreter would silently restore the leak.
    """
    try:
        if os.name != "nt":
            return
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\""
                f" | Where-Object {{ $_.CommandLine -like '*{token}*' }}"
                " | ForEach-Object { Stop-Process -Id $_.ProcessId -Force"
                " -ErrorAction SilentlyContinue }",
            ],
            capture_output=True,
            timeout=30.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _kill(pid: int) -> bool:
    """Terminate ``pid``. Returns whether the process is gone afterwards."""
    try:
        if os.name == "nt":
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=20.0,
                check=False,
            )
            return completed.returncode == 0
        os.kill(pid, 15)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _graceful_close(port: int, timeout: float = 15.0) -> bool:
    """Ask the browser on ``port`` to shut itself down. Returns whether it went.

    Why this is not the same as killing it
    --------------------------------------
    Chromium keeps its cookie store in memory and writes it to the profile's
    SQLite database on a delay. ``taskkill /F`` does not run that flush, so every
    cookie written since the last one is discarded -- measured on this machine:
    a cookie written over CDP and then hard-killed was gone on restart in 2 of 2
    trials, while the same cookie survived a graceful close in 2 of 2.

    That matters here more than anywhere else in the project. This host exists to
    hold a GitCode session across restarts so a renewal does not need a fresh QR
    scan, and the session reaches the profile through a CDP cookie write. A stop
    that kills the process therefore destroys exactly the state the host was
    built to preserve, and does it silently: the next renewal just finds no
    session and reports that the user must sign in again.

    ``Browser.close`` is the documented way to ask Chromium to exit cleanly. It
    tears down the DevTools socket it arrived on, so a dropped connection is the
    expected result rather than a failure, and the real check is whether the
    endpoint stops answering.
    """
    try:
        with urlopen(  # noqa: S310 - loopback only
            f"http://127.0.0.1:{port}/json/version", timeout=5.0
        ) as response:
            version = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - not answering means nothing to close
        return True

    ws_url = version.get("webSocketDebuggerUrl")
    if not ws_url:
        return False

    try:
        connection = CdpConnection(ws_url, timeout=timeout)
        connection.call("Browser.close", {}, timeout=timeout)
    except Exception:  # noqa: BLE001 - the socket closes as the browser exits
        pass

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _endpoint_answers(port):
            return True
        time.sleep(_POLL_INTERVAL)
    return not _endpoint_answers(port)


class AuthBrowserHost:
    """Start, inspect and stop the hidden authentication engine.

    The class owns one profile and one port, and nothing else. It does not know
    about OAuth, cookies or renewal: it starts an engine that can *host* those
    things, and the renewer does the rest.
    """

    def __init__(
        self,
        *,
        port: int = DEFAULT_AUTH_PORT,
        profile: Path | None = None,
        headless: bool = True,
        start_timeout: float = DEFAULT_START_TIMEOUT,
        visible_fallback: bool = True,
    ) -> None:
        self._port = port
        self._profile = profile or auth_profile_dir()
        self._headless = headless
        self._start_timeout = start_timeout
        #: Whether a build that rejects ``--headless=new`` may be started in a
        #: visible window anyway. ``True`` for the interactive path, where the
        #: user asked for a browser and a window is expected; ``False`` for an
        #: unattended caller that promised not to put anything on the desktop.
        #:
        #: This has to be decided *before* the launch, not after. The fallback
        #: opens a real window as part of starting, so a caller that inspected the
        #: result and then declined it would already have put the window on
        #: screen -- reporting the right thing while doing the wrong one.
        self._visible_fallback = visible_fallback
        #: Whether the *running* engine was actually started hidden, as opposed
        #: to whether one was requested. ``None`` until a launch has been
        #: observed. Kept separately from ``_headless`` because the two differ
        #: whenever a build ignores ``--headless=new``, and reporting the request
        #: instead of the outcome is how a user gets told a window will not appear
        #: when one is about to.
        self._headless_actual: bool | None = None

    # ── introspection ─────────────────────────────────────────────────────
    @property
    def port(self) -> int:
        return self._port

    @property
    def profile(self) -> Path:
        return self._profile

    @property
    def supported(self) -> bool:
        return os.name == "nt" or sys.platform != "win32"

    def is_running(self) -> bool:
        """Whether something is already serving DevTools on this port."""
        return _endpoint_answers(self._port)

    def status(self) -> AuthHostResult:
        """Report the host without changing anything.

        The mode is inferred from the endpoint rather than remembered, because
        the host outlives the process that started it: a tray that restarts has
        no memory of whether the browser it finds was started headless, and
        guessing "headless" because that is the default would be a claim it
        cannot support. What *is* knowable is that a headless Chromium reports
        ``HeadlessChrome`` in its user agent, so that is what is read.
        """
        if not self.is_running():
            return AuthHostResult(
                status=AuthHostStatus.FAILED,
                mode=AuthHostMode.STOPPED,
                port=self._port,
                profile=str(self._profile),
                detail="no DevTools endpoint is answering on this port",
            )
        mode = self._probe_mode()
        return AuthHostResult(
            status=AuthHostStatus.ALREADY_RUNNING,
            mode=mode,
            port=self._port,
            profile=str(self._profile),
            headless=mode is AuthHostMode.HEADLESS,
        )

    def _probe_mode(self) -> AuthHostMode:
        """Read the running browser's identity to tell headless from visible.

        Both ``product`` and ``userAgent`` are checked, because the headless
        marker is **not** in ``product``. Measured on Chrome 153:

        ===============  ==================================================
        field            value
        ===============  ==================================================
        ``product``      ``Chrome/153.0.8010.53``
        ``userAgent``    ``Mozilla/5.0 (...) HeadlessChrome/153.0.0.0 ...``
        ===============  ==================================================

        Checking ``product`` alone therefore reports every headless browser as
        VISIBLE. That is the opposite error from the one it was written to avoid
        and worse in effect: the host ran headless while telling the user, and
        the tray, that a window was on their screen -- and a caller that had
        asked for no window would reject its own perfectly good hidden host.

        Both fields are checked rather than just ``userAgent`` because
        ``product`` is the documented field for the browser's identity and a
        build that put the marker there instead would be equally correct.
        """
        try:
            endpoint = discover_cdp_endpoint(f"http://127.0.0.1:{self._port}", probe=True)
            browser_ws = endpoint.browser_ws_url()
            if not browser_ws:
                return AuthHostMode.VISIBLE
            # CdpConnection is imported at module scope; the redundant local
            # import that used to sit here shadowed it, so patching the module
            # attribute had no effect on this call and the probe could not be
            # tested without reaching into a private module.
            with CdpConnection(browser_ws, timeout=8.0) as conn:
                version = conn.call("Browser.getVersion", {}, timeout=6.0)
            blob = f"{version.get('product') or ''} {version.get('userAgent') or ''}"
            return AuthHostMode.HEADLESS if "Headless" in blob else AuthHostMode.VISIBLE
        except Exception:  # noqa: BLE001 - a capability probe must never raise
            return AuthHostMode.VISIBLE

    # ── lifecycle ─────────────────────────────────────────────────────────
    def ensure_running(self) -> AuthHostResult:
        """Start the engine if it is not already up. Idempotent.

        Returns a result for every outcome, including the failures. It raises
        nothing: "no browser is installed" is a normal situation with its own
        message, and an exception here would force every caller to catch it just
        to say the same thing.
        """
        if not self.supported:
            return AuthHostResult(
                status=AuthHostStatus.UNSUPPORTED,
                port=self._port,
                profile=str(self._profile),
                detail="no auth-host launcher on this platform",
            )

        if self.is_running():
            mode = self._probe_mode()
            self._headless_actual = mode is AuthHostMode.HEADLESS
            return AuthHostResult(
                status=AuthHostStatus.ALREADY_RUNNING,
                mode=mode,
                port=self._port,
                profile=str(self._profile),
                headless=mode is AuthHostMode.HEADLESS,
                detail="a DevTools endpoint is already answering",
            )

        found = find_browser()
        if found is None:
            return AuthHostResult(
                status=AuthHostStatus.NO_BROWSER_FOUND,
                port=self._port,
                profile=str(self._profile),
                detail=(
                    "no Chrome, Edge, Chromium or Brave installation was found, so "
                    "there is no engine to run the OAuth leg in"
                ),
            )

        name, executable = found

        if not self._headless:
            # Explicitly asked for a visible browser: the interactive path, and
            # the existing launcher already does it correctly.
            launch = launch_debug_browser(
                "about:blank",
                port=self._port,
                profile=self._profile,
                timeout=self._start_timeout,
            )
            self._headless_actual = False
            return AuthHostResult(
                status=(
                    AuthHostStatus.ALREADY_RUNNING
                    if launch.status is BrowserLaunchStatus.ALREADY_RUNNING
                    else AuthHostStatus.STARTED
                    if launch.ok
                    else AuthHostStatus.FAILED
                ),
                mode=AuthHostMode.VISIBLE,
                port=self._port,
                browser=launch.browser or name,
                executable=launch.executable or str(executable),
                profile=str(self._profile),
                headless=False,
                detail=launch.detail,
            )

        if not _headless_supported(executable):
            # Fall back rather than fail, and say so. A visible window is a worse
            # experience than a hidden one, but it is strictly better than no
            # session renewal at all -- and the caller is told, so the tray can
            # avoid claiming the browser is invisible.
            #
            # Unless the caller ruled it out. An unattended monitor that promised
            # not to open windows must be able to keep that promise, and it can
            # only do so here, before the launch: declining a window after it has
            # been opened is not declining it.
            if not self._visible_fallback:
                self._headless_actual = None
                return AuthHostResult(
                    status=AuthHostStatus.FAILED,
                    mode=AuthHostMode.STOPPED,
                    port=self._port,
                    browser=name,
                    executable=str(executable),
                    profile=str(self._profile),
                    headless=False,
                    detail=(
                        f"{name} does not accept --headless=new, and this caller "
                        "does not open windows, so no engine was started"
                    ),
                )
            launch = launch_debug_browser(
                "about:blank",
                port=self._port,
                profile=self._profile,
                timeout=self._start_timeout,
            )
            self._headless_actual = False
            return AuthHostResult(
                status=(
                    AuthHostStatus.ALREADY_RUNNING
                    if launch.status is BrowserLaunchStatus.ALREADY_RUNNING
                    else AuthHostStatus.STARTED
                    if launch.ok
                    else AuthHostStatus.FAILED
                ),
                mode=AuthHostMode.VISIBLE,
                port=self._port,
                browser=launch.browser or name,
                executable=launch.executable or str(executable),
                profile=str(self._profile),
                headless=False,
                detail=(
                    f"{name} does not accept --headless=new, so the authentication "
                    "engine was started in a visible window instead"
                ),
            )

        try:
            self._profile.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return AuthHostResult(
                status=AuthHostStatus.FAILED,
                port=self._port,
                browser=name,
                executable=str(executable),
                profile=str(self._profile),
                detail=f"could not create the profile directory ({type(exc).__name__})",
            )

        argv = [
            str(executable),
            "--headless=new",
            f"--remote-debugging-port={self._port}",
            f"--user-data-dir={self._profile}",
            *_QUIET_FLAGS,
            "about:blank",
        ]

        try:
            if os.name == "nt":
                creationflags = 0
                for flag in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
                    creationflags |= getattr(subprocess, flag, 0)
                subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                    argv,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    creationflags=creationflags,
                    close_fds=True,
                )
            else:  # pragma: no cover - POSIX
                subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                    argv,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                    close_fds=True,
                )
        except OSError as exc:
            return AuthHostResult(
                status=AuthHostStatus.FAILED,
                port=self._port,
                browser=name,
                executable=str(executable),
                profile=str(self._profile),
                detail=f"could not start the authentication engine ({type(exc).__name__})",
            )

        deadline = time.monotonic() + max(1.0, self._start_timeout)
        while time.monotonic() < deadline:
            if self.is_running():
                # Ask the browser what it actually is, rather than assuming the
                # flag was honoured. Some builds accept `--headless=new` on the
                # command line, ignore it, and open a window -- so a launch that
                # merely *asked* for hidden is not evidence that nothing appeared
                # on the user's screen. This is the same standard the rest of the
                # project holds itself to: a request is not an outcome.
                mode = self._probe_mode()
                self._headless_actual = mode is AuthHostMode.HEADLESS
                if mode is AuthHostMode.VISIBLE:
                    log.warning(
                        "the authentication engine on port %s is running visibly "
                        "despite --headless=new",
                        self._port,
                    )
                else:
                    log.info("authentication engine started headless on port %s", self._port)
                return AuthHostResult(
                    status=AuthHostStatus.STARTED,
                    mode=mode,
                    port=self._port,
                    browser=name,
                    executable=str(executable),
                    profile=str(self._profile),
                    headless=mode is AuthHostMode.HEADLESS,
                    detail=(
                        None
                        if mode is AuthHostMode.HEADLESS
                        else f"{name} accepted --headless=new but opened a visible "
                        "window anyway, so the authentication engine is visible"
                    ),
                )
            time.sleep(_POLL_INTERVAL)

        return AuthHostResult(
            status=AuthHostStatus.FAILED,
            port=self._port,
            browser=name,
            executable=str(executable),
            profile=str(self._profile),
            detail=(
                f"{name} was started headless but no DevTools endpoint appeared on "
                f"port {self._port} within {int(self._start_timeout)}s. This "
                f"usually means the profile at {self._profile} is already open in "
                "another window -- close every window using it and retry."
            ),
        )

    def stop(self) -> bool:
        """Stop the engine. Returns whether it is no longer answering.

        Chrome's own process tree is found through the profile directory rather
        than by remembering a PID: the process outlives this object (that is the
        design), so a PID captured at launch is worthless after a restart, while
        the profile on disk is not.

        Order matters, and it is not an optimisation. ``Browser.close`` is tried
        first because it lets Chromium flush its cookie store; the hard kill is
        only the fallback for a browser that ignores the request. Killing first
        would discard every cookie written since the last flush -- measured on
        this machine, a CDP-written cookie survived a graceful close in 2 of 2
        trials and a hard kill in 0 of 2 -- which is precisely the GitCode
        session this host exists to carry across restarts. A stop that destroys
        it forces the user through a fresh QR scan for no visible reason.
        """
        if _graceful_close(self._port):
            return True

        # It did not honour Browser.close: it is wedged, or it is not really a
        # Chromium. Terminate the tree so the port is usable again, accepting
        # the flush loss -- an unreachable host is worse than a stale one.
        log.warning(
            "the authentication engine ignored a graceful close; terminating it "
            "and accepting that its most recent cookies may not reach disk"
        )
        pids = self._pids_for_profile()
        for pid in pids:
            _kill(pid)

        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if not self.is_running():
                return True
            time.sleep(_POLL_INTERVAL)
        return not self.is_running()

    def _pids_for_profile(self) -> list[int]:
        """PIDs of Chromium processes using this host's profile directory."""
        if os.name != "nt":
            return []
        script = (
            "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' or "
            "Name='msedge.exe' or Name='brave.exe' or Name='chromium.exe'\" | "
            "Where-Object { $_.CommandLine -like '*"
            + str(self._profile)
            + "*' } | Select-Object -ExpandProperty ProcessId"
        )
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                ["pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                timeout=30.0,
                check=False,
                text=True,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        out: list[int] = []
        for line in (completed.stdout or "").splitlines():
            line = line.strip()
            if line.isdigit():
                out.append(int(line))
        return out

    def describe(self) -> str:
        """What this host actually is, which depends on what it managed to start.

        It used to say "no user-visible window" unconditionally, which is a claim
        the class cannot make: some browser builds silently ignore
        ``--headless=new`` and open a window anyway, and that is precisely the
        case :meth:`ensure_running` goes to the trouble of detecting. A
        description that contradicts the detected mode is worse than no
        description, because it is the string a user reads when deciding whether
        anything appeared on their screen.

        Before a launch has been observed it reports the *request* and says so, so
        the wording is never a prediction about a window that has not been opened
        yet.
        """
        if self._headless_actual is False:
            return "Chromium authentication engine (running in a visible window)"
        if self._headless_actual is True:
            return "hidden Chromium authentication engine (no user-visible window)"
        if self._headless:
            return "Chromium authentication engine (will start hidden if the build supports it)"
        return "Chromium authentication engine (visible window requested)"

    def __repr__(self) -> str:
        return (
            f"AuthBrowserHost(port={self._port}, profile={str(self._profile)!r}, "
            f"headless={self._headless}, running={self.is_running()})"
        )


def ensure_auth_host(
    *, port: int = DEFAULT_AUTH_PORT, headless: bool = True
) -> AuthHostResult:
    """Convenience wrapper: make sure a hidden engine is available.

    Provided so the tray and the CLI do not each construct a host and have to
    agree on the port and the profile. Two places deciding those independently is
    how the tool ends up with two browsers and one of them unusable.
    """
    return AuthBrowserHost(port=port, headless=headless).ensure_running()
