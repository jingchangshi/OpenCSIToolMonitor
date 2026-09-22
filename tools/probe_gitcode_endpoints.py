"""LIVE probe: what does a *signed-in* GitCode SPA actually request?

The bridge experiment (``probe_gitcode_sso_bridge2.py``) produced a result that
needs one more measurement to interpret. With no credential, every candidate
``/uc/api/v1/user/*`` path answered ``401`` with a gateway-shaped error envelope
(``error_code`` / ``trace_id``). With the planted GitCode session cookies, the
*same* paths answered ``404`` with an application-shaped envelope
(``timestamp`` / ``status`` / ``error`` / ``path``).

Two different layers answered. The gateway stopped rejecting the request, which
is what authentication looks like from the outside; the application then said
"no such route". So the credential was accepted and the *paths were wrong*.

That makes endpoint discovery the measurement, not an assumption. This probe
reads the paths off the wire: it opens GitCode's own site in the profile that is
already signed in, enables the ``Network`` domain, and records every XHR/fetch
the SPA issues while it renders a signed-in page.

Only the *path* is recorded. Query strings are stripped, because GitCode's URLs
carry ``token=`` and ``code=`` style parameters and the whole point is to leave
with endpoint names rather than credentials. No response body is read at all.

Usage
-----
    python tools/probe_gitcode_endpoints.py --port 9222 --url https://gitcode.com/

Labels: LIVE, NETWORK, GET only. It navigates one background tab in the profile
you point it at and issues no request of its own. It never prints a query string,
and reads no response body at all.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, "src")

from opencsi.auth.cdp import discover_cdp_endpoint  # noqa: E402
from opencsi.ws import CdpConnection  # noqa: E402

#: Resource types worth recording. ``Fetch``/``XHR`` is where an SPA's API calls
#: live; ``Document`` is recorded too so a redirect chain is visible.
_WANTED_TYPES = {"XHR", "Fetch", "Document"}

#: Hosts that are obviously telemetry or static assets, whose paths would only
#: add noise to the endpoint list.
_NOISE = (
    "hm.baidu.com",
    "google-analytics.com",
    "googletagmanager.com",
    "cdn-static.gitcode.com",
    "sentry",
    "doubleclick",
)


def record(port: int, url: str, seconds: float) -> list[dict[str, Any]]:
    endpoint = discover_cdp_endpoint(f"http://127.0.0.1:{port}", probe=True)
    browser_ws = endpoint.browser_ws_url()
    if not browser_ws:
        raise RuntimeError(f"port {port} exposes no browser-level WebSocket")

    events: list[dict[str, Any]] = []
    with CdpConnection(browser_ws, timeout=30.0) as conn:
        target = conn.call(
            "Target.createTarget", {"url": "about:blank", "background": True}, timeout=15.0
        )
        target_id = target.get("targetId")
        attached = conn.call(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}, timeout=15.0
        )
        session_id = attached.get("sessionId")
        for domain in ("Network", "Page"):
            try:
                conn.call(f"{domain}.enable", session_id=session_id, timeout=10.0)
            except Exception:  # noqa: BLE001 - enable is best-effort
                pass
        conn.call("Page.navigate", {"url": url}, session_id=session_id, timeout=15.0)

        deadline = time.monotonic() + max(2.0, seconds)
        while time.monotonic() < deadline:
            message = conn.poll_event(timeout=0.5)
            if message is None:
                continue
            method = message.get("method")
            params = message.get("params") or {}
            if method == "Network.requestWillBeSent":
                request = params.get("request") or {}
                events.append(
                    {
                        "url": str(request.get("url") or ""),
                        "method": str(request.get("method") or ""),
                        "type": str(params.get("type") or ""),
                        "status": None,
                    }
                )
            elif method == "Network.responseReceived":
                response = params.get("response") or {}
                events.append(
                    {
                        "url": str(response.get("url") or ""),
                        "method": "",
                        "type": str(params.get("type") or ""),
                        "status": response.get("status"),
                    }
                )
        try:
            conn.call("Target.closeTarget", {"targetId": target_id}, timeout=10.0)
        except Exception:  # noqa: BLE001
            pass
    return events


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--url", default="https://gitcode.com/")
    parser.add_argument("--seconds", type=float, default=12.0)
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    args = parser.parse_args()

    events = record(args.port, args.url, args.seconds)

    # Collapse to (host, method, path, status) with the query string removed.
    # This is the redaction step: a GitCode URL can carry `token=` and `code=`,
    # so the query is dropped before anything is counted or printed.
    seen: Counter[tuple[str, str, str, Any]] = Counter()
    for event in events:
        raw = event["url"]
        if not raw.startswith("http"):
            continue
        parts = urlsplit(raw)
        host = parts.netloc.lower()
        if any(noise in host for noise in _NOISE):
            continue
        if event["type"] and event["type"] not in _WANTED_TYPES:
            continue
        seen[(host, event["method"] or "-", parts.path or "/", event["status"])] += 1

    rows = [
        {"host": host, "method": method, "path": path, "status": status, "count": count}
        for (host, method, path, status), count in sorted(seen.items())
    ]

    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0

    print(f"navigation : {args.url}")
    print(f"port       : {args.port}")
    print(f"requests   : {len(events)} captured, {len(rows)} distinct")
    print()
    print(f"{'host':26s} {'method':7s} {'status':>6s} {'n':>3s}  path")
    print("-" * 100)
    for row in rows:
        status = row["status"] if row["status"] is not None else "-"
        print(
            f"{row['host']:26s} {row['method']:7s} {str(status):>6s} "
            f"{row['count']:>3d}  {row['path']}"
        )

    api = [r for r in rows if r["host"].startswith("web-api") or "/api/" in r["path"]]
    print()
    print(f"API-shaped requests ({len(api)}):")
    for row in api:
        print(f"  {row['method']:7s} {row['path']}  -> {row['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
