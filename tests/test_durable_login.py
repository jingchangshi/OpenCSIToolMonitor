"""The cross-process guarantee: login in one process, use in the next.

This is the test the architecture exists for. Everything else about browserless
authentication was already working -- ``HttpOAuthRenewer`` proved the OAuth leg
runs over plain HTTP, and the QR flow proved a credential can be obtained without
a browser. What was missing was that the credential died with the process that
created it, so ``opencsi login --qr`` followed by ``opencsi usage`` in a *new*
terminal did not work.

How "a new process" is simulated
--------------------------------
A new :class:`~opencsi.auth.store.DpapiCredentialStore`, provider and client, all
constructed after the first set has been discarded. That is precisely what a
second process does differently: it re-opens the file, re-decrypts it, and builds
fresh objects with no shared memory. Nothing is passed between the two halves --
the only channel is the bytes on disk, which is the property under test.

A real ``dist\\opencsi.exe`` run in two terminals is the Phase 10 acceptance test
and cannot be performed here (it needs a WeChat scan). What this file establishes
is that the *mechanism* is sound, so a failure in the live test localises to the
scan rather than to persistence.
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from opencsi.auth.store import (
    CredentialBundle,
    MemoryCredentialStore,
    StoredGitCodeCredential,
    StoredOpenCsiCredential,
)
from opencsi.auth.stored import (
    ACCESS_COOKIE,
    REFRESH_COOKIE,
    StoredGitCodeCredentialSource,
    StoredOpenCsiCredentialProvider,
)

ACCESS = "gitcode-access-token-abcdefghijklmnop"
REFRESH = "gitcode-refresh-token-abcdefghijklmnop"
SESSION = "opencsi-session-token-abcdefghijklmnop"


class ProcessAResult:
    """What a login process leaves behind: the store, and nothing else.

    Modelled explicitly so the test cannot accidentally keep a live object from
    process A and satisfy process B from memory. The class holds only the *path*,
    never a provider or a credential.
    """

    def __init__(self, path: Path) -> None:
        self.path = path


def process_a_login(path: Path) -> ProcessAResult:
    """A stand-in for ``opencsi login --qr``, up to the point of persisting.

    It does what :func:`opencsi.cli.login._complete_qr_login` does with the
    credential: writes the GitCode half as soon as the scan succeeds, then the
    openCsiTool half once the server has confirmed it. The network legs are not
    reproduced -- the server cannot be asked to mint a session on demand -- so
    what is exercised is the persistence contract those legs feed into.
    """
    from opencsi.auth.windows_store import DpapiCredentialStore

    store = DpapiCredentialStore(path)
    store.save_gitcode(
        StoredGitCodeCredential(
            access_token=ACCESS, refresh_token=REFRESH, username="alice"
        )
    )
    store.save_opencsi(
        StoredOpenCsiCredential(token=SESSION, expires_at=time.time() + 3600)
    )
    # Everything process A built is dropped here. Only the path crosses.
    del store
    return ProcessAResult(path)


@unittest.skipUnless(sys.platform == "win32", "the durable store is DPAPI-backed")
class DurableLoginAcrossProcessesTest(unittest.TestCase):
    """Process A signs in; process B uses the session. Nothing is shared."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "credentials.dat"

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_process_b_gets_the_session_process_a_stored(self) -> None:
        """The headline requirement, end to end."""
        from opencsi.auth.windows_store import DpapiCredentialStore

        process_a_login(self.path)

        # ── process B: nothing but the path ──────────────────────────────
        store = DpapiCredentialStore(self.path)
        provider = StoredOpenCsiCredentialProvider(store)
        self.assertEqual(provider.get_token(), SESSION)

    def test_process_b_gets_the_gitcode_credential_too(self) -> None:
        """Without this, a renewal is impossible and the next expiry needs a scan."""
        from opencsi.auth.windows_store import DpapiCredentialStore

        process_a_login(self.path)

        source = StoredGitCodeCredentialSource(DpapiCredentialStore(self.path))
        cookies = {str(c["name"]): c["value"] for c in source.read_all_cookies()}
        self.assertEqual(cookies[ACCESS_COOKIE], ACCESS)
        self.assertEqual(cookies[REFRESH_COOKIE], REFRESH)

    def test_process_b_can_renew_without_a_browser(self) -> None:
        """The stored GitCode credential reaches ``HttpOAuthRenewer``.

        The renewer is not asked to perform a round trip -- that needs the live
        service. What is checked is that it *can*: its capability probe finds the
        GitCode cookies it reads, and they came from the store rather than from a
        browser.
        """
        from opencsi.auth.http_oauth import HttpOAuthRenewer
        from opencsi.auth.windows_store import DpapiCredentialStore

        process_a_login(self.path)
        source = StoredGitCodeCredentialSource(DpapiCredentialStore(self.path))
        renewer = HttpOAuthRenewer(source, timeout=1.0)
        self.assertTrue(
            renewer.can_renew(),
            "the renewer did not find a GitCode session in the store",
        )

    def test_the_renewer_can_write_a_new_session_back_to_the_store(self) -> None:
        """A renewed token must be persisted, not merely returned.

        This is what makes the *third* process work, and it is the step whose
        absence would show up only an hour later as a session that expired with
        no way to renew it.
        """
        from opencsi.auth.windows_store import DpapiCredentialStore

        process_a_login(self.path)
        provider = StoredOpenCsiCredentialProvider(
            DpapiCredentialStore(self.path), ttl=0.0
        )
        provider.remember_token("renewed-session-token-abcdef123456", expires_in=3600)

        # A third process, again with only the path.
        fresh = StoredOpenCsiCredentialProvider(
            DpapiCredentialStore(self.path), ttl=0.0
        )
        self.assertEqual(fresh.get_token(), "renewed-session-token-abcdef123456")

    def test_renewing_does_not_destroy_the_gitcode_credential(self) -> None:
        """The defect a single ``save(bundle)`` would have caused.

        ``save_opencsi`` runs right after a renewal. If it replaced the bundle
        wholesale, the refresh token that made the renewal possible would be
        gone, and the user would need another QR scan at the next expiry.
        """
        from opencsi.auth.windows_store import DpapiCredentialStore

        process_a_login(self.path)
        provider = StoredOpenCsiCredentialProvider(
            DpapiCredentialStore(self.path), ttl=0.0
        )
        provider.remember_token("renewed-session-token-abcdef123456", expires_in=3600)

        source = StoredGitCodeCredentialSource(DpapiCredentialStore(self.path))
        cookies = {str(c["name"]): c["value"] for c in source.read_all_cookies()}
        self.assertEqual(cookies[REFRESH_COOKIE], REFRESH)

    def test_the_second_process_needs_no_browser(self) -> None:
        """Stated as an assertion, because it is the whole point.

        A second process that finds the session in the store must never consult
        CDP. Branding the browser as unavailable is how that is proved here: if
        anything in this path touched it, the provider would fail.
        """
        from opencsi.auth.windows_store import DpapiCredentialStore

        process_a_login(self.path)
        with mock.patch(
            "opencsi.auth.cdp.CdpCookieProvider._connect",
            side_effect=AssertionError("a browser was contacted"),
            create=True,
        ):
            provider = StoredOpenCsiCredentialProvider(DpapiCredentialStore(self.path))
            self.assertEqual(provider.get_token(), SESSION)

    def test_a_cleared_store_signs_the_next_process_out(self) -> None:
        """``logout`` must actually work across the process boundary."""
        from opencsi.auth.windows_store import DpapiCredentialStore

        process_a_login(self.path)
        DpapiCredentialStore(self.path).clear_all()

        provider = StoredOpenCsiCredentialProvider(DpapiCredentialStore(self.path))
        self.assertIsNone(provider.get_token())

    def test_an_unreadable_store_does_not_look_like_a_missing_session(self) -> None:
        """A storage failure must not be reported as "not signed in".

        The two need different fixes: one is a broken file, the other is a user
        who has never signed in. Collapsing them sends the user to re-authenticate
        when re-authenticating is not the problem.
        """
        from opencsi.auth.windows_store import DpapiCredentialStore
        from opencsi.errors import OpenCsiError

        self.path.write_bytes(b"corrupt")
        provider = StoredOpenCsiCredentialProvider(DpapiCredentialStore(self.path))
        with self.assertRaises(OpenCsiError):
            provider.get_token()


class DurableLoginWithoutDpapiTest(unittest.TestCase):
    """The same contract over the in-memory store, on any platform.

    Not a substitute for the DPAPI test -- memory does not survive a reboot --
    but it keeps the *provider* semantics covered in CI on Linux, where DPAPI
    does not exist, so a regression in the provider is caught on every runner
    rather than only on Windows.
    """

    def test_the_provider_reads_what_was_written(self) -> None:
        store = MemoryCredentialStore()
        store.save_opencsi(StoredOpenCsiCredential(token=SESSION))
        self.assertEqual(StoredOpenCsiCredentialProvider(store).get_token(), SESSION)

    def test_remember_token_is_visible_to_a_new_provider(self) -> None:
        store = MemoryCredentialStore()
        first = StoredOpenCsiCredentialProvider(store, ttl=0.0)
        first.remember_token("renewed-session-token-abcdef123456", expires_in=60)
        second = StoredOpenCsiCredentialProvider(store, ttl=0.0)
        self.assertEqual(second.get_token(), "renewed-session-token-abcdef123456")

    def test_an_empty_store_yields_no_token(self) -> None:
        self.assertIsNone(StoredOpenCsiCredentialProvider(MemoryCredentialStore()).get_token())

    def test_the_source_serves_the_stored_credential(self) -> None:
        store = MemoryCredentialStore(
            CredentialBundle(
                gitcode=StoredGitCodeCredential(
                    access_token=ACCESS, refresh_token=REFRESH, username="alice"
                )
            )
        )
        source = StoredGitCodeCredentialSource(store)
        cookies = {str(c["name"]): c["value"] for c in source.read_all_cookies()}
        self.assertEqual(cookies[ACCESS_COOKIE], ACCESS)
        self.assertEqual(cookies[REFRESH_COOKIE], REFRESH)

    def test_an_empty_source_returns_no_records_rather_than_raising(self) -> None:
        """The renewer must be able to fall through to another mechanism.

        Raising here would abort the renewal chain and hide the browser fallback
        that a first-time consent needs.
        """
        source = StoredGitCodeCredentialSource(MemoryCredentialStore())
        self.assertEqual(source.read_all_cookies(), [])
        self.assertFalse(source.status().available)

    def test_invalidate_keeps_the_stored_credential(self) -> None:
        """A 401 must not sign the user out permanently.

        The provider may drop its cache; it must never delete the persistent
        copy, because the credential is still the user's and one rejected request
        is not evidence that it is worthless.
        """
        store = MemoryCredentialStore()
        store.save_opencsi(StoredOpenCsiCredential(token=SESSION))
        provider = StoredOpenCsiCredentialProvider(store, ttl=0.0)
        self.assertEqual(provider.get_token(), SESSION)
        provider.invalidate()
        self.assertEqual(provider.get_token(), SESSION)
        self.assertIsNotNone(store.load().opencsi)


if __name__ == "__main__":
    unittest.main()
