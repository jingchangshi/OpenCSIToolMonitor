"""Renew an openCsiTool session with plain HTTP. No browser, no JavaScript.

The finding this module is built on
-----------------------------------
This project spent a long time asserting that the openCsiTool OAuth leg was
browser-bound, because GitCode's ``/oauth/authorize`` page is a client-rendered
SPA. That reasoning had a hole in it, and the hole is the whole point of
objective §74:

.. code-block::

    SPA requires JavaScript   ≠   the backend requires a browser

The page needs JavaScript to *render*. The backend the page talks to does not
care what rendered the request. Measured
(``tools/probe_oauth_browserless.py``), the entire leg is three ordinary
requests:

.. code-block::

    GET  /opencsitool/rest/v1/oauth2/authorization/gitcode?redirect=%2FmyTools
         │  302 -> gitcode.com/oauth/authorize?client_id=…&state=…&redirect_uri=…
         │         (also sets gitcode_oauth_session)
         ▼
    POST https://web-api.gitcode.com/uc/api/v1/oauth/checkOrAuthorize
         │  multipart/form-data: client_id, state, redirect_uri, response_type=code
         │  200 {"redirect_uri": "<callback>?code=…&state=…", …}
         ▼
    GET  <that callback>
         │  200 + Set-Cookie: token
         ▼
    GET  /opencsitool/rest/v1/user/getUserInfo  -> 200

Run on a real signed-in GitCode session, that sequence established an
openCsiTool session and ``getUserInfo`` accepted it, with no browser engine
anywhere. So the engine is not required, and this module is the consequence.

What this replaces, and what it does not
----------------------------------------
It is a drop-in :class:`~opencsi.auth.session.SessionRenewer`, so the existing
:class:`~opencsi.auth.session.SessionManager` policy -- the 5-minute margin, the
bounded retry, the cooldown -- is untouched. That architecture was verified
working and objective §2 forbids rewriting it.

:class:`~opencsi.auth.oauth_browser.BrowserOAuthRenewer` is **kept**, and is
still the right tool for the one case this cannot cover: granting consent the
first time. ``checkOrAuthorize`` returns an authorization code for a grant that
*already exists*; when it answers 200 with no ``redirect_uri``, the grant has not
been approved and a human has to decide. That is
:attr:`RenewalStatus.CONSENT_REQUIRED`, and it is reported as such rather than
worked around -- approving a third-party grant on the user's behalf is not this
tool's decision to make.

Credentials
-----------
The GitCode session is read from a running browser profile over CDP, exactly as
:class:`~opencsi.auth.cdp.CdpCookieProvider` already does for the openCsiTool
token. That keeps one source of truth for "where does the session live" and means
no new credential file appears on disk. Once read, the tokens are registered for
redaction and live only in the cookie jar of one request sequence.

Nothing here writes to a browser profile. The one thing that could look like a
write -- ``Set-Cookie: token`` -- is the *server's* answer, captured in an
in-memory jar and handed to the caller through the provider, never persisted.
"""

from __future__ import annotations

import http.cookiejar
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from ..errors import NetworkError, OpenCsiError
from ..redaction import register_secret, scrub_text
from .base import CredentialProvider
from .cdp import COOKIE_DOMAINS, COOKIE_NAME
from .session import RenewalResult, RenewalStatus

log = logging.getLogger("opencsi.auth.http_oauth")

#: The OAuth entry point. Identical to the one the browser renewer navigates to,
#: because it is the same endpoint -- only the client differs.
OAUTH_ENTRY_PATH = "/opencsitool/rest/v1/oauth2/authorization/gitcode"

#: GitCode's API host and the backend call the authorize page makes. The bundle
#: writes ``/api/v1/oauth/checkOrAuthorize`` and GitCode's axios interceptor
#: prepends ``/uc``; the prefixed path is what answers, and the unprefixed one
#: answers 401. Confirmed on the wire.
GITCODE_API_BASE = "https://web-api.gitcode.com"
CHECK_AUTHORIZE_PATH = "/uc/api/v1/oauth/checkOrAuthorize"

#: A known *authenticated* GitCode endpoint, used to tell "the SSO session is
#: gone" from "the SSO session is fine but the grant is missing". Without this
#: distinction both produce the same consent-shaped answer, and telling a signed
#: in user to sign in is the mistake ``CONSENT_REQUIRED`` exists to prevent.
GITCODE_USER_INFO_PATH = "/uc/api/v1/user/oauth/userInfo"

#: The openCsiTool cookie that constitutes the session.
TOKEN_COOKIE = COOKIE_NAME
TOKEN_DOMAINS = COOKIE_DOMAINS

#: The cookies that make up a GitCode browser session, measured on a real
#: profile (``tools/probe_gitcode_session.py``). ``GITCODE_ACCESS_TOKEN`` alone
#: was sufficient in the scope matrix, but all three are carried because the
#: refresh token is what lets a long-lived process keep going.
GITCODE_SESSION_COOKIES: tuple[str, ...] = (
    "GITCODE_ACCESS_TOKEN",
    "GITCODE_REFRESH_TOKEN",
    "GitCodeUserName",
)

DEFAULT_TIMEOUT = 45.0

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)

#: Multipart boundary. Fixed rather than random on purpose: it appears in a
#: request body that may be logged by a proxy, and a constant makes a captured
#: body obviously synthetic rather than looking like a credential.
_BOUNDARY = "----OpenCSIBrowserlessBoundary"


class BrowserlessOAuthError(OpenCsiError):
    """A step of the browserless flow failed in a way worth naming."""

    code = "BROWSERLESS_OAUTH_ERROR"
    exit_code = 13


@dataclass
class OAuthTrace:
    """What each step of the flow did (secret-free).

    Every URL is reduced to host + path: the authorization ``code`` and ``state``
    travel in query strings, and a trace that printed them would be a credential
    leak with a helpful name.

    Mutable rather than frozen: the flow fills it in as it goes, and threading a
    return value back out of every step would obscure the sequence, which is the
    part worth reading.
    """

    entry_status: int | None = None
    entry_host_path: str | None = None
    authorize_params: tuple[str, ...] = ()
    check_status: int | None = None
    check_keys: tuple[str, ...] = ()
    callback_status: int | None = None
    callback_host_path: str | None = None
    token_minted: bool = False
    #: Whether ``checkOrAuthorize`` returned an authorization code at all.
    authorization_granted: bool = False
    #: Whether the GitCode session itself was still valid, when probed.
    gitcode_session_valid: bool | None = None

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "entry_status": self.entry_status,
            "check_status": self.check_status,
            "callback_status": self.callback_status,
            "token_minted": self.token_minted,
            "authorization_granted": self.authorization_granted,
        }
        if self.entry_host_path:
            out["entry_host_path"] = self.entry_host_path
        if self.authorize_params:
            out["authorize_params"] = list(self.authorize_params)
        if self.check_keys:
            out["check_keys"] = list(self.check_keys)
        if self.callback_host_path:
            out["callback_host_path"] = self.callback_host_path
        if self.gitcode_session_valid is not None:
            out["gitcode_session_valid"] = self.gitcode_session_valid
        return out


def _host_path(url: str) -> str:
    """``host/path`` only. Drops the query, which is where secrets travel."""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.netloc}{parts.path}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface each hop instead of following it.

    The flow's content is *where* each step sends you, so following redirects
    automatically would hide the thing being measured -- and the callback has to
    be requested deliberately anyway, because it is the request that sets the
    session cookie.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, D102
        return None


class _Jar(http.cookiejar.CookieJar):
    """A cookie jar that records nothing outside its own process.

    ``http.cookiejar`` is in-memory by default; the subclass exists only so the
    intent is explicit at the call site. There is deliberately no
    ``FileCookieJar`` anywhere in this module: a session that is never written to
    disk cannot be leaked from disk.
    """


@dataclass
class _JarView:
    """Read the token out of a jar, in the shape the provider expects."""

    jar: http.cookiejar.CookieJar

    def cookies(self) -> list[Mapping[str, Any]]:
        out: list[Mapping[str, Any]] = []
        for cookie in self.jar:
            out.append(
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path,
                    "expires": cookie.expires if cookie.expires else -1,
                    "httpOnly": True,
                    "secure": bool(cookie.secure),
                }
            )
        return out


def _multipart(fields: Mapping[str, str]) -> tuple[bytes, str]:
    """Encode ``fields`` as ``multipart/form-data``.

    The real client posts a native form, not urlencoded JSON. Reproducing the
    shape matters: the same call answers 401 to a body it does not recognise,
    which is indistinguishable from "the session was rejected" if the encoding is
    wrong -- a false negative that would have made the whole flow look
    browser-bound.
    """
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(f"--{_BOUNDARY}\r\n".encode("ascii"))
        chunks.append(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii")
        )
        chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")
    chunks.append(f"--{_BOUNDARY}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={_BOUNDARY}"


@dataclass
class _SessionCookies:
    """The GitCode session, held only for the duration of one renewal."""

    records: list[dict[str, Any]] = field(default_factory=list)

    @property
    def present(self) -> bool:
        return bool(self.records)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(str(record.get("name")) for record in self.records))

    def install(self, jar: http.cookiejar.CookieJar) -> None:
        for record in self.records:
            expires = record.get("expires")
            jar.set_cookie(
                http.cookiejar.Cookie(
                    version=0,
                    name=str(record.get("name")),
                    value=str(record.get("value")),
                    port=None,
                    port_specified=False,
                    domain=str(record.get("domain") or ".gitcode.com"),
                    domain_specified=True,
                    domain_initial_dot=str(record.get("domain") or "").startswith("."),
                    path=str(record.get("path") or "/"),
                    path_specified=True,
                    secure=bool(record.get("secure")),
                    expires=int(expires) if isinstance(expires, (int, float)) and expires > 0 else None,
                    discard=False,
                    comment=None,
                    comment_url=None,
                    rest={},
                    rfc2109=False,
                )
            )


def _read_gitcode_session(source: Any, *, timeout: float) -> _SessionCookies:
    """Read the GitCode session cookies out of a CDP-backed source.

    ``source`` is anything with ``read_all_cookies()`` -- in practice a
    :class:`~opencsi.auth.cdp.CdpCookieProvider`. Reading through the existing
    provider rather than opening a second CDP connection keeps one owner of the
    browser connection and means the endpoint discovery is not duplicated.
    """
    out = _SessionCookies()
    reader = getattr(source, "read_all_cookies", None)
    if not callable(reader):
        return out
    try:
        cookies = reader(timeout=timeout)
    except OpenCsiError:
        return out
    for cookie in cookies:
        if not isinstance(cookie, Mapping):
            continue
        name = str(cookie.get("name") or "")
        domain = str(cookie.get("domain") or "")
        if name not in GITCODE_SESSION_COOKIES:
            continue
        if "gitcode.com" not in domain.lower():
            continue
        value = cookie.get("value")
        if isinstance(value, str) and value:
            register_secret(value)
        out.records.append(dict(cookie))
    return out


class HttpOAuthRenewer:
    """Renew an openCsiTool session over plain HTTP.

    Implements :class:`~opencsi.auth.session.SessionRenewer`, so it is a drop-in
    for :class:`~opencsi.auth.oauth_browser.BrowserOAuthRenewer` and the
    :class:`~opencsi.auth.session.SessionManager` policy above it is unchanged.

    Unlike the browser renewer, this one *does* write the new cookie into the
    provider, because it is the thing that obtained it -- there is no browser to
    write it into. The provider is therefore required, not optional.
    """

    name = "http-oauth"

    def __init__(
        self,
        provider: Any,
        *,
        base_url: str = "https://opencsitool.com",
        gitcode_api_base: str = GITCODE_API_BASE,
        timeout: float = DEFAULT_TIMEOUT,
        use_proxy: bool = False,
    ) -> None:
        self._provider = provider
        self._base_url = base_url.rstrip("/")
        self._gitcode_api_base = gitcode_api_base.rstrip("/")
        self._timeout = timeout
        #: ``ProxyHandler({})`` by default. The system proxy on a real machine was
        #: observed failing TLS for opencsitool.com while passing gitcode.com --
        #: see ``docs/oauth-spa-investigation.md`` -- so the default is to bypass
        #: it and the flag exists to opt back in rather than out.
        self._use_proxy = use_proxy
        self._last_trace: OAuthTrace | None = None

    # ── the protocol ──────────────────────────────────────────────────────
    @property
    def last_trace(self) -> OAuthTrace | None:
        """What the last attempt observed. Never contains a secret."""
        return self._last_trace

    def can_renew(self) -> bool:
        """Whether the GitCode session needed for this flow is reachable.

        Cheap and non-committal: it asks whether a *source* exists, not whether
        the flow will succeed. A ``False`` here is what lets the manager fall
        back to the browser renewer without having spent a round trip.
        """
        reader = getattr(self._provider, "read_all_cookies", None)
        if not callable(reader):
            return False
        try:
            cookies = reader(timeout=min(self._timeout, 10.0))
        except Exception:  # noqa: BLE001 - a capability probe must never raise
            return False
        return any(
            isinstance(cookie, Mapping)
            and str(cookie.get("name") or "") in GITCODE_SESSION_COOKIES
            and "gitcode.com" in str(cookie.get("domain") or "").lower()
            for cookie in cookies
        )

    def describe(self) -> str:
        return "openCsiTool OAuth over plain HTTP (no browser, no JavaScript)"

    # ── the flow ──────────────────────────────────────────────────────────
    def renew(
        self,
        *,
        timeout: float | None = None,
        before: CredentialProvider | None = None,
    ) -> RenewalResult:
        """Run the browserless flow once. Never raises; always reports.

        Every failure maps to a :class:`~opencsi.auth.session.RenewalStatus` the
        manager and the CLI already understand, so nothing new has to be taught
        to the layers above.
        """
        budget = self._timeout if timeout is None else timeout
        deadline = time.monotonic() + max(1.0, budget)

        def remaining() -> float:
            return max(1.0, deadline - time.monotonic())

        previous = self._peek_token(before)

        session = _read_gitcode_session(self._provider, timeout=min(10.0, remaining()))
        if not session.present:
            # No GitCode session to spend. Reported as LOGIN_REQUIRED rather than
            # a generic failure, because that is what it is: the user must sign in
            # again, and the manager's fallback will not help either.
            self._last_trace = OAuthTrace()
            return RenewalResult(
                RenewalStatus.LOGIN_REQUIRED,
                requires_interaction=True,
                detail="no GitCode session is available to renew from",
            )

        jar = _Jar()
        session.install(jar)
        opener = self._opener(jar)
        trace = OAuthTrace()
        state: dict[str, Any] = {}

        try:
            outcome = self._run_flow(opener, jar, trace, state, remaining)
        except NetworkError as exc:
            self._last_trace = trace
            return RenewalResult(
                RenewalStatus.TIMEOUT if "timed out" in str(exc).lower() else RenewalStatus.OAUTH_FAILED,
                detail=scrub_text(str(exc))[:200],
            )
        except OpenCsiError as exc:
            self._last_trace = trace
            return RenewalResult(
                RenewalStatus.OAUTH_FAILED,
                detail=scrub_text(str(exc))[:200],
            )

        self._last_trace = trace

        if outcome is not None:
            return outcome

        token = self._token_from(jar)
        if not token:
            return RenewalResult(
                RenewalStatus.OAUTH_FAILED,
                detail="the callback completed but issued no session cookie",
            )

        register_secret(token)
        changed = previous is None or previous != token
        self._store_token(token, jar)
        return RenewalResult(
            RenewalStatus.RENEWED if changed else RenewalStatus.ALREADY_VALID,
            renewed=changed,
            token_changed=changed,
            expires_in=self._expires_in(jar),
        )

    # ── internals ─────────────────────────────────────────────────────────
    def _opener(self, jar: http.cookiejar.CookieJar) -> urllib.request.OpenerDirector:
        handlers: list[Any] = [urllib.request.HTTPCookieProcessor(jar), _NoRedirect()]
        if not self._use_proxy:
            handlers.insert(0, urllib.request.ProxyHandler({}))
        return urllib.request.build_opener(*handlers)

    def _headers(self, *, gitcode: bool = False) -> dict[str, str]:
        return {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://gitcode.com" if gitcode else self._base_url,
            "Referer": "https://gitcode.com/" if gitcode else f"{self._base_url}/myTools",
        }

    def _request(
        self,
        opener: urllib.request.OpenerDirector,
        url: str,
        *,
        data: bytes | None = None,
        content_type: str | None = None,
        method: str | None = None,
        timeout: float,
        gitcode: bool = False,
    ) -> tuple[int, Mapping[str, str], bytes]:
        headers = self._headers(gitcode=gitcode)
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
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
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise NetworkError(f"could not reach {_host_path(url)} ({type(reason).__name__})") from exc
        except (TimeoutError, OSError) as exc:
            raise NetworkError(f"the request to {_host_path(url)} timed out") from exc

    def _run_flow(
        self,
        opener: urllib.request.OpenerDirector,
        jar: http.cookiejar.CookieJar,
        trace: OAuthTrace,
        state: dict[str, Any],
        remaining: Any,
    ) -> RenewalResult | None:
        """Steps 1-3. Returns a result to stop early, or ``None`` to continue."""

        # ── step 1: the entry point ───────────────────────────────────────
        entry = f"{self._base_url}{OAUTH_ENTRY_PATH}?{urllib.parse.urlencode({'redirect': '/myTools'})}"
        status, headers, _body = self._request(opener, entry, timeout=remaining())
        location = headers.get("Location") or headers.get("location") or ""
        trace.entry_status = status
        trace.entry_host_path = _host_path(location) or None

        if status not in (301, 302, 303, 307, 308) or not location:
            return RenewalResult(
                RenewalStatus.OAUTH_FAILED,
                detail=f"the OAuth entry point answered HTTP {status} without redirecting",
            )

        query = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)
        required = ("client_id", "state", "redirect_uri", "response_type")
        present = tuple(key for key in required if query.get(key))
        trace.authorize_params = present
        missing = [key for key in required if key not in present]
        if missing:
            return RenewalResult(
                RenewalStatus.OAUTH_FAILED,
                detail="the authorize URL is missing " + ", ".join(missing),
            )

        # ── step 2: checkOrAuthorize ──────────────────────────────────────
        # A *status query*, not a consent submit. It returns the authorization
        # code for a grant that already exists. It is never used to create one:
        # that endpoint is deliberately not called anywhere in this project.
        fields = {
            "client_id": query["client_id"][0],
            "state": query["state"][0],
            "redirect_uri": query["redirect_uri"][0],
            "response_type": query.get("response_type", ["code"])[0],
        }
        body, content_type = _multipart(fields)
        status, _headers, payload = self._request(
            opener,
            f"{self._gitcode_api_base}{CHECK_AUTHORIZE_PATH}",
            data=body,
            content_type=content_type,
            method="POST",
            timeout=remaining(),
            gitcode=True,
        )
        trace.check_status = status
        parsed = self._decode(payload)
        trace.check_keys = tuple(sorted(str(k) for k in parsed)) if isinstance(parsed, Mapping) else ()

        if status in (401, 403):
            # The GitCode session was not accepted. Say which of the two it is,
            # because the remedies differ completely: an expired SSO session needs
            # a sign-in, while a valid session with no grant needs one click.
            trace.gitcode_session_valid = self._probe_gitcode_session(opener, remaining())
            if trace.gitcode_session_valid is False:
                return RenewalResult(
                    RenewalStatus.LOGIN_REQUIRED,
                    requires_interaction=True,
                    detail="the GitCode session is no longer valid",
                )
            return RenewalResult(
                RenewalStatus.OAUTH_FAILED,
                detail=f"GitCode rejected the authorization request (HTTP {status})",
            )

        if status != 200 or not isinstance(parsed, Mapping):
            return RenewalResult(
                RenewalStatus.OAUTH_FAILED,
                detail=f"GitCode answered HTTP {status} to the authorization request",
            )

        callback = parsed.get("redirect_uri")
        if not isinstance(callback, str) or not callback:
            # A 200 with no code is what an unapproved grant looks like. The
            # consent page has to be answered by a human, so this is reported as
            # requiring interaction -- never worked around by submitting the
            # consent form, which is the user's decision and not this tool's.
            trace.authorization_granted = False
            trace.gitcode_session_valid = self._probe_gitcode_session(opener, remaining())
            return RenewalResult(
                RenewalStatus.CONSENT_REQUIRED,
                requires_interaction=True,
                detail=(
                    "GitCode has no existing authorization for this application, so "
                    "the approval page has to be confirmed once"
                ),
            )

        trace.authorization_granted = True
        state["callback"] = callback

        # ── step 3: the callback ──────────────────────────────────────────
        status, _headers, _body = self._request(opener, callback, timeout=remaining())
        trace.callback_status = status
        trace.callback_host_path = _host_path(callback)
        token = self._token_from(jar)
        trace.token_minted = token is not None

        if status not in (200, 302, 303) and token is None:
            return RenewalResult(
                RenewalStatus.OAUTH_FAILED,
                detail=f"the OAuth callback answered HTTP {status} and set no cookie",
            )
        return None

    def _probe_gitcode_session(
        self, opener: urllib.request.OpenerDirector, timeout: float
    ) -> bool | None:
        """Whether the GitCode SSO session itself is still valid.

        ``None`` means "could not tell", which is deliberately not ``False``: a
        transient network hiccup must not be reported as a signed-out user.
        """
        try:
            status, _headers, _body = self._request(
                opener,
                f"{self._gitcode_api_base}{GITCODE_USER_INFO_PATH}",
                timeout=timeout,
                gitcode=True,
            )
        except OpenCsiError:
            return None
        if status == 200:
            return True
        if status in (401, 403):
            return False
        return None

    @staticmethod
    def _decode(payload: bytes) -> Any:
        try:
            return json.loads(payload.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            return None

    def _token_from(self, jar: http.cookiejar.CookieJar) -> str | None:
        """The openCsiTool session cookie, preferring an unexpired one."""
        now = time.time()
        candidates = [
            cookie
            for cookie in jar
            if cookie.name == TOKEN_COOKIE
            and any(domain in cookie.domain for domain in TOKEN_DOMAINS)
        ]
        if not candidates:
            return None
        live = [c for c in candidates if not c.expires or c.expires > now]
        chosen = (live or candidates)[0]
        return chosen.value or None

    def _expires_in(self, jar: http.cookiejar.CookieJar) -> float | None:
        now = time.time()
        for cookie in jar:
            if cookie.name != TOKEN_COOKIE:
                continue
            if not any(domain in cookie.domain for domain in TOKEN_DOMAINS):
                continue
            if cookie.expires:
                return max(0.0, float(cookie.expires) - now)
        return None

    def _peek_token(self, before: CredentialProvider | None) -> str | None:
        """The credential to compare against, without triggering a fetch.

        Uses ``peek_token`` when the provider offers it, for the same reason
        :meth:`SessionManager._token_fingerprint` does: calling ``get_token()``
        would fetch a fresh value and make the comparison meaningless.
        """
        source = before if before is not None else self._provider
        peek = getattr(source, "peek_token", None)
        try:
            token = peek() if callable(peek) else source.get_token()
        except OpenCsiError:
            return None
        return token or None

    def _store_token(self, token: str, jar: http.cookiejar.CookieJar) -> None:
        """Hand the new cookie to the provider, which owns it from here.

        The provider is a live view over a browser profile, so it cannot be
        "written to" the way a jar can. What it *can* do is cache a value it was
        given, which is exactly what is needed: the session is now valid and the
        next read should not go looking for a browser that has no idea about it.
        """
        remember = getattr(self._provider, "remember_token", None)
        if callable(remember):
            expires_in = self._expires_in(jar)
            try:
                remember(token, expires_in=expires_in)
                return
            except TypeError:
                remember(token)
                return
        log.debug("the credential provider cannot cache a token; the session was renewed but not stored")

    def __repr__(self) -> str:
        return (
            f"HttpOAuthRenewer(base_url={self._base_url!r}, "
            f"use_proxy={self._use_proxy})"
        )
