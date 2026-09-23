"""The durable credential store: model, DPAPI, corruption, and secret safety.

These tests are the acceptance criteria for the store, so they are written
against the *behaviour* the architecture needs rather than against the
implementation:

* a credential survives a new store instance -- which is what "survives a new
  process" means when the test cannot fork;
* the file on disk contains none of the secret bytes;
* a partial write does not delete the other half, because ``save_opencsi`` runs
  right after a renewal and must not take the GitCode refresh token with it;
* an unreadable file is reported and **left alone**, never deleted;
* nothing that can be printed -- ``repr``, ``str``, ``as_dict``, an exception
  message -- contains a credential.

The DPAPI classes are skipped off Windows. They are *not* skipped on Windows
merely because DPAPI might be unavailable: this project ships a Windows tray, so
a broken DPAPI is a real failure and hiding it behind a skip is how it would
reach a user.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from opencsi.auth.store import (
    STORE_VERSION,
    CredentialBundle,
    CredentialStore,
    CredentialStoreError,
    CredentialStoreStatus,
    MemoryCredentialStore,
    StoredGitCodeCredential,
    StoredOpenCsiCredential,
)
from opencsi.redaction import scrub_text

#: Long enough to exceed the redaction registry's minimum, so a leak test that
#: passes really is testing the object's own repr and not the registry.
ACCESS = "gitcode-access-token-abcdefghijklmnop"
REFRESH = "gitcode-refresh-token-abcdefghijklmnop"
SESSION = "opencsi-session-token-abcdefghijklmnop"
SECRETS = (ACCESS, REFRESH, SESSION)


def a_bundle() -> CredentialBundle:
    return CredentialBundle(
        gitcode=StoredGitCodeCredential(
            access_token=ACCESS,
            refresh_token=REFRESH,
            username="alice",
            access_expires_at=2_000_000_000.0,
            refresh_expires_at=2_100_000_000.0,
        ),
        opencsi=StoredOpenCsiCredential(token=SESSION, expires_at=2_000_000_000.0),
    )


class SecretSafetyTest(unittest.TestCase):
    """§16: nothing printable may contain a credential.

    The registry in :mod:`opencsi.redaction` masks these values in free text, so
    a naive assert would pass even for an object that *does* print them. These
    checks therefore look at the object's own rendering and, where the registry
    could have helped, at the raw attribute too.
    """

    def test_the_fixture_values_would_be_masked_by_the_registry(self) -> None:
        """Guard: the leak checks below must not pass via the registry.

        If redaction is masking everything anyway, a test asserting "no secret
        in repr" proves nothing about the repr. This pins that the raw values
        appear in an unscrubbed f-string -- so the other tests are meaningful --
        while ``scrub_text`` removes them.
        """
        for secret in SECRETS:
            self.assertIn(secret, f"value={secret}")
            self.assertNotIn(secret, scrub_text(f"value={secret}"))

    def test_bundle_repr_has_no_secret(self) -> None:
        for secret in SECRETS:
            self.assertNotIn(secret, repr(a_bundle()))

    def test_gitcode_repr_has_no_secret(self) -> None:
        credential = a_bundle().gitcode
        for secret in (ACCESS, REFRESH):
            self.assertNotIn(secret, repr(credential))
            self.assertNotIn(secret, str(credential))
            self.assertNotIn(secret, f"{credential}")

    def test_opencsi_repr_has_no_secret(self) -> None:
        credential = a_bundle().opencsi
        self.assertNotIn(SESSION, repr(credential))
        self.assertNotIn(SESSION, str(credential))
        self.assertNotIn(SESSION, f"{credential}")

    def test_status_dict_has_no_secret(self) -> None:
        status = MemoryCredentialStore(a_bundle()).status()
        rendered = json.dumps(status.as_dict(), ensure_ascii=False)
        for secret in SECRETS:
            self.assertNotIn(secret, rendered)

    def test_the_reprs_say_whether_a_half_is_present(self) -> None:
        """A redacted repr still has to be useful.

        If every field became ``<redacted>`` the repr would be safe and
        worthless; the diagnostic information is which halves are set.
        """
        self.assertIn("gitcode=set", repr(a_bundle()))
        self.assertIn("opencsi=set", repr(a_bundle()))
        self.assertIn("gitcode=absent", repr(CredentialBundle()))
        self.assertIn("opencsi=absent", repr(CredentialBundle()))
        only_gitcode = CredentialBundle(gitcode=a_bundle().gitcode)
        self.assertIn("gitcode=set", repr(only_gitcode))
        self.assertIn("opencsi=absent", repr(only_gitcode))

    def test_a_store_error_message_is_scrubbed_when_the_value_is_known(self) -> None:
        """An exception message is the likeliest leak path in the codebase.

        The value is registered first -- by constructing the credential that
        holds it -- which is the state the store is in whenever a real error
        fires: it has just loaded or is about to save a credential, so the model
        has registered it. This asserts the stored argument itself was
        rewritten, not merely that ``str(exc)`` looks clean, because the logging
        filter would mask a leak on the way out and hide a class that kept the
        plaintext.
        """
        StoredGitCodeCredential(access_token=ACCESS)  # registers ACCESS
        exc = CredentialStoreError(f"failed while handling {ACCESS}")
        self.assertNotIn(ACCESS, exc.args[0])
        self.assertIn("<redacted>", exc.args[0])

    def test_an_unregistered_opaque_value_is_a_documented_limit(self) -> None:
        """The limit, asserted rather than glossed over.

        ``scrub_text`` matches credential-*shaped* text: a bare opaque string
        with no ``token=`` context and under the 48-character catch-all is not
        recognised. That is why the store's own code never formats a value into
        a message, and why the test below checks the source for that rule rather
        than trusting the scrubber.
        """
        from opencsi.redaction import clear_registry, scrub_text

        clear_registry()
        unregistered = "unregistered-opaque-value-abc"
        self.assertEqual(scrub_text(unregistered), unregistered)
        # ...but the same value in a labelled context is caught.
        self.assertNotIn(unregistered, scrub_text(f"token={unregistered}"))

    def test_a_registered_secret_is_scrubbed_from_an_error(self) -> None:
        """A value the store knows about is masked wherever it appears."""
        StoredOpenCsiCredential(token=SESSION)  # registers SESSION
        exc = CredentialStoreError(f"failed while handling {SESSION}")
        self.assertNotIn(SESSION, exc.args[0])

    def test_the_stores_own_messages_never_interpolate_a_value(self) -> None:
        """The real defence, and worth stating as an assertion.

        Pattern matching is not what keeps the store safe -- *not formatting
        values into messages* is. This pins that the store's error text is built
        from names, paths and numeric codes only.
        """
        import inspect

        from opencsi.auth import windows_store

        source = inspect.getsource(windows_store)
        for pattern in ("{self.access_token}", "{self.refresh_token}", "{token}", "{value}"):
            self.assertNotIn(
                pattern,
                source,
                f"{pattern} interpolates a credential value into a message",
            )

    def test_the_username_is_not_treated_as_a_secret(self) -> None:
        """The username is diagnostic, and masking it would defeat the purpose.

        It is also not a credential: it identifies the account, it does not
        authenticate it. ``login --status`` needs to say *who* is signed in.
        """
        self.assertIn("alice", repr(a_bundle().gitcode))


class StoreModelTest(unittest.TestCase):
    """The data model's own rules."""

    def test_roundtrip_through_persisted_form(self) -> None:
        original = a_bundle()
        restored = CredentialBundle.from_persisted(original.as_persisted())
        self.assertEqual(restored, original)

    def test_persisted_form_is_versioned(self) -> None:
        self.assertEqual(a_bundle().as_persisted()["version"], STORE_VERSION)

    def test_an_unknown_version_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            CredentialBundle.from_persisted({"version": STORE_VERSION + 1})

    def test_a_bundle_without_a_version_still_loads(self) -> None:
        """Forward tolerance for a hand-written or older file."""
        bundle = CredentialBundle.from_persisted(
            {"opencsi": {"token": SESSION, "expires_at": None}}
        )
        self.assertIsNotNone(bundle.opencsi)
        self.assertIsNone(bundle.gitcode)

    def test_a_gitcode_half_without_a_token_is_dropped_not_fatal(self) -> None:
        """A future shape must not cost the user their current session.

        The openCsiTool half is what keeps the CLI working right now; the
        GitCode half only matters at the *next* expiry. Losing the second to a
        parse error in the first would sign the user out for no reason.
        """
        bundle = CredentialBundle.from_persisted(
            {
                "version": STORE_VERSION,
                "gitcode": {"refresh_token": REFRESH},  # no access_token
                "opencsi": {"token": SESSION, "expires_at": None},
            }
        )
        self.assertIsNone(bundle.gitcode)
        self.assertIsNotNone(bundle.opencsi)

    def test_unknown_keys_inside_a_record_are_ignored(self) -> None:
        credential = StoredGitCodeCredential.from_persisted(
            {"access_token": ACCESS, "some_future_field": 1}
        )
        self.assertEqual(credential.access_token, ACCESS)

    def test_expiry_arithmetic(self) -> None:
        past = StoredOpenCsiCredential(token=SESSION, expires_at=1.0)
        future = StoredOpenCsiCredential(token=SESSION, expires_at=4_000_000_000.0)
        self.assertTrue(past.expired)
        self.assertLess(past.remaining or 0, 0)
        self.assertFalse(future.expired)
        self.assertGreater(future.remaining or 0, 0)

    def test_an_unknown_expiry_is_not_an_expiry(self) -> None:
        """``None`` means "not known", which must not read as "expired".

        Treating unknown as expired would make a session cookie with no
        ``expires`` attribute look dead on every read.
        """
        credential = StoredOpenCsiCredential(token=SESSION, expires_at=None)
        self.assertFalse(credential.expired)
        self.assertIsNone(credential.remaining)

    def test_has_refresh_token(self) -> None:
        self.assertTrue(a_bundle().gitcode.has_refresh_token)
        self.assertFalse(
            StoredGitCodeCredential(access_token=ACCESS).has_refresh_token
        )

    def test_a_bundle_is_immutable(self) -> None:
        """Immutability is what makes the partial writes safe to reason about."""
        bundle = a_bundle()
        with self.assertRaises(Exception):
            bundle.opencsi = None  # type: ignore[misc]

    def test_with_helpers_do_not_mutate_the_original(self) -> None:
        bundle = a_bundle()
        stripped = bundle.with_opencsi(None)
        self.assertIsNotNone(bundle.opencsi)
        self.assertIsNone(stripped.opencsi)


class MemoryStoreTest(unittest.TestCase):
    """The in-process store, which the durable-login test also relies on."""

    def test_partial_saves_do_not_clobber_each_other(self) -> None:
        """The defect this design exists to prevent.

        A single ``save(bundle)`` would make the openCsiTool write -- which
        happens second, after a renewal -- replace the GitCode credential that
        made the renewal possible, so the next expiry would need a QR scan.
        """
        store = MemoryCredentialStore()
        store.save_gitcode(StoredGitCodeCredential(access_token=ACCESS))
        store.save_opencsi(StoredOpenCsiCredential(token=SESSION))
        bundle = store.load()
        self.assertIsNotNone(bundle.gitcode)
        self.assertIsNotNone(bundle.opencsi)
        self.assertEqual(bundle.gitcode.access_token, ACCESS)
        self.assertEqual(bundle.opencsi.token, SESSION)

    def test_clear_opencsi_keeps_gitcode(self) -> None:
        """Logging out of the session must not cost the upstream credential."""
        store = MemoryCredentialStore(a_bundle())
        store.clear_opencsi()
        self.assertIsNone(store.load().opencsi)
        self.assertIsNotNone(store.load().gitcode)

    def test_clear_all_empties_it(self) -> None:
        store = MemoryCredentialStore(a_bundle())
        store.clear_all()
        self.assertTrue(store.load().empty)

    def test_status_reports_both_halves(self) -> None:
        status = MemoryCredentialStore(a_bundle()).status()
        self.assertTrue(status.available)
        self.assertTrue(status.has_gitcode)
        self.assertTrue(status.has_opencsi)
        self.assertEqual(status.gitcode_username, "alice")

    def test_status_of_an_empty_store(self) -> None:
        status = MemoryCredentialStore().status()
        self.assertTrue(status.available)
        self.assertTrue(status.empty)

    def test_it_satisfies_the_protocol(self) -> None:
        self.assertIsInstance(MemoryCredentialStore(), CredentialStore)

    def test_status_is_a_credential_store_status(self) -> None:
        self.assertIsInstance(MemoryCredentialStore().status(), CredentialStoreStatus)


@unittest.skipUnless(sys.platform == "win32", "DPAPI is Windows-only")
class DpapiStoreTest(unittest.TestCase):
    """The real store. Runs on Windows, where the tool actually ships."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "credentials.dat"
        from opencsi.auth.windows_store import DpapiCredentialStore

        self.store = DpapiCredentialStore(self.path)

    def tearDown(self) -> None:
        self._dir.cleanup()

    # ── acceptance: save, reload in a new instance ───────────────────────
    def test_a_missing_file_is_an_empty_bundle(self) -> None:
        """A user who never signed in is the normal starting state."""
        self.assertTrue(self.store.load().empty)

    def test_save_then_load_in_a_new_instance(self) -> None:
        """The core requirement: the credential outlives the object.

        A new instance is what the test can do instead of a new process; it
        takes the same path, re-reads the same bytes and re-decrypts them, which
        is the whole of what a second process would do differently.
        """
        from opencsi.auth.windows_store import DpapiCredentialStore

        self.store.save_gitcode(a_bundle().gitcode)
        self.store.save_opencsi(a_bundle().opencsi)

        fresh = DpapiCredentialStore(self.path)
        bundle = fresh.load()
        self.assertEqual(bundle.gitcode.access_token, ACCESS)
        self.assertEqual(bundle.gitcode.refresh_token, REFRESH)
        self.assertEqual(bundle.gitcode.username, "alice")
        self.assertEqual(bundle.opencsi.token, SESSION)
        self.assertEqual(bundle.opencsi.expires_at, 2_000_000_000.0)

    # ── acceptance: no plaintext ─────────────────────────────────────────
    def test_the_file_contains_no_plaintext(self) -> None:
        self.store.save_gitcode(a_bundle().gitcode)
        self.store.save_opencsi(a_bundle().opencsi)
        raw = self.path.read_bytes()
        for secret in SECRETS:
            self.assertNotIn(
                secret.encode("utf-8"), raw, f"{secret[:12]}... is in the clear"
            )
        # Not even the structure should be readable.
        self.assertNotIn(b"access_token", raw)
        self.assertNotIn(b"alice", raw)

    def test_the_file_is_not_valid_json(self) -> None:
        """A file you can read with a text editor is not an encrypted file."""
        self.store.save_opencsi(a_bundle().opencsi)
        with self.assertRaises(Exception):
            json.loads(self.path.read_text(encoding="utf-8", errors="strict"))

    def test_no_plaintext_temporary_file_is_ever_created(self) -> None:
        """The design writes ciphertext to ``.tmp``; nothing plaintext, ever.

        Checked by scanning the directory after a write, because a
        "serialise to a temp file then encrypt" implementation would leave
        exactly the artifact this asserts is absent.
        """
        self.store.save_gitcode(a_bundle().gitcode)
        leftovers = sorted(p.name for p in Path(self._dir.name).iterdir())
        self.assertEqual(leftovers, ["credentials.dat"])

    def test_the_temporary_file_is_cleaned_up(self) -> None:
        self.store.save_opencsi(a_bundle().opencsi)
        tmps = list(Path(self._dir.name).glob("*.tmp"))
        self.assertEqual(tmps, [], "an atomic-write temp file was left behind")

    # ── acceptance: partial writes ───────────────────────────────────────
    def test_saving_the_session_keeps_the_gitcode_credential(self) -> None:
        """The renewal path depends on this.

        ``save_opencsi`` runs immediately after a renewal. If it replaced the
        bundle wholesale it would delete the refresh token that made the
        renewal possible.
        """
        from opencsi.auth.windows_store import DpapiCredentialStore

        self.store.save_gitcode(a_bundle().gitcode)
        self.store.save_opencsi(StoredOpenCsiCredential(token="new-session-token-xyz123456"))
        bundle = DpapiCredentialStore(self.path).load()
        self.assertIsNotNone(bundle.gitcode)
        self.assertEqual(bundle.gitcode.refresh_token, REFRESH)
        self.assertEqual(bundle.opencsi.token, "new-session-token-xyz123456")

    def test_saving_gitcode_keeps_the_session(self) -> None:
        from opencsi.auth.windows_store import DpapiCredentialStore

        self.store.save_opencsi(a_bundle().opencsi)
        self.store.save_gitcode(StoredGitCodeCredential(access_token="second-access-token-12345"))
        bundle = DpapiCredentialStore(self.path).load()
        self.assertEqual(bundle.opencsi.token, SESSION)
        self.assertEqual(bundle.gitcode.access_token, "second-access-token-12345")

    # ── acceptance: clear ────────────────────────────────────────────────
    def test_clear_all_removes_the_file(self) -> None:
        self.store.save_opencsi(a_bundle().opencsi)
        self.assertTrue(self.path.exists())
        self.store.clear_all()
        self.assertFalse(self.path.exists())
        self.assertTrue(self.store.load().empty)

    def test_clear_all_on_an_absent_file_is_not_an_error(self) -> None:
        self.store.clear_all()

    def test_clear_opencsi_keeps_gitcode_on_disk(self) -> None:
        from opencsi.auth.windows_store import DpapiCredentialStore

        self.store.save_gitcode(a_bundle().gitcode)
        self.store.save_opencsi(a_bundle().opencsi)
        self.store.clear_opencsi()
        bundle = DpapiCredentialStore(self.path).load()
        self.assertIsNone(bundle.opencsi)
        self.assertIsNotNone(bundle.gitcode)

    # ── acceptance: corrupt input ────────────────────────────────────────
    def test_a_corrupt_file_raises_and_is_not_deleted(self) -> None:
        """§17. The failure must be reported, and nothing destroyed.

        The file may be another Windows user's, or a copied profile's. Either
        way it is the user's only copy of something, and silently deleting it
        to "fix" a read error destroys evidence that cannot be recreated.
        """
        self.path.write_bytes(b"not dpapi ciphertext")
        with self.assertRaises(CredentialStoreError):
            self.store.load()
        self.assertTrue(self.path.exists(), "the unreadable file was deleted")

    def test_a_corrupt_file_is_reported_not_crashed_in_status(self) -> None:
        """``doctor`` and ``login --status`` must report, not raise."""
        self.path.write_bytes(b"not dpapi ciphertext")
        status = self.store.status()
        self.assertFalse(status.available)
        self.assertIsNotNone(status.detail)
        self.assertIn("login --qr", status.detail or "")

    def test_a_corrupt_file_names_the_recovery_command(self) -> None:
        self.path.write_bytes(b"not dpapi ciphertext")
        with self.assertRaises(CredentialStoreError) as caught:
            self.store.load()
        self.assertIn("opencsi login --qr", str(caught.exception))

    def test_a_zero_length_file_reads_as_empty(self) -> None:
        """What a crash between create and write leaves behind."""
        self.path.write_bytes(b"")
        self.assertTrue(self.store.load().empty)

    def test_valid_encryption_of_the_wrong_content_is_reported(self) -> None:
        from opencsi.auth.windows_store import protect

        self.path.write_bytes(protect(b"this decrypts fine but is not JSON"))
        with self.assertRaises(CredentialStoreError) as caught:
            self.store.load()
        self.assertIn("unreadable", str(caught.exception))

    def test_a_blob_from_another_application_is_rejected(self) -> None:
        """The entropy argument binds the blob to this tool.

        ``CryptUnprotectData`` without matching entropy will happily decrypt any
        blob this user encrypted, so without it a different program on the same
        account could feed us its own data and be believed.
        """
        import ctypes

        from opencsi.auth.windows_store import _DataBlob, _blob_from, _take_blob

        blob_in = _blob_from(b'{"version":1}')
        blob_out = _DataBlob()
        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 1, ctypes.byref(blob_out)
        )
        self.assertTrue(ok, "could not build the no-entropy control blob")
        self.path.write_bytes(_take_blob(blob_out))
        with self.assertRaises(CredentialStoreError):
            self.store.load()

    def test_an_unsupported_version_is_reported(self) -> None:
        from opencsi.auth.windows_store import protect

        self.path.write_bytes(
            protect(json.dumps({"version": 99, "opencsi": {"token": "x"}}).encode())
        )
        with self.assertRaises(CredentialStoreError):
            self.store.load()

    # ── status ───────────────────────────────────────────────────────────
    def test_status_names_the_backend_and_path(self) -> None:
        status = self.store.status()
        self.assertTrue(status.available)
        self.assertEqual(status.backend, "dpapi")
        self.assertEqual(status.path, str(self.path))

    def test_status_never_contains_a_secret(self) -> None:
        self.store.save_gitcode(a_bundle().gitcode)
        self.store.save_opencsi(a_bundle().opencsi)
        rendered = json.dumps(self.store.status().as_dict(), ensure_ascii=False)
        for secret in SECRETS:
            self.assertNotIn(secret, rendered)

    def test_repr_never_contains_a_secret(self) -> None:
        self.store.save_opencsi(a_bundle().opencsi)
        self.assertNotIn(SESSION, repr(self.store))

    def test_it_satisfies_the_protocol(self) -> None:
        self.assertIsInstance(self.store, CredentialStore)

    # ── the primitive itself ─────────────────────────────────────────────
    def test_protect_and_unprotect_roundtrip(self) -> None:
        from opencsi.auth.windows_store import protect, unprotect

        plaintext = b"a value that must come back unchanged"
        self.assertEqual(unprotect(protect(plaintext)), plaintext)

    def test_ciphertext_differs_from_plaintext(self) -> None:
        from opencsi.auth.windows_store import protect

        plaintext = b"a value that must not be recognisable"
        self.assertNotEqual(protect(plaintext), plaintext)

    def test_unprotect_rejects_a_non_dpapi_blob(self) -> None:
        from opencsi.auth.windows_store import unprotect

        with self.assertRaises(CredentialStoreError):
            unprotect(b"definitely not dpapi output")


class PlatformSelectionTest(unittest.TestCase):
    """Which store a platform gets -- and that there is no weak fallback."""

    def test_the_default_store_is_dpapi_on_windows(self) -> None:
        from opencsi.auth.windows_store import open_default_store

        store = open_default_store()
        if sys.platform == "win32":
            self.assertIsNotNone(store)
            self.assertEqual(store.name, "dpapi")
            self.assertIsInstance(store, CredentialStore)
        else:
            self.assertIsNone(store)

    def test_constructing_the_dpapi_store_off_windows_fails_loudly(self) -> None:
        """A silent fallback would store a credential in the clear.

        Skipped on Windows, where the real constructor succeeds -- the negative
        case is simulated by patching the platform check, so the branch is
        covered wherever the suite runs.
        """
        from unittest import mock

        from opencsi.auth import windows_store

        if sys.platform == "win32":
            with mock.patch.object(windows_store, "supported", return_value=False):
                with self.assertRaises(CredentialStoreError):
                    windows_store.DpapiCredentialStore()
                self.assertIsNone(windows_store.open_default_store())
        else:
            with self.assertRaises(CredentialStoreError):
                windows_store.DpapiCredentialStore()

    def test_the_default_path_is_outside_the_repository(self) -> None:
        """A credential file inside a checkout would be committed eventually."""
        from opencsi.auth.windows_store import default_path

        text = str(default_path()).replace("\\", "/").lower()
        self.assertNotIn("opencsitoolmonitor", text)
        self.assertTrue(text.endswith("credentials.dat"))

    def test_the_filename_is_not_a_plaintext_sounding_one(self) -> None:
        from opencsi.auth.windows_store import CREDENTIAL_FILENAME

        self.assertTrue(CREDENTIAL_FILENAME.endswith(".dat"))
        self.assertNotIn(".json", CREDENTIAL_FILENAME)


if __name__ == "__main__":
    unittest.main()
