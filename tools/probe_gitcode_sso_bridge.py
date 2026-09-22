"""LIVE experiment: can a fresh Chromium profile be made GitCode-signed-in?

This is objective §10 and §11, turned into a measurement.

The hypothesis
--------------
``GitCodeQrAuthenticator`` obtains ``access_token`` / ``refresh_token`` over
plain HTTP. GitCode's own browser session, measured on a real signed-in profile
(``tools/probe_gitcode_session.py``), consists of exactly two HttpOnly cookies on
``.gitcode.com`` -- ``GITCODE_ACCESS_TOKEN`` and ``GITCODE_REFRESH_TOKEN`` --
plus ``GitCodeUserName``. If the QR tokens *are* those cookies, then a clean
dedicated profile can be signed in by planting them, and openCsiTool's OAuth leg
can then run with no human involvement at all.

What this probe does
--------------------
It does **not** need a QR scan to answer the mechanism question. It copies the
auth cookies out of a *source* browser that is already signed in, plants them
into a *target* browser on an isolated profile, and then asks GitCode a question
whose answer only a signed-in session can give:

    GET https://web-api.gitcode.com/uc/api/v1/user/info

Observed on this machine: ``401`` when signed out, ``200`` with a user body when
signed in. That is a *known authenticated API*, which is the evidence standard
objective §12 requires -- page text is explicitly not accepted.

Why the source is a real profile
--------------------------------
Substituting a synthetic token would prove only that ``Storage.setCookies``
works, which is not in doubt. The open question is whether *this* credential
shape is what GitCode accepts as a session, so the experiment uses a real one.

Security
--------
* No cookie value is ever printed, logged, returned or written to a file. The
  values exist only as local variables inside this process.
* The source profile is only *read* (``Storage.getCookies``). Nothing is written
  to it.
* The target is an isolated throwaway profile, never the user's real one.

Usage
-----
    # 1. Start a target browser on an isolated profile (once):
    #    chrome.exe --headless=new --remote-debugging-port=9333 \
    #      --user-data-dir=%LOCALAPPDATA%\\OpenCSI\\auth-test-profile about:blank
    #
    # 2. Prove the target is genuinely signed out, then bridge, then re-check:
    python tools/probe_gitcode_sso_bridge.py --source-port 9222 --target-port 9333

Labels: LIVE, NETWORK, AUTH_SIDE_EFFECT (it writes cookies into the target
profile), GET only against every remote API. It never prints a cookie value. It
is never run by the test suite and is never part of CI.
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from typing import Any, Mapping

sys.path.insert(0, "src")

from opencsi.auth.cdp import discover_cdp_endpoint  # noqa: E402
from opencsi.ws import CdpConnection  # noqa: E402

#: The cookies that constitute a GitCode browser session. Measured, not assumed:
#: see the output of ``probe_gitcode_session.py --inventory`` on a signed-in
#: profile. ``GitCodeUserName`` rides along because it is what the site's own
#: front end reads to render a name without an extra call.
BRIDGE_COOKIES = ("GITCODE_ACCESS_TOKEN", "GITCODE_REFRESH_TOKEN", "GitCodeUserName")

#: A known *authenticated* GitCode endpoint. 401 signed out, 200 signed in.
#: Established by probing several candidates; see docs/gitcode-qr-protocol.md.
AUTH_CHECK_URL = "https://web-api.gitcode.com/uc/api/v1/user/info"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)


def read_cookies(port: int) -> list[Mapping[str, Any]]:
    endpoint = discover_cdp_endpoint(f"http://127.0.0.1:{port}", probe=True)
    browser_ws = endpoint.browser_ws_url()
    if not browser_ws:
        raise RuntimeError(f"port {port} exposes no browser-level WebSocket")
    with CdpConnection(browser_ws, timeout=20.0) as conn:
        result = conn.call("Storage.getCookies", {}, timeout=15.0)
    cookies = result.get("cookies")
    return [c for c in cookies if isinstance(c, Mapping)] if isinstance(cookies, list) else []


def bridge_cookies(cookies: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Select the session cookies, preserving their real attributes.

    The attributes matter and are copied rather than invented: ``httpOnly`` and
    ``secure`` are part of what the server set, and a cookie replayed without
    ``httpOnly`` would be a subtly different credential. ``expires`` is carried
    over so the bridged session has the same lifetime as the original.
    """
    out: list[dict[str, Any]] = []
    for cookie in cookies:
        name = str(cookie.get("name") or "")
        domain = str(cookie.get("domain") or "")
        if name not in BRIDGE_COOKIES:
            continue
        if "gitcode.com" not in domain.lower():
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
        same_site = str(cookie.get("sameSite") or "")
        if same_site in ("Strict", "Lax", "None"):
            record["sameSite"] = same_site
        out.append(record)
    return out


def set_cookies(port: int, cookies: list[dict[str, Any]]) -> None:
    endpoint = discover_cdp_endpoint(f"http://127.0.0.1:{port}", probe=True)
    browser_ws = endpoint.browser_ws_url()
    if not browser_ws:
        raise RuntimeError(f"port {port} exposes no browser-level WebSocket")
    with CdpConnection(browser_ws, timeout=20.0) as conn:
        result = conn.call("Storage.setCookies", {"cookies": cookies}, timeout=15.0)
    # A CDP call that returns without raising has still told us nothing about
    # whether the jar accepted the write, so the caller re-reads.
    del result


def clear_gitcode_cookies(port: int) -> int:
    """Remove every gitcode.com cookie from the target, to reset the experiment.

    Uses ``Network.deleteCookies`` on an attached page session rather than
    ``Storage.deleteCookies``: the latter is *not* in Chrome's protocol --
    ``Storage`` has only ``getCookies``/``setCookies`` -- and the call fails with
    "wasn't found", which is how this was discovered.
    """
    endpoint = discover_cdp_endpoint(f"http://127.0.0.1:{port}", probe=True)
    browser_ws = endpoint.browser_ws_url()
    if not browser_ws:
        raise RuntimeError(f"port {port} exposes no browser-level WebSocket")
    removed = 0
    with CdpConnection(browser_ws, timeout=20.0) as conn:
        cookies = conn.call("Storage.getCookies", {}, timeout=15.0).get("cookies") or []
        targets = conn.call("Target.getTargets", {}, timeout=10.0).get("targetInfos") or []
        page = next(
            (
                t
                for t in targets
                if isinstance(t, Mapping) and t.get("type") == "page"
            ),
            None,
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
            else:
                conn.call("Storage.deleteCookies", params, timeout=10.0)
            removed += 1
    return removed


def auth_probe(cookie_header: str | None) -> tuple[int, int]:
    """Ask the known authenticated endpoint. Returns ``(status, body_length)``.

    The body is measured, never printed: a signed-in answer contains the user's
    own account details.
    """
    request = urllib.request.Request(
        AUTH_CHECK_URL,
        headers={
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://gitcode.com",
            "Referer": "https://gitcode.com/",
            **({"Cookie": cookie_header} if cookie_header else {}),
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=20.0) as response:
            return response.status, len(response.read())
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except Exception:  # noqa: BLE001
            body = b""
        status = exc.code
        exc.close()
        return status, len(body)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise RuntimeError(f"could not reach GitCode: {type(exc).__name__}") from exc


def cookie_header(cookies: list[Mapping[str, Any]]) -> str:
    """A Cookie header built from the selected session cookies.

    Built here rather than reusing a jar so the comparison is exactly "these
    values, sent as cookies" -- which is the hypothesis under test.
    """
    return "; ".join(
        f"{cookie.get('name')}={cookie.get('value')}"
        for cookie in cookies
        if cookie.get("name") in BRIDGE_COOKIES
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-port", type=int, default=9222)
    parser.add_argument("--target-port", type=int, default=9333)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="delete the target's gitcode cookies first, to prove the baseline",
    )
    args = parser.parse_args()

    print("== step 1: read the source profile's GitCode session ==")
    source = read_cookies(args.source_port)
    selected = bridge_cookies(source)
    present = sorted(str(c.get("name")) for c in selected)
    print(f"source cookies total   : {len(source)}")
    print(f"GitCode session cookies: {present or '(none)'}")
    if not selected:
        print()
        print("VERDICT: INCONCLUSIVE -- the source profile is not signed in to")
        print("         GitCode, so there is no credential to bridge.")
        return 1

    print()
    print("== step 2: establish the target's baseline ==")
    if args.reset:
        removed = clear_gitcode_cookies(args.target_port)
        print(f"cleared {removed} gitcode cookie(s) from the target")
    target_before = [
        c
        for c in read_cookies(args.target_port)
        if str(c.get("name") or "") in BRIDGE_COOKIES
    ]
    print(f"target holds session cookies before bridge: {len(target_before)}")

    print()
    print("== step 3: ask the known authenticated API, with no cookie ==")
    try:
        status, length = auth_probe(None)
    except RuntimeError as exc:
        print(f"network failure: {exc}")
        return 1
    print(f"GET {AUTH_CHECK_URL}")
    print(f"  no cookie  -> HTTP {status} ({length} bytes)")
    baseline_signed_out = status in (401, 403)

    print()
    print("== step 4: plant the session cookies into the target profile ==")
    set_cookies(args.target_port, selected)
    planted = [
        c
        for c in read_cookies(args.target_port)
        if str(c.get("name") or "") in BRIDGE_COOKIES
    ]
    print(f"target holds session cookies after bridge: {len(planted)}")
    print("  (names only: " + ", ".join(sorted(str(c.get('name')) for c in planted)) + ")")

    print()
    print("== step 5: ask the same API with the bridged credential ==")
    try:
        status, length = auth_probe(cookie_header(selected))
    except RuntimeError as exc:
        print(f"network failure: {exc}")
        return 1
    print(f"  with cookie -> HTTP {status} ({length} bytes)")
    signed_in = status == 200

    print()
    print("== verdict ==")
    print(f"baseline signed out      : {baseline_signed_out}")
    print(f"bridged credential works : {signed_in}")
    if signed_in:
        print()
        print("BRIDGE_FEASIBLE: the GitCode session is reproducible from the")
        print("session cookies alone, so a dedicated profile can be signed in")
        print("without any user interaction and the openCsiTool OAuth leg can")
        print("run unattended.")
        return 0
    print()
    print("BRIDGE_NOT_DEMONSTRATED: the bridged cookies were not accepted by")
    print("the authenticated API. This does NOT mean the mechanism is")
    print("impossible -- it means this particular credential shape is not")
    print("sufficient, and the remaining possibilities (a device/UA binding,")
    print("a missing cookie, or a server-side session) would have to be")
    print("ruled out before concluding anything stronger.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
