"""CDP credential provider, exercised against an in-process fake DevTools server.

Covers the behaviours that matter in the field:

* endpoint discovery precedence (explicit > env > ports > marker file);
* cookie selection among several candidates;
* both read strategies (page-level ``/json/list`` and browser-level attach);
* the Chrome 147+ default-profile failure and its actionable hint;
* ``invalidate()`` semantics that 401 recovery depends on;
* that no failure mode leaks the cookie value.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from fake_devtools import (
    FAKE_COOKIE,
    OTHER_COOKIES,
    FakeDevToolsServer,
    opencsitool_cookie,
)

# Importing helpers puts ``src`` on sys.path (no install step required).
import helpers  # noqa: F401  (imported for its path side effect)

from opencsi.auth.cdp import (
    COOKIE_NAME,
    DEFAULT_PORTS,
    CdpCookieProvider,
    CdpEndpoint,
    discover_cdp_endpoint,
    read_devtools_active_port,
    select_token_cookie,
)
from opencsi.errors import (
    CdpUnavailableError,
    CookieNotFoundError,
    NoBrowserTargetError,
)


class CookieSelectionTest(unittest.TestCase):
    """``select_token_cookie`` must pick the right cookie, deterministically."""

    def test_picks_the_opencsitool_token(self) -> None:
        cookies = [*OTHER_COOKIES, opencsitool_cookie()]
        chosen = select_token_cookie(cookies)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["value"], FAKE_COOKIE)

    def test_returns_none_when_absent(self) -> None:
        self.assertIsNone(select_token_cookie(OTHER_COOKIES))
        self.assertIsNone(select_token_cookie([]))

    def test_ignores_a_wrong_domain(self) -> None:
        cookies = [opencsitool_cookie(domain="evil.example")]
        self.assertIsNone(select_token_cookie(cookies))

    def test_ignores_an_empty_value(self) -> None:
        self.assertIsNone(select_token_cookie([opencsitool_cookie(value="")]))

    def test_prefers_the_non_expired_cookie(self) -> None:
        expired = opencsitool_cookie(value="expiredvalue123", expires_in=-100)
        live = opencsitool_cookie(value="livevalue123456", expires_in=3600)
        chosen = select_token_cookie([expired, live])
        self.assertEqual(chosen["value"], "livevalue123456")

    def test_falls_back_to_an_expired_cookie_when_that_is_all_there_is(self) -> None:
        """An expired cookie still goes to the server, which gives a clear 401.

        Refusing to send it would produce a confusing "not logged in" instead of
        the accurate "session expired".
        """
        expired = opencsitool_cookie(value="expiredvalue123", expires_in=-100)
        chosen = select_token_cookie([expired])
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["value"], "expiredvalue123")

    def test_prefers_the_longer_lived_of_two_live_cookies(self) -> None:
        soon = opencsitool_cookie(value="soonvalue123456", expires_in=120)
        later = opencsitool_cookie(value="latervalue12345", expires_in=7200)
        self.assertEqual(select_token_cookie([soon, later])["value"], "latervalue12345")

    def test_ignores_non_mapping_entries(self) -> None:
        chosen = select_token_cookie([None, "junk", 42, opencsitool_cookie()])
        self.assertEqual(chosen["value"], FAKE_COOKIE)

    def test_cookie_name_is_configurable(self) -> None:
        cookies = [{"name": "custom", "value": "abcdefgh", "domain": "opencsitool.com"}]
        self.assertIsNone(select_token_cookie(cookies))
        self.assertIsNotNone(select_token_cookie(cookies, name="custom"))

    def test_default_cookie_name_is_token(self) -> None:
        self.assertEqual(COOKIE_NAME, "token")


class EndpointParsingTest(unittest.TestCase):
    """``discover_cdp_endpoint`` precedence and URL handling.

    Discovery consults real ``DevToolsActivePort`` marker files as a last
    resort, so every test here disables that lookup. Without it the result would
    depend on whether the machine running the suite happens to have Chrome open,
    which would make the suite non-hermetic and intermittently wrong.
    """

    def setUp(self) -> None:
        import opencsi.auth.cdp as cdp_module

        self._cdp = cdp_module
        self._real_profile_dirs = cdp_module._profile_dirs
        cdp_module._profile_dirs = lambda: []

    def tearDown(self) -> None:
        self._cdp._profile_dirs = self._real_profile_dirs

    def test_explicit_http_url_wins(self) -> None:
        with FakeDevToolsServer() as server:
            endpoint = discover_cdp_endpoint(server.base_url, probe=True)
            self.assertEqual(endpoint.port, server.port)
            self.assertIn("argument", endpoint.source)

    def test_explicit_url_without_scheme_is_accepted(self) -> None:
        with FakeDevToolsServer() as server:
            endpoint = discover_cdp_endpoint(f"127.0.0.1:{server.port}", probe=True)
            self.assertEqual(endpoint.port, server.port)

    def test_explicit_ws_url_is_accepted(self) -> None:
        endpoint = discover_cdp_endpoint(
            "ws://127.0.0.1:9222/devtools/browser/abc", probe=False
        )
        self.assertEqual(endpoint.port, 9222)
        self.assertEqual(endpoint.browser_ws_url(), "ws://127.0.0.1:9222/devtools/browser/abc")

    def test_an_unreachable_explicit_endpoint_raises_rather_than_falling_back(self) -> None:
        """An explicitly requested endpoint must fail loudly.

        Silently falling back to a discovered port would query a *different*
        browser than the one the user named, which is worse than an error.
        """
        with FakeDevToolsServer() as decoy:
            with self.assertRaises(CdpUnavailableError):
                discover_cdp_endpoint("http://127.0.0.1:1", ports=(decoy.port,), probe=True)

    def test_environment_variable_is_used(self) -> None:
        """A reachable ``$OPENCSI_CDP_URL`` is honoured."""
        with FakeDevToolsServer() as server:
            os.environ["OPENCSI_TEST_CDP"] = server.base_url
            try:
                found = discover_cdp_endpoint(None, env_var="OPENCSI_TEST_CDP", probe=True)
                self.assertEqual(found.port, server.port)
            finally:
                del os.environ["OPENCSI_TEST_CDP"]

    def test_explicit_beats_the_environment(self) -> None:
        with FakeDevToolsServer() as first, FakeDevToolsServer() as second:
            os.environ["OPENCSI_TEST_CDP"] = second.base_url
            try:
                found = discover_cdp_endpoint(
                    first.base_url, env_var="OPENCSI_TEST_CDP", probe=True
                )
                self.assertEqual(found.port, first.port)
            finally:
                del os.environ["OPENCSI_TEST_CDP"]

    def test_a_stale_environment_variable_falls_through_to_ports(self) -> None:
        """A dead ``$OPENCSI_CDP_URL`` must not brick discovery.

        Falling through to the port scan is deliberate: an environment variable
        left over from an earlier session should not make the tool unusable.
        """
        with FakeDevToolsServer() as server:
            os.environ["OPENCSI_TEST_CDP"] = "http://127.0.0.1:1"
            try:
                found = discover_cdp_endpoint(
                    None,
                    env_var="OPENCSI_TEST_CDP",
                    ports=(server.port,),
                    probe=True,
                )
                self.assertEqual(found.port, server.port)
            finally:
                del os.environ["OPENCSI_TEST_CDP"]

    def test_raises_when_nothing_is_reachable(self) -> None:
        with self.assertRaises(CdpUnavailableError):
            discover_cdp_endpoint(None, ports=(1,), probe=True)

    def test_unreachable_endpoint_error_is_actionable(self) -> None:
        try:
            discover_cdp_endpoint(None, ports=(1,), probe=True)
        except CdpUnavailableError as exc:
            self.assertTrue(exc.hint)
        else:  # pragma: no cover
            self.fail("expected CdpUnavailableError")

    def test_default_ports_are_the_documented_set(self) -> None:
        self.assertEqual(DEFAULT_PORTS, (9222, 9223, 9224))

    def test_devtools_active_port_reader_tolerates_a_missing_file(self) -> None:
        from pathlib import Path

        self.assertIsNone(read_devtools_active_port(Path("does-not-exist-xyz")))

    def test_devtools_active_port_reader_parses_a_marker_file(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp)
            (profile / "DevToolsActivePort").write_text(
                "9333\n/devtools/browser/abc-123\n", encoding="utf-8"
            )
            found = read_devtools_active_port(profile)
            self.assertEqual(found, (9333, "/devtools/browser/abc-123"))

    def test_devtools_active_port_reader_ignores_garbage(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp)
            (profile / "DevToolsActivePort").write_text("not-a-port\n", encoding="utf-8")
            self.assertIsNone(read_devtools_active_port(profile))


class ProviderAgainstFakeServerTest(unittest.TestCase):
    """End-to-end reads through a real socket, against the fake server."""

    def test_reads_the_cookie_via_a_page_target(self) -> None:
        with FakeDevToolsServer() as server:
            provider = CdpCookieProvider(server.base_url)
            token = provider.get_token()
            self.assertEqual(token, FAKE_COOKIE)
            # The page strategy should have been enough.
            self.assertTrue(any("json/list" in r for r in server.requests))

    def test_reads_the_cookie_via_the_browser_endpoint(self) -> None:
        """With no page target, the provider falls back to a browser attach."""
        with FakeDevToolsServer(pages=[]) as server:
            provider = CdpCookieProvider(server.base_url)
            token = provider.get_token()
            self.assertEqual(token, FAKE_COOKIE)
            methods = [name for name, _ in server.calls]
            self.assertIn("Storage.getCookies", methods)

    def test_browser_strategy_attaches_a_session_when_needed(self) -> None:
        """A browser exposing cookies only per-session is still handled.

        ``Storage.getCookies`` returns nothing and ``/json/list`` offers no page
        socket, so the provider must reach the cookies by attaching a target and
        reading ``Network.getCookies`` on the flattened session.
        """

        def on_call(method, params):
            if method == "Storage.getCookies":
                return {"cookies": []}
            return None

        with FakeDevToolsServer(list_pages=[], on_call=on_call) as server:
            provider = CdpCookieProvider(server.base_url)
            token = provider.get_token()
            self.assertEqual(token, FAKE_COOKIE)
            methods = [name for name, _ in server.calls]
            self.assertIn("Storage.getCookies", methods)
            self.assertIn("Target.attachToTarget", methods)
            self.assertIn("Network.getCookies", methods)

    def test_no_page_target_at_all_is_reported_clearly(self) -> None:
        """No tab to attach to is a distinct, actionable failure."""

        def on_call(method, params):
            if method == "Storage.getCookies":
                return {"cookies": []}
            return None

        with FakeDevToolsServer(pages=[], on_call=on_call) as server:
            provider = CdpCookieProvider(server.base_url)
            with self.assertRaises(NoBrowserTargetError) as ctx:
                provider.get_token()
            self.assertTrue(ctx.exception.hint)

    def test_connected_but_cookieless_reports_not_found_not_unavailable(self) -> None:
        """The distinction the client must preserve.

        A working DevTools connection holding no openCsiTool cookie means "sign
        in"; it must not be reported as a broken endpoint, which would send the
        user off to restart their browser for no reason.
        """
        with FakeDevToolsServer(cookies=[]) as server:
            provider = CdpCookieProvider(server.base_url)
            try:
                provider.get_token()
            except CookieNotFoundError:
                pass  # correct: the socket worked, the cookie is missing
            except CdpUnavailableError as exc:  # pragma: no cover - regression
                self.fail(f"misreported a missing cookie as a broken endpoint: {exc}")
            else:  # pragma: no cover
                self.fail("expected CookieNotFoundError")

    def test_missing_cookie_raises_the_right_error(self) -> None:
        with FakeDevToolsServer(cookies=list(OTHER_COOKIES)) as server:
            provider = CdpCookieProvider(server.base_url)
            with self.assertRaises(CookieNotFoundError):
                provider.get_token()

    def test_cookie_not_found_error_is_actionable(self) -> None:
        with FakeDevToolsServer(cookies=[]) as server:
            provider = CdpCookieProvider(server.base_url)
            try:
                provider.get_token()
            except CookieNotFoundError as exc:
                self.assertTrue(exc.hint)
                self.assertIn("opencsitool.com", exc.hint)

    def test_status_is_secret_free(self) -> None:
        with FakeDevToolsServer() as server:
            provider = CdpCookieProvider(server.base_url)
            provider.get_token()
            status = provider.status()
            self.assertTrue(status.available)
            self.assertNotIn(FAKE_COOKIE, str(status.as_dict()))
            self.assertNotIn(FAKE_COOKIE, repr(status))

    def test_status_reports_expiry(self) -> None:
        with FakeDevToolsServer(cookies=[opencsitool_cookie(expires_in=1800)]) as server:
            provider = CdpCookieProvider(server.base_url)
            provider.get_token()
            status = provider.status()
            self.assertIsNotNone(status.expires_in)
            self.assertGreater(status.expires_in, 1500)
            self.assertFalse(status.expired)

    def test_repr_never_contains_the_cookie(self) -> None:
        with FakeDevToolsServer() as server:
            provider = CdpCookieProvider(server.base_url)
            provider.get_token()
            self.assertNotIn(FAKE_COOKIE, repr(provider))
            self.assertNotIn(FAKE_COOKIE, str(provider))

    def test_endpoint_is_exposed_for_diagnostics(self) -> None:
        with FakeDevToolsServer() as server:
            provider = CdpCookieProvider(server.base_url)
            provider.get_token()
            self.assertIsNotNone(provider.endpoint)
            self.assertNotIn(FAKE_COOKIE, provider.describe_endpoint())

    def test_browser_version_is_reported_when_available(self) -> None:
        with FakeDevToolsServer() as server:
            provider = CdpCookieProvider(server.base_url)
            provider.get_token()
            self.assertIn("Chrome/153", str(provider.browser))


class Chrome147FailureModeTest(unittest.TestCase):
    """The default-profile refusal must be reported with a usable fix.

    Chrome 147+ stops serving ``/json/*`` entirely on the default profile, so
    the endpoint can only be located through the profile's
    ``DevToolsActivePort`` marker file. That is the real-world path, and it is
    reproduced here: the marker file is a temp file pointing at the fake server.
    """

    def setUp(self) -> None:
        import opencsi.auth.cdp as cdp_module

        self._cdp = cdp_module
        self._real_profile_dirs = cdp_module._profile_dirs
        self._tmp = tempfile.TemporaryDirectory()
        self.profile = Path(self._tmp.name)
        cdp_module._profile_dirs = lambda: [self.profile]

    def tearDown(self) -> None:
        self._cdp._profile_dirs = self._real_profile_dirs
        self._tmp.cleanup()

    def _write_marker(self, port: int, ws_path: str) -> None:
        (self.profile / "DevToolsActivePort").write_text(
            f"{port}\n{ws_path}\n", encoding="utf-8"
        )

    def test_json_api_disabled_still_works_via_the_marker_file(self) -> None:
        """The Chrome 147+ happy path: no /json/*, but the marker file suffices.

        This is the case that actually matters for users on current Chrome --
        the endpoint must still be usable, not merely diagnosable.
        """
        with FakeDevToolsServer(json_api=False) as server:
            self._write_marker(server.port, "/devtools/browser/FAKE-BROWSER-ID")
            provider = CdpCookieProvider(ports=(server.port,), timeout=2.0)
            self.assertEqual(provider.get_token(), FAKE_COOKIE)
            # /json/version is still probed during discovery (it is how a
            # reachable-but-unhelpful endpoint is told apart from a dead one),
            # but no page-level WebSocket may be needed: the browser socket
            # recovered from the marker file is the route that works.
            self.assertFalse(
                any("/devtools/page/" in r for r in server.requests),
                "the page-level strategy should not have been usable",
            )
            methods = [name for name, _ in server.calls]
            self.assertIn("Storage.getCookies", methods)

    def test_refused_upgrade_hint_names_the_dedicated_profile_fix(self) -> None:
        with FakeDevToolsServer(
            json_api=False, pages=[], refuse_browser_ws=True
        ) as server:
            self._write_marker(server.port, "/devtools/browser/FAKE-BROWSER-ID")
            provider = CdpCookieProvider(ports=(server.port,), timeout=1.0)
            try:
                provider.get_token()
            except CdpUnavailableError as exc:
                self.assertIsNotNone(exc.hint)
                self.assertIn("--user-data-dir", exc.hint)
                self.assertIn("--remote-debugging-port", exc.hint)
            else:  # pragma: no cover
                self.fail("expected CdpUnavailableError")

    def test_failure_detail_is_exposed_for_doctor(self) -> None:
        with FakeDevToolsServer(
            json_api=False, pages=[], refuse_browser_ws=True
        ) as server:
            self._write_marker(server.port, "/devtools/browser/FAKE-BROWSER-ID")
            provider = CdpCookieProvider(ports=(server.port,), timeout=1.0)
            status = provider.status()
            self.assertFalse(status.available)
            self.assertIsNotNone(status.detail)
            self.assertIsNotNone(provider.last_hint)
            self.assertIn("--user-data-dir", provider.last_hint)

    def test_marker_file_lets_discovery_succeed_despite_the_404(self) -> None:
        """This is the whole point of the marker fallback."""
        with FakeDevToolsServer(json_api=False, pages=[]) as server:
            self._write_marker(server.port, "/devtools/browser/FAKE-BROWSER-ID")
            endpoint = discover_cdp_endpoint(None, ports=(server.port,), probe=True)
            self.assertEqual(endpoint.port, server.port)
            self.assertIsNotNone(endpoint.browser_ws_url())

    def test_forbidden_upgrade_is_reported(self) -> None:
        with FakeDevToolsServer(allow_ws=False) as server:
            provider = CdpCookieProvider(server.base_url, timeout=1.0)
            with self.assertRaises(CdpUnavailableError):
                provider.get_token()

    def test_no_json_api_and_no_marker_file_is_unavailable(self) -> None:
        """Both routes gone: the error must still be actionable, not a hang."""
        with FakeDevToolsServer(
            json_api=False, pages=[], list_pages=[], refuse_browser_ws=True
        ) as server:
            # No marker file written, so the ws path is unknowable.
            provider = CdpCookieProvider(ports=(server.port,), timeout=1.0)
            with self.assertRaises(CdpUnavailableError) as ctx:
                provider.get_token()
            self.assertTrue(ctx.exception.hint)


class InvalidationSemanticsTest(unittest.TestCase):
    """401 recovery depends on ``invalidate()`` being a *cache* drop."""

    def test_invalidate_allows_a_fresh_read(self) -> None:
        with FakeDevToolsServer() as server:
            provider = CdpCookieProvider(server.base_url, discover=False)
            self.assertEqual(provider.get_token(), FAKE_COOKIE)
            before = len(server.requests)
            provider.invalidate()
            self.assertEqual(provider.get_token(), FAKE_COOKIE)
            self.assertGreater(len(server.requests), before)

    def test_ttl_reuses_the_cached_value(self) -> None:
        with FakeDevToolsServer() as server:
            provider = CdpCookieProvider(server.base_url, discover=False, ttl=60.0)
            provider.get_token()
            before = len(server.requests)
            provider.get_token()
            self.assertEqual(len(server.requests), before)

    def test_refresh_bypasses_the_ttl(self) -> None:
        with FakeDevToolsServer() as server:
            provider = CdpCookieProvider(server.base_url, discover=False, ttl=600.0)
            provider.get_token()
            before = len(server.requests)
            provider.refresh()
            self.assertGreater(len(server.requests), before)

    def test_a_near_expiry_cookie_is_refreshed_proactively(self) -> None:
        with FakeDevToolsServer(cookies=[opencsitool_cookie(expires_in=10)]) as server:
            provider = CdpCookieProvider(server.base_url, discover=False, ttl=600.0)
            provider.get_token()
            before = len(server.requests)
            provider.get_token()
            # Under the 30s margin the provider re-reads instead of trusting it.
            self.assertGreater(len(server.requests), before)

    def test_a_dead_endpoint_raises_rather_than_returning_none(self) -> None:
        """``get_token`` must not silently return ``None`` for a broken source.

        Returning ``None`` would collapse "your browser needs restarting" into
        "not signed in", which sends the user down the wrong path. The provider
        raises an error carrying the real code and hint instead.
        """
        provider = CdpCookieProvider("http://127.0.0.1:1", timeout=0.5)
        with self.assertRaises(CdpUnavailableError) as ctx:
            provider.get_token()
        self.assertTrue(ctx.exception.hint)
        self.assertEqual(provider.status().available, False)


class PortsConfigurationTest(unittest.TestCase):
    def test_none_means_the_defaults(self) -> None:
        provider = CdpCookieProvider(ports=None, discover=False)
        self.assertEqual(provider._ports, DEFAULT_PORTS)

    def test_empty_sequence_means_the_defaults(self) -> None:
        provider = CdpCookieProvider(ports=(), discover=False)
        self.assertEqual(provider._ports, DEFAULT_PORTS)

    def test_explicit_ports_are_used(self) -> None:
        provider = CdpCookieProvider(ports=(9001, 9002), discover=False)
        self.assertEqual(provider._ports, (9001, 9002))

    def test_discovery_finds_the_fake_server_by_port(self) -> None:
        with FakeDevToolsServer() as server:
            endpoint = discover_cdp_endpoint(None, ports=(server.port,), probe=True)
            self.assertEqual(endpoint.port, server.port)


if __name__ == "__main__":
    unittest.main(verbosity=2)
