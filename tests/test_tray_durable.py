"""The tray's normal path is the secure store, not a browser.

The defect these tests pin, stated plainly: the tray's QR sign-in obtained a real
session and then handed it to nobody. ``GitCodeCookieSource`` and
``HttpOAuthRenewer`` each held it in their own memory, while the
``MonitorService`` went on reading the provider it was constructed with -- which
had never seen the new credential. The tray therefore reported a successful scan
and then showed nothing, and a restart started over.

The fix is a shared durable store rather than reaching into the monitor's
internals. Two properties follow, and both are tested here because both are
load-bearing:

1. ``_sign_in_qr`` writes both halves to the store.
2. ``MonitorService._maybe_recover_browser`` does not start a browser when the
   store already holds a credential -- the "Tray -> must start Chrome first"
   dependency this round removes.
"""

from __future__ import annotations

import sys
import unittest
from unittest import mock

import helpers  # noqa: F401  (imported for its sys.path side effect)

from opencsi.auth.store import (
    CredentialBundle,
    MemoryCredentialStore,
    StoredGitCodeCredential,
    StoredOpenCsiCredential,
)
from opencsi.auth.stored import (
    CompositeCredentialProvider,
    StoredOpenCsiCredentialProvider,
)

ACCESS = "gitcode-access-token-abcdefghijklmnop"
REFRESH = "gitcode-refresh-token-abcdefghijklmnop"
SESSION = "opencsi-session-token-abcdefghijklmnop"


def a_service(*, provider=None, session=None, **cfg):
    """A MonitorService with the smallest client that will do.

    ``cfg`` is forwarded to ``MonitorConfig``. Tests that assert a recovery was
    *attempted* must pass ``auto_recover_auth_host=True``: both browser
    recoveries are opt-in since §9.2, so without it nothing is ever tried and the
    assertion would pass for the wrong reason -- the wrong reason being the very
    bug this file exists to catch.
    """
    from opencsi.monitor import MonitorConfig, MonitorService

    class _Client:
        def __init__(self) -> None:
            self.provider = provider
            self.session = session
            self.last_renewal = None

        def get_my_tools(self, *args, **kwargs):
            raise RuntimeError("no network in this test")

    return MonitorService(_Client(), config=MonitorConfig(**cfg))


class MonitorDoesNotStartABrowserWhenCredentialIsStoredTest(unittest.TestCase):
    """Phase 9: the durable store makes the browser recovery unnecessary."""

    def _service_with_store(self, store, **cfg) -> object:
        provider = StoredOpenCsiCredentialProvider(store, ttl=0.0)

        class _Session:
            credentials = provider

        return a_service(provider=provider, session=_Session(), **cfg)

    def test_a_stored_session_suppresses_browser_recovery(self) -> None:
        """The headline property: no Chrome, no Edge, no auth host.

        Opt-in is *also* enabled here, and that is the point: even with the user's
        permission to start a browser, a stored credential means one is not
        needed. Asserting this with recovery disabled would test the config
        default rather than the store check.
        """
        store = MemoryCredentialStore(
            CredentialBundle(opencsi=StoredOpenCsiCredential(token=SESSION))
        )
        service = self._service_with_store(store, auto_recover_auth_host=True)
        with mock.patch(
            "opencsi.monitor.service.MonitorService._try_auth_host"
        ) as host:
            self.assertFalse(service._maybe_recover_browser())
            host.assert_not_called()

    def test_an_empty_store_still_permits_recovery(self) -> None:
        """The migration path must keep working.

        A user who is signed in through a browser profile and has never scanned
        has nothing in the store, so the browser path is exactly what should run --
        provided they opted in (§9.2). Suppressing it would break the upgrade.
        """
        store = MemoryCredentialStore()
        service = self._service_with_store(store, auto_recover_auth_host=True)
        with mock.patch(
            "opencsi.monitor.service.MonitorService._try_auth_host", return_value=True
        ) as host:
            self.assertTrue(service._maybe_recover_browser())
            host.assert_called_once()

    def test_an_unreadable_store_does_not_suppress_recovery(self) -> None:
        """A broken store must not strand the tray with no attempt made.

        Failing towards "try the browser" is the safe direction: the browser path
        may well work, whereas claiming a credential exists when it cannot be
        read leaves the tray on a permanent error.
        """
        from opencsi.auth.store import CredentialStoreError

        class _Broken:
            name = "broken"

            def load(self):
                raise CredentialStoreError("unreadable")

            def save_opencsi(self, credential):  # pragma: no cover
                raise CredentialStoreError("unreadable")

            def save_gitcode(self, credential):  # pragma: no cover
                raise CredentialStoreError("unreadable")

        store = _Broken()
        service = self._service_with_store(store, auto_recover_auth_host=True)
        with mock.patch(
            "opencsi.monitor.service.MonitorService._try_auth_host", return_value=True
        ) as host:
            self.assertTrue(service._maybe_recover_browser())
            host.assert_called_once()

    def test_an_empty_store_does_not_launch_a_browser_by_itself(self) -> None:
        """§9.2, asserted where it matters: an empty store alone starts nothing.

        The test above shows recovery is *permitted* when the store is empty; this
        one shows it is not *performed* without opt-in. Both are needed, because
        "empty store" is exactly the post-reboot case and the whole point of the
        change is that it no longer opens Chromium by itself.
        """
        service = self._service_with_store(MemoryCredentialStore())
        with mock.patch(
            "opencsi.monitor.service.MonitorService._try_auth_host"
        ) as host:
            self.assertFalse(service._maybe_recover_browser())
            host.assert_not_called()

    def test_a_composite_provider_is_looked_into(self) -> None:
        """A composite hides which source answered, so the check must recurse.

        Otherwise a stored credential behind a composite would be invisible and
        the browser would be started anyway -- which is the bug, one layer down.
        """
        store = MemoryCredentialStore(
            CredentialBundle(opencsi=StoredOpenCsiCredential(token=SESSION))
        )
        stored = StoredOpenCsiCredentialProvider(store, ttl=0.0)
        composite = CompositeCredentialProvider([stored])

        class _Session:
            credentials = composite

        service = a_service(
            provider=composite, session=_Session(), auto_recover_auth_host=True
        )
        with mock.patch(
            "opencsi.monitor.service.MonitorService._try_auth_host"
        ) as host:
            self.assertFalse(service._maybe_recover_browser())
            host.assert_not_called()

    def test_a_browser_provider_does_not_suppress_recovery(self) -> None:
        """The check is specifically about a *durable* credential.

        A browser provider is not one, and treating it as though it were would
        skip the recovery the tray needs when that browser has gone away.
        """
        provider = mock.Mock()
        provider.name = "cdp"
        provider.peek_token.return_value = "some-token"

        class _Session:
            credentials = provider

        service = a_service(
            provider=provider, session=_Session(), auto_recover_auth_host=True
        )
        with mock.patch(
            "opencsi.monitor.service.MonitorService._try_auth_host", return_value=True
        ) as host:
            self.assertTrue(service._maybe_recover_browser())
            host.assert_called_once()

    def test_no_session_object_does_not_suppress_recovery(self) -> None:
        service = a_service(
            provider=None, session=None, auto_recover_auth_host=True
        )
        with mock.patch(
            "opencsi.monitor.service.MonitorService._try_auth_host", return_value=True
        ) as host:
            self.assertTrue(service._maybe_recover_browser())
            host.assert_called_once()

    def test_the_probe_never_raises(self) -> None:
        """It runs on the worker thread; a throwing probe would kill the tick."""
        provider = mock.Mock()
        provider.name = "secure-store"
        provider.peek_token.side_effect = RuntimeError("boom")
        provider.get_token.side_effect = RuntimeError("boom")

        class _Session:
            credentials = provider

        service = a_service(provider=provider, session=_Session())
        # Must not raise. Whether it returns True or False is secondary to that.
        self.assertIn(service._maybe_recover_browser(), (True, False))


class TraySignInPersistenceTest(unittest.TestCase):
    """_sign_in_qr must write the credential somewhere that outlives it."""

    def _run_sign_in_qr(self, store):
        """Drive _sign_in_qr with the QR flow and network stubbed."""
        from opencsi.auth import gitcode_qr
        from opencsi.auth.session import RenewalResult, RenewalStatus
        from opencsi.cli import tray as tray_cli

        class _Result:
            ok = True
            username = "alice"
            status = mock.Mock(value="SUCCESS")

            def credentials(self):
                return {"access_token": ACCESS, "refresh_token": REFRESH}

        class _Authenticator:
            def __init__(self, *a, **k):
                pass

            def login(self, **k):
                return _Result()

        class _Source:
            name = "qr-credentials"

            def __init__(self, **kwargs):
                self._token = SESSION
                self.cookie_names = ("GITCODE_ACCESS_TOKEN", "GITCODE_REFRESH_TOKEN")

            def get_token(self):
                return self._token

            def read_all_cookies(self, **kwargs):
                return []

        class _Renewer:
            name = "http-oauth"
            last_trace = None

            def __init__(self, *a, **k):
                pass

            def renew(self, **k):
                return RenewalResult(RenewalStatus.RENEWED, renewed=True, expires_in=3600)

        class _Snapshot:
            has_data = True

        class _Service:
            snapshot = _Snapshot()

            def refresh_now(self, **kwargs):
                return self.snapshot

        with mock.patch.object(
            gitcode_qr, "GitCodeQrAuthenticator", _Authenticator
        ), mock.patch(
            "opencsi.auth.http_oauth.GitCodeCookieSource", _Source
        ), mock.patch(
            "opencsi.auth.http_oauth.HttpOAuthRenewer", _Renewer
        ), mock.patch.object(
            tray_cli, "_open_store", return_value=store
        ):
            tray_cli._sign_in_qr(_Service())

    def test_the_gitcode_credential_is_stored(self) -> None:
        """Without this, the next expiry needs another scan."""
        store = MemoryCredentialStore()
        self._run_sign_in_qr(store)
        self.assertIsNotNone(store.load().gitcode)
        self.assertEqual(store.load().gitcode.refresh_token, REFRESH)

    def test_the_session_is_stored(self) -> None:
        """The tray's sign-in must survive the tray exiting."""
        store = MemoryCredentialStore()
        self._run_sign_in_qr(store)
        self.assertIsNotNone(store.load().opencsi)
        self.assertEqual(store.load().opencsi.token, SESSION)

    def test_both_halves_reach_the_store(self) -> None:
        store = MemoryCredentialStore()
        self._run_sign_in_qr(store)
        self.assertEqual(store.writes, 2, "both halves should be written separately")

    def test_the_stored_username_is_kept(self) -> None:
        store = MemoryCredentialStore()
        self._run_sign_in_qr(store)
        self.assertEqual(store.load().gitcode.username, "alice")

    def test_no_store_does_not_break_the_sign_in(self) -> None:
        """A platform with no secure store must still sign in for this run.

        Reporting the session as failed because it could not be saved would
        discard a success that is real, just not durable.
        """
        self._run_sign_in_qr(None)

    def test_a_store_that_fails_to_write_does_not_crash_the_tray(self) -> None:
        class _Failing:
            def save_gitcode(self, credential):
                raise OSError("disk full")

            def save_opencsi(self, credential):
                raise OSError("disk full")

        self._run_sign_in_qr(_Failing())

    def test_the_store_is_not_written_when_a_scan_fails(self) -> None:
        """A cancelled scan must leave nothing behind.

        Saving before knowing the scan succeeded would overwrite a working
        credential with a cancelled attempt.
        """
        from opencsi.auth import gitcode_qr
        from opencsi.cli import tray as tray_cli

        class _Result:
            ok = False
            username = None
            status = mock.Mock(value="CANCELLED")

            def credentials(self):
                return {}

        class _Authenticator:
            def __init__(self, *a, **k):
                pass

            def login(self, **k):
                return _Result()

        store = MemoryCredentialStore()
        with mock.patch.object(
            gitcode_qr, "GitCodeQrAuthenticator", _Authenticator
        ), mock.patch.object(tray_cli, "_open_store", return_value=store):
            tray_cli._sign_in_qr(mock.Mock())
        self.assertEqual(store.writes, 0)


class ServiceCredentialInvalidationTest(unittest.TestCase):
    """Telling the service's provider to re-read, without patching internals."""

    def test_it_calls_invalidate_on_the_service_provider(self) -> None:
        from opencsi.cli.tray import _invalidate_service_credential

        provider = mock.Mock()

        class _Session:
            credentials = provider

        service = mock.Mock()
        service._client.session = _Session()
        _invalidate_service_credential(service)
        provider.invalidate.assert_called_once()

    def test_it_does_not_assign_the_provider(self) -> None:
        """The rule this round exists to enforce.

        Assigning ``service._client.provider = ...`` patches a private field,
        races the worker thread, and leaves the stored credential absent -- so the
        next process would start over anyway. The shared store is the mechanism.

        Checked by parsing the function's AST rather than by searching its text:
        the docstring names the pattern in order to explain why it is *not* used,
        so a substring search would report the explanation as the offence.
        """
        import ast
        import inspect
        import textwrap

        from opencsi.cli import tray as tray_cli

        tree = ast.parse(textwrap.dedent(inspect.getsource(tray_cli._sign_in_qr)))
        assignments: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                assignments.append(ast.unparse(target))
        offenders = [t for t in assignments if "_client" in t or "provider =" in t]
        self.assertEqual(
            offenders,
            [],
            f"the tray assigns into the client's internals: {offenders}",
        )

    def test_the_tray_writes_the_store_instead(self) -> None:
        """The positive form of the same rule."""
        import inspect

        from opencsi.cli import tray as tray_cli

        source = inspect.getsource(tray_cli._sign_in_qr)
        self.assertIn("_open_store", source)
        self.assertIn("save_gitcode", source)
        self.assertIn("save_opencsi", source)

    def test_it_never_raises(self) -> None:
        from opencsi.cli.tray import _invalidate_service_credential

        service = mock.Mock()
        service._client.session.credentials.invalidate.side_effect = RuntimeError("x")
        _invalidate_service_credential(service)  # must not raise

    def test_a_service_without_a_provider_is_survivable(self) -> None:
        from opencsi.cli.tray import _invalidate_service_credential

        _invalidate_service_credential(object())


class OpenStoreTest(unittest.TestCase):
    """Which store the tray uses, and that it has no weak fallback."""

    def test_off_windows_there_is_no_store(self) -> None:
        from opencsi.cli.tray import _open_store

        if sys.platform == "win32":
            store = _open_store()
            self.assertIsNotNone(store)
            self.assertEqual(store.name, "dpapi")
        else:
            self.assertIsNone(_open_store())

    def test_a_failure_to_construct_the_store_yields_none(self) -> None:
        from opencsi.cli import tray as tray_cli

        with mock.patch(
            "opencsi.auth.windows_store.open_default_store",
            side_effect=OSError("no"),
        ):
            self.assertIsNone(tray_cli._open_store())


if __name__ == "__main__":
    unittest.main()
