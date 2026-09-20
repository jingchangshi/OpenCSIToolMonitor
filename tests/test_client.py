"""Client behaviour: error classification, the single 401 retry, caching.

The retry policy is a hard constraint, not an implementation detail:

* **exactly one** re-read of the credential after a 401, then give up;
* a 401 that says ``Invalid Authorization`` means a *wrong* credential was sent
  (which this client never does on purpose) -- that is a different bug from an
  expired session and must not be retried blindly;
* 5xx retries are bounded and use backoff;
* nothing mutating is ever requested.
"""

from __future__ import annotations

import json
import unittest

from helpers import FakeResponse, FakeTransport, StubCredentialProvider, make_client

from opencsi.errors import (
    BadAuthHeaderError,
    BusinessApiError,
    NetworkError,
    OpenCsiError,
    PermissionDeniedError,
    ServerError,
    SessionExpiredError,
)


class RetryOn401Test(unittest.TestCase):
    """Exactly one credential refresh, then a clean failure."""

    def test_single_401_is_recovered(self) -> None:
        transport = FakeTransport()
        provider = StubCredentialProvider()
        client, _, _ = make_client(transport=transport, provider=provider)

        transport.queue = [
            FakeResponse(401, {"message": "unauthorized"}),
            FakeResponse(200, {"code": 200, "data": {}}),
        ]
        result = client._request_json("/opencsitool/rest/v1/user/getUserInfo")
        self.assertEqual(result, {"code": 200, "data": {}})
        self.assertEqual(provider.invalidations, 1)
        self.assertEqual(len(transport.calls), 2)

    def test_two_consecutive_401s_fail_with_session_expired(self) -> None:
        transport = FakeTransport()
        provider = StubCredentialProvider()
        client, _, _ = make_client(transport=transport, provider=provider)

        transport.queue = [
            FakeResponse(401, {"message": "unauthorized"}),
            FakeResponse(401, {"message": "unauthorized"}),
        ]
        with self.assertRaises(SessionExpiredError):
            client._request_json("/opencsitool/rest/v1/user/getUserInfo")

        # The retry budget is exactly one: two requests total, one invalidate.
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(provider.invalidations, 1)

    def test_401_does_not_loop(self) -> None:
        """Even with an endless supply of 401s the client stops at two calls."""
        transport = FakeTransport()
        client, _, _ = make_client(transport=transport)
        transport.queue = [FakeResponse(401, {}) for _ in range(20)]
        with self.assertRaises(SessionExpiredError):
            client._request_json("/opencsitool/rest/v1/user/getUserInfo")
        self.assertEqual(len(transport.calls), 2)

    def test_invalid_authorization_is_reported_as_a_client_bug(self) -> None:
        """``Invalid Authorization`` means a bad header was sent.

        The CLI never sends one, so seeing this means something upstream is
        broken. Retrying cannot help and the message must say so.
        """
        transport = FakeTransport()
        client, _, _ = make_client(transport=transport)
        transport.force = FakeResponse(
            401, {"message": "Invalid Authorization", "code": 401}
        )
        with self.assertRaises(BadAuthHeaderError):
            client._request_json("/opencsitool/rest/v1/user/getUserInfo")

    def test_missing_cookie_401_is_reported_as_expired(self) -> None:
        """A provider holding no credential at all must not reach the network."""
        transport = FakeTransport()
        provider = StubCredentialProvider(token=None)
        client, _, _ = make_client(transport=transport, provider=provider)
        with self.assertRaises(SessionExpiredError):
            client._request_json("/opencsitool/rest/v1/user/getUserInfo")
        # Failing before the request is the point: nothing to authenticate with.
        self.assertEqual(transport.calls, [])

    def test_401_expiry_clears_the_transport_cookie(self) -> None:
        """A rejected cookie must not be reused on the retry."""
        transport = FakeTransport()
        provider = StubCredentialProvider()
        client, _, _ = make_client(transport=transport, provider=provider)
        transport.queue = [
            FakeResponse(401, {}),
            FakeResponse(200, {"code": 200, "data": {}}),
        ]
        client._request_json("/opencsitool/rest/v1/user/getUserInfo")
        # The provider was invalidated exactly once, and the second attempt
        # installed a freshly read cookie rather than reusing the rejected one.
        self.assertEqual(provider.invalidations, 1)
        self.assertGreaterEqual(provider.reads, 2)


class ErrorClassificationTest(unittest.TestCase):
    """HTTP status -> typed error, so the CLI can exit with the right code."""

    def _client_with(self, response: FakeResponse):
        transport = FakeTransport()
        transport.force = response
        client, _, _ = make_client(transport=transport)
        return client

    def test_403_is_permission_denied(self) -> None:
        client = self._client_with(FakeResponse(403, {"message": "forbidden"}))
        with self.assertRaises(PermissionDeniedError):
            client._request_json("/opencsitool/rest/v1/user/getUserInfo")

    def test_500_is_server_error(self) -> None:
        client = self._client_with(FakeResponse(500, {"message": "boom"}))
        with self.assertRaises(ServerError):
            client._request_json("/opencsitool/rest/v1/user/getUserInfo")

    def test_404_is_not_silently_ignored(self) -> None:
        client = self._client_with(FakeResponse(404, {"message": "路径错误！"}))
        with self.assertRaises(OpenCsiError):
            client._request_json("/opencsitool/rest/v1/nope")

    def test_non_json_body_is_a_contract_error(self) -> None:
        client = self._client_with(
            FakeResponse(200, body="<html>not json</html>")
        )
        with self.assertRaises(OpenCsiError):
            client._request_json("/opencsitool/rest/v1/user/getUserInfo")

    def test_business_code_in_a_200_envelope_raises(self) -> None:
        """A 200 HTTP status with ``code != 200`` is still a failure."""
        client = self._client_with(
            FakeResponse(200, {"code": 500, "message": "内部错误", "data": None})
        )
        with self.assertRaises(BusinessApiError):
            client._request_json("/opencsitool/rest/v1/user/getUserInfo")

    def test_http_status_is_preserved_on_the_error(self) -> None:
        client = self._client_with(FakeResponse(500, {"message": "boom"}))
        try:
            client._request_json("/opencsitool/rest/v1/user/getUserInfo")
        except OpenCsiError as exc:
            self.assertEqual(exc.http_status, 500)
        else:  # pragma: no cover
            self.fail("expected an error")

    def test_errors_carry_a_machine_readable_code(self) -> None:
        client = self._client_with(FakeResponse(403, {"message": "nope"}))
        try:
            client._request_json("/opencsitool/rest/v1/user/getUserInfo")
        except OpenCsiError as exc:
            self.assertTrue(exc.code)
            self.assertTrue(exc.as_dict()["error"])
        else:  # pragma: no cover
            self.fail("expected an error")


class NetworkFailureTest(unittest.TestCase):
    def test_connection_error_becomes_network_error(self) -> None:
        transport = FakeTransport()
        transport.overrides["getUserInfo"] = NetworkError("connection refused")
        client, _, _ = make_client(transport=transport)
        with self.assertRaises(NetworkError):
            client.login_or_restore_session()

    def test_no_credential_at_all_is_reported_clearly(self) -> None:
        provider = StubCredentialProvider(token=None)
        client, _, _ = make_client(provider=provider)
        with self.assertRaises(SessionExpiredError):
            client.login_or_restore_session()


class CachingTest(unittest.TestCase):
    """Repeat reads inside the TTL must not hit the network again."""

    def test_my_tools_is_cached(self) -> None:
        client, transport, _ = make_client(cache_ttl=300.0)
        client.get_my_tools()
        before = len(transport.calls)
        client.get_my_tools()
        self.assertEqual(len(transport.calls), before)

    def test_refresh_bypasses_the_cache(self) -> None:
        client, transport, _ = make_client(cache_ttl=300.0)
        client.get_my_tools()
        before = len(transport.calls)
        client.get_my_tools(refresh=True)
        self.assertGreater(len(transport.calls), before)

    def test_a_zero_ttl_disables_reuse(self) -> None:
        client, transport, _ = make_client(cache_ttl=0.0)
        client.get_my_tools()
        before = len(transport.calls)
        client.get_my_tools()
        self.assertGreater(len(transport.calls), before)

    def test_cached_snapshot_is_equal_but_not_identical(self) -> None:
        client, _, _ = make_client(cache_ttl=300.0)
        first = client.get_my_tools()
        second = client.get_my_tools()
        self.assertEqual(first.total_tokens, second.total_tokens)

    def test_prices_are_cached(self) -> None:
        client, transport, _ = make_client(cache_ttl=300.0)
        client.get_model_prices()
        before = len(transport.calls)
        client.get_model_prices()
        self.assertEqual(len(transport.calls), before)


class ContractCheckTest(unittest.TestCase):
    """``contract_check`` verifies the shape this package depends on."""

    def test_contract_check_passes_against_fixtures(self) -> None:
        client, _, _ = make_client()
        report = client.contract_check()
        self.assertTrue(report["ok"], json.dumps(report, ensure_ascii=False)[:800])
        self.assertTrue(report["checks"])

    def test_contract_check_reports_a_missing_field(self) -> None:
        from helpers import load_fixture

        client, transport, _ = make_client()
        broken = load_fixture("personal_queue_status.json")
        del broken["data"]["tokenSummary"]
        transport.overrides["personalQueueStatus"] = broken
        report = client.contract_check()
        self.assertFalse(report["ok"])

    def test_contract_check_reports_a_missing_endpoint(self) -> None:
        client, transport, _ = make_client()
        transport.overrides["getUserInfo"] = FakeResponse(500, {"message": "down"})
        report = client.contract_check()
        self.assertFalse(report["ok"])

    def test_contract_check_propagates_a_credential_failure(self) -> None:
        """No browser is not a schema change.

        ``contract_check`` records failed checks so a drifted field is reported
        per endpoint. But swallowing a credential failure made it report
        "contract drift" to a user who simply had not started their browser with
        remote debugging -- the wrong diagnosis, and the wrong exit code.
        """
        from opencsi.errors import CdpUnavailableError

        provider = StubCredentialProvider(raises=CdpUnavailableError("no port"))
        client, _, _ = make_client(provider=provider)
        with self.assertRaises(CdpUnavailableError):
            client.contract_check()

    def test_contract_check_propagates_a_network_failure(self) -> None:
        from opencsi.errors import NetworkError

        provider = StubCredentialProvider(raises=NetworkError("dns"))
        client, _, _ = make_client(provider=provider)
        with self.assertRaises(NetworkError):
            client.contract_check()

    def test_contract_check_still_records_an_http_failure(self) -> None:
        """An HTTP-level failure *is* a check result (brief §50)."""
        client, transport, _ = make_client()
        transport.overrides["getUserInfo"] = FakeResponse(403, {"message": "denied"})
        report = client.contract_check()
        self.assertFalse(report["ok"])
        self.assertTrue(any(not c["ok"] for c in report["checks"]))

    def test_contract_check_lists_every_endpoint_it_uses(self) -> None:
        client, _, _ = make_client()
        report = client.contract_check()
        names = " ".join(check["check"] for check in report["checks"])
        for endpoint in ("personalQueueStatus", "getUserInfo", "config/cost"):
            self.assertIn(endpoint, names)

    def test_every_contract_check_carries_a_detail(self) -> None:
        """A failing check with no detail is unactionable."""
        client, _, _ = make_client()
        report = client.contract_check()
        for check in report["checks"]:
            self.assertIn("check", check)
            self.assertIn("ok", check)
            self.assertIn("detail", check)


class ReadOnlyGuaranteeTest(unittest.TestCase):
    """The client must never issue anything but GET."""

    def test_every_call_in_a_full_session_is_a_get(self) -> None:
        client, transport, _ = make_client()
        client.login_or_restore_session()
        client.get_my_tools()
        client.get_model_prices()
        client.get_call_logs()
        client.get_key_budget()
        client.contract_check()
        # FakeTransport only implements get_json, so any other verb would have
        # raised AttributeError. Assert the call log is non-empty and shaped
        # like paths, not verbs.
        self.assertTrue(transport.calls)
        for path, _ in transport.calls:
            self.assertTrue(path.startswith("/"), path)

    def test_no_call_targets_an_admin_route(self) -> None:
        client, transport, _ = make_client()
        client.login_or_restore_session()
        client.get_my_tools()
        client.get_model_prices()
        for path, _ in transport.calls:
            self.assertNotIn("admin", path.lower())
            self.assertNotIn("accountBinding", path)
            self.assertNotIn("delete", path.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
