"""Session lifecycle: reload vs renewal vs interactive login.

This is the regression suite for the one-hour login bug. It pins the three
semantics apart:

* **reload** re-reads the same source and may find a newer value;
* **renewal** causes a *new* session to be issued (OAuth round-trip);
* **login** needs the user.

Everything runs against the in-process fake DevTools server, so no test needs a
browser, a network, or a real credential.
"""

from __future__ import annotations

import time
import unittest

from fake_devtools import (
    FAKE_COOKIE,
    RENEWED_COOKIE,
    FakeDevToolsServer,
    OAuthScenario,
    opencsitool_cookie,
)

import helpers  # noqa: F401  (imported for its sys.path side effect)

from opencsi.auth.cdp import CdpCookieProvider
from opencsi.auth.manual import ManualCookieProvider
from opencsi.auth.oauth_browser import BrowserOAuthRenewer
from opencsi.auth.session import (
    LoginResult,
    LoginStatus,
    RenewalResult,
    RenewalStatus,
    SessionManager,
)


def _renewer(server: FakeDevToolsServer, **kwargs) -> BrowserOAuthRenewer:
    return BrowserOAuthRenewer(
        server.base_url,
        timeout=kwargs.pop("timeout", 10.0),
        connect_timeout=5.0,
        # Sub-second cadence: the tests assert on the state machine, and the
        # production 0.5 s poll plus 1.5 s settle would add ~4 s per renewal.
        poll_interval=0.05,
        settle_delay=0.05,
        **kwargs,
    )


class NeedsRenewalTest(unittest.TestCase):
    """``needs_renewal`` is a local decision and must not touch the network."""

    def test_valid_credential_does_not_need_renewal(self) -> None:
        provider = ManualCookieProvider(FAKE_COOKIE, expires_at=time.time() + 3600)
        manager = SessionManager(provider)
        self.assertFalse(manager.needs_renewal())

    def test_expiring_credential_needs_renewal(self) -> None:
        provider = ManualCookieProvider(FAKE_COOKIE, expires_at=time.time() + 60)
        manager = SessionManager(provider, renew_margin=300.0)
        self.assertTrue(manager.needs_renewal())

    def test_expired_credential_needs_renewal(self) -> None:
        provider = ManualCookieProvider(FAKE_COOKIE, expires_at=time.time() - 5)
        manager = SessionManager(provider)
        self.assertTrue(manager.needs_renewal())

    def test_unknown_expiry_does_not_trigger_renewal(self) -> None:
        """A session cookie with no ``expires`` must not renew on every call."""
        provider = ManualCookieProvider(FAKE_COOKIE)
        manager = SessionManager(provider)
        self.assertFalse(manager.needs_renewal())

    def test_missing_credential_does_not_trigger_renewal(self) -> None:
        manager = SessionManager(ManualCookieProvider())
        self.assertFalse(manager.needs_renewal())


class NoRenewerTest(unittest.TestCase):
    """Without a renewer the manager must say so, not pretend to work."""

    def test_renew_reports_unsupported(self) -> None:
        manager = SessionManager(ManualCookieProvider(FAKE_COOKIE))
        result = manager.renew()
        self.assertIs(result.status, RenewalStatus.UNSUPPORTED)
        self.assertTrue(result.requires_interaction)
        self.assertFalse(result.renewed)

    def test_ensure_valid_is_a_no_op_when_valid(self) -> None:
        provider = ManualCookieProvider(FAKE_COOKIE, expires_at=time.time() + 3600)
        result = SessionManager(provider).ensure_valid()
        self.assertIs(result.status, RenewalStatus.ALREADY_VALID)


class ManagerRenewalPolicyTest(unittest.TestCase):
    """The manager's own policy: cooldown, reload-first, bounded work."""

    def setUp(self) -> None:
        self.scenario = OAuthScenario(outcome="renew")
        self.server = FakeDevToolsServer(
            cookies=[opencsitool_cookie(FAKE_COOKIE, expires_in=60.0)],
            oauth=self.scenario,
        )
        self.addCleanup(self.server.close)
        self.provider = CdpCookieProvider(self.server.base_url, discover=False, ttl=0.0)
        self.provider.refresh()
        self.manager = SessionManager(
            self.provider, renewer=_renewer(self.server), renew_margin=300.0
        )

    def test_ensure_valid_renews_an_expiring_credential(self) -> None:
        result = self.manager.ensure_valid()
        self.assertIs(result.status, RenewalStatus.RENEWED)
        self.assertTrue(result.renewed)

    def test_cooldown_prevents_a_second_oauth_round_trip(self) -> None:
        """A refresh loop must not re-run OAuth on every tick."""
        self.manager.ensure_valid()
        first = len(self.scenario.navigated_urls)
        self.manager.renew()
        self.assertEqual(len(self.scenario.navigated_urls), first)

    def test_force_bypasses_the_cooldown(self) -> None:
        self.manager.renew(force=True)
        first = len(self.scenario.navigated_urls)
        self.manager.renew(force=True)
        self.assertGreater(len(self.scenario.navigated_urls), first)

    def test_reload_alone_can_satisfy_recovery(self) -> None:
        """When the browser already holds a newer cookie, no OAuth is needed."""
        self.server.cookies = [opencsitool_cookie(RENEWED_COOKIE, expires_in=3600.0)]
        result = self.manager.reload_then_renew()
        self.assertIs(result.status, RenewalStatus.RENEWED)
        self.assertTrue(result.token_changed)
        self.assertEqual(self.scenario.navigated_urls, [])

    def test_reload_falls_through_to_renewal(self) -> None:
        """Same cookie after reload -> the OAuth round-trip must run."""
        result = self.manager.reload_then_renew()
        self.assertIs(result.status, RenewalStatus.RENEWED)
        self.assertGreaterEqual(len(self.scenario.navigated_urls), 1)

    def test_last_renewal_is_recorded_for_diagnostics(self) -> None:
        self.manager.ensure_valid()
        self.assertIsNotNone(self.manager.last_renewal)
        self.assertIs(self.manager.last_renewal.status, RenewalStatus.RENEWED)

    def test_describe_is_secret_free(self) -> None:
        text = self.manager.describe()
        self.assertIn("credential=cdp", text)
        self.assertIn("renewer=browser-oauth", text)
        self.assertNotIn(FAKE_COOKIE, text)
        self.assertNotIn(RENEWED_COOKIE, text)

    def test_a_renewer_that_raises_does_not_escape(self) -> None:
        class Exploding:
            name = "exploding"

            def renew(self, *, timeout=None):
                raise RuntimeError("boom")

            def can_renew(self) -> bool:
                return True

            def describe(self) -> str:
                return "exploding"

        manager = SessionManager(self.provider, renewer=Exploding())
        result = manager.renew(force=True)
        self.assertIs(result.status, RenewalStatus.OAUTH_FAILED)
        self.assertFalse(result.renewed)


class ResultShapeTest(unittest.TestCase):
    """Results are the CLI's contract and must never carry a token."""

    def test_renewal_result_dict_is_secret_free(self) -> None:
        result = RenewalResult(
            RenewalStatus.RENEWED,
            renewed=True,
            token_changed=True,
            expires_in=3600.0,
            detail="renewed",
        )
        payload = result.as_dict()
        self.assertEqual(payload["status"], "RENEWED")
        self.assertTrue(payload["renewed"])
        self.assertNotIn(FAKE_COOKIE, str(payload))
        self.assertNotIn("token_value", payload)

    def test_login_result_dict_is_secret_free(self) -> None:
        payload = LoginResult(LoginStatus.SUCCEEDED, method="qr").as_dict()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["method"], "qr")

    def test_status_enum_compares_by_identity(self) -> None:
        self.assertIs(RenewalStatus("RENEWED"), RenewalStatus.RENEWED)
        self.assertIsNot(RenewalStatus.RENEWED, RenewalStatus.OAUTH_FAILED)


if __name__ == "__main__":
    unittest.main()
