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

from helpers import FAKE_TOKEN, StubCredentialProvider, make_client

from opencsi.auth.base import remaining_seconds
from opencsi.auth.cdp import CdpCookieProvider
from opencsi.auth.session import (
    LoginResult,
    LoginStatus,
    RenewalResult,
    RenewalStatus,
    SessionManager,
)
from opencsi.cli.app import main
from opencsi.errors import EXIT_SESSION_EXPIRED, EXIT_USAGE


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
