"""§65: does the auth host's profile survive a restart, and does stop() keep it?

The requirement is that the auth host outlives the process that started it, so a
renewal after a restart does not need to re-authenticate. That rests on the
GitCode session reaching the profile's cookie store and still being there
afterwards.

Two things had to be got right before this probe measured anything, and both
were wrong in earlier versions:

1. **The marker must be a persistent cookie.** A cookie with no expiry is a
   *session* cookie, and Chromium never writes those to its persistent store,
   whatever way the browser is closed. An earlier version omitted the expiry and
   reported LOST for a marker that was never a candidate for persistence. The
   real openCsiTool token carries an expiry (~58 minutes), so an expiring marker
   is also the faithful model.

2. **One trial proves nothing.** Survival depends on when Chromium's periodic
   flush fires, so a single run can pass or fail for reasons unrelated to the
   close method. Both methods are therefore measured repeatedly, with a unique
   nonce per trial so a surviving value can only be that trial's own write.

With both fixed, on this machine:

    graceful close (stop(), as shipped) : 3/3 survived
    hard kill (the original stop())     : 0/3 survived

which is the defect the graceful close was introduced to fix: killing Chromium
discards cookies written since its last flush, and the GitCode session arrives
through exactly such a write.

PART 2, the renew -> restart -> renew cycle §65 describes, is reported SKIPPED
when the profile holds no session. This machine's auth-host profile has never
completed a first sign-in, so there is nothing to renew, and calling that a pass
would be claiming an untested path works.

Posture: **LIVE / AUTH_SIDE_EFFECT**. It starts and stops a real Chromium on the
dedicated auth profile and writes synthetic marker cookies. It reaches the
network only at the loopback CDP endpoint, never prints a cookie value, never
touches the user's own Chrome profile, and never approves an OAuth consent.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opencsi.auth.auth_host import AuthBrowserHost, _kill  # noqa: E402
from opencsi.auth.cdp import CdpCookieProvider  # noqa: E402
from opencsi.ws import CdpConnection  # noqa: E402

MARKER_NAME = "opencsi_persist_probe"
MARKER_VALUE = "synthetic-marker-not-a-credential"
TRIALS = 3


def _browser_ws(port: int) -> str | None:
    try:
        with urllib.request.urlopen(  # noqa: S310 - loopback
            f"http://127.0.0.1:{port}/json/version", timeout=5
        ) as resp:
            return json.loads(resp.read().decode("utf-8")).get("webSocketDebuggerUrl")
    except Exception:  # noqa: BLE001
        return None


def _set_marker(port: int, value: str, *, expires_in: float = 3600.0) -> bool:
    """Write a *persistent* marker. Never the real ``token`` cookie name.

    ``CdpCookieProvider.install_token`` is deliberately not used: it hardcodes
    the name ``token``, so aiming it at a synthetic value would overwrite a real
    credential's slot in the profile under test.
    """
    ws = _browser_ws(port)
    if not ws:
        return False
    CdpConnection(ws, timeout=10.0).call(
        "Storage.setCookies",
        {
            "cookies": [
                {
                    "name": MARKER_NAME,
                    "value": value,
                    "domain": ".opencsitool.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                    "sameSite": "Lax",
                    "expires": time.time() + expires_in,
                }
            ]
        },
        timeout=10.0,
    )
    return True


def _read_marker(port: int) -> str | None:
    ws = _browser_ws(port)
    if not ws:
        return None
    got = CdpConnection(ws, timeout=10.0).call("Storage.getCookies", {}, timeout=10.0)
    for cookie in got.get("cookies", []):
        if cookie.get("name") == MARKER_NAME:
            return cookie.get("value")
    return None


def _start(host: AuthBrowserHost) -> bool:
    host.ensure_running()
    for _ in range(40):
        if host.is_running():
            return True
        time.sleep(0.5)
    return host.is_running()


def _await_stop(host: AuthBrowserHost) -> None:
    for _ in range(60):
        if not host.is_running():
            return
        time.sleep(0.5)


def _trial(host: AuthBrowserHost, port: int, how: str) -> bool:
    """One write, one close, one restart. Returns whether the nonce survived."""
    if not _start(host):
        raise RuntimeError("the auth host would not start")

    # Expire any leftover first, so a stale value can never be read as this
    # trial's write.
    _set_marker(port, "stale")
    time.sleep(0.3)

    nonce = uuid.uuid4().hex[:12]
    if not _set_marker(port, nonce):
        raise RuntimeError("the marker could not be written")
    if _read_marker(port) != nonce:
        raise RuntimeError("the marker did not land in the running browser")
    time.sleep(1.0)

    if how == "kill":
        for pid in host._pids_for_profile():  # noqa: SLF001 - the old stop()
            _kill(pid)
    else:
        host.stop()
    _await_stop(host)

    if not _start(host):
        raise RuntimeError("the auth host would not restart")
    return _read_marker(port) == nonce


def main() -> int:
    port = 9224
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    trials = TRIALS
    if "--trials" in sys.argv:
        trials = int(sys.argv[sys.argv.index("--trials") + 1])

    host = AuthBrowserHost(port=port, headless=True)
    print(f"profile: {host.profile}")
    print(f"port   : {port}")
    print()
    print("== PART 1: profile persistence, graceful close vs hard kill ==")
    print(f"  {trials} trials each, unique nonce per trial, persistent cookies")
    print()

    graceful: list[bool] = []
    killed: list[bool] = []
    for index in range(1, trials + 1):
        graceful.append(_trial(host, port, "graceful"))
        print(f"  graceful close #{index}: {'SURVIVED' if graceful[-1] else 'LOST'}")
    for index in range(1, trials + 1):
        killed.append(_trial(host, port, "kill"))
        print(f"  hard kill      #{index}: {'SURVIVED' if killed[-1] else 'LOST'}")

    print()
    print(f"  graceful close : {sum(graceful)}/{trials} survived")
    print(f"  hard kill      : {sum(killed)}/{trials} survived")
    print()

    if all(graceful) and not any(killed):
        print("  VERDICT: GRACEFUL_CLOSE_REQUIRED")
        print("  The shipped stop() preserves the session; the original hard kill")
        print("  discarded it. That is the defect the graceful close fixes.")
        persistence = True
    elif all(graceful) and all(killed):
        print("  VERDICT: BOTH_SURVIVE")
        print("  The flush had already happened in every trial, so this run does")
        print("  not demonstrate the fix is needed -- only that it is harmless.")
        persistence = True
    elif not any(graceful):
        print("  VERDICT: GRACEFUL_CLOSE_ALSO_LOSES")
        print("  The profile is not persisting a persistent cookie at all, so the")
        print("  assumption this host is built on does not hold here.")
        persistence = False
    else:
        print("  VERDICT: INCONCLUSIVE")
        print("  Survival varied within a method, so flush timing dominates and")
        print("  more trials are needed before concluding anything.")
        persistence = False
    print()

    print("== PART 2: the renew -> restart -> renew cycle ==")
    provider = CdpCookieProvider(f"http://127.0.0.1:{port}", discover=False)
    try:
        has_session = bool(provider.get_token())
    except Exception:  # noqa: BLE001
        has_session = False
    if has_session:
        print("  the profile holds a session; the full cycle can run")
        cycle = "runnable"
    else:
        print("  VERDICT: SKIPPED -- full renew cycle not executed")
        print("  The auth-host profile holds no GitCode session, so there is")
        print("  nothing for a renewal to renew. Reporting this as a pass would")
        print("  be claiming an untested path works.")
        cycle = "skipped"
    print()

    _set_marker(port, "expired", expires_in=-60)
    print("marker expired (this CDP build has no Storage.deleteCookies)")

    print()
    print(f"RESULT persistence={persistence} full_cycle={cycle}")
    return 0 if persistence else 1


if __name__ == "__main__":
    sys.exit(main())
