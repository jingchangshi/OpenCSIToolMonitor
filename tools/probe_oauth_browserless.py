"""LIVE reproduction: is the openCsiTool OAuth leg really browserless?

This is the independent re-run of the finding in
``docs/oauth-spa-investigation.md``, written so the claim can be checked rather
than believed. It exists because "the SPA needs JavaScript" and "the OAuth leg
needs a browser" were conflated for a long time in this project, and the only way
to settle it is to complete the flow without a browser and observe a real
``getUserInfo``.

The claim under test
--------------------
The authorize page is a client-rendered SPA, but the *backend* it talks to is
three ordinary HTTP requests:

.. code-block::

    GET  /opencsitool/rest/v1/oauth2/authorization/gitcode?redirect=%2FmyTools
         -> 302 to gitcode.com/oauth/authorize?...            (sets gitcode_oauth_session)
    POST https://web-api.gitcode.com/uc/api/v1/oauth/checkOrAuthorize
         multipart: client_id, state, redirect_uri, response_type=code
         -> 200 {"redirect_uri": "...code=...&state=..."}
    GET  <that redirect_uri>            (the openCsiTool callback)
         -> 200 + Set-Cookie: token

If that sequence completes, the openCsiTool session is established with no
browser engine anywhere in the loop, and the objective's "browser engine
required" premise is refuted rather than merely worked around.

Safety
------
* **GET only plus one POST**, and the POST is ``checkOrAuthorize`` -- a *status
  query* that returns the code for a grant that already exists. It is not the
  consent-submit endpoint (``POST /uc/api/v1/oauth/authorize``), which is
  deliberately **never called** here: approving a third-party grant is the
  user's decision and automating it is out of bounds.
* The credential is read from a profile that is already signed in. Nothing is
  written to that profile.
* The authorization ``code`` and ``state`` are sensitive and are **never
  printed**: URLs are reduced to host + path, and query strings are dropped
  before anything is emitted.
* The resulting ``token`` cookie is verified with ``getUserInfo`` and then
  discarded with the process. It is never written to disk.

Usage
-----
    python tools/probe_oauth_browserless.py --source-port 9222
    python tools/probe_oauth_browserless.py --source-port 9222 --json

Labels: LIVE, NETWORK, AUTH_SIDE_EFFECT (it completes a real OAuth round trip
and can mint a session cookie), GET only apart from ``checkOrAuthorize``, which
is a status query. It never prints a secret. It is never run by the test suite
and is never part of CI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from typing import Any, Mapping
from urllib.parse import parse_qs, urlencode, urlparse

sys.path.insert(0, "src")

from opencsi.auth.cdp import discover_cdp_endpoint  # noqa: E402
from opencsi.ws import CdpConnection  # noqa: E402

APP_BASE = "https://opencsitool.com"
OAUTH_ENTRY = "/opencsitool/rest/v1/oauth2/authorization/gitcode"
CHECK_PATH = "/uc/api/v1/oauth/checkOrAuthorize"
GITCODE_API = "https://web-api.gitcode.com"
USER_INFO = "/opencsitool/rest/v1/user/getUserInfo"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)

#: Cookies that make up the GitCode session, in the order a real profile holds
#: them. Measured, not guessed (tools/probe_gitcode_session.py).
SSO_COOKIES = ("GITCODE_ACCESS_TOKEN", "GITCODE_REFRESH_TOKEN", "GitCodeUserName")


def fingerprint(value: str) -> str:
    return f"len={len(value):<5d} sha256={hashlib.sha256(value.encode()).hexdigest()[:12]}"


def redact(url: str) -> str:
    """Host and path only. A code or state lives in the query and must not print."""
    parts = urlparse(url)
    return f"{parts.netloc}{parts.path}"


def read_session_cookies(port: int) -> list[dict[str, Any]]:
    """Read the GitCode session out of an already-signed-in profile (read-only)."""
    endpoint = discover_cdp_endpoint(f"http://127.0.0.1:{port}", probe=True)
    browser_ws = endpoint.browser_ws_url()
    if not browser_ws:
        raise RuntimeError(f"port {port} exposes no browser-level WebSocket")
    with CdpConnection(browser_ws, timeout=20.0) as conn:
        cookies = conn.call("Storage.getCookies", {}, timeout=15.0).get("cookies") or []
    out: list[dict[str, Any]] = []
    for cookie in cookies:
        if not isinstance(cookie, Mapping):
            continue
        if str(cookie.get("name") or "") not in SSO_COOKIES:
            continue
        if "gitcode.com" not in str(cookie.get("domain") or "").lower():
            continue
        out.append(dict(cookie))
    return out


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface each hop instead of following it.

    The flow's whole content is in *where* each step sends you, so following
    redirects automatically would hide the thing being measured.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _opener(jar: CookieJar, *, follow: bool = False) -> urllib.request.OpenerDirector:
    handlers: list[Any] = [urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(jar)]
    if not follow:
        handlers.append(_NoRedirect)
    return urllib.request.build_opener(*handlers)


def _request(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    data: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    method: str | None = None,
    timeout: float = 25.0,
) -> tuple[int, Mapping[str, str], bytes]:
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://gitcode.com",
            "Referer": "https://gitcode.com/",
            **(dict(headers) if headers else {}),
        },
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read()
        except Exception:  # noqa: BLE001
            pass
        status, head = exc.code, dict(exc.headers)
        exc.close()
        return status, head, body


def multipart(fields: Mapping[str, str]) -> tuple[bytes, str]:
    """Encode ``fields`` as multipart/form-data.

    The real client posts a native form, so the body is built by hand rather than
    sent as urlencoded JSON -- the server is documented to answer 401 to the
    wrong shape, and reproducing the shape is the point.
    """
    boundary = "----OpenCSIProbeBoundary7d1a"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        )
        chunks.append(str(value).encode())
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-port", type=int, default=9222)
    parser.add_argument("--redirect", default="/myTools")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    steps: list[dict[str, Any]] = []

    def step(name: str, **fields: Any) -> None:
        record = {"step": name, **fields}
        steps.append(record)
        if not args.json:
            detail = "  ".join(f"{k}={v}" for k, v in fields.items())
            print(f"  {name:22s} {detail}")

    print("== 1. read the GitCode session from an already-signed-in profile ==")
    cookies = read_session_cookies(args.source_port)
    names = sorted(str(c.get("name")) for c in cookies)
    print(f"  cookies found: {names or '(none)'}")
    if not cookies:
        print()
        print("SKIP: the source profile is not signed in to GitCode, so there is")
        print("      no session to run the OAuth leg with. This probe proves")
        print("      nothing in that state and says so rather than guessing.")
        return 2

    jar = CookieJar()
    for cookie in cookies:
        # Replay through a real CookieJar so the request is byte-for-byte what a
        # browser would send, including the domain scoping.
        from http.cookiejar import Cookie

        jar.set_cookie(
            Cookie(
                version=0,
                name=str(cookie.get("name")),
                value=str(cookie.get("value")),
                port=None,
                port_specified=False,
                domain=str(cookie.get("domain") or ".gitcode.com"),
                domain_specified=True,
                domain_initial_dot=str(cookie.get("domain") or "").startswith("."),
                path=str(cookie.get("path") or "/"),
                path_specified=True,
                secure=bool(cookie.get("secure")),
                expires=int(cookie["expires"]) if cookie.get("expires", -1) > 0 else None,
                discard=False,
                comment=None,
                comment_url=None,
                rest={},
                rfc2109=False,
            )
        )

    opener = _opener(jar, follow=False)

    print()
    print("== 2. openCsiTool OAuth entry point (expect 302 to GitCode) ==")
    entry = f"{APP_BASE}{OAUTH_ENTRY}?{urlencode({'redirect': args.redirect})}"
    status, headers, _body = _request(opener, entry)
    location = headers.get("Location") or headers.get("location") or ""
    step("entry", status=status, to=redact(location) or "(no location)")
    if status not in (301, 302, 303, 307, 308) or not location:
        print()
        print("VERDICT: PURE_HTTP_OAUTH_NOT_REPRODUCED -- the entry point did not")
        print("         redirect to an authorize URL, so the sequence below cannot")
        print("         be exercised. This is 'not reproduced', not 'impossible'.")
        return 1

    authorize = urlparse(location)
    query = parse_qs(authorize.query)
    required = ("client_id", "state", "redirect_uri", "response_type")
    present = {key: bool(query.get(key)) for key in required}
    step("authorize params", **present)
    missing = [key for key, ok in present.items() if not ok]
    if missing:
        print()
        print(f"VERDICT: PURE_HTTP_OAUTH_NOT_REPRODUCED -- the authorize URL is")
        print(f"         missing {missing}, so the POST below cannot be built.")
        return 1

    print()
    print("== 3. POST checkOrAuthorize (a status query, not a consent submit) ==")
    fields = {
        "client_id": query["client_id"][0],
        "state": query["state"][0],
        "redirect_uri": query["redirect_uri"][0],
        "response_type": query.get("response_type", ["code"])[0],
    }
    body, content_type = multipart(fields)
    status, _headers, payload = _request(
        opener,
        f"{GITCODE_API}{CHECK_PATH}",
        data=body,
        headers={"Content-Type": content_type},
        method="POST",
    )
    keys: list[str] = []
    try:
        parsed = json.loads(payload.decode("utf-8", "replace"))
        if isinstance(parsed, Mapping):
            keys = sorted(str(k) for k in parsed)
    except ValueError:
        parsed = None
    step("checkOrAuthorize", status=status, bytes=len(payload), keys=keys)

    if status != 200 or not isinstance(parsed, Mapping):
        print()
        print(f"VERDICT: PURE_HTTP_OAUTH_NOT_REPRODUCED -- checkOrAuthorize answered")
        print(f"         HTTP {status}. A 401 here means the GitCode session was not")
        print("         accepted; it does not mean a browser is required.")
        return 1

    callback = parsed.get("redirect_uri")
    if not isinstance(callback, str) or not callback:
        print()
        print("VERDICT: BROWSERLESS_FLOW_REQUIRES_CONSENT -- checkOrAuthorize")
        print("         answered 200 but returned no redirect_uri, which is what a")
        print("         grant that has not been approved looks like. Completing it")
        print("         would mean submitting the consent form, which this probe")
        print("         deliberately never does.")
        return 3

    print()
    print("== 4. follow the callback (expect the openCsiTool token cookie) ==")
    before = {cookie.name for cookie in jar}
    status, headers, _body = _request(opener, callback)
    after = {cookie.name: cookie for cookie in jar}
    step("callback", status=status, to=redact(callback))
    minted = sorted(after.keys() - before)
    step("cookies minted", names=minted or "(none)")

    token = after.get("token")
    if token is None:
        print()
        print("VERDICT: PURE_HTTP_OAUTH_NOT_REPRODUCED -- the callback did not set")
        print("         a token cookie, so no session was established.")
        return 1

    print()
    print("== 5. verify the session with getUserInfo (the only accepted proof) ==")
    status, _headers, payload = _request(opener, f"{APP_BASE}{USER_INFO}")
    identity_keys: list[str] = []
    try:
        body_json = json.loads(payload.decode("utf-8", "replace"))
        if isinstance(body_json, Mapping):
            data = body_json.get("data")
            if isinstance(data, Mapping):
                identity_keys = sorted(str(k) for k in data)
            else:
                identity_keys = sorted(str(k) for k in body_json)
    except ValueError:
        pass
    step("getUserInfo", status=status, bytes=len(payload), keys=identity_keys[:8])

    print()
    print("== verdict ==")
    print(f"  token cookie      : {fingerprint(str(token.value))}")
    print(f"  getUserInfo       : HTTP {status}")
    if status == 200:
        print()
        print("PURE_HTTP_OAUTH_FEASIBLE: the openCsiTool session was established and")
        print("verified with no browser engine anywhere in the flow. The authorize")
        print("page is an SPA, but the OAuth leg is three ordinary HTTP requests.")
        print()
        print("Scope note: this proves renewal and repeat authorization are")
        print("browserless for a grant that already exists. It does NOT prove")
        print("first-time consent can be granted without a browser, and this probe")
        print("never submits the consent form.")
        return 0
    print()
    print("PURE_HTTP_OAUTH_NOT_REPRODUCED: a token cookie was issued but")
    print("getUserInfo did not accept it.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
