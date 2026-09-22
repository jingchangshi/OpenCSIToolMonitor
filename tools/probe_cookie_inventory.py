"""Read-only diagnostic: which auth cookies does the CDP browser still hold?

Answers one question -- is silent renewal failing because of a bug, or because
the GitCode SSO session itself is gone? Prints only names, domains and expiry,
never values, so no credential can reach a terminal or a log.

This is read-only in the strict sense: it calls ``Storage.getCookies`` and
nothing else. It performs no navigation, writes no cookie, touches no network
of its own, and never prints a token value.
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "src")

from opencsi.auth.cdp import discover_cdp_endpoint  # noqa: E402
from opencsi.ws import CdpConnection  # noqa: E402


def main() -> int:
    endpoint = discover_cdp_endpoint(ports=(9222,))
    browser_ws = endpoint.browser_ws_url()
    if not browser_ws:
        print("no browser-level WebSocket")
        return 1

    with CdpConnection(browser_ws, timeout=10.0) as conn:
        result = conn.call("Storage.getCookies", {}, timeout=10.0)

    cookies = result.get("cookies", [])
    print(f"total cookies: {len(cookies)}")
    print()

    now = time.time()
    interesting = [
        c
        for c in cookies
        if any(
            key in str(c.get("domain", "")).lower()
            for key in ("gitcode", "opencsitool", "csdn")
        )
    ]

    if not interesting:
        print("NO gitcode/opencsitool cookies at all")
        return 0

    print(f"{'domain':28s} {'name':22s} {'expires':>18s}  httpOnly")
    print("-" * 80)
    for cookie in sorted(interesting, key=lambda c: (c.get("domain", ""), c.get("name", ""))):
        expires = cookie.get("expires")
        if expires in (None, -1):
            when = "session"
        else:
            remaining = expires - now
            when = f"{remaining / 3600:+.1f}h" if remaining > 0 else "EXPIRED"
        print(
            f"{str(cookie.get('domain')):28s} "
            f"{str(cookie.get('name')):22s} "
            f"{when:>18s}  {cookie.get('httpOnly')}"
        )

    print()
    has_token = any(c.get("name") == "token" and "opencsitool" in str(c.get("domain")) for c in interesting)
    print(f"openCsiTool 'token' present : {has_token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
