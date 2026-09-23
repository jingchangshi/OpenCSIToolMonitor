"""Can a non-JS client issue the GitCode authorize API calls the SPA makes?

``probe_oauth_spa_static.py`` recovers the call shapes from the JavaScript and
``probe_oauth_spa_dynamic.py`` observes them on the wire. Neither answers the
question that decides whether silent renewal needs a browser: *is the sequence
reproducible by a client that runs no JavaScript at all?*

This probe tries exactly that, and nothing more:

1. read the GitCode session cookies out of a dedicated automation profile via
   CDP (browser-level ``Storage.getCookies``, so no page is touched);
2. build a ``CookieJar`` from them and request the openCsiTool OAuth entry point
   **without** following redirects, so the ``302`` to GitCode's authorize URL is
   readable rather than swallowed;
3. issue the one backend call the SPA issues -- a ``POST`` to GitCode's
   ``checkOrAuthorize`` -- as a multipart form, the same shape the SPA sends;
4. report whether the response carries a ``redirect_uri``, and if it does,
   whether following it mints an openCsiTool ``token`` cookie.

What it deliberately does **not** do:

* it never calls ``/uc/api/v1/oauth/authorize``, the consent-submit endpoint.
  Approving a third-party application grant is the account holder's decision
  and must never be automated, so that endpoint is reported as a finding and
  left alone;
* it never calls an openCsiTool business endpoint. The only openCsiTool path it
  touches is the OAuth authorization entry and the callback it redirects to;
* it never prints a secret. Cookie values, ``code`` and ``state`` are reported
  only as lengths and SHA-256 fingerprints, and every URL is printed with its
  query string stripped.

LIVE / NETWORK / AUTH_SIDE_EFFECT / read-only on business data
--------------------------------------------------------------
LIVE and NETWORK: it talks to gitcode.com, web-api.gitcode.com and
opencsitool.com. AUTH_SIDE_EFFECT: completing the flow mints an openCsiTool
session cookie -- an authentication action identical to the silent renewal the
product already performs, not a business write. read-only on business data: no
business endpoint is called. It never POSTs to a consent endpoint.

Usage
-----
    python tools/probe_oauth_spa_pure_http.py --port 9333
"""
#: labels: LIVE, NETWORK, AUTH_SIDE_EFFECT

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import sys
import urllib.error
import urllib.request
import uuid
from typing import Any
from urllib.parse import parse_qsl, urlsplit

sys.path.insert(0, "src")

from opencsi.auth.cdp import discover_cdp_endpoint  # noqa: E402
from opencsi.ws import CdpConnection  # noqa: E402

ENTRY = (
    "https://opencsitool.com/opencsitool/rest/v1/oauth2/authorization/gitcode"
    "?redirect=%2FmyTools"
)

#: Where the SPA's own axios call lands, after the bundle's ``/uc`` base and its
#: ``/api/v1/oauth/`` rewrite rule are applied. Both are read out of the bundle,
#: not guessed; see the findings document.
CHECK_OR_AUTHORIZE = "https://web-api.gitcode.com/uc/api/v1/oauth/checkOrAuthorize"

#: The consent-submit endpoint. Named here so the finding is recorded, and
#: never called.
CONSENT_SUBMIT_PATH = "/uc/api/v1/oauth/authorize"

#: The flow under test is the one silent renewal uses, which requires an
#: *existing* grant. Nothing here creates one.
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)

SECRET_PARAMS = {"code", "state", "xauth_token", "access_token", "refresh_token"}


def fingerprint(value: str) -> str:
    """``len=…, sha256=<first 12>`` -- comparable without being disclosive."""
    digest = hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:12]
    return f"len={len(value):<6d} sha256={digest}"


def safe(url: str) -> str:
    """Host + path, with any query string dropped.

    ``code`` and ``state`` ride in the query, so a URL is never printed whole.
    """
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def query_names(url: str) -> list[str]:
    return sorted({name for name, _v in parse_qsl(urlsplit(url).query, keep_blank_values=True)})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface the 302 instead of following it into the SPA shell."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def read_browser_cookies(port: int) -> list[dict[str, Any]]:
    endpoint = discover_cdp_endpoint(f"http://127.0.0.1:{port}", probe=True)
    with CdpConnection(endpoint.browser_ws_url(), timeout=20.0) as conn:
        result = conn.call("Storage.getCookies", {}, timeout=15.0)
    cookies = result.get("cookies")
    return [c for c in cookies if isinstance(c, dict)] if isinstance(cookies, list) else []


def build_jar(
    cookies: list[dict[str, Any]], *, mode: str
) -> tuple[http.cookiejar.CookieJar, list[str]]:
    """Plant GitCode cookies into a fresh jar.

    Three scopes, because "it works" is only useful if we know *what* it took:

    ``all``
        every ``gitcode.com`` cookie the profile holds, WAF and analytics
        included. The control.
    ``sso``
        only the named SSO cookies (``GITCODE_ACCESS_TOKEN``,
        ``GITCODE_REFRESH_TOKEN``, ``GitCodeUserName``). This is the honest test
        of "a QR login, which yields GitCode credentials and nothing else, is
        enough" -- and it is the scope a future QR-based renewal could plant.
    ``access``
        only ``GITCODE_ACCESS_TOKEN``. The narrowest thing that could possibly
        authenticate.

    In every scope the openCsiTool ``token`` is excluded. That exclusion is what
    makes the result meaningful: if one appears at the end, this flow minted it
    rather than carried it in.
    """
    allowed = {
        "all": None,
        "sso": {"GITCODE_ACCESS_TOKEN", "GITCODE_REFRESH_TOKEN", "GitCodeUserName"},
        "access": {"GITCODE_ACCESS_TOKEN"},
    }[mode]

    jar = http.cookiejar.CookieJar()
    planted: list[str] = []
    for raw in cookies:
        domain = str(raw.get("domain") or "")
        name = str(raw.get("name") or "")
        if "gitcode" not in domain.lower():
            continue
        if allowed is not None and name not in allowed:
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
        planted.append(name)
    return jar, sorted(set(planted))


def multipart(fields: dict[str, str]) -> tuple[bytes, str]:
    """A ``multipart/form-data`` body, matching the SPA's ``FormData`` shape."""
    boundary = "----WebKitFormBoundary" + uuid.uuid4().hex[:16]
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8")
        )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def opener_for(jar: http.cookiejar.CookieJar, *, follow: bool) -> urllib.request.OpenerDirector:
    handlers: list[Any] = [
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(jar),
    ]
    if not follow:
        handlers.append(_NoRedirect())
    return urllib.request.build_opener(*handlers)


def attempt(cookies: list[dict[str, Any]], mode: str, emit) -> bool:  # noqa: ANN001
    """Run the whole three-step flow once with one cookie scope.

    Returns True when an openCsiTool ``token`` cookie was minted.
    """
    jar, planted = build_jar(cookies, mode=mode)
    emit(f"--- scope={mode}: {len(planted)} gitcode cookie(s) ---")
    emit(f"    {', '.join(planted) if planted else '(none)'}")
    if not planted:
        emit("    nothing to plant; skipping")
        emit("")
        return False

    # --- step 1: the entry point, without following the redirect -----------
    opener = opener_for(jar, follow=False)
    try:
        with opener.open(
            urllib.request.Request(ENTRY, headers={"User-Agent": UA}), timeout=45.0
        ) as response:
            status, location = response.status, response.headers.get("Location", "")
    except urllib.error.HTTPError as exc:
        status, location = exc.code, exc.headers.get("Location", "")
    except Exception as exc:  # noqa: BLE001
        emit(f"[1] failed: {type(exc).__name__}")
        emit("")
        return False

    emit(f"[1] GET  {safe(ENTRY)}  ->  {status}  {safe(location) or '(no Location)'}")
    if not location:
        emit("    no redirect -- cannot continue")
        emit("")
        return False

    params = dict(parse_qsl(urlsplit(location).query, keep_blank_values=True))
    emit(f"    params: {sorted(params)}")
    for name in sorted(set(params) & SECRET_PARAMS):
        emit(f"    {name:8s}: {fingerprint(params[name])} (value withheld)")
    emit(f"    client_id   : {params.get('client_id', '')}")
    emit(f"    redirect_uri: {params.get('redirect_uri', '')}")
    emit("")

    # --- step 2: the SPA's one backend call, reproduced --------------------
    fields = {
        "client_id": params.get("client_id", ""),
        "state": params.get("state", ""),
        "redirect_uri": params.get("redirect_uri", ""),
        "response_type": "code",
    }
    body, content_type = multipart(fields)
    request = urllib.request.Request(
        CHECK_OR_AUTHORIZE,
        data=body,
        method="POST",
        headers={
            "User-Agent": UA,
            "Content-Type": content_type,
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://gitcode.com",
            "Referer": "https://gitcode.com/",
        },
    )
    emit(f"[2] POST {safe(CHECK_OR_AUTHORIZE)}")
    emit(f"    fields: {sorted(fields)}  (values withheld)")
    try:
        with opener_for(jar, follow=True).open(request, timeout=45.0) as response:
            status, payload = response.status, response.read(65536)
    except urllib.error.HTTPError as exc:
        status, payload = exc.code, exc.read(65536)
    except Exception as exc:  # noqa: BLE001
        emit(f"    failed: {type(exc).__name__}: {exc}")
        emit("")
        return False

    emit(f"    status: {status}")
    try:
        parsed = json.loads(payload.decode("utf-8", "replace"))
    except ValueError:
        emit(f"    body  : non-JSON, {len(payload)} bytes")
        parsed = {}
    emit(f"    keys  : {_shape(parsed)}")

    # The SPA reads ``response.data.data``, but axios' ``data`` is the HTTP body,
    # so the JSON body *is* the payload: the fields sit at the top level. Accept
    # a nested envelope too, so a server-side change in either direction is
    # reported rather than silently read as "no redirect_uri".
    body_obj = parsed if isinstance(parsed, dict) else {}
    inner = body_obj.get("data") if isinstance(body_obj.get("data"), dict) else body_obj
    redirect_uri = inner.get("redirect_uri")
    if not redirect_uri:
        emit("    no redirect_uri in the response -- the flow stops here")
        emit("")
        return False

    emit(f"    redirect: {safe(redirect_uri)}")
    emit(f"    params  : {query_names(redirect_uri)}")
    for name in sorted(set(query_names(redirect_uri)) & SECRET_PARAMS):
        emit(f"    {name:8s}: present, value withheld")
    emit("")

    # --- step 3: follow the callback and see whether a token appears -------
    emit(f"[3] GET  {safe(redirect_uri)}   (the openCsiTool callback)")
    try:
        with opener_for(jar, follow=True).open(
            urllib.request.Request(redirect_uri, headers={"User-Agent": UA}), timeout=45.0
        ) as response:
            final_status = response.status
    except urllib.error.HTTPError as exc:
        final_status = exc.code
    except Exception as exc:  # noqa: BLE001
        emit(f"    failed: {type(exc).__name__}: {exc}")
        emit("")
        return False

    emit(f"    status: {final_status}")
    minted = [c for c in jar if "opencsitool" in c.domain.lower() and c.name == "token"]
    if minted:
        emit(f"    TOKEN MINTED: {fingerprint(minted[0].value)}")
        emit("")
        return True
    emit("    no openCsiTool token cookie appeared")
    emit("")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9333)
    parser.add_argument(
        "--scopes",
        default="all,sso,access",
        help="comma-separated cookie scopes to try, in order",
    )
    args = parser.parse_args()

    out: list[str] = []

    def emit(line: str = "") -> None:
        print(line)
        out.append(line)

    cookies = read_browser_cookies(args.port)
    emit(f"browser cookies read : {len(cookies)}")
    emit(f"note: the openCsiTool `token` cookie is excluded from every scope, so a")
    emit(f"      token at the end was minted by this flow rather than carried in.")
    emit(f"note: the consent-submit endpoint {CONSENT_SUBMIT_PATH} is NOT called by this")
    emit(f"      probe. It is named here only so the finding is on the record; approving")
    emit(f"      a third-party grant is the account holder's decision, not a step to")
    emit(f"      automate. This probe exercises the existing-grant (renewal) path only.")
    emit("")

    results: dict[str, bool] = {}
    for mode in [m.strip() for m in args.scopes.split(",") if m.strip()]:
        results[mode] = attempt(cookies, mode, emit)

    emit("=" * 68)
    for mode, ok in results.items():
        emit(f"  scope={mode:8s} token minted: {'yes' if ok else 'no'}")

    if any(results.values()):
        emit("")
        emit("RESULT: PURE_HTTP_OAUTH_FEASIBLE -- a client running no JavaScript")
        emit("        completed the flow. The browser is only a cookie store.")
        return 0
    emit("")
    emit("RESULT: not reproduced from any scope tried.")
    return 1


def _shape(value: Any, *, depth: int = 0) -> Any:
    """Key/type/length skeleton, so a token is never echoed."""
    if depth > 3:
        return "<depth-limit>"
    if isinstance(value, dict):
        return {str(k): _shape(v, depth=depth + 1) for k, v in list(value.items())[:20]}
    if isinstance(value, list):
        return [f"...x{len(value)}"]
    if isinstance(value, str):
        return f"str(len={len(value)})"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "num"
    return type(value).__name__


if __name__ == "__main__":
    raise SystemExit(main())
