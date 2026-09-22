"""Start a Chromium-family browser that the tool can actually talk to.

Why this module exists
----------------------
Every failure path in this project ends with the same sentence: "start a browser
with ``--remote-debugging-port``". Until now nothing actually did that. The user
was told to run a command line they had to assemble themselves, and the one
action a stuck user is most likely to take -- ``opencsi login``, or the tray's
"Sign in..." item -- opened the login page with :mod:`webbrowser`, which starts
the *default* browser with **no debugging port at all**.

The result was a closed loop with no exit:

```
no CDP endpoint  ->  "sign in"        (LOGIN_REQUIRED)
        ^                    |
        |                    v
        +---- login opens a browser that has no CDP port
```

The user signs in, the cookie is written to the *wrong* profile, and the tool
still cannot see it. Telling someone to sign in is only honest if signing in can
work, so this module makes it work.

What it does
------------
1. Finds an installed Chrome / Edge / Chromium / Brave.
2. Starts it with a **dedicated** ``--user-data-dir`` and
   ``--remote-debugging-port``, opening the login page.
3. Waits, bounded, for the DevTools endpoint to answer.

The dedicated profile is the same one the README has always told users to
create, so a browser started here is the same browser the rest of the tool
already expects -- not a new, parallel setup.

What it deliberately does not do
--------------------------------
It never touches the user's normal profile. Chrome 147+ refuses remote debugging
on the default ``user-data-dir`` (see :mod:`opencsi.auth.cdp`), so reusing the
user's real profile is not merely impolite, it does not work. And a profile that
holds the user's everyday browsing is not something a usage monitor should be
pointed at anyway.

Launching a browser is an *authentication* action, not a business write. It
issues no request to openCsiTool and changes no server-side state.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

log = logging.getLogger("opencsi.auth.browser_launch")

#: Directory name of the dedicated automation profile, under the user's local
#: app data. Matches the README instructions and :mod:`opencsi.auth.cdp`.
DEDICATED_PROFILE_DIRNAME = "opencsi-cdp-profile"

#: The port the rest of the tool probes first.
DEFAULT_DEBUG_PORT = 9222

#: How long to wait for a freshly started browser to answer on its port. Chrome
#: cold-starts in well under this on the machines this targets; the wait exists
#: so a slow disk produces a slow success rather than a false failure.
DEFAULT_LAUNCH_TIMEOUT = 20.0

_POLL_INTERVAL = 0.25


class BrowserLaunchStatus(str, Enum):
    """Outcome of a launch attempt. Compared by identity, never parsed."""

    #: We started a browser and its DevTools endpoint answered.
    LAUNCHED = "LAUNCHED"
    #: A usable endpoint was already there; nothing was started.
    ALREADY_RUNNING = "ALREADY_RUNNING"
    #: No Chrome/Edge/Chromium/Brave installation was found.
    NO_BROWSER_FOUND = "NO_BROWSER_FOUND"
    #: The process started but never exposed a DevTools endpoint.
    FAILED = "FAILED"
    #: This platform has no launcher implementation.
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class BrowserLaunch:
    """What happened, in a form a caller can branch on and a user can read."""

    status: BrowserLaunchStatus
    browser: str | None = None
    executable: str | None = None
    port: int | None = None
    profile: str | None = None
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in (
            BrowserLaunchStatus.LAUNCHED,
            BrowserLaunchStatus.ALREADY_RUNNING,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "browser": self.browser,
            "executable": self.executable,
            "port": self.port,
            "profile": self.profile,
            "detail": self.detail,
        }


def dedicated_profile_dir() -> Path:
    """Where the dedicated automation profile lives.

    Windows and macOS use ``%LOCALAPPDATA%`` / ``~/Library/Application Support``
    conventions; everything else falls back to XDG or ``~/.local/share``. Kept
    in one function so the launcher and the documentation cannot drift apart.
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / DEDICATED_PROFILE_DIRNAME
    if sys.platform == "darwin":  # pragma: no cover - macOS
        return Path.home() / "Library" / "Application Support" / DEDICATED_PROFILE_DIRNAME
    xdg = os.environ.get("XDG_DATA_HOME")
    root = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return root / DEDICATED_PROFILE_DIRNAME


def _windows_candidates() -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    env = os.environ
    roots = [
        env.get("PROGRAMFILES"),
        env.get("PROGRAMFILES(X86)"),
        env.get("LOCALAPPDATA"),
    ]
    # (display name, relative path under each root)
    rels = [
        ("chrome", Path("Google") / "Chrome" / "Application" / "chrome.exe"),
        ("edge", Path("Microsoft") / "Edge" / "Application" / "msedge.exe"),
        ("brave", Path("BraveSoftware") / "Brave-Browser" / "Application" / "brave.exe"),
        ("chromium", Path("Chromium") / "Application" / "chrome.exe"),
    ]
    for root in roots:
        if not root:
            continue
        for name, rel in rels:
            out.append((name, Path(root) / rel))
    return out


def _macos_candidates() -> list[tuple[str, Path]]:  # pragma: no cover - macOS
    apps = Path("/Applications")
    return [
        ("chrome", apps / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome"),
        ("edge", apps / "Microsoft Edge.app" / "Contents" / "MacOS" / "Microsoft Edge"),
        (
            "brave",
            apps / "Brave Browser.app" / "Contents" / "MacOS" / "Brave Browser",
        ),
        ("chromium", apps / "Chromium.app" / "Contents" / "MacOS" / "Chromium"),
    ]


#: Names to look for on ``PATH`` on platforms without a fixed install layout.
_LINUX_COMMANDS: tuple[tuple[str, str], ...] = (
    ("chrome", "google-chrome"),
    ("chrome", "google-chrome-stable"),
    ("chromium", "chromium"),
    ("chromium", "chromium-browser"),
    ("edge", "microsoft-edge"),
    ("edge", "microsoft-edge-stable"),
    ("brave", "brave-browser"),
)


def find_browser() -> tuple[str, Path] | None:
    """The first installed Chromium-family browser, as ``(name, path)``.

    Returns ``None`` rather than raising: "no browser is installed" is a normal
    situation with its own message, not an exception to be caught and reworded.
    """
    if os.name == "nt":
        for name, path in _windows_candidates():
            try:
                if path.is_file():
                    return name, path
            except OSError:
                continue
        return None

    if sys.platform == "darwin":  # pragma: no cover - macOS
        for name, path in _macos_candidates():
            try:
                if path.is_file():
                    return name, path
            except OSError:
                continue
        return None

    for name, command in _LINUX_COMMANDS:
        found = shutil.which(command)
        if found:
            return name, Path(found)
    return None


def _endpoint_answers(port: int, *, timeout: float = 0.6) -> bool:
    """Whether something is serving the DevTools protocol on ``port``.

    Only the browser-level ``/json/version`` endpoint is consulted. It is the
    documented readiness signal, and unlike a bare TCP connect it cannot be
    satisfied by an unrelated process that happens to hold the port.
    """
    from .cdp import probe_http_endpoint

    try:
        return probe_http_endpoint(f"http://127.0.0.1:{port}", timeout=timeout) is not None
    except Exception:  # noqa: BLE001 - a probe must never raise
        return False


def launch_debug_browser(
    url: str,
    *,
    port: int = DEFAULT_DEBUG_PORT,
    profile: Path | None = None,
    timeout: float = DEFAULT_LAUNCH_TIMEOUT,
    wait: bool = True,
) -> BrowserLaunch:
    """Start (or reuse) a browser whose DevTools endpoint this tool can use.

    Idempotent by design: if the port already answers, nothing is started and
    ``ALREADY_RUNNING`` is returned. Launching a second instance against the same
    profile would silently forward the URL to the first one *without* a debugging
    port, which is the exact failure this module exists to remove.

    ``wait=False`` returns as soon as the process is spawned, for callers that
    would rather poll themselves.
    """
    profile_dir = profile or dedicated_profile_dir()

    # Nothing to do if a usable endpoint is already up. Checked first because it
    # is both the cheapest path and the one that avoids spawning a doomed
    # second instance.
    if _endpoint_answers(port):
        return BrowserLaunch(
            status=BrowserLaunchStatus.ALREADY_RUNNING,
            port=port,
            profile=str(profile_dir),
            detail=f"a DevTools endpoint is already answering on port {port}",
        )

    found = find_browser()
    if found is None:
        return BrowserLaunch(
            status=BrowserLaunchStatus.NO_BROWSER_FOUND,
            port=port,
            profile=str(profile_dir),
            detail=(
                "no Chrome, Edge, Chromium or Brave installation was found; "
                "install one, or start your browser manually with "
                f"--remote-debugging-port={port}"
            ),
        )

    name, executable = found

    try:
        profile_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return BrowserLaunch(
            status=BrowserLaunchStatus.FAILED,
            browser=name,
            executable=str(executable),
            port=port,
            profile=str(profile_dir),
            detail=f"could not create the profile directory: {type(exc).__name__}",
        )

    argv = [
        str(executable),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        # Without this, Chrome shows a "restore pages?" bubble after an unclean
        # shutdown, which sits on top of the login page the user was sent to.
        "--no-first-run",
        "--no-default-browser-check",
        url,
    ]

    try:
        # Detached on purpose: the browser must outlive this process, which for
        # the CLI means the command can exit while the window stays open.
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
        return BrowserLaunch(
            status=BrowserLaunchStatus.FAILED,
            browser=name,
            executable=str(executable),
            port=port,
            profile=str(profile_dir),
            detail=f"could not start the browser: {type(exc).__name__}",
        )

    log.info("started %s with DevTools on port %s", name, port)

    if not wait:
        return BrowserLaunch(
            status=BrowserLaunchStatus.LAUNCHED,
            browser=name,
            executable=str(executable),
            port=port,
            profile=str(profile_dir),
        )

    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if _endpoint_answers(port):
            return BrowserLaunch(
                status=BrowserLaunchStatus.LAUNCHED,
                browser=name,
                executable=str(executable),
                port=port,
                profile=str(profile_dir),
            )
        time.sleep(_POLL_INTERVAL)

    # The process spawned but never opened a port. The overwhelmingly common
    # cause is that this profile is *already* open in another browser window:
    # Chromium then treats the launch as "open a tab over there", and no
    # debugging endpoint is ever created. Saying so is far more useful than a
    # timeout message, because it names the thing the user can close.
    return BrowserLaunch(
        status=BrowserLaunchStatus.FAILED,
        browser=name,
        executable=str(executable),
        port=port,
        profile=str(profile_dir),
        detail=(
            f"{name} started but no DevTools endpoint appeared on port {port} "
            f"within {int(timeout)}s. This usually means the profile at "
            f"{profile_dir} is already open in another window -- close every "
            f"window using it and retry."
        ),
    )
