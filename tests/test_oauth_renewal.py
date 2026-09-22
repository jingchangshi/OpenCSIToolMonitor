"""Silent OAuth renewal, exercised against the in-process fake DevTools server.

No test here needs a real browser or a network. What they pin down is the
*evidence standard* for renewal: a round-trip that merely completes is not a
renewal, and the suite asserts that a completed-but-unchanged round-trip is
never reported as ``RENEWED``.

They also pin the two properties the design promises the user:

* renewal happens in a **new background target** which is closed afterwards, so
  a tab the user is looking at is never navigated;
* every failure mode maps to a *distinct* status, so a caller can tell "GitCode
  wants a human" apart from "the browser is not reachable" apart from "it timed
  out" -- none of which may be collapsed into a generic error.
"""

from __future__ import annotations

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
from opencsi.auth.oauth_browser import BrowserOAuthRenewer, RenewalEvidence
from opencsi.auth.session import RenewalStatus


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


class SilentRenewalTest(unittest.TestCase):
    """The renewal state machine, one outcome per test."""

    def setUp(self) -> None:
        self.scenario = OAuthScenario(outcome="renew")
        self.server = FakeDevToolsServer(
            cookies=[opencsitool_cookie(FAKE_COOKIE, expires_in=60.0)],
            oauth=self.scenario,
        )
        self.addCleanup(self.server.close)

    def _provider(self) -> CdpCookieProvider:
        return CdpCookieProvider(self.server.base_url, discover=False, ttl=0.0)

    def test_renew_succeeds_and_reports_the_new_token(self) -> None:
        provider = self._provider()
        provider.refresh()  # install the old value
        result = _renewer(self.server).renew(before=provider)

        self.assertIs(result.status, RenewalStatus.RENEWED)
        self.assertTrue(result.renewed)
        self.assertTrue(result.token_changed)
        self.assertIsNotNone(result.expires_in)
        self.assertGreater(result.expires_in or 0, 600)

    def test_renew_creates_and_closes_a_background_target(self) -> None:
        """Renewal must not hijack a tab the user is looking at."""
        provider = self._provider()
        provider.refresh()
        _renewer(self.server).renew(before=provider)

        self.assertIn("Target.createTarget", self.scenario.calls)
        self.assertIn("Page.navigate", self.scenario.calls)
        self.assertIn("Target.closeTarget", self.scenario.calls)
        self.assertEqual(
            self.scenario.created_targets, self.scenario.closed_targets
        )
        # The existing page target must never be navigated: only the new one.
        self.assertEqual(len(self.scenario.navigated_urls), 1)
        self.assertIn("/oauth2/authorization/gitcode", self.scenario.navigated_urls[0])

    def test_renew_navigates_the_oauth_entry_with_the_redirect_parameter(self) -> None:
        provider = self._provider()
        provider.refresh()
        _renewer(self.server).renew(before=provider)
        url = self.scenario.navigated_urls[0]
        self.assertTrue(url.startswith("https://opencsitool.com/"))
        self.assertIn("redirect=%2FmyTools", url)

    def test_login_required_when_gitcode_asks_for_a_human(self) -> None:
        self.scenario.outcome = "login"
        provider = self._provider()
        provider.refresh()
        result = _renewer(self.server, timeout=6.0).renew(before=provider)

        self.assertIs(result.status, RenewalStatus.LOGIN_REQUIRED)
        self.assertTrue(result.requires_interaction)
        self.assertFalse(result.renewed)

    def test_timeout_is_reported_as_timeout_not_success(self) -> None:
        self.scenario.outcome = "timeout"
        provider = self._provider()
        provider.refresh()
        result = _renewer(self.server, timeout=2.0).renew(before=provider)

        self.assertIs(result.status, RenewalStatus.TIMEOUT)
        self.assertFalse(result.renewed)

    def test_an_unanswered_consent_page_is_not_reported_as_a_timeout(self) -> None:
        """The defect: a parked approval form was blamed on the browser.

        ``timeout`` and ``consent`` park on the *same URL*, so the renewer cannot
        tell them apart by path. It used to poll until the budget expired and
        then say "the browser may be slow or GitCode may be unreachable" --
        neither of which was true: the browser was idle and GitCode had answered
        immediately with a form waiting for a click. Measured live, approving it
        issued a fresh 60-minute token.
        """
        self.scenario.outcome = "consent"
        provider = self._provider()
        provider.refresh()
        result = _renewer(self.server, timeout=6.0).renew(before=provider)

        self.assertIs(result.status, RenewalStatus.CONSENT_REQUIRED)
        self.assertIsNot(result.status, RenewalStatus.TIMEOUT)
        self.assertIsNot(result.status, RenewalStatus.LOGIN_REQUIRED)
        self.assertTrue(result.requires_interaction)
        self.assertFalse(result.renewed)

    def test_the_consent_outcome_does_not_blame_the_network(self) -> None:
        """The detail must name the real blocker, since that is what a user reads."""
        self.scenario.outcome = "consent"
        provider = self._provider()
        provider.refresh()
        result = _renewer(self.server, timeout=6.0).renew(before=provider)

        detail = (result.detail or "").lower()
        self.assertIn("approval", detail)
        # The old, misleading wording must be gone.
        self.assertNotIn("slow", detail)
        self.assertNotIn("unreachable", detail)

    def test_consent_is_detected_faster_than_the_full_budget(self) -> None:
        """Detection must end the wait, not merely relabel it at the end.

        A generous budget with a fast result proves the loop broke out early. If
        the fix only changed the label applied after the deadline, this would
        take the whole 30 seconds.
        """
        import time as _time

        self.scenario.outcome = "consent"
        provider = self._provider()
        provider.refresh()
        started = _time.monotonic()
        result = _renewer(self.server, timeout=30.0).renew(before=provider)
        elapsed = _time.monotonic() - started

        self.assertIs(result.status, RenewalStatus.CONSENT_REQUIRED)
        self.assertLess(elapsed, 15.0, "the consent page was not detected early")

    def test_a_consent_page_is_never_reported_as_success(self) -> None:
        """A stale cookie in the jar must not be mistaken for a new one.

        The jar still holds the previous token while the consent form is up, so
        a renewer that checked "is there a cookie?" before "is a human needed?"
        would report a dead session as healthy.
        """
        self.scenario.outcome = "consent"
        provider = self._provider()
        provider.refresh()
        result = _renewer(self.server, timeout=6.0).renew(before=provider)

        self.assertFalse(result.renewed)
        self.assertFalse(result.token_changed)
        self.assertIsNot(result.status, RenewalStatus.RENEWED)

    def test_the_consent_probe_returns_a_boolean_not_page_content(self) -> None:
        """The probe must not be able to leak what is on the page.

        The consent page shows the signed-in account name, and an authorize URL
        can carry a ticket. The probe therefore answers a yes/no question and the
        expression returns nothing else, so no page text can reach a log.
        """
        from opencsi.auth.oauth_browser import BrowserOAuthRenewer as _R

        seen: list[str] = []
        original = self.server.on_call

        def spy(method, params):
            if method == "Runtime.evaluate":
                seen.append(str(params.get("expression") or ""))
            return original(method, params) if original else None

        self.server.on_call = spy
        self.addCleanup(lambda: setattr(self.server, "on_call", original))

        self.scenario.outcome = "consent"
        provider = self._provider()
        provider.refresh()
        _R(
            self.server.base_url,
            timeout=6.0,
            connect_timeout=5.0,
            poll_interval=0.05,
            settle_delay=0.05,
        ).renew(before=provider)

        probes = [e for e in seen if "querySelectorAll" in e]
        self.assertTrue(probes, "the consent probe was never sent")
        for expression in probes:
            # It returns a boolean; it never reads text out of the document.
            self.assertIn("return true", expression)
            self.assertNotIn("innerText=", expression)
            self.assertNotIn("location.href", expression)

    def test_the_probe_matches_the_labels_the_real_page_actually_shows(self) -> None:
        """Anchor the word list to observed reality, not to a guess.

        The live GitCode authorize page was read directly while diagnosing this
        defect, and its buttons were exactly ``['取消', '授权']``. If someone trims
        the approval words and drops 授权, the probe silently stops matching the
        real page and the fix becomes decorative -- every test would still pass,
        because the fake answers the probe without consulting the words at all.

        ``取消`` is asserted *absent* on purpose: it is the cancel button, and
        treating it as approval would mean the probe reports "waiting for the
        user" as soon as the user declines.
        """
        from opencsi.auth.oauth_browser import BrowserOAuthRenewer

        seen: list[str] = []
        server = FakeDevToolsServer(
            cookies=[opencsitool_cookie(FAKE_COOKIE, expires_in=60.0)],
            oauth=OAuthScenario(outcome="consent"),
        )
        self.addCleanup(server.close)
        original = server.on_call

        def spy(method, params):
            if method == "Runtime.evaluate":
                seen.append(str(params.get("expression") or ""))
            return original(method, params) if original else None

        server.on_call = spy
        provider = CdpCookieProvider(server.base_url, discover=False, ttl=0.0)
        provider.refresh()
        BrowserOAuthRenewer(
            server.base_url,
            timeout=6.0,
            connect_timeout=5.0,
            poll_interval=0.05,
            settle_delay=0.05,
        ).renew(before=provider)

        probes = [e for e in seen if "querySelectorAll" in e]
        self.assertTrue(probes, "the consent probe was never sent")
        expression = probes[0]

        # 授权, as it appears on the real page.
        self.assertIn("\\u6388\\u6743", expression)
        # 取消 must NOT be among the approval words.
        self.assertNotIn("\\u53d6\\u6d88", expression)

    def test_an_unanswered_consent_page_does_not_leave_a_tab_open(self) -> None:
        """Cleanup must not depend on the outcome being a success."""
        self.scenario.outcome = "consent"
        provider = self._provider()
        provider.refresh()
        _renewer(self.server, timeout=6.0).renew(before=provider)

        self.assertEqual(self.scenario.created_targets, self.scenario.closed_targets)
        self.assertTrue(self.scenario.created_targets, "no target was ever created")

    def test_unchanged_cookie_is_not_reported_as_renewed(self) -> None:
        """The exact mistake the module exists to prevent: load != authenticated."""
        self.scenario.outcome = "noop"
        provider = self._provider()
        provider.refresh()
        result = _renewer(self.server).renew(before=provider)

        self.assertIsNot(result.status, RenewalStatus.RENEWED)
        self.assertFalse(result.renewed)
        self.assertFalse(result.token_changed)

    def test_no_cookie_after_the_round_trip_is_a_failure(self) -> None:
        self.scenario.outcome = "no_cookie"
        provider = self._provider()
        provider.refresh()
        result = _renewer(self.server).renew(before=provider)

        self.assertIs(result.status, RenewalStatus.OAUTH_FAILED)
        self.assertFalse(result.renewed)

    def test_cdp_unavailable_is_its_own_status(self) -> None:
        renewer = BrowserOAuthRenewer(
            "http://127.0.0.1:1", timeout=2.0, connect_timeout=1.0
        )
        result = renewer.renew()
        self.assertIs(result.status, RenewalStatus.CDP_UNAVAILABLE)

    def test_no_browser_websocket_is_reported_distinctly(self) -> None:
        """A page-only endpoint cannot create a background tab."""
        server = FakeDevToolsServer(oauth=OAuthScenario())
        self.addCleanup(server.close)
        renewer = BrowserOAuthRenewer(
            f"ws://{server.host}:{server.port}/devtools/page/PAGE1",
            timeout=2.0,
            connect_timeout=3.0,
        )
        result = renewer.renew()
        self.assertIs(result.status, RenewalStatus.CDP_UNAVAILABLE)
        self.assertIn("page-level", result.detail or "")

    def test_renewal_closes_the_target_even_when_it_times_out(self) -> None:
        """Cleanup must not depend on the happy path."""
        self.scenario.outcome = "timeout"
        provider = self._provider()
        provider.refresh()
        _renewer(self.server, timeout=2.0).renew(before=provider)
        self.assertEqual(self.scenario.created_targets, self.scenario.closed_targets)

    def test_a_failing_close_does_not_fail_the_renewal(self) -> None:
        """A leaked tab is a nuisance; a false failure would be a bug."""
        self.scenario.close_target_fails = True
        provider = self._provider()
        provider.refresh()
        result = _renewer(self.server).renew(before=provider)
        self.assertIs(result.status, RenewalStatus.RENEWED)


class ColdStartTest(unittest.TestCase):
    """Renewal when there is no credential yet -- the case it exists for.

    Both tests here are regressions found by running against a real browser, not
    by reasoning. The openCsiTool cookie had been deleted (exactly the situation
    silent renewal is for), the round-trip installed a fresh 3598-second token,
    and the code reported ``ALREADY_VALID`` -- twice, in two different ways.
    """

    def setUp(self) -> None:
        self.scenario = OAuthScenario(outcome="renew")
        self.server = FakeDevToolsServer(
            cookies=[opencsitool_cookie(FAKE_COOKIE, expires_in=60.0)],
            oauth=self.scenario,
        )
        self.addCleanup(self.server.close)

    def _provider(self) -> CdpCookieProvider:
        return CdpCookieProvider(self.server.base_url, discover=False, ttl=0.0)

    def test_a_credential_appearing_where_there_was_none_is_a_renewal(self) -> None:
        """No prior token means the comparison cannot fire -- but it *is* new.

        ``token_changed`` is false by construction when ``old_token`` is None,
        so treating "neither comparison fired" as ALREADY_VALID reports a
        successful renewal as a no-op. Observed on a real browser.
        """
        provider = self._provider()
        # Deliberately NOT refreshed: the provider holds no token, which is the
        # state a user is in when their cookie has expired.
        result = _renewer(self.server).renew(before=provider)

        self.assertIs(result.status, RenewalStatus.RENEWED)
        self.assertTrue(result.renewed)
        self.assertIsNotNone(result.expires_in)
        self.assertGreater(result.expires_in or 0, 600)

    def test_a_cold_start_renewal_is_not_merely_already_valid(self) -> None:
        """The specific wrong answer that was actually produced."""
        provider = self._provider()
        result = _renewer(self.server).renew(before=provider)
        self.assertIsNot(
            result.status,
            RenewalStatus.ALREADY_VALID,
            "a token that appeared from nothing was reported as already valid",
        )

    def test_a_timeout_that_still_installed_a_token_is_a_success(self) -> None:
        """Exceeding the budget does not undo a renewal that did happen.

        The first cold-start run reported TIMEOUT, and a valid 3598-second token
        was in the browser immediately afterwards. Deciding TIMEOUT without
        looking at the jar reports a working session as broken -- its own kind
        of lie, and the opposite failure to the one the module guards against.
        """
        self.scenario.outcome = "timeout_then_renew"
        provider = self._provider()
        result = _renewer(self.server, timeout=3.0).renew(before=provider)

        self.assertIs(result.status, RenewalStatus.RENEWED)
        self.assertTrue(result.renewed)
        # The overrun is still reported, because it means the budget is tight.
        self.assertIn("outran its budget", result.detail or "")

    def test_a_timeout_with_no_token_is_still_a_timeout(self) -> None:
        """The fix must not turn a real timeout into a false success."""
        self.scenario.outcome = "timeout"
        provider = self._provider()
        result = _renewer(self.server, timeout=2.0).renew(before=provider)
        self.assertIs(result.status, RenewalStatus.TIMEOUT)
        self.assertFalse(result.renewed)


class EvidenceTest(unittest.TestCase):
    """The evidence object is the audit trail; it must be secret-free."""

    def test_evidence_records_both_expiries_and_change_flags(self) -> None:
        scenario = OAuthScenario(outcome="renew")
        server = FakeDevToolsServer(
            cookies=[opencsitool_cookie(FAKE_COOKIE, expires_in=60.0)], oauth=scenario
        )
        self.addCleanup(server.close)
        provider = CdpCookieProvider(server.base_url, discover=False, ttl=0.0)
        provider.refresh()

        renewer = _renewer(server)
        renewer.renew(before=provider)
        evidence = renewer.last_evidence

        self.assertIsInstance(evidence, RenewalEvidence)
        assert evidence is not None
        self.assertTrue(evidence.token_changed)
        self.assertTrue(evidence.expiry_extended)
        self.assertFalse(evidence.landed_on_login_page)
        self.assertGreater(
            evidence.new_expires_in or 0, evidence.old_expires_in or 0
        )

    def test_evidence_dict_never_contains_a_token(self) -> None:
        evidence = RenewalEvidence(
            navigated_url_host="opencsitool.com",
            final_url_host="opencsitool.com",
            landed_on_login_page=False,
            token_changed=True,
            expiry_extended=True,
            old_expires_in=60.0,
            new_expires_in=3600.0,
        )
        payload = evidence.as_dict()
        self.assertNotIn(FAKE_COOKIE, str(payload))
        self.assertNotIn(RENEWED_COOKIE, str(payload))
        # The invariant is about *values*, not key names: `token_changed` is a
        # boolean flag and legitimately says "token". What must never appear is
        # a key whose value is a credential.
        for key, value in payload.items():
            if "token" in key:
                self.assertIsInstance(
                    value,
                    bool,
                    f"{key!r} looks like it could hold a credential",
                )

    def test_the_oauth_url_carries_no_secret_of_ours(self) -> None:
        """The URL we *build* has only a redirect path; state comes from GitCode."""
        url = BrowserOAuthRenewer("http://127.0.0.1:9222").oauth_url()
        self.assertNotIn("client_id", url)
        self.assertNotIn("token=", url)
        self.assertTrue(url.endswith("redirect=%2FmyTools"))

    def test_repr_is_secret_free(self) -> None:
        text = repr(BrowserOAuthRenewer("http://127.0.0.1:9222"))
        self.assertIn("<redacted>", text)
        self.assertNotIn(FAKE_COOKIE, text)


if __name__ == "__main__":
    unittest.main()
