"""The GitCode refresh-token grant: classification, rotation, and persistence.

The exchange itself is tested against a local HTTP server rather than the live
service, so the whole parse-and-classify path runs on every CI runner with no
credential and no network. What cannot be tested offline -- that GitCode actually
honours the grant -- is measured by ``tools/probe_gitcode_refresh.py``, whose
findings the module records.

The tests that matter most are the *failure* classifications. Collapsing every
error into ``LOGIN_REQUIRED`` is the tempting shortcut and the expensive one: it
turns a wifi dropout into "scan the QR code again", and it turns a changed
response shape into a user re-authenticating forever without ever fixing the bug.
"""

from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import helpers  # noqa: F401  (imported for its sys.path side effect)

from opencsi.auth.gitcode_refresh import (
    GITCODE_TOKEN_URL,
    GitCodeTokenRefresher,
    RefreshStatus,
    StoredGitCodeRefresher,
    _classify_http_error,
)
from opencsi.auth.session import RenewalResult, RenewalStatus
from opencsi.auth.store import (
    CredentialBundle,
    MemoryCredentialStore,
    StoredGitCodeCredential,
)

ACCESS = "gitcode-access-token-abcdefghijklmnop"
REFRESH = "gitcode-refresh-token-abcdefghijklmnop"
NEW_ACCESS = "gitcode-access-token-zyxwvutsrqponmlk"
NEW_REFRESH = "gitcode-refresh-token-zyxwvutsrqponmlk"


def a_credential(**overrides) -> StoredGitCodeCredential:
    base = {
        "access_token": ACCESS,
        "refresh_token": REFRESH,
        "username": "alice",
        "access_expires_at": time.time() + 60,
    }
    base.update(overrides)
    return StoredGitCodeCredential(**base)


class _Handler(BaseHTTPRequestHandler):
    """A stub token endpoint. Records the last request it was asked."""

    #: Set per test: ``(status, body)`` to answer with.
    response: tuple[int, str] = (200, "{}")
    last_query: str = ""
    last_headers: dict[str, str] = {}
    last_body: bytes = b""
    requests: int = 0

    def do_POST(self) -> None:  # noqa: N802 - the stdlib names it this
        type(self).requests += 1
        type(self).last_query = self.path
        type(self).last_headers = {
            key.lower(): value for key, value in self.headers.items()
        }
        length = int(self.headers.get("Content-Length") or 0)
        type(self).last_body = self.rfile.read(length) if length else b""
        status, body = type(self).response
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *args) -> None:  # noqa: ANN002 - silence the stub
        return


class StubEndpointMixin:
    """Run a stub GitCode token endpoint on a loopback port."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._server = HTTPServer(("127.0.0.1", 0), _Handler)
        cls._thread = threading.Thread(target=cls._server.serve_forever, daemon=True)
        cls._thread.start()
        cls.url = f"http://127.0.0.1:{cls._server.server_port}/oauth/token"

    @classmethod
    def tearDownClass(cls) -> None:
        cls._server.shutdown()
        cls._server.server_close()

    def refresher(self, **kwargs) -> GitCodeTokenRefresher:
        kwargs.setdefault("timeout", 5.0)
        return GitCodeTokenRefresher(token_url=self.url, **kwargs)


class ConstantsTest(unittest.TestCase):
    """The facts the design rests on, pinned so a change is visible."""

    def test_the_endpoint_is_the_account_token_refresh_path(self) -> None:
        """The QR credential belongs to GitCode's account-token system.

        Three independent lines of evidence, recorded in the module docstring:
        GitCode's own frontend renews via ``POST
        web-api.gitcode.com/uc/api/v1/user/token/refresh`` (current production
        bundle, verbatim URL/headers/body); a no-credential probe of that path
        answers its own ``400004 REFRESH_TOKEN_ERROR`` family; and the frontend
        never calls ``gitcode.com/oauth/token`` anywhere in the bundle. The
        OAuth-application endpoint *did* refuse the QR refresh token with
        ``refresh_token不存在或已过期`` -- the same answer a fabricated token
        gets -- which is what "wrong credential system" looks like.
        """
        self.assertEqual(
            GITCODE_TOKEN_URL,
            "https://web-api.gitcode.com/uc/api/v1/user/token/refresh",
        )
        self.assertIn("web-api.gitcode.com", GITCODE_TOKEN_URL)
        self.assertIn("/uc/api/v1/user/token/refresh", GITCODE_TOKEN_URL)


class NeedsRefreshTest(unittest.TestCase):
    """When to spend a request."""

    def setUp(self) -> None:
        self.refresher = GitCodeTokenRefresher()

    def test_a_token_with_weeks_left_is_left_alone(self) -> None:
        credential = a_credential(access_expires_at=time.time() + 1296000)
        self.assertFalse(self.refresher.needs_refresh(credential))

    def test_a_token_inside_the_margin_is_refreshed(self) -> None:
        credential = a_credential(access_expires_at=time.time() + 60)
        self.assertTrue(self.refresher.needs_refresh(credential))

    def test_an_expired_token_is_refreshed(self) -> None:
        credential = a_credential(access_expires_at=time.time() - 10)
        self.assertTrue(self.refresher.needs_refresh(credential))

    def test_an_unknown_expiry_is_not_refreshed_proactively(self) -> None:
        """Unknown expiry waits for the demand-driven retry instead.

        The QR response carries no ``expires_in``, so "unknown" is the normal
        state of a QR credential, and a proactive refresh would spend one
        request per renewal cycle against a credential whose state the answer
        does not report. GitCode's own frontend is demand-driven -- it refreshes
        when an API call is refused, then retries -- and
        :class:`RefreshingGitCodeRenewer` mirrors that at the moment the refresh
        is actually needed.
        """
        self.assertFalse(self.refresher.needs_refresh(a_credential(access_expires_at=None)))

    def test_the_margin_is_configurable(self) -> None:
        credential = a_credential(access_expires_at=time.time() + 3600)
        self.assertGreater(
            self.refresher.needs_refresh(credential, margin=86400), False
        )
        self.assertTrue(self.refresher.needs_refresh(credential, margin=7200))


class NoRefreshTokenTest(unittest.TestCase):
    def test_a_credential_without_a_refresh_token_needs_a_login(self) -> None:
        result = GitCodeTokenRefresher().refresh(
            StoredGitCodeCredential(access_token=ACCESS)
        )
        self.assertEqual(result.status, RefreshStatus.NO_REFRESH_TOKEN)
        self.assertTrue(result.needs_login)
        self.assertFalse(result.ok)

    def test_not_needed_is_reported_without_a_request(self) -> None:
        """A refresh that is not due must not touch the network.

        Proven by pointing the refresher at a URL that cannot resolve: if the
        expiry check did not short-circuit, this would attempt a connection.
        """
        refresher = GitCodeTokenRefresher(token_url="http://127.0.0.1:1/oauth/token")
        credential = a_credential(access_expires_at=time.time() + 1296000)
        result = refresher.refresh(credential)
        self.assertEqual(result.status, RefreshStatus.NOT_NEEDED)
        self.assertTrue(result.ok)


class FailureClassificationTest(unittest.TestCase):
    """The distinctions that keep a user from re-authenticating for no reason.

    Only the endpoint's *own* credential-refusal answer sends the user to scan:
    401, or 400 carrying ``error_code 400004 REFRESH_TOKEN_ERROR`` (the shape
    the real endpoint answered to a fabricated token). Every other 4xx is a
    response this code does not recognise -- a changed API, a WAF interstitial,
    a proxy -- and re-authenticating does not fix any of those.
    """

    def test_the_endpoint_refusal_code_means_the_refresh_token_is_dead(self) -> None:
        status, detail = _classify_http_error(
            400,
            '{"error_code":400004,"error_code_name":"REFRESH_TOKEN_ERROR",'
            '"error_message":"token校验失败或者refreshToken已失效"}',
        )
        self.assertEqual(status, RefreshStatus.LOGIN_REQUIRED)
        self.assertTrue(detail)

    def test_a_401_means_the_refresh_token_is_dead(self) -> None:
        status, _detail = _classify_http_error(401, '{"error_code":401}')
        self.assertEqual(status, RefreshStatus.LOGIN_REQUIRED)

    def test_an_unrecognised_400_is_a_protocol_error(self) -> None:
        """An unknown 4xx is not evidence about the credential."""
        status, _detail = _classify_http_error(400, '{"error_code":400,"error_message":"?"}')
        self.assertEqual(status, RefreshStatus.PROTOCOL_ERROR)
        self.assertNotEqual(status, RefreshStatus.LOGIN_REQUIRED)

    def test_a_403_is_a_protocol_error(self) -> None:
        status, _detail = _classify_http_error(403, "forbidden")
        self.assertEqual(status, RefreshStatus.PROTOCOL_ERROR)

    def test_a_429_is_a_network_class_failure(self) -> None:
        """Throttling says "try later"; it says nothing about the credential."""
        status, _detail = _classify_http_error(429, "slow down")
        self.assertEqual(status, RefreshStatus.NETWORK_ERROR)
        self.assertNotEqual(status, RefreshStatus.LOGIN_REQUIRED)

    def test_a_418_names_the_waf_and_not_the_credential(self) -> None:
        """CloudWAF's interstitial is an environment answer, not a token answer.

        Measured on 2026-10-24: a header-minimal refresh POST got CloudWAF's
        HTML 418 page while the credential was never evaluated. The remedy is
        Origin/Referer headers; "scan again" would be both wrong and, one
        request later, visibly wrong.
        """
        status, detail = _classify_http_error(418, "<html>CloudWAF</html>")
        self.assertEqual(status, RefreshStatus.PROTOCOL_ERROR)
        self.assertNotEqual(status, RefreshStatus.LOGIN_REQUIRED)
        self.assertIn("WAF", detail)

    def test_a_5xx_does_not_send_the_user_to_sign_in(self) -> None:
        """A server fault is not a credential fault.

        Telling the user to scan again because GitCode returned 503 is wrong, and
        visibly wrong a minute later when the service recovers and their old
        credential would have worked.
        """
        status, _detail = _classify_http_error(503, "upstream unavailable")
        self.assertEqual(status, RefreshStatus.NETWORK_ERROR)
        self.assertNotEqual(status, RefreshStatus.LOGIN_REQUIRED)

    def test_an_unexpected_status_is_a_protocol_error(self) -> None:
        status, _detail = _classify_http_error(302, "")
        self.assertEqual(status, RefreshStatus.PROTOCOL_ERROR)

    def test_an_error_body_is_scrubbed(self) -> None:
        """An error response is one of the few places a token arrives unasked."""
        from opencsi.redaction import scrub_text

        body = f'{{"access_token":"{ACCESS}","error":"nope"}}'
        _status, detail = _classify_http_error(400, body)
        self.assertNotIn(ACCESS, detail)
        self.assertNotIn(ACCESS, scrub_text(detail))


class ExchangeTest(StubEndpointMixin, unittest.TestCase):
    """The real parse path, against a local endpoint."""

    def test_a_successful_refresh_returns_a_replacement_credential(self) -> None:
        _Handler.response = (
            200,
            json.dumps(
                {
                    "access_token": NEW_ACCESS,
                    "refresh_token": NEW_REFRESH,
                    "expires_in": 1296000,
                    "scope": "all_user",
                }
            ),
        )
        result = self.refresher().refresh(a_credential(), force=True)
        self.assertEqual(result.status, RefreshStatus.REFRESHED)
        self.assertTrue(result.ok)
        self.assertEqual(result.credential.access_token, NEW_ACCESS)
        self.assertEqual(result.expires_in, 1296000.0)

    def test_an_enveloped_200_is_accepted(self) -> None:
        """A ``{"data": {...}}`` wrapper must not become a protocol error."""
        _Handler.response = (
            200,
            json.dumps({"data": {"access_token": NEW_ACCESS, "refresh_token": NEW_REFRESH}}),
        )
        result = self.refresher().refresh(a_credential(), force=True)
        self.assertEqual(result.status, RefreshStatus.REFRESHED)
        self.assertEqual(result.credential.access_token, NEW_ACCESS)

    def test_the_request_is_the_frontend_shape(self) -> None:
        """Bearer credential in the header, refresh token in the form body.

        This is the shape GitCode's own frontend sends (current production
        bundle, verbatim): nothing in the query string -- which also keeps the
        refresh token out of any URL a proxy could log.
        """
        _Handler.response = (200, json.dumps({"access_token": NEW_ACCESS}))
        self.refresher().refresh(a_credential(), force=True)

        self.assertNotIn("?", _Handler.last_query, "nothing travels in a query string")
        self.assertEqual(
            _Handler.last_headers.get("authorization"), f"Bearer {ACCESS}"
        )
        self.assertIn(
            "application/x-www-form-urlencoded",
            _Handler.last_headers.get("content-type", ""),
        )
        # CloudWAF answered 418 to the header-minimal first run (2026-10-24);
        # with Origin/Referer the same request reached the API.
        self.assertEqual(_Handler.last_headers.get("origin"), "https://gitcode.com")
        self.assertEqual(_Handler.last_headers.get("referer"), "https://gitcode.com/")
        self.assertEqual(
            _Handler.last_body.decode("ascii"),
            f"refresh_token={urllib.parse.quote(REFRESH)}",
        )

    def test_the_request_does_not_send_a_client_secret(self) -> None:
        """This endpoint authenticates the caller by token, not by app secret.

        Sending a ``client_secret`` this tool does not have would be a guess, and
        a wrong one would look like a rejected credential.
        """
        _Handler.response = (200, json.dumps({"access_token": NEW_ACCESS}))
        self.refresher().refresh(a_credential(), force=True)
        sent = _Handler.last_body.decode("utf-8", "replace")
        self.assertNotIn("client_secret", sent)
        self.assertNotIn("client_id", sent)
        self.assertNotIn("redirect_uri", sent)

    def test_a_rotated_refresh_token_is_reported(self) -> None:
        _Handler.response = (
            200,
            json.dumps({"access_token": NEW_ACCESS, "refresh_token": NEW_REFRESH}),
        )
        result = self.refresher().refresh(a_credential(), force=True)
        self.assertTrue(result.rotated)
        self.assertEqual(result.credential.refresh_token, NEW_REFRESH)

    def test_an_unchanged_refresh_token_is_reported_as_not_rotated(self) -> None:
        _Handler.response = (
            200,
            json.dumps({"access_token": NEW_ACCESS, "refresh_token": REFRESH}),
        )
        result = self.refresher().refresh(a_credential(), force=True)
        self.assertFalse(result.rotated)
        self.assertEqual(result.credential.refresh_token, REFRESH)

    def test_a_response_without_a_refresh_token_keeps_the_stored_one(self) -> None:
        """The stored refresh token remains valid if none is returned.

        Discarding it here would end the credential's life at the first refresh.
        """
        _Handler.response = (200, json.dumps({"access_token": NEW_ACCESS}))
        result = self.refresher().refresh(a_credential(), force=True)
        self.assertFalse(result.rotated)
        self.assertEqual(result.credential.refresh_token, REFRESH)

    def test_a_200_without_an_access_token_is_a_protocol_error(self) -> None:
        """A changed response shape is this tool's bug, not the user's fault.

        Reporting it as LOGIN_REQUIRED would send the user to re-authenticate
        forever without ever fixing the parser.
        """
        _Handler.response = (200, json.dumps({"token": "something else"}))
        result = self.refresher().refresh(a_credential(), force=True)
        self.assertEqual(result.status, RefreshStatus.PROTOCOL_ERROR)
        self.assertFalse(result.needs_login)

    def test_a_non_json_200_is_a_protocol_error(self) -> None:
        _Handler.response = (200, "<html>maintenance</html>")
        result = self.refresher().refresh(a_credential(), force=True)
        self.assertEqual(result.status, RefreshStatus.PROTOCOL_ERROR)

    def test_a_json_array_200_is_a_protocol_error(self) -> None:
        _Handler.response = (200, "[]")
        result = self.refresher().refresh(a_credential(), force=True)
        self.assertEqual(result.status, RefreshStatus.PROTOCOL_ERROR)

    def test_the_endpoint_refusal_is_login_required(self) -> None:
        """The 400004 REFRESH_TOKEN_ERROR shape the real endpoint produces."""
        _Handler.response = (
            400,
            json.dumps(
                {
                    "error_code": 400004,
                    "error_code_name": "REFRESH_TOKEN_ERROR",
                    "error_message": "token校验失败或者refreshToken已失效",
                }
            ),
        )
        result = self.refresher().refresh(a_credential(), force=True)
        self.assertEqual(result.status, RefreshStatus.LOGIN_REQUIRED)
        self.assertTrue(result.needs_login)

    def test_an_unrecognised_400_is_a_protocol_error(self) -> None:
        """The old endpoint's generic BAD_REQUEST is not a credential answer."""
        _Handler.response = (
            400,
            json.dumps({"error_code": 400, "error_message": "refresh_token不存在或已过期"}),
        )
        result = self.refresher().refresh(a_credential(), force=True)
        self.assertEqual(result.status, RefreshStatus.PROTOCOL_ERROR)
        self.assertFalse(result.needs_login)

    def test_a_500_is_a_network_error_not_a_login_requirement(self) -> None:
        _Handler.response = (500, "internal error")
        result = self.refresher().refresh(a_credential(), force=True)
        self.assertEqual(result.status, RefreshStatus.NETWORK_ERROR)
        self.assertFalse(result.needs_login)

    def test_an_unreachable_endpoint_is_a_network_error(self) -> None:
        """Nothing listening: a connection failure, not a dead credential."""
        refresher = GitCodeTokenRefresher(
            token_url="http://127.0.0.1:1/oauth/token", timeout=2.0
        )
        result = refresher.refresh(a_credential(), force=True)
        self.assertEqual(result.status, RefreshStatus.NETWORK_ERROR)
        self.assertFalse(result.needs_login)

    def test_the_status_code_is_recorded_for_diagnostics(self) -> None:
        _Handler.response = (200, json.dumps({"access_token": NEW_ACCESS}))
        refresher = self.refresher()
        refresher.refresh(a_credential(), force=True)
        self.assertEqual(refresher.last_status, 200)

    def test_the_refresher_never_raises(self) -> None:
        """A refresher that throws would abort a caller's recovery sequence."""
        for status in (200, 400, 401, 403, 500, 502, 418):
            _Handler.response = (status, "whatever")
            result = self.refresher().refresh(a_credential(), force=True)
            self.assertIsInstance(result.status, RefreshStatus)

    def test_the_result_repr_has_no_token(self) -> None:
        _Handler.response = (
            200,
            json.dumps({"access_token": NEW_ACCESS, "refresh_token": NEW_REFRESH}),
        )
        result = self.refresher().refresh(a_credential(), force=True)
        self.assertNotIn(NEW_ACCESS, repr(result))
        self.assertNotIn(NEW_REFRESH, repr(result))
        self.assertNotIn(NEW_ACCESS, str(result.as_dict()))

    def test_the_expiry_is_computed_from_expires_in(self) -> None:
        _Handler.response = (
            200,
            json.dumps({"access_token": NEW_ACCESS, "expires_in": 3600}),
        )
        before = time.time()
        result = self.refresher().refresh(a_credential(), force=True)
        after = time.time()
        expires_at = result.credential.access_expires_at
        self.assertIsNotNone(expires_at)
        self.assertGreaterEqual(expires_at, before + 3600)
        self.assertLessEqual(expires_at, after + 3600)

    def test_a_missing_expires_in_leaves_the_expiry_unknown(self) -> None:
        _Handler.response = (200, json.dumps({"access_token": NEW_ACCESS}))
        result = self.refresher().refresh(a_credential(), force=True)
        self.assertIsNone(result.credential.access_expires_at)
        self.assertIsNone(result.expires_in)

    def test_a_boolean_expires_in_is_not_treated_as_a_number(self) -> None:
        """``True`` is an ``int`` in Python, and would compute a 1970 expiry."""
        _Handler.response = (
            200,
            json.dumps({"access_token": NEW_ACCESS, "expires_in": True}),
        )
        result = self.refresher().refresh(a_credential(), force=True)
        self.assertIsNone(result.credential.access_expires_at)


class PersistenceTest(StubEndpointMixin, unittest.TestCase):
    """Writing the replacement back, and what happens when that fails."""

    def test_a_refreshed_credential_replaces_the_stored_one(self) -> None:
        _Handler.response = (
            200,
            json.dumps(
                {"access_token": NEW_ACCESS, "refresh_token": NEW_REFRESH, "expires_in": 3600}
            ),
        )
        store = MemoryCredentialStore(CredentialBundle(gitcode=a_credential()))
        result = StoredGitCodeRefresher(store, refresher=self.refresher()).refresh(force=True)
        self.assertEqual(result.status, RefreshStatus.REFRESHED)
        stored = store.load().gitcode
        self.assertEqual(stored.access_token, NEW_ACCESS)
        self.assertEqual(stored.refresh_token, NEW_REFRESH)

    def test_a_rotated_refresh_token_is_written_back(self) -> None:
        """The failure this prevents appears a fortnight after the cause.

        Keeping the old refresh token after a rotation means the *next* refresh
        fails and the user is sent for a QR scan they did not need.
        """
        _Handler.response = (
            200,
            json.dumps({"access_token": NEW_ACCESS, "refresh_token": NEW_REFRESH}),
        )
        store = MemoryCredentialStore(CredentialBundle(gitcode=a_credential()))
        StoredGitCodeRefresher(store, refresher=self.refresher()).refresh(force=True)
        self.assertEqual(store.load().gitcode.refresh_token, NEW_REFRESH)

    def test_the_username_survives_a_refresh(self) -> None:
        """The token endpoint does not return one, so it must be carried over."""
        _Handler.response = (200, json.dumps({"access_token": NEW_ACCESS}))
        store = MemoryCredentialStore(CredentialBundle(gitcode=a_credential()))
        StoredGitCodeRefresher(store, refresher=self.refresher()).refresh(force=True)
        self.assertEqual(store.load().gitcode.username, "alice")

    def test_a_failed_refresh_does_not_touch_the_store(self) -> None:
        _Handler.response = (400, json.dumps({"error_code": 400004}))
        store = MemoryCredentialStore(CredentialBundle(gitcode=a_credential()))
        before = store.writes
        result = StoredGitCodeRefresher(store, refresher=self.refresher()).refresh(force=True)
        self.assertEqual(result.status, RefreshStatus.LOGIN_REQUIRED)
        self.assertEqual(store.writes, before, "the store was written on a failure")
        self.assertEqual(store.load().gitcode.access_token, ACCESS)

    def test_a_network_failure_does_not_touch_the_store(self) -> None:
        refresher = GitCodeTokenRefresher(
            token_url="http://127.0.0.1:1/oauth/token", timeout=2.0
        )
        store = MemoryCredentialStore(CredentialBundle(gitcode=a_credential()))
        before = store.writes
        result = StoredGitCodeRefresher(store, refresher=refresher).refresh(force=True)
        self.assertEqual(result.status, RefreshStatus.NETWORK_ERROR)
        self.assertEqual(store.writes, before)

    def test_an_empty_store_reports_no_refresh_token(self) -> None:
        store = MemoryCredentialStore()
        result = StoredGitCodeRefresher(store, refresher=self.refresher()).refresh()
        self.assertEqual(result.status, RefreshStatus.NO_REFRESH_TOKEN)

    def test_a_refresh_preserves_the_opencsi_half(self) -> None:
        """Refreshing the upstream credential must not sign the user out.

        The session is the half that keeps the CLI working right now; a GitCode
        refresh is about the next fortnight, and must not cost the present.
        """
        from opencsi.auth.store import StoredOpenCsiCredential

        _Handler.response = (200, json.dumps({"access_token": NEW_ACCESS}))
        store = MemoryCredentialStore(
            CredentialBundle(
                gitcode=a_credential(),
                opencsi=StoredOpenCsiCredential(token="session-token-abcdefghij"),
            )
        )
        StoredGitCodeRefresher(store, refresher=self.refresher()).refresh(force=True)
        self.assertEqual(store.load().opencsi.token, "session-token-abcdefghij")

    def test_a_not_needed_result_is_honoured(self) -> None:
        store = MemoryCredentialStore(
            CredentialBundle(
                gitcode=a_credential(access_expires_at=time.time() + 1296000)
            )
        )
        result = StoredGitCodeRefresher(store, refresher=self.refresher()).refresh()
        self.assertEqual(result.status, RefreshStatus.NOT_NEEDED)
        self.assertEqual(store.writes, 0)

    def test_the_stored_refresher_never_raises(self) -> None:
        store = MemoryCredentialStore(CredentialBundle(gitcode=a_credential()))
        result = StoredGitCodeRefresher(
            store,
            refresher=GitCodeTokenRefresher(
                token_url="http://127.0.0.1:1/oauth/token", timeout=2.0
            ),
        ).refresh(force=True)
        self.assertIsInstance(result.status, RefreshStatus)


class RenewingRenewerTest(StubEndpointMixin, unittest.TestCase):
    """The composed renewer: proactive gate, demand recovery, honest failure.

    ``RefreshingGitCodeRenewer`` is the production path -- the tray renews
    through it -- and until now it had no test of its own. These drive the real
    refresher against the real store and a stub endpoint, with a fake OAuth leg
    whose answer the test scripts.
    """

    def setUp(self) -> None:
        type(self)._server.requests = 0
        _Handler.requests = 0

    def store(self) -> MemoryCredentialStore:
        return MemoryCredentialStore(CredentialBundle(gitcode=a_credential()))

    @staticmethod
    def fake_leg(results: list[RenewalResult]):
        """An OAuth leg that answers from a script and records its calls."""
        calls: list[str] = []

        class _Leg:
            name = "fake-leg"

            def can_renew(self) -> bool:
                return True

            def renew(self, *, timeout=None, before=None) -> RenewalResult:
                calls.append("renew")
                return results.pop(0) if results else RenewalResult(
                    RenewalStatus.RENEWED, renewed=True, token_changed=True
                )

        return _Leg(), calls

    def composed(self, store, leg):
        from opencsi.auth.gitcode_refresh import RefreshingGitCodeRenewer

        return RefreshingGitCodeRenewer(
            StoredGitCodeRefresher(store, refresher=self.refresher()), leg
        )

    def test_a_known_valid_expiry_skips_the_proactive_refresh(self) -> None:
        """A healthy credential costs the renewal zero refresh requests."""
        store = self.store()
        store.load().gitcode.__post_init__  # touch, keeps linters honest
        leg, calls = self.fake_leg([RenewalResult(RenewalStatus.RENEWED, renewed=True)])
        store._bundle = CredentialBundle(
            gitcode=a_credential(access_expires_at=time.time() + 90_000)
        )
        renewer = self.composed(store, leg)
        result = renewer.renew()
        self.assertEqual(result.status, RenewalStatus.RENEWED)
        self.assertEqual(_Handler.requests, 0, "a refresh fired for a valid token")
        self.assertEqual(calls, ["renew"])

    def test_an_unknown_expiry_skips_the_proactive_refresh(self) -> None:
        """The QR credential's normal state makes no proactive request."""
        store = self.store()
        store._bundle = CredentialBundle(gitcode=a_credential(access_expires_at=None))
        leg, _ = self.fake_leg([RenewalResult(RenewalStatus.RENEWED, renewed=True)])
        self.composed(store, leg).renew()
        self.assertEqual(_Handler.requests, 0)

    def test_a_dead_session_triggers_one_recovery_refresh_and_a_retry(self) -> None:
        """The demand-driven path: leg fails, refresh runs, leg is retried.

        This is the recovery the L2 goal rests on: the GitCode access token has
        died, the refresh token is still good, and nobody scans anything.
        """
        store = self.store()
        store._bundle = CredentialBundle(gitcode=a_credential(access_expires_at=None))
        leg, calls = self.fake_leg([
            RenewalResult(RenewalStatus.LOGIN_REQUIRED, detail="session gone"),
            RenewalResult(RenewalStatus.RENEWED, renewed=True, token_changed=True),
        ])
        _Handler.response = (
            200,
            json.dumps({"access_token": NEW_ACCESS, "refresh_token": NEW_REFRESH}),
        )
        renewer = self.composed(store, leg)
        result = renewer.renew()

        self.assertEqual(result.status, RenewalStatus.RENEWED)
        self.assertEqual(_Handler.requests, 1, "exactly one recovery refresh")
        self.assertEqual(calls, ["renew", "renew"])
        stored = store.load().gitcode
        self.assertEqual(stored.access_token, NEW_ACCESS)
        self.assertEqual(stored.refresh_token, NEW_REFRESH)

    def test_a_refused_recovery_is_not_asked_twice(self) -> None:
        """A refresh token the server refuses is spent once, not twice.

        The proactive refresh already came back LOGIN_REQUIRED (400004), so the
        recovery path must not spend a second request on the same dead token.
        """
        store = self.store()
        store._bundle = CredentialBundle(gitcode=a_credential(access_expires_at=None))
        leg, calls = self.fake_leg([
            RenewalResult(RenewalStatus.LOGIN_REQUIRED, detail="session gone"),
        ])
        _Handler.response = (
            400,
            json.dumps({"error_code": 400004, "error_code_name": "REFRESH_TOKEN_ERROR"}),
        )
        renewer = self.composed(store, leg)
        result = renewer.renew()

        self.assertEqual(result.status, RenewalStatus.LOGIN_REQUIRED)
        self.assertEqual(_Handler.requests, 1, "the dead token was asked for twice")
        self.assertEqual(calls, ["renew"])
        self.assertIn("can no longer be refreshed", result.detail or "")
        self.assertIn("400004", result.detail or "")
        self.assertIn("session gone", result.detail or "")

    def test_a_successful_refresh_that_still_fails_reports_both_facts(self) -> None:
        """A new credential is not proof the leg will accept it."""
        store = self.store()
        store._bundle = CredentialBundle(gitcode=a_credential(access_expires_at=None))
        leg, _ = self.fake_leg([
            RenewalResult(RenewalStatus.LOGIN_REQUIRED, detail="refused again"),
            RenewalResult(RenewalStatus.LOGIN_REQUIRED, detail="refused again"),
        ])
        _Handler.response = (200, json.dumps({"access_token": NEW_ACCESS}))
        renewer = self.composed(store, leg)
        result = renewer.renew()

        self.assertEqual(result.status, RenewalStatus.LOGIN_REQUIRED)
        self.assertEqual(_Handler.requests, 1)
        self.assertEqual(store.load().gitcode.access_token, NEW_ACCESS)
        self.assertIn("refused again", result.detail or "")
        self.assertIn("still refused", result.detail or "")

    def test_a_healthy_renewal_never_touches_the_refresh_path(self) -> None:
        store = MemoryCredentialStore(
            CredentialBundle(gitcode=a_credential(access_expires_at=time.time() + 90_000))
        )
        leg, calls = self.fake_leg([RenewalResult(RenewalStatus.RENEWED, renewed=True)])
        self.composed(store, leg).renew()
        self.assertEqual(_Handler.requests, 0)
        self.assertEqual(calls, ["renew"])
        self.assertEqual(store.writes, 0)



class RefreshResultShapeTest(unittest.TestCase):
    """The result is secret-free, and its JSON is what a script reads."""

    def test_as_dict_has_no_token_field(self) -> None:
        result = StoredGitCodeRefresher(MemoryCredentialStore()).refresh()
        rendered = json.dumps(result.as_dict(), ensure_ascii=False)
        self.assertNotIn("access_token", rendered)
        self.assertNotIn("refresh_token", rendered)
        self.assertNotIn(ACCESS, rendered)

    def test_the_status_is_reported_by_name(self) -> None:
        result = StoredGitCodeRefresher(MemoryCredentialStore()).refresh()
        self.assertEqual(result.as_dict()["status"], "NO_REFRESH_TOKEN")
        self.assertTrue(result.as_dict()["requires_login"])

    def test_a_protocol_error_is_not_a_login_requirement(self) -> None:
        """Stated as its own assertion because it is the expensive mistake."""
        from opencsi.auth.gitcode_refresh import RefreshResult

        result = RefreshResult(RefreshStatus.PROTOCOL_ERROR, detail="shape changed")
        self.assertFalse(result.needs_login)
        self.assertFalse(result.ok)


if __name__ == "__main__":
    unittest.main()
