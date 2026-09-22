"""CLI surface for session inspection and renewal.

Covers the parts of ``opencsi login`` and ``opencsi doctor`` that exist because
of the session-lifecycle split:

* ``login --status`` reports without changing anything;
* ``login --renew`` maps each :class:`RenewalStatus` to a distinct exit code and
  verifies a reported success with a real request;
* ``doctor`` reports silent-renewal capability and tray prerequisites;
* the read/inspect paths never silently renew, so a diagnostic shows the problem
  instead of repairing it.

Everything runs against a stub client/provider; no test touches a browser.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import time
import unittest
import unittest.mock
from pathlib import Path

from helpers import FAKE_TOKEN, StubCredentialProvider, make_client

from opencsi.auth.base import remaining_seconds
from opencsi.auth.cdp import CdpCookieProvider
from opencsi.auth.oauth_browser import RenewalCapability
from opencsi.auth.session import (
    LoginResult,
    LoginStatus,
    RenewalResult,
    RenewalStatus,
    SessionManager,
)
from opencsi.cli.app import main
from opencsi.errors import EXIT_SESSION_EXPIRED, EXIT_USAGE

ROOT = Path(__file__).resolve().parent.parent


def run_cli(
    argv: list[str], *, client=None, renewer=None, provider=None
) -> tuple[int, str, str]:
    """Invoke ``main`` capturing stdout/stderr, optionally faking seams.

    ``login --renew`` reaches the provider and the renewer through
    ``CliContext``, so those are the seams patched here rather than a
    module-level name in ``login`` -- patching a name the command no longer
    reads would make the test pass while the real path stayed broken.
    """
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        import opencsi.cli.context as ctx_module

        originals = (
            ctx_module.CliContext.make_client,
            ctx_module.CliContext.make_provider,
            ctx_module.CliContext.make_renewer,
        )
        try:
            if client is not None:

                def fake_make_client(self, *, provider=None, renew=True):  # noqa: ANN001
                    return client

                ctx_module.CliContext.make_client = fake_make_client
            if provider is not None:
                ctx_module.CliContext.make_provider = lambda self: provider
            if renewer is not None:
                ctx_module.CliContext.make_renewer = (
                    lambda self, prov, *, base_url: renewer
                )
            code = main(argv)
        finally:
            (
                ctx_module.CliContext.make_client,
                ctx_module.CliContext.make_provider,
                ctx_module.CliContext.make_renewer,
            ) = originals
    return code, out.getvalue(), err.getvalue()


class _StubRenewer:
    """A renewer whose result is scripted, so exit mapping is testable."""

    name = "stub-renewer"

    def __init__(self, result: RenewalResult) -> None:
        self._result = result
        self.calls = 0

    def renew(self, *, timeout=None, before=None) -> RenewalResult:
        self.calls += 1
        return self._result

    def can_renew(self) -> bool:
        return True

    def describe(self) -> str:
        return "stub renewer"


class _FakeCdpProvider(CdpCookieProvider):
    """A browser-shaped provider that never touches a browser.

    ``login --renew`` refuses a non-browser credential, because a pasted token
    has no SSO session to re-run OAuth against. That check is on the *type*, so
    a test that wants to exercise renewal has to supply a browser-shaped
    provider -- subclassing is the honest way to do that, rather than weakening
    the production check to make a stub fit.
    """

    def __init__(self, token: str | None = "TOKEN") -> None:
        super().__init__(discover=False)
        self._token = token
        self._expires_at = time.time() + 3600.0
        self._read_at = time.time()

    def get_token(self) -> str | None:
        return self._token

    def peek_token(self) -> str | None:
        return self._token

    def invalidate(self) -> None:
        self._token = None

    def refresh(self) -> str | None:
        return self._token

    def status(self):
        from opencsi.auth.base import CredentialStatus

        return CredentialStatus(
            available=self._token is not None,
            source="cdp",
            expires_at=self._expires_at,
            expires_in=remaining_seconds(self._expires_at),
        )


def _stub_client(*, token: str | None = "TOKEN"):
    """A client wired to the offline fake transport (see ``helpers``)."""
    client, _transport, _provider = make_client(provider=_FakeCdpProvider(token))
    return client


def _client_with_renewer(result: RenewalResult, *, token: str | None = "TOKEN"):
    client = _stub_client(token=token)
    manager = SessionManager(client.credentials, renewer=_StubRenewer(result))
    client.session = manager
    return client


class LoginStatusTest(unittest.TestCase):
    """``login --status`` must observe, never mutate."""

    def test_status_reports_the_credential_and_session(self) -> None:
        client = _stub_client()
        code, out, _ = run_cli(["login", "--status"], client=client)
        self.assertEqual(code, 0)
        self.assertIn("Session", out)
        self.assertIn("OK", out)

    def test_status_json_has_the_documented_shape(self) -> None:
        client = _stub_client()
        code, out, _ = run_cli(["login", "--status", "--json"], client=client)
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertIn("credential", payload)
        self.assertIn("renewal", payload)
        self.assertIn("needs_renewal", payload["renewal"])

    def test_status_does_not_renew(self) -> None:
        """A status check that performed OAuth would hide the expiry it reports."""
        result = RenewalResult(RenewalStatus.RENEWED, renewed=True)
        client = _client_with_renewer(result)
        run_cli(["login", "--status"], client=client)
        self.assertEqual(client.session.renewer.calls, 0)

    def test_status_reports_a_failed_session_with_the_real_code(self) -> None:
        client = _stub_client(token=None)
        client.session = SessionManager(client.credentials)
        code, out, err = run_cli(["login", "--status", "--json"], client=client)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertIn("error", payload)
        self.assertNotEqual(code, 0)


class RenewExitCodeTest(unittest.TestCase):
    """Each renewal outcome maps to one documented exit code."""

    def _run(self, result: RenewalResult, *, token: str | None = "TOKEN", argv=None, client=None):
        """Run ``login --renew`` with a scripted renewer.

        The provider and renewer are injected through ``CliContext``, which is
        where ``_renew`` actually obtains them.
        """
        provider = _FakeCdpProvider(token)
        renewer = _StubRenewer(result)
        client = client if client is not None else _stub_client(token=token)
        return run_cli(
            argv or ["login", "--renew"],
            client=client,
            provider=provider,
            renewer=renewer,
        ), renewer

    def test_renewed_and_verified_exits_zero(self) -> None:
        result = RenewalResult(RenewalStatus.RENEWED, renewed=True, token_changed=True)
        (code, out, _), _ = self._run(result, argv=["login", "--renew", "--json"])
        payload = json.loads(out)
        self.assertTrue(payload["verified"])
        self.assertEqual(code, 0)

    def test_login_required_exits_with_the_session_code(self) -> None:
        result = RenewalResult(RenewalStatus.LOGIN_REQUIRED, requires_interaction=True)
        (code, _, err), _ = self._run(result)
        self.assertEqual(code, EXIT_SESSION_EXPIRED)
        self.assertIn("SSO session is gone", err)

    def test_cdp_unavailable_exits_with_the_cdp_code(self) -> None:
        (code, _, _), _ = self._run(RenewalResult(RenewalStatus.CDP_UNAVAILABLE))
        self.assertEqual(code, 10)

    def test_timeout_exits_with_the_network_code(self) -> None:
        (code, _, _), _ = self._run(RenewalResult(RenewalStatus.TIMEOUT))
        self.assertEqual(code, 30)

    def test_already_valid_exits_zero(self) -> None:
        (code, _, _), _ = self._run(RenewalResult(RenewalStatus.ALREADY_VALID))
        self.assertEqual(code, 0)

    def test_oauth_failed_exits_with_the_session_code(self) -> None:
        (code, _, _), _ = self._run(RenewalResult(RenewalStatus.OAUTH_FAILED))
        self.assertEqual(code, EXIT_SESSION_EXPIRED)

    def test_consent_required_exits_with_the_session_code(self) -> None:
        """Not the network code: nothing about this is a network problem."""
        result = RenewalResult(RenewalStatus.CONSENT_REQUIRED, requires_interaction=True)
        (code, _, err), _ = self._run(result)
        self.assertEqual(code, EXIT_SESSION_EXPIRED)
        self.assertIn("approval", err.lower())

    def test_every_renewal_status_has_a_documented_exit_code(self) -> None:
        """The map must be exhaustive, because the fallback is silent.

        ``_RENEWAL_EXIT.get(status, 1)`` turns any status someone forgets into
        exit 1, which is not a documented renewal outcome at all -- so a new
        status would be reported as a generic failure rather than crashing or
        being noticed. This asserts membership directly, so adding a status
        without deciding its exit code fails here.
        """
        from opencsi.auth.session import RenewalStatus
        from opencsi.cli.login import _RENEWAL_EXIT

        missing = [s.name for s in RenewalStatus if s not in _RENEWAL_EXIT]
        self.assertEqual(missing, [], f"no exit code decided for: {missing}")

    def test_every_qr_status_has_a_documented_exit_code(self) -> None:
        """The QR map had the same silent fallback and no guard at all.

        ``login --qr`` ended in ``{...}.get(result.status, 1)``, so a new
        ``QrLoginStatus`` would have been reported as exit 1 -- not a documented
        outcome for this command -- and every test would still have passed. The
        renewal map was guarded when that class of bug was found; this one was
        not, which is the "where else does this happen?" question left unasked.

        ``SUCCEEDED`` is exempt because the success path returns 0 before the map
        is consulted, and asserting its presence would contradict the code.
        """
        from opencsi.auth.gitcode_qr import QrLoginStatus
        from opencsi.cli.login import _qr_exit_codes

        codes = _qr_exit_codes()
        missing = [
            s.name for s in QrLoginStatus if s is not QrLoginStatus.SUCCEEDED and s not in codes
        ]
        self.assertEqual(missing, [], f"no exit code decided for: {missing}")

    def test_the_qr_map_never_returns_a_success_code(self) -> None:
        """A failure must not be reported as success by a mapping mistake."""
        from opencsi.cli.login import _qr_exit_codes

        self.assertNotIn(0, set(_qr_exit_codes().values()))

    def test_unverified_success_is_not_reported_as_ok(self) -> None:
        """A renewed cookie the server rejects must not exit 0.

        The cookie's presence is not proof the session works; only a real
        request is. This is the same evidence standard the renewer applies
        internally, carried through to the CLI's own success flag.
        """
        result = RenewalResult(RenewalStatus.RENEWED, renewed=True, token_changed=True)
        # The verification request is made with a provider holding no token, so
        # loading the identity fails even though renewal "succeeded".
        (code, out, _), _ = self._run(
            result, token=None, argv=["login", "--renew", "--json"]
        )
        payload = json.loads(out)
        self.assertFalse(payload["verified"])
        self.assertFalse(payload["ok"])
        self.assertEqual(code, EXIT_SESSION_EXPIRED)

    def test_renew_json_never_contains_the_token(self) -> None:
        from helpers import FAKE_TOKEN

        result = RenewalResult(RenewalStatus.RENEWED, renewed=True, token_changed=True)
        (_, out, err), _ = self._run(
            result, token=FAKE_TOKEN, argv=["login", "--renew", "--json"]
        )
        self.assertNotIn(FAKE_TOKEN, out)
        self.assertNotIn(FAKE_TOKEN, err)

    def test_renew_asks_for_renewal_exactly_once(self) -> None:
        """The whole path is bounded: one renewal per invocation, no loop."""
        result = RenewalResult(RenewalStatus.RENEWED, renewed=True, token_changed=True)
        (_, _, _), renewer = self._run(result)
        self.assertEqual(renewer.calls, 1)

    def test_renew_rejects_a_manual_credential(self) -> None:
        """A pasted token has no SSO session to re-run OAuth against."""
        from opencsi.auth.manual import ManualCookieProvider

        # ``--renew --manual`` is already refused by the argument parser, so
        # this exercises the guard in the command itself: a manual provider
        # reached by any route must not be handed to an OAuth renewer.
        code, _, err = run_cli(
            ["login", "--renew"], provider=ManualCookieProvider("TOKENVALUE0123456789")
        )
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("browser-backed", err)

    def test_manual_and_renew_are_mutually_exclusive_at_the_parser(self) -> None:
        code, _, err = run_cli(["login", "--renew", "--manual"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("not allowed with", err)


class RenewalCapabilityConsistencyTest(unittest.TestCase):
    """``login --status`` and ``doctor`` must agree about silent renewal.

    They did not. ``--status`` built its session with ``renew=False``, so it read
    ``session.renewer is None`` -- true by construction -- and printed
    "unavailable" on a machine where ``doctor`` reported renewal working. A
    status command that reports a working feature as broken is worse than one
    that says nothing, because the user acts on it.

    Both now ask ``renewal_capability``, so the only way they can diverge is if
    someone reintroduces a second answer. These tests make that fail loudly.
    """

    def test_status_does_not_claim_renewal_is_unavailable_when_it_is_not(self) -> None:
        """The specific regression: a working renewer must not read as missing."""
        provider = _FakeCdpProvider("TOKEN")
        client = _stub_client(token="TOKEN")
        with unittest.mock.patch(
            "opencsi.auth.oauth_browser.renewal_capability"
        ) as probe:
            probe.return_value = RenewalCapability(True, "GitCode SSO available")
            code, out, _ = run_cli(
                ["login", "--status", "--json"], client=client, provider=provider
            )
        payload = json.loads(out)
        self.assertTrue(
            payload["renewal"]["available"],
            "login --status reported renewal unavailable while the capability "
            "probe said it was available",
        )
        self.assertEqual(code, 0)

    def test_status_reports_the_reason_when_renewal_is_unavailable(self) -> None:
        """A bare "unavailable" is not actionable; the reason is."""
        provider = _FakeCdpProvider("TOKEN")
        client = _stub_client(token="TOKEN")
        with unittest.mock.patch(
            "opencsi.auth.oauth_browser.renewal_capability"
        ) as probe:
            probe.return_value = RenewalCapability(False, "no browser endpoint")
            _, out, _ = run_cli(
                ["login", "--status", "--json"], client=client, provider=provider
            )
        payload = json.loads(out)
        self.assertFalse(payload["renewal"]["available"])
        self.assertIn("no browser endpoint", payload["renewal"]["reason"])

    def test_doctor_and_status_use_the_same_probe(self) -> None:
        """Neither command may answer this question on its own."""
        for module in ("src/opencsi/cli/login.py", "src/opencsi/cli/doctor.py"):
            source = (ROOT / module).read_text(encoding="utf-8")
            self.assertIn(
                "renewal_capability",
                source,
                f"{module} does not use the shared capability probe",
            )

    def test_a_manual_credential_is_reported_as_unable_to_renew(self) -> None:
        """The probe's own answer for the case that has no upstream session."""
        from opencsi.auth.manual import ManualCookieProvider
        from opencsi.auth.oauth_browser import renewal_capability

        capability = renewal_capability(ManualCookieProvider("TOKEN"))
        self.assertFalse(capability.available)
        self.assertIn("manual", capability.reason.lower())


class SsoPresenceTest(unittest.TestCase):
    """The capability probe must not claim an SSO session it never looked for.

    It used to. Having established only that a browser-level WebSocket answers,
    it returned the reason "GitCode SSO available" -- a statement about a cookie
    it had not read. On a machine where the next renewal would park on GitCode's
    approval page, `doctor` therefore reported health, and the troubleshooting
    guide told the user that seeing that line meant they had recovered.
    """

    def _capability(self, cookies, *, ws="ws://127.0.0.1:9222/devtools/browser/ABC"):
        """Run the real probe with discovery and cookie reads stubbed."""
        from unittest import mock

        from opencsi.auth.cdp import CdpCookieProvider
        from opencsi.auth.oauth_browser import renewal_capability

        class _Endpoint:
            def browser_ws_url(self):
                return ws

        provider = CdpCookieProvider("http://127.0.0.1:9222", discover=False, ttl=0.0)
        with mock.patch(
            "opencsi.auth.oauth_browser.make_cdp_renewer"
        ) as make, mock.patch(
            "opencsi.auth.oauth_browser.CdpConnection"
        ) as conn_cls:
            make.return_value._resolve_endpoint.return_value = _Endpoint()
            conn = conn_cls.return_value.__enter__.return_value
            conn.call.return_value = {"cookies": cookies}
            return renewal_capability(provider)

    def test_a_missing_gitcode_sso_cookie_is_reported_honestly(self) -> None:
        capability = self._capability(
            [{"name": "token", "domain": "opencsitool.com"}]
        )
        self.assertTrue(capability.available, "the machinery is still usable")
        self.assertNotIn(
            "GitCode SSO available",
            capability.reason,
            "the probe claimed an SSO session it did not find",
        )
        self.assertIn("sign-in", capability.reason.lower())

    def test_a_present_gitcode_sso_cookie_keeps_the_optimistic_reason(self) -> None:
        capability = self._capability(
            [
                {"name": "token", "domain": "opencsitool.com"},
                {"name": "GITCODE_ACCESS_TOKEN", "domain": ".gitcode.com"},
            ]
        )
        self.assertTrue(capability.available)
        self.assertIn("GitCode SSO available", capability.reason)

    def test_an_unreadable_cookie_jar_does_not_assert_the_user_is_signed_out(self) -> None:
        """Unknown must not be reported as missing.

        A transient DevTools hiccup would otherwise become a confident warning
        that the user has been signed out -- the same class of error as claiming
        the session is fine without looking.
        """
        capability = self._capability(None)
        self.assertTrue(capability.available)
        self.assertIn("GitCode SSO available", capability.reason)

    def test_the_probe_never_returns_a_cookie_value(self) -> None:
        """Only names are examined, so nothing secret can reach a report."""
        capability = self._capability(
            [{"name": "GITCODE_ACCESS_TOKEN", "domain": ".gitcode.com", "value": "SECRET"}]
        )
        self.assertNotIn("SECRET", capability.reason)
        self.assertNotIn("SECRET", str(capability.as_dict()))


class DoctorSessionTest(unittest.TestCase):
    """``doctor`` reports the session lifecycle, not just reachability."""

    def test_doctor_reports_silent_renewal(self) -> None:
        client = _stub_client()
        code, out, _ = run_cli(["doctor", "--skip-contract"], client=client)
        self.assertIn("silent renewal", out)

    def test_doctor_reports_tray_support(self) -> None:
        client = _stub_client()
        _, out, _ = run_cli(["doctor", "--skip-contract"], client=client)
        self.assertIn("tray support", out)

    def test_doctor_json_has_the_new_checks(self) -> None:
        client = _stub_client()
        _, out, _ = run_cli(
            ["doctor", "--skip-contract", "--json"], client=client
        )
        payload = json.loads(out)
        names = [c["check"] for c in payload["checks"]]
        self.assertIn("silent renewal", names)
        self.assertIn("tray support", names)

    def test_doctor_does_not_renew(self) -> None:
        """doctor must show the real state, not silently repair it."""
        result = RenewalResult(RenewalStatus.RENEWED, renewed=True)
        client = _client_with_renewer(result)
        run_cli(["doctor", "--skip-contract"], client=client)
        self.assertEqual(client.session.renewer.calls, 0)

    def test_no_renew_env_disables_renewal_and_doctor_says_so(self) -> None:
        client = _stub_client()
        original = os.environ.get("OPENCSI_NO_RENEW")
        os.environ["OPENCSI_NO_RENEW"] = "1"
        try:
            _, out, _ = run_cli(["doctor", "--skip-contract"], client=client)
        finally:
            if original is None:
                os.environ.pop("OPENCSI_NO_RENEW", None)
            else:
                os.environ["OPENCSI_NO_RENEW"] = original
        self.assertIn("silent renewal", out)


class LoginResultShapeTest(unittest.TestCase):
    """The login result type is part of the session layer's contract."""

    def test_login_result_reports_unsupported_without_an_authenticator(self) -> None:
        manager = SessionManager(StubCredentialProvider())
        result = manager.login()
        self.assertIs(result.status, LoginStatus.UNSUPPORTED)
        self.assertFalse(result.ok)

    def test_login_delegates_to_the_authenticator(self) -> None:
        class Auth:
            name = "stub-auth"

            def login(self, *, timeout=None):
                return LoginResult(LoginStatus.SUCCEEDED, method="stub")

            def describe(self) -> str:
                return "stub"

        manager = SessionManager(StubCredentialProvider(), authenticator=Auth())
        result = manager.login()
        self.assertTrue(result.ok)
        self.assertEqual(result.method, "stub")


if __name__ == "__main__":
    unittest.main()
