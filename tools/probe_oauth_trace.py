"""Read-only trace of where the silent OAuth round-trip actually gets to.

Prints the tab's location at each poll, so a TIMEOUT can be attributed to a
specific step instead of guessed at. Hosts and paths only -- never a query
string, which can carry `code` and `state`.

It does navigate a background tab, because that is the thing being traced, but
it is otherwise read-only: it creates its own target, closes it again, never
touches the user's current page, and never prints a token value. Page text is
read only in the failure branch, only to name the blocking control, and only
the first 400 characters.
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "src")

from opencsi.auth.oauth_browser import BrowserOAuthRenewer  # noqa: E402
from opencsi.cli.context import make_context  # noqa: E402
from opencsi.ws import CdpConnection  # noqa: E402


def _split(url: str) -> str:
    """host + path only, with any query or fragment dropped."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).netloc.lower()


def main() -> int:
    ctx, args = make_context(["--no-proxy", "--renew-timeout", "60"])
    provider = ctx.make_provider()
    renewer = ctx.make_renewer(provider, base_url=args.base_url or "https://opencsitool.com")

    print(f"renewer      : {renewer.describe()}")
    print(f"oauth_url    : {_split(renewer.oauth_url())}")
    print()

    endpoint = renewer._resolve_endpoint()
    browser_ws = endpoint.browser_ws_url()
    print(f"browser ws   : {_split(browser_ws)}")
    print()

    with CdpConnection(browser_ws, timeout=15.0) as conn:
        created = conn.call(
            "Target.createTarget", {"url": "about:blank", "background": True}, timeout=15.0
        )
        target_id = created.get("targetId")
        print(f"target       : created ({'yes' if target_id else 'NO'})")

        attached = conn.call(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}, timeout=15.0
        )
        session_id = attached.get("sessionId")
        print(f"attached     : {'yes' if session_id else 'NO'}")

        try:
            conn.call("Page.enable", session_id=session_id, timeout=15.0)
        except Exception as exc:
            print(f"Page.enable  : {type(exc).__name__}")

        url = renewer.oauth_url()
        conn.call("Page.navigate", {"url": url}, session_id=session_id, timeout=15.0)
        print("navigated    : yes")
        print()

        def evaluate(expression: str):
            try:
                result = conn.call(
                    "Runtime.evaluate",
                    {"expression": expression, "returnByValue": True},
                    session_id=session_id,
                    timeout=10.0,
                )
                return (result.get("result") or {}).get("value")
            except Exception as exc:
                return f"<{type(exc).__name__}>"

        print("polling location (host+path only, query dropped):")
        deadline = time.monotonic() + 40.0
        last = None
        step = 0
        landed = False
        while time.monotonic() < deadline:
            time.sleep(1.5)
            step += 1
            href = evaluate("location.href")
            shown = _split(href) if isinstance(href, str) and "://" in href else href
            if shown != last:
                print(f"  {step:3d} {shown}")
                last = shown
            # Host-based, like the real code. A substring test on the whole href
            # also matches `redirect_uri=https://opencsitool.com/...`, which made
            # an earlier version of this probe report a return that never
            # happened.
            if isinstance(href, str) and _host(href).endswith("opencsitool.com"):
                landed = True
                print(f"  {step:3d} -- back on the app; waiting for Set-Cookie")
                time.sleep(3.0)
                break

        # What is on the page? Text only, and only for diagnosis -- the product
        # code never reads page content, and this prints no query string.
        if not landed:
            print()
            print("page text (first 400 chars, whitespace collapsed):")
            text = evaluate(
                "(document.body && document.body.innerText || '')"
                ".replace(/\\s+/g,' ').slice(0,400)"
            )
            print(f"  {text}")
            print()
            print("buttons/links visible:")
            labels = evaluate(
                "Array.from(document.querySelectorAll('button,a'))"
                ".map(e=>(e.innerText||'').trim()).filter(Boolean).slice(0,12)"
            )
            print(f"  {labels}")

        cookies = conn.call("Storage.getCookies", {}, timeout=15.0).get("cookies", [])
        token = [
            c
            for c in cookies
            if c.get("name") == "token" and "opencsitool" in str(c.get("domain", ""))
        ]
        print()
        print(f"openCsiTool token cookies now: {len(token)}")

        conn.call("Target.closeTarget", {"targetId": target_id}, timeout=10.0)
        print("target closed")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
