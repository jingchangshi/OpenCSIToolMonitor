"""LIVE experiment, phase 2: does a planted GitCode session authenticate *in-browser*?

Phase 1 (``probe_gitcode_sso_bridge.py``) asked GitCode's authenticated API from
*outside* the browser, with a hand-built ``Cookie`` header, and got ``404`` where
a signed-out request got ``401``. That is a real difference, but it does not
settle the question, because a raw HTTP client is not the same client the site
expects: no ``Origin``-bound storage, no browser TLS/HTTP fingerprint, and
possibly the wrong endpoint entirely.

This probe removes those confounds. It evaluates ``fetch`` **inside a page on
gitcode.com**, so the request is a genuine same-origin browser request carrying
whatever the profile's cookie jar holds. It then plants the session cookies via
CDP and repeats the *same* requests, so the only variable is the credential.

Endpoint discovery is part of the measurement, not an assumption: several
candidate paths are tried and their status codes compared, because "which
endpoint is the authenticated one" is exactly what the earlier document left
open. Status codes and JSON *key names* are reported; no body value is ever
printed.

Usage
-----
    python tools/probe_gitcode_sso_bridge2.py --source-port 9222 --target-port 9333

Labels: LIVE, NETWORK, AUTH_SIDE_EFFECT (plants cookies in the target profile),
GET only against every remote endpoint. It never prints a body value. It is never
run by the test suite.
"""
#: labels: LIVE, NETWORK, AUTH_SIDE_EFFECT

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Mapping

sys.path.insert(0, "src")

from opencsi.auth.cdp import discover_cdp_endpoint  # noqa: E402
from opencsi.ws import CdpConnection  # noqa: E402

BRIDGE_COOKIES = ("GITCODE_ACCESS_TOKEN", "GITCODE_REFRESH_TOKEN", "GitCodeUserName")

#: Candidate authenticated endpoints. The first group is the set the signed-in
#: SPA was *observed* calling (``probe_gitcode_endpoints.py`` against a real
#: signed-in profile), which is why ``/uc/api/v1/user/oauth/userInfo`` leads:
#: it answered 200 on the wire. The second group is the guessed set the earlier
#: phase tried, kept so the contrast between "observed" and "assumed" stays
#: visible in the output.
CANDIDATES = (
    "/uc/api/v1/user/oauth/userInfo",
    "/uc/api/v1/user/oauth/token",
    "/uc/api/v1/user/notify/targetInfo",
    "/uc/api/v1/user/info",
    "/uc/api/v1/user/current",
    "/uc/api/v1/user/profile",
)

#: The page the probe runs from. Same-origin with the API host's parent domain,
#: which is what makes the cookies apply.
PAGE_ORIGIN = "https://gitcode.com"

#: Reports, for each candidate, the HTTP status and the JSON key names of the
#: body. Key names only -- a signed-in body carries the account's own details.
_PROBE_JS = """
(async () => {
  const paths = %s;
  const out = [];
  for (const p of paths) {
    const row = { path: p };
    try {
      const r = await fetch(p, {
        method: 'GET',
        credentials: 'include',
        headers: { 'Accept': 'application/json, text/plain, */*' },
      });
      row.status = r.status;
      const text = await r.text();
      row.bytes = text.length;
      try {
        const j = JSON.parse(text);
        row.keys = Object.keys(j).slice(0, 12);
        if (j && typeof j === 'object' && j.data && typeof j.data === 'object') {
          row.dataKeys = Object.keys(j.data).slice(0, 12);
        }
      } catch (e) { row.keys = null; }
    } catch (e) {
      row.error = String(e).slice(0, 60);
    }
    out.push(row);
  }
  return JSON.stringify(out);
})()
"""


def _endpoint(port: int):
    endpoint = discover_cdp_endpoint(f"http://127.0.0.1:{port}", probe=True)
    browser_ws = endpoint.browser_ws_url()
    if not browser_ws:
        raise RuntimeError(f"port {port} exposes no browser-level WebSocket")
    return browser_ws


def read_cookies(port: int) -> list[Mapping[str, Any]]:
    with CdpConnection(_endpoint(port), timeout=20.0) as conn:
        result = conn.call("Storage.getCookies", {}, timeout=15.0)
    cookies = result.get("cookies")
    return [c for c in cookies if isinstance(c, Mapping)] if isinstance(cookies, list) else []


def select(cookies: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for cookie in cookies:
        name = str(cookie.get("name") or "")
        domain = str(cookie.get("domain") or "")
        if name not in BRIDGE_COOKIES or "gitcode.com" not in domain.lower():
            continue
        record: dict[str, Any] = {
            "name": name,
            "value": cookie.get("value"),
            "domain": domain,
            "path": str(cookie.get("path") or "/"),
            "secure": bool(cookie.get("secure")),
            "httpOnly": bool(cookie.get("httpOnly")),
        }
        expires = cookie.get("expires")
        if isinstance(expires, (int, float)) and expires > 0:
            record["expires"] = float(expires)
        out.append(record)
    return out


def plant(port: int, cookies: list[dict[str, Any]]) -> None:
    with CdpConnection(_endpoint(port), timeout=20.0) as conn:
        conn.call("Storage.setCookies", {"cookies": cookies}, timeout=15.0)


def clear(port: int) -> int:
    removed = 0
    with CdpConnection(_endpoint(port), timeout=20.0) as conn:
        cookies = conn.call("Storage.getCookies", {}, timeout=15.0).get("cookies") or []
        targets = conn.call("Target.getTargets", {}, timeout=10.0).get("targetInfos") or []
        page = next(
            (t for t in targets if isinstance(t, Mapping) and t.get("type") == "page"), None
        )
        session_id = None
        if page is not None:
            attached = conn.call(
                "Target.attachToTarget",
                {"targetId": page.get("targetId"), "flatten": True},
                timeout=10.0,
            )
            session_id = attached.get("sessionId")
        for cookie in cookies:
            if "gitcode.com" not in str(cookie.get("domain") or "").lower():
                continue
            params = {
                "name": str(cookie.get("name") or ""),
                "domain": str(cookie.get("domain") or ""),
                "path": str(cookie.get("path") or "/"),
            }
            if session_id:
                conn.call("Network.deleteCookies", params, session_id=session_id, timeout=10.0)
            removed += 1
    return removed


def run_probe(port: int) -> list[dict[str, Any]]:
    """Open a gitcode.com tab, evaluate the candidate fetches, return the rows."""
    browser_ws = _endpoint(port)
    with CdpConnection(browser_ws, timeout=25.0) as conn:
        target = conn.call(
            "Target.createTarget",
            {"url": f"{PAGE_ORIGIN}/login", "background": True},
            timeout=15.0,
        )
        target_id = target.get("targetId")
        attached = conn.call(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}, timeout=15.0
        )
        session_id = attached.get("sessionId")
        conn.call("Page.enable", session_id=session_id, timeout=10.0)
        # Let the document settle so same-origin fetches are allowed.
        time.sleep(4.0)
        result = conn.call(
            "Runtime.evaluate",
            {
                "expression": _PROBE_JS % json.dumps(list(CANDIDATES)),
                "awaitPromise": True,
                "returnByValue": True,
            },
            session_id=session_id,
            timeout=60.0,
        )
        try:
            conn.call("Target.closeTarget", {"targetId": target_id}, timeout=10.0)
        except Exception:  # noqa: BLE001
            pass
    value = (result.get("result") or {}).get("value")
    if not isinstance(value, str):
        raise RuntimeError("the in-page probe returned nothing usable")
    rows = json.loads(value)
    return rows if isinstance(rows, list) else []


def summarise(label: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    print(f"  {label}")
    statuses: dict[str, Any] = {}
    for row in rows:
        status = row.get("status")
        statuses[str(row.get("path"))] = status
        detail = f"    {str(row.get('path')):32s} -> "
        if status is None:
            detail += f"ERROR {row.get('error')}"
        else:
            detail += f"HTTP {status} ({row.get('bytes')} bytes)"
            if row.get("dataKeys"):
                detail += f" dataKeys={row['dataKeys']}"
            elif row.get("keys"):
                detail += f" keys={row['keys']}"
        print(detail)
    return statuses


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-port", type=int, default=9222)
    parser.add_argument("--target-port", type=int, default=9333)
    args = parser.parse_args()

    print("== source: read the signed-in GitCode session ==")
    source = select(read_cookies(args.source_port))
    names = sorted(str(c.get("name")) for c in source)
    print(f"  session cookies available: {names or '(none)'}")
    if not source:
        print("  INCONCLUSIVE: the source profile is not signed in to GitCode.")
        return 1

    print()
    print("== target: reset, then baseline ==")
    print(f"  cleared {clear(args.target_port)} gitcode cookie(s)")
    before_rows = run_probe(args.target_port)
    before = summarise("baseline (signed out, in-browser same-origin fetch)", before_rows)

    print()
    print("== target: plant the session cookies ==")
    plant(args.target_port, source)
    after_rows = run_probe(args.target_port)
    after = summarise("after bridge (same requests, credential added)", after_rows)

    print()
    print("== verdict ==")
    changed = {
        path: (before.get(path), after.get(path))
        for path in before
        if before.get(path) != after.get(path)
    }
    print(f"  endpoints whose status changed: {changed or '(none)'}")
    authenticated = [
        path
        for path, (b, a) in changed.items()
        if a == 200 and b in (401, 403)
    ]
    if authenticated:
        print()
        print(f"BRIDGE_FEASIBLE: {authenticated} answered 200 with the planted")
        print("credential where it answered 401/403 without it. The GitCode session")
        print("is reproducible from the session cookies alone, in a real browser")
        print("request, so a dedicated profile can be signed in unattended.")
        return 0
    print()
    print("BRIDGE_NOT_DEMONSTRATED: no endpoint moved from 401/403 to 200. The")
    print("planted cookies are therefore not sufficient for a browser session as")
    print("far as this measurement can see. This is a statement about what was")
    print("observed, not a proof that no such mechanism exists.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
