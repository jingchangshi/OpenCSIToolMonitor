"""LIVE research probe: what is a GitCode browser session actually made of?

Answers the question objective §9 asks, and nothing else:

    which cookies / storage entries does a signed-in GitCode browser hold,
    and which of them can a *fresh* profile be given to be considered
    signed in?

It never prints a cookie or storage *value*. Every entry is reported as a name
plus, where a value exists, its length and a short SHA-256 fingerprint, so two
tokens can be compared as ``same``/``different`` without either being disclosed
(objective §8). That is the whole point: the comparison is the finding, the
values are not.

Usage
-----
    python tools/probe_gitcode_session.py --inventory
        Read-only: list the GitCode auth surface of whatever browser is on
        ``--port``. No navigation, no writes.

    python tools/probe_gitcode_session.py --navigate
        Navigate a background tab to GitCode and re-read, so the "signed out"
        baseline and any session cookies the site sets can be observed.

    python tools/probe_gitcode_session.py --authorize
        Follow openCsiTool's OAuth entry point in the background tab and report
        only the *host* and *path* it settles on -- which is how "SSO alive" is
        told from "login page" without scraping a single byte of page text.

LIVE / NETWORK / AUTH_SIDE_EFFECT
--------------------------------------------
``--inventory`` is GET only. ``--navigate`` and ``--authorize`` issue GET
navigations to gitcode.com and opencsitool.com; the latter can mint a session
cookie server-side, which is why it is not run by the test suite and is never
part of CI (objective §44). It never prints a cookie or storage value.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from typing import Any, Mapping

sys.path.insert(0, "src")

from opencsi.auth.cdp import discover_cdp_endpoint  # noqa: E402
from opencsi.ws import CdpConnection  # noqa: E402

#: Cookie names that mean "GitCode still knows who this is", plus the ones the
#: objective names explicitly. Reported whether present or absent.
GITCODE_INTEREST = (
    "GITCODE_ACCESS_TOKEN",
    "GITCODE_REFRESH_TOKEN",
    "GitCodeUserName",
    "gitcode_oauth_session",
    "gitcode_session",
    "sessionid",
    "csrftoken",
)

OPENSITOOL_INTEREST = ("token", "SESSION", "JSESSIONID")

#: Storage keys worth looking for by name. A mini-program/OAuth SPA commonly
#: keeps its bearer token in ``localStorage`` rather than a cookie, and
#: objective §10 is explicit that this must be *observed* rather than guessed.
STORAGE_INTEREST = (
    "token",
    "access_token",
    "refresh_token",
    "GITCODE_ACCESS_TOKEN",
    "gitcode_token",
    "user",
    "userInfo",
)


def fingerprint(value: str) -> str:
    """``len=…, sha256=<first 12>`` -- comparable without being disclosive."""
    digest = hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:12]
    return f"len={len(value):<5d} sha256={digest}"


def _host_of(url: str) -> str:
    if not url:
        return ""
    rest = url.split("://", 1)[-1]
    return rest.split("/", 1)[0].split(":", 1)[0].lower()


def _path_of(url: str) -> str:
    if not url:
        return ""
    rest = url.split("://", 1)[-1]
    if "/" not in rest:
        return "/"
    return "/" + rest.split("/", 1)[1].split("?", 1)[0].split("#", 1)[0]


def describe_cookie(cookie: Mapping[str, Any], *, now: float) -> str:
    """One cookie, by metadata only. Never the value."""
    name = str(cookie.get("name") or "")
    domain = str(cookie.get("domain") or "")
    expires = cookie.get("expires")
    if expires in (None, -1, 0):
        lifetime = "session"
    else:
        remaining = float(expires) - now
        lifetime = f"{remaining / 3600:+.1f}h" if remaining > 0 else "EXPIRED"
    flags = "".join(
        (
            "H" if cookie.get("httpOnly") else "-",
            "S" if cookie.get("secure") else "-",
        )
    )
    same_site = str(cookie.get("sameSite") or "-")
    value = cookie.get("value")
    shape = fingerprint(str(value)) if isinstance(value, str) and value else "(empty)"
    return (
        f"  {name:24s} {domain:22s} {lifetime:>10s}  {flags}  "
        f"{same_site:8s} {shape}"
    )


def read_cookies(conn: CdpConnection) -> list[Mapping[str, Any]]:
    result = conn.call("Storage.getCookies", {}, timeout=15.0)
    cookies = result.get("cookies")
    return [c for c in cookies if isinstance(c, Mapping)] if isinstance(cookies, list) else []


def read_storage(conn: CdpConnection, session_id: str, origin: str) -> dict[str, Any]:
    """``localStorage`` + ``sessionStorage`` key *names* and value shapes."""
    out: dict[str, Any] = {}
    expression = (
        "(function(){var r={local:{},session:{}};"
        "try{for(var i=0;i<localStorage.length;i++){var k=localStorage.key(i);"
        "r.local[k]=(localStorage.getItem(k)||'').length;}}catch(e){r.localError=1;}"
        "try{for(var j=0;j<sessionStorage.length;j++){var k2=sessionStorage.key(j);"
        "r.session[k2]=(sessionStorage.getItem(k2)||'').length;}}catch(e){r.sessionError=1;}"
        "return JSON.stringify(r);})()"
    )
    try:
        result = conn.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True},
            session_id=session_id,
            timeout=15.0,
        )
    except Exception as exc:  # noqa: BLE001 - a probe must not raise
        return {"error": type(exc).__name__}
    raw = (result.get("result") or {}).get("value")
    if isinstance(raw, str):
        try:
            out = json.loads(raw)
        except ValueError:
            out = {"unparsed": True}
    out["origin"] = origin
    return out


def open_tab(conn: CdpConnection, url: str) -> str:
    result = conn.call(
        "Target.createTarget", {"url": url, "background": True}, timeout=15.0
    )
    target_id = result.get("targetId")
    if not target_id:
        raise RuntimeError("could not create a background tab")
    return str(target_id)


def attach(conn: CdpConnection, target_id: str) -> str:
    attached = conn.call(
        "Target.attachToTarget", {"targetId": target_id, "flatten": True}, timeout=15.0
    )
    session_id = attached.get("sessionId")
    if not session_id:
        raise RuntimeError("could not attach to the tab")
    return str(session_id)


def location(conn: CdpConnection, session_id: str) -> str:
    result = conn.call(
        "Runtime.evaluate",
        {"expression": "location.href", "returnByValue": True},
        session_id=session_id,
        timeout=15.0,
    )
    value = (result.get("result") or {}).get("value")
    return value if isinstance(value, str) else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9333)
    parser.add_argument("--inventory", action="store_true", help="read-only cookie/storage inventory")
    parser.add_argument("--navigate", action="store_true", help="navigate to GitCode first")
    parser.add_argument("--authorize", action="store_true", help="follow openCsiTool OAuth")
    parser.add_argument("--wait", type=float, default=6.0)
    args = parser.parse_args()

    if not (args.inventory or args.navigate or args.authorize):
        args.inventory = True

    endpoint = discover_cdp_endpoint(f"http://127.0.0.1:{args.port}", probe=True)
    browser_ws = endpoint.browser_ws_url()
    if not browser_ws:
        print("no browser-level WebSocket")
        return 1

    now = time.time()
    with CdpConnection(browser_ws, timeout=20.0) as conn:
        version = conn.call("Browser.getVersion", {}, timeout=15.0)
        print(f"browser          : {version.get('product')}")
        print(f"user agent       : {version.get('userAgent')}")
        print()

        session_id = None
        final_url = ""
        if args.navigate or args.authorize:
            url = (
                "https://opencsitool.com/opencsitool/rest/v1/oauth2/"
                "authorization/gitcode?redirect=%2FmyTools"
                if args.authorize
                else "https://gitcode.com/login"
            )
            target_id = open_tab(conn, "about:blank")
            session_id = attach(conn, target_id)
            conn.call("Page.navigate", {"url": url}, session_id=session_id, timeout=15.0)
            deadline = time.monotonic() + max(1.0, args.wait)
            while time.monotonic() < deadline:
                time.sleep(0.5)
                final_url = location(conn, session_id)
            final_url = location(conn, session_id)
            print(f"navigated        : {_host_of(url)}{_path_of(url)}")
            print(f"settled on       : {_host_of(final_url)}{_path_of(final_url)}")
            print()

        cookies = read_cookies(conn)
        print(f"total cookies in profile: {len(cookies)}")

        gitcode = [
            c
            for c in cookies
            if "gitcode" in str(c.get("domain") or "").lower()
            or str(c.get("name") or "") in GITCODE_INTEREST
        ]
        app = [
            c
            for c in cookies
            if "opencsitool" in str(c.get("domain") or "").lower()
            or str(c.get("name") or "") in OPENSITOOL_INTEREST
        ]

        print()
        print(f"GitCode-related cookies ({len(gitcode)}):")
        if gitcode:
            for cookie in sorted(
                gitcode, key=lambda c: (str(c.get("domain")), str(c.get("name")))
            ):
                print(describe_cookie(cookie, now=now))
        else:
            print("  (none)")

        print()
        print(f"openCsiTool-related cookies ({len(app)}):")
        if app:
            for cookie in sorted(
                app, key=lambda c: (str(c.get("domain")), str(c.get("name")))
            ):
                print(describe_cookie(cookie, now=now))
        else:
            print("  (none)")

        # The explicit checklist the objective names, present or not.
        names_by_domain: dict[str, set[str]] = {}
        for cookie in cookies:
            names_by_domain.setdefault(
                str(cookie.get("domain") or "").lower(), set()
            ).add(str(cookie.get("name") or ""))
        print()
        print("named GitCode auth cookies:")
        all_gitcode_names = {
            name for domain, names in names_by_domain.items() if "gitcode" in domain for name in names
        }
        for name in GITCODE_INTEREST:
            print(f"  {name:24s} {'present' if name in all_gitcode_names else 'absent'}")

        if session_id is not None:
            print()
            for origin in ("https://gitcode.com", "https://opencsitool.com"):
                storage = read_storage(conn, session_id, origin)
                print(f"storage on {origin}: {json.dumps(storage, ensure_ascii=False)}")

        try:
            conn.call("Target.closeTarget", {"targetId": target_id}, timeout=10.0)
        except Exception:  # noqa: BLE001
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
