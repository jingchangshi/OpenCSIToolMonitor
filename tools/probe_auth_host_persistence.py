"""§65: does the auth host's profile survive a stop/restart?

The requirement is that the auth host outlives the process that started it, so a
renewal after a restart does not need to re-authenticate. That rests entirely on
the profile persisting on disk.

The full cycle §65 describes (renew -> usage -> restart -> renew) needs a signed
in GitCode session inside the auth-host profile, and this machine has none: the
profile holds zero cookies, because no first sign-in has ever been completed in
it. So this probe separates the two halves rather than claim a pass:

  PART 1 (credential-free) -- plant a *synthetic marker* cookie, stop the host,
    restart it, read the marker back. That is a real cookie write and a real
    process restart, and it proves the property §65 is actually about.

  PART 2 (needs a credential) -- the renew cycle. Reported SKIPPED when the
    profile holds no session, because running it would prove nothing and calling
    it a pass would be claiming an untested path works.

Posture: **LIVE / AUTH_SIDE_EFFECT**. It starts and stops a real Chromium on the
dedicated auth profile and writes one synthetic cookie. It reaches the network
only at the loopback CDP endpoint. It never prints a cookie value, never touches
the user's own Chrome profile, and never approves an OAuth consent.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opencsi.auth.auth_host import AuthBrowserHost  # noqa: E402
from opencsi.auth.cdp import CdpCookieProvider  # noqa: E402
from opencsi.ws import CdpConnection  # noqa: E402

MARKER_NAME = "opencsi_persist_probe"
MARKER_VALUE = "synthetic-marker-not-a-credential"


def _browser_ws(port: int) -> str | None:
    try:
        with urllib.request.urlopen(  # noqa: S310 - loopback
            f"http://127.0.0.1:{port}/json/version", timeout=5
        ) as resp:
            return json.loads(resp.read().decode("utf-8")).get("webSocketDebuggerUrl")
    except Exception:  # noqa: BLE001
        return None


def _set_marker(port: int, *, expires: float | None = None) -> bool:
    """Write the marker. Uses a distinct name, never the real ``token`` cookie.

    ``CdpCookieProvider.install_token`` is deliberately *not* used here: it
    hardcodes the cookie name ``token``, so pointing it at a synthetic value
    would overwrite a real credential's slot in the profile under test.
    """
    ws = _browser_ws(port)
    if not ws:
        return False
    conn = CdpConnection(ws, timeout=10.0)
    cookie: dict[str, object] = {
        "name": MARKER_NAME,
        "value": MARKER_VALUE,
        "domain": ".opencsitool.com",
        "path": "/",
        "secure": True,
        "httpOnly": True,
        "sameSite": "Lax",
    }
    if expires is not None:
        cookie["expires"] = expires
    conn.call("Storage.setCookies", {"cookies": [cookie]}, timeout=10.0)
    return True


def _read_marker(port: int) -> str | None:
    ws = _browser_ws(port)
    if not ws:
        return None
    conn = CdpConnection(ws, timeout=10.0)
    got = conn.call("Storage.getCookies", {}, timeout=10.0)
    for cookie in got.get("cookies", []):
        if cookie.get("name") == MARKER_NAME:
            return cookie.get("value")
    return None


def main() -> int:
    port = 9224
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    host = AuthBrowserHost(port=port, headless=True)
    print(f"profile: {host.profile}")
    print(f"port   : {port}")
    print()

    print("== PART 1: does the profile survive a stop/restart? ==")
    if not host.is_running():
        print("  starting the host...")
        host.ensure_running()
    if not host.is_running():
        print("  VERDICT: HOST_UNAVAILABLE")
        return 2

    if not _set_marker(port):
        print("  VERDICT: COOKIE_WRITE_UNAVAILABLE")
        return 2
    before = _read_marker(port)
    print(f"  marker before restart: {before!r}")
    if before != MARKER_VALUE:
        print("  VERDICT: MARKER_DID_NOT_LAND")
        return 2

    print("  stopping the host...")
    stopped = host.stop()
    print(f"  stop() -> {stopped}; running now: {host.is_running()}")
    if host.is_running():
        print("  VERDICT: HOST_DID_NOT_STOP")
        return 2

    # A stop that deleted the profile would force a re-authentication on the
    # next renewal, which is the failure this probe exists to catch.
    cookies_db = Path(host.profile) / "Default" / "Network" / "Cookies"
    print(f"  Cookies db present after stop: {cookies_db.is_file()}")

    print("  restarting the host...")
    restarted = host.ensure_running()
    print(f"  ensure_running -> {restarted.status.value} mode={restarted.mode.value}")
    for _ in range(20):
        if host.is_running():
            break
        time.sleep(0.5)

    after = _read_marker(port)
    print(f"  marker after restart : {after!r}")

    persisted = after == MARKER_VALUE
    print()
    if persisted:
        print("  VERDICT: PROFILE_PERSISTENCE_CONFIRMED")
        print("  Written before the stop, read after the restart: what carries")
        print("  state across a restart is the profile on disk, not process memory.")
    else:
        print("  VERDICT: PROFILE_PERSISTENCE_NOT_CONFIRMED")
    print()

    print("== PART 2: the renew -> restart -> renew cycle ==")
    provider = CdpCookieProvider(f"http://127.0.0.1:{port}", discover=False)
    try:
        has_session = bool(provider.get_token())
    except Exception:  # noqa: BLE001
        has_session = False
    if has_session:
        print("  the profile holds a session; run probe_auth_host_cycle.py")
        cycle = "runnable"
    else:
        print("  VERDICT: SKIPPED -- full renew cycle not executed")
        print("  The auth-host profile holds no GitCode session, so there is")
        print("  nothing for a renewal to renew. Reporting this as a pass would")
        print("  be claiming an untested path works.")
        cycle = "skipped"
    print()

    _set_marker(port, expires=1)
    print("marker expired (this CDP build has no Storage.deleteCookies)")

    print()
    print(f"RESULT persistence={persisted} full_cycle={cycle}")
    return 0 if persisted else 1


if __name__ == "__main__":
    sys.exit(main())
