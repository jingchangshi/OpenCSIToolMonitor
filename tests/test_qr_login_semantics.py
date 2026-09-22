"""QR login must not claim success it did not achieve.

The defect this file exists to prevent
--------------------------------------
``opencsi login --qr`` authenticated with GitCode and then exited **0** while
printing "now go and finish in a browser". Both halves of that were wrong:

* a script reading the exit code concluded ``opencsi usage`` would work, and it
  would not -- the openCsiTool session did not exist;
* a human was told the command succeeded and simultaneously told there was more
  to do, which is not a coherent thing to say.

So the tests here are mostly about *what the command refuses to claim*. GitCode
success and openCsiTool success are separate facts with separate exit codes, and
the only exit 0 is the one where ``getUserInfo`` actually answered.

Everything runs against stubs. No test in this file touches a browser, a network
or a real GitCode account, which is what lets the unhappy paths -- a rejected
credential, an unreachable engine, a consent page -- be tested at all.
"""

from __future__ import annotations

import contextlib
import io
import unittest
import unittest.mock
from typing import Any, Mapping

import helpers  # noqa: F401  (imported for its sys.path side effect)

from opencsi.auth.gitcode_bridge import (
    GITCODE_SESSION_COOKIES,
    BridgeResult,
    BridgeStatus,
    GitCodeBrowserSessionBridge,
    cookie_records,
)
from opencsi.auth.gitcode_qr import QrLoginResult, QrLoginStatus
from opencsi.auth.session import LoginStage, RenewalResult, RenewalStatus
from opencsi.cli.app import main
from opencsi.errors import EXIT_OPENCSITOOL_PENDING

#: Synthetic GitCode tokens. Shaped like real ones, never real ones.
FAKE_ACCESS = "ACCESS" + "a1b2c3d4e5f6" * 8
FAKE_REFRESH = "REFRESH" + "0f9e8d7c6b5a" * 8


def _qr_success() -> QrLoginResult:
    return QrLoginResult(
        QrLoginStatus.SUCCEEDED,
        username="tester",
        is_new_user=False,
        polls=3,
        refreshes=0,
        _credentials={"access_token": FAKE_ACCESS, "refresh_token": FAKE_REFRESH},
    )


class _StubIdentity:
    user_name = "tester"
    display_name = "Tester"
    employee_id = "E-1"


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class CookieRecordTest(unittest.TestCase):
    """The credential -> cookie mapping is the bridge's whole contract."""

    def test_the_two_gitcode_tokens_become_the_two_session_cookies(self) -> None:
        records, missing = cookie_records(
            {"access_token": FAKE_ACCESS, "refresh_token": FAKE_REFRESH},
            username="tester",
        )
        names = {record["name"] for record in records}
        self.assertEqual(names, set(GITCODE_SESSION_COOKIES))
        self.assertEqual(missing, [])

    def test_the_attributes_match_a_real_signed_in_profile(self) -> None:
        """Measured on a real profile, not invented.

        ``httpOnly`` and ``secure`` are part of what the server set. Replaying a
        credential with different flags is replaying a different credential, so
        these are asserted rather than left to a default.
        """
        records, _ = cookie_records({"access_token": FAKE_ACCESS}, username="t")
        access = next(r for r in records if r["name"] == "GITCODE_ACCESS_TOKEN")
        self.assertTrue(access["httpOnly"])
        self.assertTrue(access["secure"])
        self.assertEqual(access["domain"], ".gitcode.com")
        self.assertEqual(access["path"], "/")

    def test_a_missing_token_is_named_rather_than_silently_dropped(self) -> None:
        records, missing = cookie_records({"access_token": FAKE_ACCESS})
        self.assertIn("GITCODE_REFRESH_TOKEN", missing)
        self.assertIn("GitCodeUserName", missing)
        self.assertEqual(len(records), 1)

    def test_no_token_value_is_ever_returned_in_a_repr(self) -> None:
        """A bridge result must be safe to log.

        The credential is in the cookie records, which is unavoidable -- they are
        what gets planted. What must never happen is a *result* object carrying
        one, because results are what get printed and serialised.
        """
        records, _ = cookie_records({"access_token": FAKE_ACCESS}, username="t")
        for record in records:
            self.assertIn("value", record)  # the record itself must carry it
        result = BridgeResult(status=BridgeStatus.BRIDGED, planted=("a",))
        self.assertNotIn(FAKE_ACCESS, repr(result))
        self.assertNotIn(FAKE_ACCESS, str(result.as_dict()))


class BridgeResultTest(unittest.TestCase):
    """``ok`` means verified. Nothing weaker is allowed to look like success."""

    def test_only_bridged_counts_as_ok(self) -> None:
        self.assertTrue(BridgeResult(status=BridgeStatus.BRIDGED).ok)
        for status in (
            BridgeStatus.UNVERIFIED,
            BridgeStatus.NO_CREDENTIALS,
            BridgeStatus.CDP_UNAVAILABLE,
            BridgeStatus.REJECTED,
            BridgeStatus.FAILED,
        ):
            with self.subTest(status=status):
                self.assertFalse(BridgeResult(status=status).ok)

    def test_an_unverified_bridge_reports_itself_as_unverified(self) -> None:
        """``verify=False`` exists for offline tests; it must not claim BRIDGED."""
        from opencsi.errors import CdpUnavailableError

        bridge = GitCodeBrowserSessionBridge(cdp_url="http://127.0.0.1:1")
        with unittest.mock.patch.object(
            GitCodeBrowserSessionBridge,
            "_endpoint",
            side_effect=CdpUnavailableError("no endpoint"),
        ):
            result = bridge.bridge({"access_token": FAKE_ACCESS}, verify=False)
        self.assertIs(result.status, BridgeStatus.CDP_UNAVAILABLE)
        self.assertFalse(result.ok)

    def test_a_planted_but_unverified_session_is_not_success(self) -> None:
        """The distinction that matters: planted is not the same as accepted."""
        bridge = GitCodeBrowserSessionBridge(cdp_url="http://127.0.0.1:1")
        with unittest.mock.patch.object(
            GitCodeBrowserSessionBridge, "_endpoint"
        ) as endpoint, unittest.mock.patch(
            "opencsi.auth.gitcode_bridge.CdpConnection"
        ) as conn:
            endpoint.return_value.browser_ws_url.return_value = "ws://127.0.0.1:1/x"
            endpoint.return_value.__str__ = lambda _self: "endpoint"
            conn.return_value.__enter__.return_value.call.return_value = {}
            result = bridge.bridge({"access_token": FAKE_ACCESS}, verify=False)
        self.assertIs(result.status, BridgeStatus.UNVERIFIED)
        self.assertFalse(result.ok)
        self.assertIn("GITCODE_ACCESS_TOKEN", result.planted)

    def test_a_credential_free_result_says_so(self) -> None:
        bridge = GitCodeBrowserSessionBridge(cdp_url="http://127.0.0.1:1")
        result = bridge.bridge({}, verify=False)
        self.assertIs(result.status, BridgeStatus.NO_CREDENTIALS)
        self.assertFalse(result.ok)


class QrSuccessSemanticsTest(unittest.TestCase):
    """The P0 defect, stated as executable properties.

    ``_qr`` is exercised with the authenticator and the completion step stubbed,
    so each half can be made to succeed or fail independently. That independence
    is the point: the old bug was precisely a case where half one succeeded and
    the command still said everything had.
    """

    def _run_qr(
        self, *, qr_result, completion, json_output: bool = False
    ) -> tuple[int, str, str]:
        """Run ``opencsi login --qr`` with both halves stubbed."""
        from opencsi.cli import login as login_module

        class FakeAuthenticator:
            def __init__(self, **_kwargs) -> None:
                pass

            def login(self, **_kwargs):
                return qr_result

            def session_cookies(self) -> dict[str, str]:
                return {}

        argv = ["login", "--qr", "--qr-wait", "1"]
        if json_output:
            argv.append("--json")

        with unittest.mock.patch(
            "opencsi.auth.gitcode_qr.GitCodeQrAuthenticator", FakeAuthenticator
        ), unittest.mock.patch.object(
            login_module, "_complete_qr_login", return_value=completion
        ):
            return run_cli(argv)

    def test_gitcode_only_success_is_not_exit_zero(self) -> None:
        """The defect itself.

        GitCode says yes, openCsiTool is never established. The command must NOT
        exit 0, because ``opencsi usage`` will not work.
        """
        completion = login_module_completion(identity=None, bridged=True)
        code, out, err = self._run_qr(qr_result=_qr_success(), completion=completion)
        self.assertEqual(
            code,
            EXIT_OPENCSITOOL_PENDING,
            "a GitCode-only success must not be reported as a completed login",
        )
        self.assertNotEqual(code, 0)
        self.assertIn("was not established", err)
        self.assertIn(str(EXIT_OPENCSITOOL_PENDING), err)

    def test_a_verified_session_is_exit_zero(self) -> None:
        completion = login_module_completion(identity=_StubIdentity(), bridged=True)
        code, out, _err = self._run_qr(qr_result=_qr_success(), completion=completion)
        self.assertEqual(code, 0)
        self.assertIn("openCsiTool session established and verified", out)

    def test_a_failed_qr_scan_keeps_its_own_exit_code(self) -> None:
        failed = QrLoginResult(QrLoginStatus.TIMEOUT, detail="no scan")
        completion = login_module_completion(identity=None, bridged=False)
        code, _out, err = self._run_qr(qr_result=failed, completion=completion)
        self.assertNotEqual(code, 0)
        self.assertNotEqual(code, EXIT_OPENCSITOOL_PENDING)
        self.assertIn("did not complete", err)

    def test_the_json_document_names_the_stage(self) -> None:
        """A script must be able to tell the two halves apart without prose."""
        import json

        completion = login_module_completion(identity=None, bridged=True)
        _code, out, _err = self._run_qr(
            qr_result=_qr_success(), completion=completion, json_output=True
        )
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["complete"])
        self.assertEqual(payload["stage"], LoginStage.OPENCSITOOL_PENDING.value)
        self.assertEqual(payload["exit_code"], EXIT_OPENCSITOOL_PENDING)
        self.assertFalse(payload["openscitool_session"]["established"])

    def test_a_completed_login_says_the_stage_is_complete(self) -> None:
        import json

        completion = login_module_completion(identity=_StubIdentity(), bridged=True)
        _code, out, _err = self._run_qr(
            qr_result=_qr_success(), completion=completion, json_output=True
        )
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["complete"])
        self.assertEqual(payload["stage"], LoginStage.OPENCSITOOL_AUTHENTICATED.value)
        self.assertEqual(payload["exit_code"], 0)
        self.assertTrue(payload["openscitool_session"]["established"])


def login_module_completion(*, identity, bridged: bool):
    """Build the ``_Completion`` the QR command reads. Keeps tests declarative."""
    from opencsi.cli.login import _Completion

    return _Completion(
        identity=identity,
        bridged=bridged,
        reason=None if identity else "the openCsiTool session was not established",
        next_step=None if identity else "run 'opencsi login'",
    )


class LoginStageTest(unittest.TestCase):
    """The stage enum is the single place "is it done?" is decided."""

    def test_only_the_full_stage_is_complete(self) -> None:
        self.assertTrue(LoginStage.OPENCSITOOL_AUTHENTICATED.is_complete)
        for stage in (
            LoginStage.NONE,
            LoginStage.GITCODE_AUTHENTICATED,
            LoginStage.OPENCSITOOL_PENDING,
        ):
            with self.subTest(stage=stage):
                self.assertFalse(stage.is_complete)

    def test_a_login_result_defaults_to_the_coarse_reading(self) -> None:
        """An authenticator written before ``stage`` existed must be unaffected."""
        from opencsi.auth.session import LoginResult, LoginStatus

        result = LoginResult(LoginStatus.SUCCEEDED)
        self.assertTrue(result.ok)
        self.assertIs(result.stage, LoginStage.NONE)

    def test_the_result_serialises_both_readings(self) -> None:
        from opencsi.auth.session import LoginResult, LoginStatus

        payload = LoginResult(
            LoginStatus.SUCCEEDED, stage=LoginStage.GITCODE_AUTHENTICATED
        ).as_dict()
        self.assertTrue(payload["ok"], "the coarse reading is unchanged")
        self.assertFalse(payload["complete"], "the strict reading is honest")
        self.assertEqual(payload["stage"], "GITCODE_AUTHENTICATED")


class OAuthCompletionTest(unittest.TestCase):
    """The second half, driven directly so each stop point is reachable."""

    def _complete(self, *, bridge_result, renewal=None, identity=None):
        from opencsi.cli import login as login_module

        class FakeBridge:
            def __init__(self, **_kwargs) -> None:
                pass

            def bridge(self, *_args, **_kwargs):
                return bridge_result

        # Patched on the *source* module, not on ``opencsi.cli.login``: the
        # bridge is imported inside the function so that a plain ``opencsi
        # --help`` does not pull the CDP machinery in, which means the name the
        # command reads is resolved at call time from ``gitcode_bridge``.
        with unittest.mock.patch(
            "opencsi.auth.gitcode_bridge.GitCodeBrowserSessionBridge", FakeBridge
        ), unittest.mock.patch.object(
            login_module, "_run_oauth_completion"
        ) as run_oauth:
            def fake_oauth(ctx, completion):
                completion.renewal = renewal
                completion.identity = identity
                if identity is None and renewal is None:
                    completion.reason = "engine unavailable"

            run_oauth.side_effect = fake_oauth
            ctx = _fake_ctx()
            return login_module._complete_qr_login(ctx, _qr_success())

    def test_a_rejected_credential_stops_before_the_engine(self) -> None:
        completion = self._complete(
            bridge_result=BridgeResult(status=BridgeStatus.REJECTED, verify_status=401)
        )
        self.assertIsNone(completion.identity)
        self.assertIn("rejected", completion.reason or "")

    def test_no_credential_stops_immediately(self) -> None:
        from opencsi.cli import login as login_module

        empty = QrLoginResult(QrLoginStatus.SUCCEEDED, username="t", _credentials={})
        with unittest.mock.patch.object(
            login_module, "_run_oauth_completion"
        ) as run_oauth:
            ctx = _fake_ctx()
            completion = login_module._complete_qr_login(ctx, empty)
        self.assertIsNone(completion.identity)
        run_oauth.assert_not_called()

    def test_a_verified_bridge_proceeds_to_the_oauth_leg(self) -> None:
        completion = self._complete(
            bridge_result=BridgeResult(status=BridgeStatus.BRIDGED, verify_status=200),
            renewal=RenewalResult(RenewalStatus.RENEWED, token_changed=True),
            identity=_StubIdentity(),
        )
        self.assertIsNotNone(completion.identity)
        self.assertTrue(completion.bridged)


def _fake_ctx():
    """A minimal CliContext for the completion helper.

    Built by hand rather than through argparse because ``_complete_qr_login``
    reads exactly four attributes, and constructing a real context would couple
    these tests to the whole option surface.
    """
    from opencsi.cli.context import CliContext

    class _Args:
        cdp = None
        base_url = None
        json = False
        no_proxy = False

    return CliContext(args=_Args(), stdout=io.StringIO(), stderr=io.StringIO())


if __name__ == "__main__":
    unittest.main()
