"""The browserless OAuth renewer and the chain that prefers it.

What these tests are guarding
-----------------------------
Two separate claims, and it matters that they are separate:

1. **The flow is HTTP.** The openCsiTool OAuth leg needs no browser engine, and
   the module's whole reason for existing is that the project believed otherwise
   for a long time. The step tests below pin the exact sequence -- entry 302,
   ``checkOrAuthorize`` multipart POST, callback, token -- so a refactor that
   quietly reintroduces a browser dependency fails here.

2. **The chain prefers it, and stops when a human is needed.** ``CONSENT_REQUIRED``
   and ``LOGIN_REQUIRED`` must halt the chain rather than falling through to the
   browser renewer. Falling through would delay a message the user needs and could
   bury a real consent requirement behind a misleading "renewal failed".

Everything runs against a stub transport. No test here opens a socket: the
network shapes being asserted are the ones measured in
``tools/probe_oauth_browserless.py``, reproduced from recorded responses rather
than re-fetched. That is what lets the unhappy paths -- a rejected SSO session, an
unapproved grant, a malformed authorize URL -- be tested at all, since none of
them can be produced on demand against a live account.
"""

from __future__ import annotations

import email.utils
import http.cookiejar
import http.cookies
import json
import time
import unittest
import urllib.parse
from typing import Any, Mapping

import helpers  # noqa: F401  (imported for its sys.path side effect)

from opencsi.auth.http_oauth import (
    CHECK_AUTHORIZE_PATH,
    GITCODE_SESSION_COOKIES,
    OAUTH_ENTRY_PATH,
    HttpOAuthRenewer,
    OAuthTrace,
)
from opencsi.auth.session import FallbackRenewer, RenewalResult, RenewalStatus

# Synthetic credentials, shaped like the real ones and never the real ones.
FAKE_GITCODE_ACCESS = "gc_access_" + "a1b2c3d4" * 6
FAKE_OPENCSITOOL_TOKEN = "ocs_token_" + "9f8e7d6c" * 6


def gitcode_cookies() -> list[dict[str, Any]]:
    """A GitCode browser session, as CDP reports it."""
    return [
        {
            "name": "GITCODE_ACCESS_TOKEN",
            "value": FAKE_GITCODE_ACCESS,
            "domain": ".gitcode.com",
            "path": "/",
            "secure": True,
            "expires": 0,
        },
        {
            "name": "GitCodeUserName",
            "value": "tester",
            "domain": ".gitcode.com",
            "path": "/",
            "secure": True,
            "expires": 0,
        },
    ]


class _StubProvider:
    """A credential provider with a scripted cookie store.

    ``read_all_cookies`` is the seam the renewer reads through, so the stub only
    has to answer that plus the token accessors. ``remember_token`` records what
    it was given, which is how "the renewed session reached the provider" is
    asserted without inspecting anything private.
    """

    name = "stub"

    def __init__(self, *, cookies: list[dict[str, Any]] | None = None, token: str | None = None):
        self._cookies = list(cookies or [])
        self._token = token
        self.remembered: list[str] = []
        self.raise_on_read: Exception | None = None

    def read_all_cookies(self, *, timeout: float | None = None):
        if self.raise_on_read is not None:
            raise self.raise_on_read
        return list(self._cookies)

    def get_token(self):
        return self._token

    def peek_token(self):
        return self._token

    def remember_token(self, token: str, *, expires_in: float | None = None) -> None:
        self.remembered.append(token)
        self._token = token


class _StubOpener:
    """A scripted transport.

    Routes are keyed by ``(host, path)`` rather than by full URL, because the
    query string is where the authorization code lives and a test that matched on
    it would be asserting the wrong thing. Each route answers with a
    ``(status, headers, body)`` triple, or a callable that receives the request
    body so the multipart payload can be inspected.

    It applies ``Set-Cookie`` to the jar itself. In production that is urllib's
    ``HTTPCookieProcessor``, but this stub *replaces* the opener rather than
    sitting inside one, so it has to honour the same contract -- otherwise the
    callback would appear to set no cookie and every success path would read as a
    failure for a reason that has nothing to do with the code under test.
    """

    def __init__(self, routes: Mapping[tuple[str, str], Any]) -> None:
        self._routes = dict(routes)
        self.requests: list[dict[str, Any]] = []
        self._jar: Any = None

    def for_jar(self, jar: Any) -> "_StubOpener":
        """Bind the jar the renewer built, so Set-Cookie has somewhere to land."""
        self._jar = jar
        return self

    def open(self, request, timeout=None):  # noqa: ANN001, D102
        del timeout
        parts = urllib.parse.urlsplit(request.full_url)
        key = (parts.netloc, parts.path)
        self.requests.append(
            {
                "method": request.get_method(),
                "host": parts.netloc,
                "path": parts.path,
                "query": urllib.parse.parse_qs(parts.query),
                "body": request.data,
                "content_type": request.get_header("Content-type"),
            }
        )
        if key not in self._routes:
            raise AssertionError(f"the stub has no route for {key}")
        route = self._routes[key]
        if callable(route):
            response = route(request)
        else:
            status, headers, body = route
            response = _StubResponse(status, headers, body)
        self._absorb(response, parts.netloc)
        return response

    def _absorb(self, response: "_StubResponse", host: str) -> None:
        """Feed the response's Set-Cookie headers into the bound jar."""
        if self._jar is None:
            return
        raw = response.headers.get("Set-Cookie")
        if not raw:
            return
        parsed = http.cookies.SimpleCookie()
        parsed.load(raw)
        for morsel in parsed.values():
            domain = morsel["domain"] or host
            # ``Max-Age`` / ``Expires`` must be honoured, not discarded. The real
            # server sends a ~3600 s cookie, and a stub that always produced a
            # session cookie would make the expiry path untestable -- which is how
            # the expiry bug below stayed invisible.
            expires: int | None = None
            if morsel["max-age"]:
                try:
                    expires = int(time.time()) + int(morsel["max-age"])
                except (TypeError, ValueError):
                    expires = None
            elif morsel["expires"]:
                try:
                    expires = int(
                        email.utils.mktime_tz(email.utils.parsedate_tz(morsel["expires"]))
                    )
                except (TypeError, ValueError):
                    expires = None
            self._jar.set_cookie(
                http.cookiejar.Cookie(
                    version=0,
                    name=morsel.key,
                    value=morsel.value,
                    port=None,
                    port_specified=False,
                    domain=domain,
                    domain_specified=bool(morsel["domain"]),
                    domain_initial_dot=domain.startswith("."),
                    path=morsel["path"] or "/",
                    path_specified=bool(morsel["path"]),
                    secure=bool(morsel["secure"]),
                    expires=expires,
                    discard=expires is None,
                    comment=None,
                    comment_url=None,
                    rest={},
                    rfc2109=False,
                )
            )


class _StubResponse:
    """The subset of ``http.client.HTTPResponse`` the renewer reads."""

    def __init__(self, status: int, headers: Mapping[str, str], body: bytes) -> None:
        self.status = status
        self.headers = dict(headers)
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_StubResponse":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


AUTHORIZE_URL = (
    "https://gitcode.com/oauth/authorize?client_id=CID&state=ST&"
    "redirect_uri=" + urllib.parse.quote("https://opencsitool.com/cb") + "&response_type=code"
)
CALLBACK_URL = "https://opencsitool.com/opencsitool/rest/v1/oauth2/authorization/callback/gitcode?code=CODE&state=ST"


def _install(renewer: HttpOAuthRenewer, opener: _StubOpener) -> _StubOpener:
    """Point the renewer at the stub, keeping the real flow code under test.

    ``for_jar`` is called with the jar the renewer constructs at call time, so
    the stub can honour ``Set-Cookie`` exactly as ``HTTPCookieProcessor`` would.
    """
    renewer._opener = lambda jar: opener.for_jar(jar)  # noqa: SLF001 - the seam under test
    return opener


def _set_cookie(name: str, value: str, domain: str = "opencsitool.com") -> str:
    return f"{name}={value}; Path=/; Domain={domain}; HttpOnly; Secure"


class FlowShapeTest(unittest.TestCase):
    """The three-request sequence, pinned step by step."""

    def _renewer(self, routes, *, cookies=None, token=None):
        provider = _StubProvider(cookies=gitcode_cookies() if cookies is None else cookies, token=token)
        renewer = HttpOAuthRenewer(provider, use_proxy=False)
        _install(renewer, _StubOpener(routes))
        return renewer, provider

    def test_the_whole_flow_renews_and_stores_the_session(self) -> None:
        routes = {
            ("opencsitool.com", OAUTH_ENTRY_PATH): (302, {"Location": AUTHORIZE_URL}, b""),
            ("web-api.gitcode.com", CHECK_AUTHORIZE_PATH): (
                200,
                {},
                json.dumps({"redirect_uri": CALLBACK_URL, "reauth_required": None}).encode(),
            ),
            ("opencsitool.com", "/opencsitool/rest/v1/oauth2/authorization/callback/gitcode"): (
                302,
                {"Set-Cookie": _set_cookie("token", FAKE_OPENCSITOOL_TOKEN)},
                b"",
            ),
        }
        renewer, provider = self._renewer(routes)
        result = renewer.renew()

        self.assertIs(result.status, RenewalStatus.RENEWED)
        self.assertTrue(result.token_changed)
        self.assertTrue(result.renewed)
        self.assertEqual(provider.remembered, [FAKE_OPENCSITOOL_TOKEN])

    def test_the_callback_is_what_mints_the_cookie(self) -> None:
        """Not the POST. Pinned because it is the step a reimplementation drops.

        ``checkOrAuthorize`` returns the *code*; only requesting the callback
        turns that code into a session. A flow that stops after the POST looks
        successful and establishes nothing.
        """
        routes = {
            ("opencsitool.com", OAUTH_ENTRY_PATH): (302, {"Location": AUTHORIZE_URL}, b""),
            ("web-api.gitcode.com", CHECK_AUTHORIZE_PATH): (
                200,
                {},
                json.dumps({"redirect_uri": CALLBACK_URL}).encode(),
            ),
            ("opencsitool.com", "/opencsitool/rest/v1/oauth2/authorization/callback/gitcode"): (
                302,
                {"Set-Cookie": _set_cookie("token", FAKE_OPENCSITOOL_TOKEN)},
                b"",
            ),
        }
        renewer, _provider = self._renewer(routes)
        renewer.renew()
        trace = renewer.last_trace
        self.assertIsNotNone(trace)
        self.assertTrue(trace.authorization_granted)
        self.assertTrue(trace.token_minted)
        self.assertEqual(trace.callback_status, 302)

    def test_the_post_is_multipart_with_the_four_form_fields(self) -> None:
        """The shape is load-bearing: the same call 401s on a body it dislikes.

        Sending JSON here would look like "the session was rejected", which is
        exactly the false negative that made the flow appear browser-bound.
        """
        captured: dict[str, Any] = {}

        def check(request):  # noqa: ANN001
            captured["body"] = request.data
            captured["content_type"] = request.get_header("Content-type")
            return _StubResponse(200, {}, json.dumps({"redirect_uri": CALLBACK_URL}).encode())

        routes = {
            ("opencsitool.com", OAUTH_ENTRY_PATH): (302, {"Location": AUTHORIZE_URL}, b""),
            ("web-api.gitcode.com", CHECK_AUTHORIZE_PATH): check,
            ("opencsitool.com", "/opencsitool/rest/v1/oauth2/authorization/callback/gitcode"): (
                302,
                {"Set-Cookie": _set_cookie("token", FAKE_OPENCSITOOL_TOKEN)},
                b"",
            ),
        }
        renewer, _provider = self._renewer(routes)
        renewer.renew()

        self.assertIn("multipart/form-data", captured["content_type"])
        body = captured["body"].decode()
        for field in ("client_id", "state", "redirect_uri", "response_type"):
            with self.subTest(field=field):
                self.assertIn(f'name="{field}"', body)
        self.assertIn("code", body)  # response_type=code

    def test_the_check_endpoint_is_the_uc_prefixed_path(self) -> None:
        """``/uc`` is prepended by an interceptor; the unprefixed path 401s."""
        routes = {
            ("opencsitool.com", OAUTH_ENTRY_PATH): (302, {"Location": AUTHORIZE_URL}, b""),
            ("web-api.gitcode.com", CHECK_AUTHORIZE_PATH): (
                200,
                {},
                json.dumps({"redirect_uri": CALLBACK_URL}).encode(),
            ),
            ("opencsitool.com", "/opencsitool/rest/v1/oauth2/authorization/callback/gitcode"): (
                302,
                {"Set-Cookie": _set_cookie("token", FAKE_OPENCSITOOL_TOKEN)},
                b"",
            ),
        }
        renewer, _provider = self._renewer(routes)
        renewer.renew()
        paths = [r["path"] for r in renewer._opener(None).requests]  # noqa: SLF001
        self.assertIn("/uc/api/v1/oauth/checkOrAuthorize", paths)


class RefusalTest(unittest.TestCase):
    """What the renewer refuses to do, and what it refuses to claim."""

    def _renewer(self, routes, *, cookies=None, token=None):
        provider = _StubProvider(cookies=gitcode_cookies() if cookies is None else cookies, token=token)
        renewer = HttpOAuthRenewer(provider, use_proxy=False)
        _install(renewer, _StubOpener(routes))
        return renewer, provider

    def test_no_gitcode_session_is_login_required(self) -> None:
        renewer, _provider = self._renewer({}, cookies=[])
        result = renewer.renew()
        self.assertIs(result.status, RenewalStatus.LOGIN_REQUIRED)
        self.assertTrue(result.requires_interaction)
        self.assertFalse(result.ok)

    def test_an_unapproved_grant_is_consent_required_not_failure(self) -> None:
        """A 200 with no code means "a human must approve", not "it broke".

        This is the one case the browser renewer exists to cover, and reporting it
        as a generic failure would send the user looking for a bug instead of
        clicking the approval page.
        """
        routes = {
            ("opencsitool.com", OAUTH_ENTRY_PATH): (302, {"Location": AUTHORIZE_URL}, b""),
            ("web-api.gitcode.com", CHECK_AUTHORIZE_PATH): (
                200,
                {},
                json.dumps({"redirect_uri": None, "reauth_required": None}).encode(),
            ),
            ("web-api.gitcode.com", "/uc/api/v1/user/oauth/userInfo"): (200, {}, b"{}"),
        }
        renewer, _provider = self._renewer(routes)
        result = renewer.renew()
        self.assertIs(result.status, RenewalStatus.CONSENT_REQUIRED)
        self.assertTrue(result.requires_interaction)
        self.assertFalse(result.ok)

    def test_the_consent_endpoint_is_never_called(self) -> None:
        """Approving a third-party grant is the user's decision, not this tool's.

        Asserted against the *requests actually made*, not against the source: a
        comment promising restraint is not evidence of it.
        """
        routes = {
            ("opencsitool.com", OAUTH_ENTRY_PATH): (302, {"Location": AUTHORIZE_URL}, b""),
            ("web-api.gitcode.com", CHECK_AUTHORIZE_PATH): (
                200,
                {},
                json.dumps({"redirect_uri": None}).encode(),
            ),
            ("web-api.gitcode.com", "/uc/api/v1/user/oauth/userInfo"): (200, {}, b"{}"),
        }
        renewer, _provider = self._renewer(routes)
        renewer.renew()
        for request in renewer._opener(None).requests:  # noqa: SLF001
            with self.subTest(path=request["path"]):
                self.assertNotEqual(request["path"], "/uc/api/v1/oauth/authorize")

    def test_an_expired_sso_session_is_told_apart_from_a_missing_grant(self) -> None:
        """Both answer 401-ish; the remedies are completely different."""
        routes = {
            ("opencsitool.com", OAUTH_ENTRY_PATH): (302, {"Location": AUTHORIZE_URL}, b""),
            ("web-api.gitcode.com", CHECK_AUTHORIZE_PATH): (401, {}, b'{"error_code":"X"}'),
            ("web-api.gitcode.com", "/uc/api/v1/user/oauth/userInfo"): (401, {}, b"{}"),
        }
        renewer, _provider = self._renewer(routes)
        result = renewer.renew()
        self.assertIs(result.status, RenewalStatus.LOGIN_REQUIRED)
        self.assertIs(renewer.last_trace.gitcode_session_valid, False)

    def test_a_rejected_authorization_with_a_live_session_is_not_a_signin_prompt(
        self,
    ) -> None:
        routes = {
            ("opencsitool.com", OAUTH_ENTRY_PATH): (302, {"Location": AUTHORIZE_URL}, b""),
            ("web-api.gitcode.com", CHECK_AUTHORIZE_PATH): (403, {}, b"{}"),
            ("web-api.gitcode.com", "/uc/api/v1/user/oauth/userInfo"): (200, {}, b"{}"),
        }
        renewer, _provider = self._renewer(routes)
        result = renewer.renew()
        self.assertIs(result.status, RenewalStatus.OAUTH_FAILED)
        self.assertIs(renewer.last_trace.gitcode_session_valid, True)
        self.assertNotIn("sign in", (result.detail or "").lower())

    def test_an_entry_point_that_does_not_redirect_is_reported(self) -> None:
        renewer, _provider = self._renewer(
            {("opencsitool.com", OAUTH_ENTRY_PATH): (500, {}, b"")}
        )
        result = renewer.renew()
        self.assertIs(result.status, RenewalStatus.OAUTH_FAILED)
        self.assertIn("500", result.detail or "")

    def test_a_missing_authorize_parameter_is_named(self) -> None:
        incomplete = "https://gitcode.com/oauth/authorize?client_id=CID&state=ST"
        renewer, _provider = self._renewer(
            {("opencsitool.com", OAUTH_ENTRY_PATH): (302, {"Location": incomplete}, b"")}
        )
        result = renewer.renew()
        self.assertIs(result.status, RenewalStatus.OAUTH_FAILED)
        self.assertIn("redirect_uri", result.detail or "")

    def test_no_token_from_the_callback_is_a_failure_not_a_success(self) -> None:
        routes = {
            ("opencsitool.com", OAUTH_ENTRY_PATH): (302, {"Location": AUTHORIZE_URL}, b""),
            ("web-api.gitcode.com", CHECK_AUTHORIZE_PATH): (
                200,
                {},
                json.dumps({"redirect_uri": CALLBACK_URL}).encode(),
            ),
            ("opencsitool.com", "/opencsitool/rest/v1/oauth2/authorization/callback/gitcode"): (
                200,
                {},
                b"",
            ),
        }
        renewer, provider = self._renewer(routes)
        result = renewer.renew()
        self.assertIs(result.status, RenewalStatus.OAUTH_FAILED)
        self.assertFalse(result.ok)
        self.assertEqual(provider.remembered, [])


class SecretSafetyTest(unittest.TestCase):
    """The renewer handles live credentials, so its outputs must be safe to log."""

    def test_the_trace_reduces_urls_to_host_and_path(self) -> None:
        """A code and a state travel in query strings. Neither may be printed."""
        trace = OAuthTrace(
            entry_host_path="gitcode.com/oauth/authorize",
            callback_host_path="opencsitool.com/opencsitool/rest/v1/oauth2/authorization/callback/gitcode",
        )
        rendered = str(trace.as_dict())
        self.assertNotIn("code=", rendered)
        self.assertNotIn("state=", rendered)
        self.assertNotIn("?", rendered)

    def test_a_result_carries_no_token(self) -> None:
        result = RenewalResult(RenewalStatus.RENEWED, token_changed=True)
        self.assertNotIn(FAKE_OPENCSITOOL_TOKEN, repr(result))
        self.assertNotIn(FAKE_OPENCSITOOL_TOKEN, str(result.as_dict()))

    def test_the_gitcode_cookie_names_are_the_measured_ones(self) -> None:
        self.assertEqual(
            set(GITCODE_SESSION_COOKIES),
            {"GITCODE_ACCESS_TOKEN", "GITCODE_REFRESH_TOKEN", "GitCodeUserName"},
        )


class RenewalSurvivesInvalidationTest(unittest.TestCase):
    """The new session must still be there after ``SessionManager`` is done.

    Two bugs lived here, in different halves of the same hand-off, and both made
    a *successful* browserless renewal report "the new cookie was rejected":

    1. ``SessionManager.renew`` called ``invalidate()`` unconditionally on
       success. For a browser-driven renewal that is right -- the cookie went
       into the browser, so the cache is stale. For a browserless one it destroys
       the only copy that exists, because the browser never learned the cookie.
    2. ``opencsi login --renew`` built a *fresh* provider to verify with, which
       re-read the browser and found nothing.

    Neither was caught by the source run, which happened to reuse the provider;
    running the frozen binary is what exposed them. Both are pinned here.
    """

    def _provider(self):
        from opencsi.auth.cdp import CdpCookieProvider

        provider = CdpCookieProvider("http://127.0.0.1:9222", discover=False)
        provider.remember_token(FAKE_OPENCSITOOL_TOKEN, expires_in=3599)
        return provider

    def test_a_remembered_token_is_not_discarded_by_a_successful_renewal(
        self,
    ) -> None:
        """Bug 1. The provider already holds the new value, so keep it."""
        from opencsi.auth.session import RenewalResult, RenewalStatus, SessionManager

        provider = self._provider()
        self.assertTrue(provider.holds_remembered_token())
        self.assertIsNotNone(provider.peek_token())

        class _Renewer:
            name = "stub-http"

            def can_renew(self):
                return True

            def renew(self, *, timeout=None, before=None):
                return RenewalResult(RenewalStatus.RENEWED, renewed=True)

        session = SessionManager(provider, renewer=_Renewer())
        result = session.renew(force=True)

        self.assertTrue(result.renewed)
        self.assertIsNotNone(
            provider.peek_token(),
            "the renewed token was thrown away, so the next read goes to the "
            "browser -- which never learned it -- and reports a rejection",
        )

    def test_a_browser_read_token_is_still_discarded(self) -> None:
        """The original behaviour must survive the fix.

        A browser-driven renewal writes into the browser, so the cached value is
        stale and the next read *must* go and fetch the new one.
        """
        from opencsi.auth.cdp import CdpCookieProvider
        from opencsi.auth.session import RenewalResult, RenewalStatus, SessionManager

        provider = CdpCookieProvider("http://127.0.0.1:9222", discover=False)
        # Not remembered: this is what a browser read looks like.
        provider._token = "OLD" * 40
        provider._read_at = 0.0
        self.assertFalse(provider.holds_remembered_token())

        class _Renewer:
            name = "stub-browser"

            def can_renew(self):
                return True

            def renew(self, *, timeout=None, before=None):
                return RenewalResult(RenewalStatus.RENEWED, renewed=True)

        session = SessionManager(provider, renewer=_Renewer())
        session.renew(force=True)

        self.assertIsNone(
            provider.peek_token(),
            "a browser-driven renewal must force the next read back to the browser",
        )

    def test_a_provider_that_cannot_answer_falls_back_safely(self) -> None:
        """Providers without the exact signal must not crash the renewal.

        ``_StubProvider`` deliberately has no ``holds_remembered_token``, so this
        exercises the introspection fallback rather than mocking it.
        """
        from opencsi.auth.session import RenewalResult, RenewalStatus, SessionManager

        provider = _StubProvider(token=FAKE_OPENCSITOOL_TOKEN)
        self.assertFalse(hasattr(provider, "holds_remembered_token"))

        class _Renewer:
            name = "stub-http"

            def can_renew(self):
                return True

            def renew(self, *, timeout=None, before=None):
                return RenewalResult(RenewalStatus.RENEWED, renewed=True)

        session = SessionManager(provider, renewer=_Renewer())
        result = session.renew(force=True)

        # The renewal is still reported honestly; only the cache decision differs.
        self.assertTrue(result.renewed)
        self.assertIsNotNone(provider.get_token())

    def test_the_verification_step_reuses_the_provider(self) -> None:
        """Bug 2, asserted at the source: the verify call must not re-read.

        A fresh provider would go to the browser. This pins the *shape* of the
        fix rather than its effect, because the effect needs a live browser --
        and the shape is what a future edit would break.
        """
        import inspect

        from opencsi.cli import login as login_module

        source = inspect.getsource(login_module._renew)
        self.assertIn(
            "provider=provider",
            source,
            "the verify client must reuse the renewing provider; building a new "
            "one re-reads the browser and rejects a working browserless renewal",
        )
        self.assertNotIn(
            "provider=ctx.make_provider()",
            source,
            "a fresh provider discards the token the renewer just installed",
        )


class BrowserPersistenceTest(unittest.TestCase):
    """A renewed session must outlive the process that renewed it.

    The browser is this project's credential store -- a cookie is never written
    to disk, which is what makes ``opencsi usage`` work as a separate process.
    A browserless renewal mints the session over HTTP, so the browser never
    learns the cookie unless something puts it there. Without that step
    ``opencsi login --renew`` succeeds and the next ``opencsi usage`` fails: a
    partial success reported as a complete one.
    """

    def _renewer(self, provider):
        renewer = HttpOAuthRenewer(provider, use_proxy=False)
        _install(
            renewer,
            _StubOpener(
                {
                    ("opencsitool.com", OAUTH_ENTRY_PATH): (302, {"Location": AUTHORIZE_URL}, b""),
                    ("web-api.gitcode.com", CHECK_AUTHORIZE_PATH): (
                        200,
                        {},
                        json.dumps({"redirect_uri": CALLBACK_URL}).encode(),
                    ),
                    (
                        "opencsitool.com",
                        "/opencsitool/rest/v1/oauth2/authorization/callback/gitcode",
                    ): (
                        302,
                        {"Set-Cookie": _set_cookie("token", FAKE_OPENCSITOOL_TOKEN)},
                        b"",
                    ),
                }
            ),
        )
        return renewer

    def test_the_minted_cookie_is_written_into_the_browser(self) -> None:
        """The property that makes the session survive process exit."""
        provider = _StubProvider(cookies=gitcode_cookies())
        installed: list[tuple[str, object]] = []

        def install_token(token, *, expires_in=None):
            installed.append((token, expires_in))
            return True

        provider.install_token = install_token
        renewer = self._renewer(provider)
        result = renewer.renew()

        self.assertIs(result.status, RenewalStatus.RENEWED)
        self.assertEqual(
            [t for t, _ in installed],
            [FAKE_OPENCSITOOL_TOKEN],
            "the renewed cookie never reached the browser, so it dies with this process",
        )
        self.assertIs(renewer.last_persisted, True)

    def test_the_expiry_is_passed_as_a_relative_lifetime(self) -> None:
        """CDP wants an absolute epoch; the provider does the conversion.

        Asserted at this boundary because passing the absolute value through here
        would set an expiry in 1970 and the cookie would be silently dropped --
        which looks exactly like a rejected write.

        The route sets ``Max-Age`` because the real callback does (a ~3600 s
        cookie is what the server sends). A cookie with no expiry is a session
        cookie, and ``None`` is the correct lifetime for it -- which is why the
        other tests in this class do not assert a number here.
        """
        provider = _StubProvider(cookies=gitcode_cookies())
        seen: list[object] = []

        def install_token(token, *, expires_in=None):
            seen.append(expires_in)
            return True

        provider.install_token = install_token
        renewer = HttpOAuthRenewer(provider, use_proxy=False)
        _install(
            renewer,
            _StubOpener(
                {
                    ("opencsitool.com", OAUTH_ENTRY_PATH): (302, {"Location": AUTHORIZE_URL}, b""),
                    ("web-api.gitcode.com", CHECK_AUTHORIZE_PATH): (
                        200,
                        {},
                        json.dumps({"redirect_uri": CALLBACK_URL}).encode(),
                    ),
                    (
                        "opencsitool.com",
                        "/opencsitool/rest/v1/oauth2/authorization/callback/gitcode",
                    ): (
                        302,
                        {
                            "Set-Cookie": (
                                f"token={FAKE_OPENCSITOOL_TOKEN}; Path=/; "
                                "Domain=opencsitool.com; HttpOnly; Secure; Max-Age=3599"
                            )
                        },
                        b"",
                    ),
                }
            ),
        )
        renewer.renew()

        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0], (int, float))
        self.assertGreater(seen[0], 0)
        # A relative lifetime, not a Unix timestamp.
        self.assertLess(seen[0], 86400 * 2, "this looks like an absolute epoch, not a lifetime")
        self.assertAlmostEqual(float(seen[0]), 3599, delta=5)

    def test_a_session_cookie_without_an_expiry_passes_none(self) -> None:
        """A session cookie must stay a session cookie.

        Inventing a lifetime for it would extend a credential past the point the
        server intended, which is a security change made silently.
        """
        provider = _StubProvider(cookies=gitcode_cookies())
        seen: list[object] = []

        def install_token(token, *, expires_in=None):
            seen.append(expires_in)
            return True

        provider.install_token = install_token
        self._renewer(provider).renew()

        self.assertEqual(seen, [None])

    def test_a_failed_browser_write_does_not_fail_the_renewal(self) -> None:
        """The renewal really did succeed; only its persistence failed.

        Reporting the whole thing as a failure would hide a working renewal
        behind an error about a separate, recoverable step.
        """
        provider = _StubProvider(cookies=gitcode_cookies())
        provider.install_token = lambda token, **kw: False
        renewer = self._renewer(provider)
        result = renewer.renew()

        self.assertIs(result.status, RenewalStatus.RENEWED)
        self.assertIs(renewer.last_persisted, False)
        # Still cached in-process, so the current run keeps working.
        self.assertEqual(provider.remembered, [FAKE_OPENCSITOOL_TOKEN])

    def test_a_raising_browser_write_does_not_escape(self) -> None:
        provider = _StubProvider(cookies=gitcode_cookies())

        def install_token(token, **kw):
            raise RuntimeError("CDP blew up")

        provider.install_token = install_token
        renewer = self._renewer(provider)
        result = renewer.renew()

        self.assertIs(result.status, RenewalStatus.RENEWED)
        self.assertIs(renewer.last_persisted, False)

    def test_a_provider_that_cannot_write_reports_none_not_false(self) -> None:
        """``None`` and ``False`` are different facts.

        ``None`` means "this provider has no way to write one" -- a manual
        provider, or a stub. ``False`` means a write was attempted and failed.
        Collapsing them would make "not applicable" read as "broken".
        """
        provider = _StubProvider(cookies=gitcode_cookies())
        self.assertFalse(hasattr(provider, "install_token"))
        renewer = self._renewer(provider)
        result = renewer.renew()

        self.assertIs(result.status, RenewalStatus.RENEWED)
        self.assertIsNone(renewer.last_persisted)


class _FakeRenewer:
    """A renewer with a scripted answer, for chain tests."""

    def __init__(self, name: str, status: RenewalStatus, *, can: bool = True) -> None:
        self.name = name
        self._status = status
        self._can = can
        self.calls = 0

    def can_renew(self) -> bool:
        return self._can

    def renew(self, *, timeout=None, before=None) -> RenewalResult:  # noqa: ANN001
        del timeout, before
        self.calls += 1
        return RenewalResult(self._status, renewed=self._status is RenewalStatus.RENEWED)

    def describe(self) -> str:
        return self.name


class FallbackChainTest(unittest.TestCase):
    """The ordering policy, tested without a browser or a network."""

    def test_the_first_success_wins_and_the_rest_are_not_tried(self) -> None:
        first = _FakeRenewer("http-oauth", RenewalStatus.RENEWED)
        second = _FakeRenewer("browser-oauth", RenewalStatus.RENEWED)
        chain = FallbackRenewer([first, second])
        result = chain.renew()
        self.assertTrue(result.ok)
        self.assertEqual(second.calls, 0, "a success must stop the chain")
        self.assertEqual(chain.last_mechanism, "http-oauth")

    def test_an_unavailable_mechanism_falls_through(self) -> None:
        first = _FakeRenewer("http-oauth", RenewalStatus.RENEWED, can=False)
        second = _FakeRenewer("browser-oauth", RenewalStatus.RENEWED)
        chain = FallbackRenewer([first, second])
        result = chain.renew()
        self.assertTrue(result.ok)
        self.assertEqual(first.calls, 0)
        self.assertEqual(second.calls, 1)
        self.assertEqual(chain.last_mechanism, "browser-oauth")

    def test_a_failed_mechanism_falls_through(self) -> None:
        first = _FakeRenewer("http-oauth", RenewalStatus.OAUTH_FAILED)
        second = _FakeRenewer("browser-oauth", RenewalStatus.RENEWED)
        chain = FallbackRenewer([first, second])
        self.assertTrue(chain.renew().ok)
        self.assertEqual(second.calls, 1)

    def test_consent_required_stops_the_chain(self) -> None:
        """The important negative.

        A consent requirement is not something the browser renewer can fix by
        being tried, and falling through would delay a message the user needs.
        """
        first = _FakeRenewer("http-oauth", RenewalStatus.CONSENT_REQUIRED)
        second = _FakeRenewer("browser-oauth", RenewalStatus.RENEWED)
        chain = FallbackRenewer([first, second])
        result = chain.renew()
        self.assertIs(result.status, RenewalStatus.CONSENT_REQUIRED)
        self.assertEqual(second.calls, 0)

    def test_login_required_stops_the_chain(self) -> None:
        """If the SSO session is gone, a second reader of it cannot help."""
        first = _FakeRenewer("http-oauth", RenewalStatus.LOGIN_REQUIRED)
        second = _FakeRenewer("browser-oauth", RenewalStatus.RENEWED)
        chain = FallbackRenewer([first, second])
        result = chain.renew()
        self.assertIs(result.status, RenewalStatus.LOGIN_REQUIRED)
        self.assertEqual(second.calls, 0)

    def test_the_report_names_every_mechanism_that_was_tried(self) -> None:
        """A user seeing "renewal failed" needs to know what was attempted."""
        first = _FakeRenewer("http-oauth", RenewalStatus.OAUTH_FAILED)
        second = _FakeRenewer("browser-oauth", RenewalStatus.OAUTH_FAILED)
        chain = FallbackRenewer([first, second])
        result = chain.renew()
        self.assertFalse(result.ok)
        self.assertIn("http-oauth", result.detail or "")
        self.assertIn("browser-oauth", result.detail or "")

    def test_a_raising_mechanism_does_not_abort_the_chain(self) -> None:
        """One mechanism crashing must not deny the next its chance."""

        class Exploding(_FakeRenewer):
            def renew(self, *, timeout=None, before=None):  # noqa: ANN001
                raise RuntimeError("boom")

        chain = FallbackRenewer([Exploding("http-oauth", RenewalStatus.RENEWED), _FakeRenewer("browser-oauth", RenewalStatus.RENEWED)])
        self.assertTrue(chain.renew().ok)

    def test_a_raising_capability_probe_is_treated_as_unavailable(self) -> None:
        """A probe that throws must not abort the chain for a mechanism that works."""

        class BadProbe(_FakeRenewer):
            def can_renew(self) -> bool:
                raise RuntimeError("probe exploded")

        second = _FakeRenewer("browser-oauth", RenewalStatus.RENEWED)
        chain = FallbackRenewer([BadProbe("http-oauth", RenewalStatus.RENEWED), second])
        self.assertTrue(chain.renew().ok)
        self.assertEqual(second.calls, 1)

    def test_an_empty_chain_is_unsupported_not_ok(self) -> None:
        result = FallbackRenewer([]).renew()
        self.assertIs(result.status, RenewalStatus.UNSUPPORTED)
        self.assertFalse(result.ok)

    def test_can_renew_is_true_when_any_member_can(self) -> None:
        chain = FallbackRenewer(
            [
                _FakeRenewer("a", RenewalStatus.RENEWED, can=False),
                _FakeRenewer("b", RenewalStatus.RENEWED, can=True),
            ]
        )
        self.assertTrue(chain.can_renew())

    def test_describe_lists_the_chain_in_order(self) -> None:
        chain = FallbackRenewer(
            [_FakeRenewer("http-oauth", RenewalStatus.RENEWED), _FakeRenewer("browser-oauth", RenewalStatus.RENEWED)]
        )
        self.assertIn("http-oauth, browser-oauth", chain.describe())


if __name__ == "__main__":
    unittest.main()
