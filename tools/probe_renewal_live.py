"""Diagnose why the silent OAuth renewal reports TIMEOUT instead of a state.

Read-only against the live browser: it creates one background target, follows
the OAuth redirect and reports where it lands. It never prints a cookie value.
"""

from __future__ import annotations

import json
import sys
import time

sys.path.insert(0, "src")

from opencsi.auth.cdp import CdpCookieProvider  # noqa: E402
from opencsi.auth.oauth_browser import BrowserOAuthRenewer  # noqa: E402
from opencsi.ws import CdpConnection  # noqa: E402


def main() -> int:
    provider = CdpCookieProvider()
    try:
        endpoint = provider.probe_endpoint()
    except Exception as exc:  # noqa: BLE001
        print(f"could not resolve a CDP endpoint: {type(exc).__name__}: {exc}")
        return 1

    ws_url = endpoint.browser_ws_url()
    print(f"browser ws: {str(ws_url)[:70]}...")
    print(f"source    : {endpoint.source}")
    print(f"oauth url : {BrowserOAuthRenewer(provider).oauth_url()}")

    for host in ("opencsitool.com", "gitcode.com"):
        try:
            with CdpConnection(ws_url) as conn:
                result = conn.call(
                    "Network.getCookies", {"urls": [f"https://{host}/"]}
                )
            names = sorted(c.get("name", "?") for c in (result.get("cookies") or []))
            print(f"cookies[{host}]: {names or '(none)'}")
        except Exception as exc:  # noqa: BLE001
            print(f"cookies[{host}]: error {type(exc).__name__}: {exc}")

    renewer = BrowserOAuthRenewer(endpoint.browser_ws_url(), timeout=45.0)
    print("\n--- driving OAuth in a background target ---")
    started = time.monotonic()
    result = renewer.renew()
    elapsed = time.monotonic() - started
    print(f"\noutcome   : {result.status.value} after {elapsed:.1f}s")
    print(f"detail    : {result.detail}")
    evidence = getattr(renewer, "last_evidence", None)
    if evidence is not None:
        payload = (
            evidence.as_dict() if hasattr(evidence, "as_dict") else vars(evidence)
        )
        print(f"evidence  : {json.dumps(payload, indent=2, default=str)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
