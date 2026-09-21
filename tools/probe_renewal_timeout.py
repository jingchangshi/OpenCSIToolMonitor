"""Does a cold-start renewal time out *and still succeed*?

Hypothesis: the first `login --renew` reported TIMEOUT, yet afterwards the
browser held a valid 3598-second token. If true, the OAuth round-trip completed
*after* the deadline, and reporting TIMEOUT is misleading -- the session was in
fact renewed.

Method: read the current expiry, delete the cookie, run one renewal with a
short budget while timing it, then check whether a token exists and how long it
has left. Read-only apart from the delete, which is the operation under test and
is reversed by the renewal itself.
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "src")

from opencsi.auth.cdp import CdpCookieProvider  # noqa: E402
from opencsi.auth.oauth_browser import BrowserOAuthRenewer  # noqa: E402
from opencsi.ws import CdpConnection  # noqa: E402

HOST = "opencsitool.com"
COOKIE = "token"


def cookie_expiry(conn, host: str, name: str) -> float | None:
    """Seconds until the cookie expires, or ``None`` when it is absent.

    Uses ``Storage.getCookies``: ``Network.getCookies`` is a page-domain method
    and does not exist on a browser-level WebSocket, which is the only kind
    silent renewal can use.
    """
    result = conn.call("Storage.getCookies")
    now = time.time()
    for entry in result.get("cookies") or []:
        if entry.get("name") != name:
            continue
        domain = str(entry.get("domain") or "")
        if host not in domain:
            continue
        expires = float(entry.get("expires") or 0)
        if expires <= 0:
            return None
        return expires - now
    return None


def cookie_domains(conn, host: str, name: str) -> list[str]:
    """Every stored domain for a cookie name, so deletes hit the right one."""
    result = conn.call("Storage.getCookies")
    out = []
    for entry in result.get("cookies") or []:
        if entry.get("name") != name:
            continue
        domain = str(entry.get("domain") or "")
        if host in domain:
            out.append(domain)
    return out


def first_page_target(conn) -> str | None:
    """A page target id, so page-scoped domains (Network) can be used."""
    result = conn.call("Target.getTargets")
    for info in result.get("targetInfos") or []:
        if info.get("type") == "page":
            return str(info.get("targetId"))
    return None


def main() -> int:
    provider = CdpCookieProvider()
    endpoint = provider.probe_endpoint()
    ws_url = endpoint.browser_ws_url()

    with CdpConnection(ws_url) as conn:
        before = cookie_expiry(conn, HOST, COOKIE)
        print(f"token before      : {before if before is None else round(before, 1)}s")

        if before is None:
            print("no token to delete; nothing to test")
            return 0

        # Clear the openCsiTool token only, leaving the GitCode SSO session on
        # gitcode.com intact -- exactly the real scenario silent renewal exists
        # for. This needs a *page* target: the Network domain, and therefore
        # Network.deleteCookies, is page-scoped, not browser-scoped.
        target = first_page_target(conn)
        if target is None:
            print("no page target to delete the cookie through")
            return 1
        attached = conn.call(
            "Target.attachToTarget", {"targetId": target, "flatten": True}
        )
        session_id = attached.get("sessionId")
        try:
            conn.call("Network.enable", session_id=session_id)
            for domain in cookie_domains(conn, HOST, COOKIE):
                conn.call(
                    "Network.deleteCookies",
                    {"name": COOKIE, "domain": domain},
                    session_id=session_id,
                )
        finally:
            conn.call(
                "Target.detachFromTarget", {"sessionId": session_id}
            )
        with CdpConnection(ws_url) as verify:
            gone = cookie_expiry(verify, HOST, COOKIE)
        print(f"token after delete: {gone}")
        if gone is not None:
            print("the cookie could not be deleted; aborting so nothing is disturbed")
            return 1

    # A deliberately short budget, to reproduce the cold-start deadline.
    budget = 20.0
    renewer = BrowserOAuthRenewer(ws_url, timeout=budget)
    started = time.monotonic()
    result = renewer.renew()
    elapsed = time.monotonic() - started

    print(f"\nrenewal budget    : {budget}s")
    print(f"renewal took      : {elapsed:.1f}s")
    print(f"reported outcome  : {result.status.value}")
    print(f"detail            : {result.detail}")

    # The question that matters: is the session actually alive now?
    time.sleep(1.0)
    with CdpConnection(ws_url) as conn:
        after = cookie_expiry(conn, HOST, COOKIE)
    print(f"\ntoken after renew : {after if after is None else round(after, 1)}s")

    if result.status.value == "TIMEOUT" and after is not None:
        print("\nCONFIRMED: the renewal SUCCEEDED but was reported as TIMEOUT.")
        print("The round-trip outlived the budget, and the outcome was not re-checked.")
        return 2
    if after is None:
        print("\nThe session is genuinely gone; the reported outcome stands.")
        return 0
    print("\nThe outcome matched reality.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
