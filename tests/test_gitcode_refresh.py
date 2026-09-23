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
from http.server import BaseHTTPRequestHandler, HTTPServer

from opencsi.auth.gitcode_refresh import (
    GITCODE_ACCESS_TOKEN_SECONDS,
    GITCODE_TOKEN_URL,
    GitCodeTokenRefresher,
    RefreshStatus,
    StoredGitCodeRefresher,
    _classify_http_error,
)
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
    """A stub token endpoint. Records the last query it was asked."""

    #: Set per test: ``(status, body)`` to answer with.
    response: tuple[int, str] = (200, "{}")
    last_query: str = ""

    def do_POST(self) -> None:  # noqa: N802 - the stdlib names it this
        type(self).last_query = self.path
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
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

    def test_the_endpoint_is_on_the_gitcode_host_not_the_api_host(self) -> None:
        """Established by probing: these are different services.

        ``checkOrAuthorize`` lives on ``web-api.gitcode.com``; the token endpoint
        does not. Pointing this at the API host would 404 rather than explain
        itself, which is exactly the wrong guess that costs an afternoon.
        """
        self.assertEqual(GITCODE_TOKEN_URL, "https://gitcode.com/oauth/token")
        self.assertNotIn("web-api", GITCODE_TOKEN_URL)

    def test_the_documented_token_lifetime_is_fifteen_days(self) -> None:
        """The number the whole refresh token exists for.

        The openCsiTool session lasts an hour; this is 15 days. If the lifetime
        were also an hour, a refresh token would buy nothing.
        """
        self.assertEqual(GITCODE_ACCESS_TOKEN_SECONDS, 1296000)
        self.assertEqual(GITCODE_ACCESS_TOKEN_SECONDS / 86400, 15.0)


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

    def test_an_unknown_expiry_is_refreshed(self) -> None:
        """Deliberately the opposite of the session cookie's rule.

        A session cookie with no ``expires`` never expires. An access token with
        no recorded expiry is one whose expiry this tool failed to record --
        refreshing costs one request, and not refreshing costs a scan.
        """
        self.assertTrue(self.refresher.needs_refresh(a_credential(access_expires_at=None)))

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
    """The distinctions that keep a user from re-authenticating for no reason."""

    def test_a_4xx_means_the_refresh_token_is_dead(self) -> None:
        status, detail = _classify_http_error(400, '{"error_code":400}')
        self.assertEqual(status, RefreshStatus.LOGIN_REQUIRED)
        self.assertTrue(detail)

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
                    "expires_in": GITCODE_ACCESS_TOKEN_SECONDS,
                    "scope": "all_user",
                }
            ),
        )
        result = self.refresher().refresh(a_credential(), force=True)
        self.assertEqual(result.status, RefreshStatus.REFRESHED)
        self.assertTrue(result.ok)
        self.assertEqual(result.credential.access_token, NEW_ACCESS)
        self.assertEqual(result.expires_in, float(GITCODE_ACCESS_TOKEN_SECONDS))

    def test_the_request_carries_the_grant_type_and_token(self) -> None:
        _Handler.response = (200, json.dumps({"access_token": NEW_ACCESS}))
        self.refresher().refresh(a_credential(), force=True)
        self.assertIn("grant_type=refresh_token", _Handler.last_query)
        self.assertIn("refresh_token=", _Handler.last_query)

    def test_the_request_does_not_send_a_client_secret(self) -> None:
        """Neither is required for the refresh grant, and neither is sent.

        Sending a ``client_secret`` this tool does not have would be a guess, and
        a wrong one would look like a rejected credential.
        """
        _Handler.response = (200, json.dumps({"access_token": NEW_ACCESS}))
        self.refresher().refresh(a_credential(), force=True)
        self.assertNotIn("client_secret", _Handler.last_query)
        self.assertNotIn("client_id", _Handler.last_query)
        self.assertNotIn("redirect_uri", _Handler.last_query)

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

    def test_a_400_is_login_required(self) -> None:
        _Handler.response = (
            400,
            json.dumps({"error_code": 400, "error_message": "refresh_token不存在或已过期"}),
        )
        result = self.refresher().refresh(a_credential(), force=True)
        self.assertEqual(result.status, RefreshStatus.LOGIN_REQUIRED)
        self.assertTrue(result.needs_login)

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
        _Handler.response = (400, json.dumps({"error_code": 400}))
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
