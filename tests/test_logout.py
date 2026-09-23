"""``opencsi logout``: clearing local credentials, and what it must not do.

The verb is small, and the decisions in it are not:

* it must not revoke anything remotely. "Log out on this laptop" is not "invalidate
  every session I have", and a tool that reaches the network when asked to forget
  something locally cannot be used offline with confidence.
* the default must keep the GitCode credential. That half is what makes signing
  back in need no QR scan, and a user who wants their session gone today almost
  never wants to re-scan tomorrow.
* a store that cannot be written must be reported, not reported as success --
  claiming credentials are gone while the file is still there is the worst
  possible answer to this question.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401  (imported for its sys.path side effect)

from opencsi.auth.store import (
    CredentialBundle,
    CredentialStoreError,
    StoredGitCodeCredential,
    StoredOpenCsiCredential,
)


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    """Run the CLI in-process, capturing stdout and stderr.

    Defined here rather than shared, matching every other test module in this
    suite: the capture has to wrap the same call the real entry point makes, and
    a shared version would hide which entry point a test was exercising.
    """
    from opencsi.cli.app import main

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(argv)
        except SystemExit as exc:  # argparse exits on a usage error
            code = int(exc.code or 0)
    return code, out.getvalue(), err.getvalue()

ACCESS = "gitcode-access-token-abcdefghijklmnop"
REFRESH = "gitcode-refresh-token-abcdefghijklmnop"
SESSION = "opencsi-session-token-abcdefghijklmnop"


def a_bundle() -> CredentialBundle:
    return CredentialBundle(
        gitcode=StoredGitCodeCredential(
            access_token=ACCESS, refresh_token=REFRESH, username="alice"
        ),
        opencsi=StoredOpenCsiCredential(token=SESSION),
    )


class _FakeStore:
    """A store that records what was asked of it."""

    name = "fake"

    def __init__(self, *, fail: bool = False) -> None:
        self.bundle = a_bundle()
        self.calls: list[str] = []
        self.path = Path("C:/fake/credentials.dat")
        self._fail = fail

    def load(self):
        return self.bundle

    def _maybe_fail(self) -> None:
        if self._fail:
            raise CredentialStoreError("the credential file could not be written")

    def clear_opencsi(self) -> None:
        self.calls.append("clear_opencsi")
        self._maybe_fail()
        self.bundle = self.bundle.with_opencsi(None)

    def clear_gitcode(self) -> None:
        self.calls.append("clear_gitcode")
        self._maybe_fail()

    def clear_all(self) -> None:
        self.calls.append("clear_all")
        self._maybe_fail()
        self.bundle = CredentialBundle()

    def save_gitcode(self, credential) -> None:  # pragma: no cover - protocol
        self.calls.append("save_gitcode")

    def save_opencsi(self, credential) -> None:  # pragma: no cover - protocol
        self.calls.append("save_opencsi")

    def status(self):  # pragma: no cover - protocol
        raise NotImplementedError


def run_logout(store, *args: str) -> tuple[int, str, str]:
    """Run ``opencsi logout`` with the store stubbed."""
    from opencsi.cli import logout as logout_module

    with mock.patch(
        "opencsi.auth.windows_store.open_default_store", return_value=store
    ):
        return run_cli(["logout", *args])


class LogoutTest(unittest.TestCase):
    """The verb's contract."""

    def test_the_default_clears_only_the_session(self) -> None:
        """Keeping the GitCode half is what avoids a second QR scan."""
        store = _FakeStore()
        code, out, _err = run_logout(store)
        self.assertEqual(code, 0)
        self.assertIn("clear_opencsi", store.calls)
        self.assertNotIn("clear_all", store.calls)
        self.assertIsNotNone(store.load().gitcode)
        self.assertIsNone(store.load().opencsi)
        self.assertIn("local credentials cleared", out)

    def test_the_default_says_the_gitcode_credential_was_kept(self) -> None:
        """The user must be told which command works without a scan."""
        store = _FakeStore()
        _code, out, _err = run_logout(store)
        self.assertIn("GitCode credential was kept", out)

    def test_forget_gitcode_clears_everything(self) -> None:
        store = _FakeStore()
        code, _out, _err = run_logout(store, "--forget-gitcode")
        self.assertEqual(code, 0)
        self.assertIn("clear_all", store.calls)
        self.assertTrue(store.load().empty)

    def test_all_is_the_same_as_forget_gitcode(self) -> None:
        store = _FakeStore()
        run_logout(store, "--all")
        self.assertIn("clear_all", store.calls)

    def test_a_failed_clear_is_not_reported_as_success(self) -> None:
        """The worst possible answer here is a false "cleared"."""
        store = _FakeStore(fail=True)
        code, out, err = run_logout(store)
        self.assertNotEqual(code, 0)
        self.assertIn("could not be cleared", err)
        self.assertNotIn("local credentials cleared", out)

    def test_no_store_is_not_an_error(self) -> None:
        """On a platform with no store there is nothing to remove.

        A user asking to forget something that cannot exist is already in the
        state they asked for, so a failure code would be wrong.
        """
        code, out, _err = run_logout(None)
        self.assertEqual(code, 0)
        self.assertIn("cleared", out)

    def test_no_store_with_no_store_flag_is_a_usage_error(self) -> None:
        store = _FakeStore()
        code, _out, err = run_logout(store, "--no-store")
        self.assertNotEqual(code, 0)
        self.assertIn("no stored credential to clear", err)

    def test_the_json_document_reports_the_scope(self) -> None:
        import json as _json

        store = _FakeStore()
        _code, out, _err = run_logout(store, "--json")
        payload = _json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["cleared"])
        self.assertTrue(payload["gitcode_credential_kept"])

    def test_json_after_forget_all_says_the_credential_is_gone(self) -> None:
        import json as _json

        store = _FakeStore()
        _code, out, _err = run_logout(store, "--all", "--json")
        payload = _json.loads(out)
        self.assertFalse(payload["gitcode_credential_kept"])

    def test_the_output_contains_no_credential(self) -> None:
        store = _FakeStore()
        _code, out, err = run_logout(store, "--json")
        for secret in (ACCESS, REFRESH, SESSION):
            self.assertNotIn(secret, out)
            self.assertNotIn(secret, err)


class NoRemoteRevocationTest(unittest.TestCase):
    """The property the module docstring promises, asserted rather than stated."""

    def test_logout_makes_no_network_call(self) -> None:
        """Forgetting something locally must work offline.

        Checked by making every transport constructor raise: if the command
        reached for the network, this would fail rather than pass silently.
        """
        from opencsi.cli import logout as logout_module

        with mock.patch(
            "opencsi.auth.windows_store.open_default_store", return_value=_FakeStore()
        ), mock.patch(
            "opencsi.transport.HttpTransport",
            side_effect=AssertionError("logout opened a transport"),
            create=True,
        ):
            code, _out, _err = run_cli(["logout"])
        self.assertEqual(code, 0)

    def test_the_module_does_not_mention_a_revoke_endpoint(self) -> None:
        """A guard against one being added quietly later."""
        import inspect

        from opencsi.cli import logout as logout_module

        source = inspect.getsource(logout_module).lower()
        for forbidden in ("revoke", "logout_url", "/oauth/revoke"):
            # The docstring explains why revocation is absent, so the word may
            # appear in prose -- but never in a call.
            self.assertNotIn(f"{forbidden}(", source.replace(" ", ""))


@unittest.skipUnless(sys.platform == "win32", "the DPAPI store is Windows-only")
class LogoutAgainstTheRealStoreTest(unittest.TestCase):
    """The same contract over a real DPAPI file, end to end."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "credentials.dat"
        from opencsi.auth.windows_store import DpapiCredentialStore

        self.store = DpapiCredentialStore(self.path)
        self.store.save_gitcode(a_bundle().gitcode)
        self.store.save_opencsi(a_bundle().opencsi)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_the_default_leaves_the_gitcode_half_on_disk(self) -> None:
        from opencsi.auth.windows_store import DpapiCredentialStore

        code, _out, _err = run_logout(self.store)
        self.assertEqual(code, 0)
        bundle = DpapiCredentialStore(self.path).load()
        self.assertIsNone(bundle.opencsi)
        self.assertIsNotNone(bundle.gitcode)
        self.assertEqual(bundle.gitcode.refresh_token, REFRESH)

    def test_all_removes_the_file(self) -> None:
        code, _out, _err = run_logout(self.store, "--all")
        self.assertEqual(code, 0)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
