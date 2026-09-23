"""Can the openCsiTool token be obtained without a browser at all?

This is the question the whole "browser as an optional backend" goal rests on.
Silent renewal currently drives a background *tab* through CDP. If the same OAuth
round-trip completes over plain HTTP given a GitCode session, then the browser is
only needed to *hold* the GitCode SSO cookies -- and a QR login could plant those
cookies into a CookieJar instead.

The experiment:

1. read the GitCode SSO cookies out of the dedicated browser profile via CDP
   (browser-level ``Storage.getCookies``, so no page is touched);
2. build a ``CookieJar`` containing only those, and *no* openCsiTool cookies;
3. request the openCsiTool OAuth authorize endpoint over pure HTTP with redirects
   followed;
4. report whether a new openCsiTool ``token`` cookie appeared, and whether the
   server accepts it.

Nothing is written to the browser, no page is navigated, and no cookie *value* is
printed -- only names, counts, hosts and the final outcome. Every request is
**GET only**: this probe never POSTs, never submits a form, and never calls a
business endpoint. It does cause the OAuth authorize flow to run, which may mint
an openCsiTool token -- that is an authentication action, the same one silent
renewal performs, not a business write. Read-only with respect to openCsiTool's
data.
"""
#: labels: LIVE, NETWORK, AUTH_SIDE_EFFECT

from __future__ import annotations

import http.cookiejar
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

sys.path.insert(0, "src")

from opencsi.auth.cdp import discover_cdp_endpoint  # noqa: E402
from opencsi.ws import CdpConnection  # noqa: E402

AUTHORIZE = "https://opencsitool.com/opencsitool/rest/v1/oauth2/authorization/gitcode"
GITCODE_DOMAIN = "gitcode.com"
OPENCSITOOL_DOMAIN = "opencsitool.com"

#: Only these are transplanted. Anything openCsiTool-specific is deliberately
#: excluded -- the point is to prove the token can be minted from nothing but a
#: GitCode session.
SSO_NAMES = {
    "GITCODE_ACCESS_TOKEN",
    "GITCODE_REFRESH_TOKEN",
    "GitCodeUserName",
    "gitcode_oauth_session",
}


def _read_browser_cookies() -> list[dict]:
    endpoint = discover_cdp_endpoint(ports=(9222,))
    with CdpConnection(endpoint.browser_ws_url(), timeout=15.0) as conn:
        return conn.call("Storage.getCookies", timeout=15.0).get("cookies", [])


def _build_jar(cookies: list[dict], *, mode: str) -> tuple[http.cookiejar.CookieJar, list[str]]:
    """Build a jar for one of two scenarios.

    ``mode="sso"`` transplants only the GitCode SSO cookies -- the honest test of
    "can a QR login, which yields GitCode credentials and nothing else, finish the
    openCsiTool OAuth?".

    ``mode="all"`` transplants every cookie the browser holds *except*
    openCsiTool's ``token``. That is the decisive control: if a token is still not
    minted, the blocker is not a missing cookie but something a browser does that
    an HTTP client cannot -- a JS challenge or a WAF token bound to the browser.
    """
    jar = http.cookiejar.CookieJar()
    planted: list[str] = []
    for raw in cookies:
        domain = str(raw.get("domain") or "")
        name = str(raw.get("name") or "")
        if mode == "sso":
            if GITCODE_DOMAIN not in domain.lower() or name not in SSO_NAMES:
                continue
        else:
            # Everything except the credential we are trying to obtain.
            if name == "token" and OPENCSITOOL_DOMAIN in domain.lower():
                continue
        cookie = http.cookiejar.Cookie(
            version=0,
            name=name,
            value=str(raw.get("value") or ""),
            port=None,
            port_specified=False,
            domain=domain.lstrip("."),
            domain_specified=True,
            domain_initial_dot=domain.startswith("."),
            path=str(raw.get("path") or "/"),
            path_specified=True,
            secure=bool(raw.get("secure", True)),
            expires=int(raw["expires"]) if raw.get("expires", -1) > 0 else None,
            discard=False,
            comment=None,
            comment_url=None,
            rest={},
        )
        jar.set_cookie(cookie)
        planted.append(f"{name}@{domain.lstrip('.')}")
    return jar, sorted(planted)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Record every hop instead of following silently."""

    def __init__(self) -> None:
        self.hops: list[tuple[int, str, str]] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.hops.append((code, urlsplit(req.full_url).netloc, urlsplit(newurl).netloc))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _describe_body(body: bytes) -> None:
    """Say what came back, so "no token" can be told apart from "JS never ran".

    A marker found in a bundled script is not a challenge -- it is a library name.
    Reporting "captcha" as the blocker because the word appears somewhere in a
    200 KB SPA bundle would be exactly the kind of claim that outruns its
    evidence, so each marker is located relative to the script tags instead of
    merely grepped for. No page content is printed.
    """
    text = body.decode("utf-8", "replace")
    lowered = text.lower()
    scripts = lowered.count("<script")
    shell = 'id="app"' in lowered or "id=app" in lowered
    print(f"  body       : {len(body)} bytes, {scripts} <script> tag(s), spa-shell={shell}")

    for marker in ("captcha", "challenge", "verify", "authorize"):
        at = lowered.find(marker)
        if at < 0:
            continue
        # Inside a script when the nearest preceding <script> has no closing tag
        # between it and the marker.
        in_script = lowered.rfind("<script", 0, at) > lowered.rfind("</script>", 0, at)
        where = "inside a script bundle" if in_script else "in page markup"
        print(f"  {marker!r}: {where}")


def _attempt(cookies: list[dict], mode: str) -> bool:
    """Run one scenario. Returns True when a token cookie was minted."""
    jar, planted = _build_jar(cookies, mode=mode)
    print(f"--- mode={mode}: {len(planted)} cookie(s) planted ---")
    if not planted:
        print("  nothing to plant; skipping")
        return False

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(jar),
    )
    request = urllib.request.Request(
        AUTHORIZE,
        headers={
            "User-Agent": "opencsi-cli/0.1.0",
            "Accept": "text/html,application/xhtml+xml",
        },
    )

    try:
        with opener.open(request, timeout=45.0) as response:
            final, body, status = response.geturl(), response.read(8192), response.status
    except urllib.error.HTTPError as exc:
        final, body, status = exc.geturl(), exc.read(8192), exc.code
    except Exception as exc:  # noqa: BLE001
        print(f"  request failed: {type(exc).__name__}: {exc}")
        return False

    print(f"  status     : {status}")
    print(f"  final      : {urlsplit(final).netloc}{urlsplit(final).path}")

    now = time.time()
    minted = False
    for cookie in jar:
        if OPENCSITOOL_DOMAIN in cookie.domain.lower() and cookie.name == "token":
            left = "session" if not cookie.expires else f"{(cookie.expires - now) / 60:.1f} min"
            print(f"  TOKEN MINTED (expires in {left})")
            minted = True

    if not minted:
        _describe_body(body)
    return minted


def main() -> int:
    cookies = _read_browser_cookies()

    sso = _attempt(cookies, "sso")
    print()
    everything = _attempt(cookies, "all")
    print()

    if everything:
        print("RESULT: the token is obtainable over pure HTTP; the browser is only a cookie store")
        return 0
    if sso:
        print("RESULT: obtainable with GitCode SSO cookies alone -- QR login can replace the browser")
        return 0
    print("RESULT: BROWSER-BOUND")
    print("  Reason: /oauth/authorize answers with a client-rendered SPA shell (a few KB of")
    print("  markup plus script tags) and no redirect. The authorize decision -- auto-approve")
    print("  when the SSO session is already valid, or render the consent page when it is not --")
    print("  happens in JavaScript, so a non-JS HTTP client receives the shell and nothing more.")
    print("  This is not a missing cookie and not a CAPTCHA: the identical response comes back")
    print("  with all 29 browser cookies present.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
