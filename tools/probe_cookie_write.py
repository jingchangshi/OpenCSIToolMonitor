"""Probe whether a minted session can be installed into the browser over CDP.

Read-only with respect to the *network*: it sends no HTTP request to any service
and never prints a cookie value. It only exercises the CDP cookie-write API
against an already-running browser, to find out whether that API is usable --
because the alternative is writing a credential to disk, which this project
forbids.

Why this matters
----------------
The browserless renewal path mints a real openCsiTool session over plain HTTP.
The browser it read the GitCode credential from never learns the new cookie, so
the token exists only in the memory of the process that renewed. That makes
``opencsi login --renew`` succeed and the very next ``opencsi usage`` fail, which
is a partial success reported as a complete one.

The browser is already this project's credential store -- that is the documented
design, and the reason a cookie is never written to disk. So the fix is to hand
the minted cookie *to the browser*, not to a file.

Safety posture: touches no network, never prints a token value, writes nothing to
disk. It does set a cookie in the target browser, which is the operation under
test; that cookie is a synthetic marker, not a credential.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opencsi.ws import CdpConnection  # noqa: E402

MARKER_NAME = "opencsi_probe_marker"
MARKER_VALUE = "probe-only-not-a-credential"


def _http_json(url: str):
    with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - loopback
        return json.loads(resp.read().decode("utf-8"))


def _browser_ws(port: int) -> str | None:
    try:
        version = _http_json(f"http://127.0.0.1:{port}/json/version")
    except Exception as exc:  # noqa: BLE001
        print(f"  port {port}: no DevTools ({type(exc).__name__})")
        return None
    return version.get("webSocketDebuggerUrl")


def main() -> int:
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 9222
    print(f"probing the cookie-write API on 127.0.0.1:{port}")
    print()

    ws_url = _browser_ws(port)
    if not ws_url:
        print("VERDICT: NO_BROWSER")
        return 2

    conn = CdpConnection(ws_url, timeout=10.0)
    try:
        # ``CdpConnection`` opens the socket on first use, so the connection is
        # proven by the first real call rather than by an explicit connect().
        print("connecting to the browser-level socket")

        # Storage.setCookies is the browser-wide write. It takes a list of cookie
        # params, and a browser-wide call must NOT carry a ``url`` -- supplying
        # both is what makes the API reject the call.
        params = {
            "cookies": [
                {
                    "name": MARKER_NAME,
                    "value": MARKER_VALUE,
                    "domain": ".opencsitool.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                    "sameSite": "Lax",
                }
            ]
        }
        try:
            result = conn.call("Storage.setCookies", params, timeout=10.0)
            print(f"Storage.setCookies -> {json.dumps(result)[:200]}")
            print("  write accepted")
        except Exception as exc:  # noqa: BLE001
            print(f"Storage.setCookies FAILED: {type(exc).__name__}: {exc}")
            print("VERDICT: SETCOOKIES_UNUSABLE")
            return 1

        # Read it back through the browser-wide walk to prove it landed.
        found = None
        try:
            got = conn.call("Storage.getCookies", {}, timeout=10.0)
            for cookie in got.get("cookies", []):
                if cookie.get("name") == MARKER_NAME:
                    found = cookie
                    break
        except Exception as exc:  # noqa: BLE001
            print(f"Storage.getCookies FAILED: {type(exc).__name__}: {exc}")

        if found:
            print(
                "read back: present, "
                f"domain={found.get('domain')} path={found.get('path')} "
                f"secure={found.get('secure')} httpOnly={found.get('httpOnly')}"
            )
        else:
            print("read back: NOT FOUND -- the write did not take effect")
            print("VERDICT: WRITE_NOT_VISIBLE")
            return 1

        # Clean up: the marker must not outlive the probe. There is no
        # ``Storage.deleteCookies`` in this CDP version, so the marker is
        # overwritten with an expiry in the past, which the browser then drops.
        try:
            conn.call(
                "Storage.setCookies",
                {
                    "cookies": [
                        {
                            "name": MARKER_NAME,
                            "value": "expired",
                            "domain": ".opencsitool.com",
                            "path": "/",
                            "expires": 1,
                        }
                    ]
                },
                timeout=10.0,
            )
            got = conn.call("Storage.getCookies", {}, timeout=10.0)
            still = [c["name"] for c in got.get("cookies", []) if c.get("name") == MARKER_NAME]
            print("cleanup: marker removed" if not still else f"cleanup: marker STILL present {still}")
        except Exception as exc:  # noqa: BLE001
            print(f"cleanup FAILED ({type(exc).__name__}); remove {MARKER_NAME} by hand")

        print()
        print("VERDICT: COOKIE_WRITE_AVAILABLE")
        return 0
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
