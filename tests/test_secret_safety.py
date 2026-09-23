"""Prove no credential reaches any output surface (objective §16).

Checked by planting a distinctive value in each credential slot and then
exercising every surface the tool can print to, asserting the value appears in
none of them. The values are shaped like real ones -- long, opaque, and matching
the redaction heuristics' length threshold -- so a failure here means a real
credential would leak too.
"""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401  (imported for its sys.path side effect)

from opencsi.auth.store import (
    CredentialBundle,
    MemoryCredentialStore,
    StoredGitCodeCredential,
    StoredOpenCsiCredential,
)
from opencsi.redaction import MASK

# Long enough to trip the opaque-blob catch-all, and distinctive enough to grep.
GITCODE_ACCESS = "SECRETgitcodeAccess000111222333444555"
GITCODE_REFRESH = "SECRETgitcodeRefresh000111222333444555"
OPENCSI_TOKEN = "SECRETopencsiSession000111222333444555"

SECRETS = (GITCODE_ACCESS, GITCODE_REFRESH, OPENCSI_TOKEN)


def a_store() -> MemoryCredentialStore:
    return MemoryCredentialStore(
        CredentialBundle(
            gitcode=StoredGitCodeCredential(
                access_token=GITCODE_ACCESS,
                refresh_token=GITCODE_REFRESH,
                username="alice",
            ),
            opencsi=StoredOpenCsiCredential(token=OPENCSI_TOKEN),
        )
    )


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    from opencsi.cli.app import main

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(argv)
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, out.getvalue(), err.getvalue()


class NoSecretInAnyOutputTest(unittest.TestCase):
    """Every surface, one at a time."""

    def _assert_clean(self, *texts: str) -> None:
        for text in texts:
            for secret in SECRETS:
                self.assertNotIn(
                    secret, text, f"a credential reached an output surface:\n{text}"
                )

    def test_the_bundle_repr_is_safe(self) -> None:
        """The first thing anyone prints when debugging.

        The bundle reports *presence*, not content: "set"/"absent". That is the
        right shape, because a bundle repr is what appears in a crash dump and a
        masked-but-present field still invites someone to go looking.
        """
        text = repr(a_store().load())
        self.assertIn("set", text)
        self._assert_clean(text)

    def test_each_credential_repr_is_safe(self) -> None:
        bundle = a_store().load()
        for part in (repr(bundle.gitcode), repr(bundle.opencsi)):
            self.assertIn(MASK, part)
            self._assert_clean(part)

    def test_a_stored_credential_masks_its_own_value_everywhere(self) -> None:
        """Registration is what makes an *unmarked* value safe.

        The shapes (`token=...`, `sk-...`, 48+ char blobs) are caught by pattern,
        but a bare opaque value is only caught because constructing a credential
        registers it. This asserts that end-to-end property rather than the
        pattern list, because that is the one that actually holds in production.
        """
        from opencsi.redaction import scrub_text

        a_store()  # constructing these registers all three values
        for secret in SECRETS:
            self.assertIn(MASK, scrub_text(secret))
            self.assertIn(MASK, scrub_text(f"cannot read {secret}"))

    def test_an_unregistered_value_is_the_documented_limit(self) -> None:
        """The honest boundary, asserted so it is not mistaken for coverage.

        A value that was never registered *and* matches no shape is not masked.
        That is accepted rather than "fixed" by lowering the length threshold,
        because a catch-all short enough to catch this would also redact ordinary
        identifiers -- usernames, tool names, file paths -- and a log that masks
        everything is a log nobody can read. Anything that *holds* a credential
        registers it, which is the stronger guarantee.
        """
        from opencsi.redaction import clear_registry, scrub_text

        clear_registry()
        try:
            unregistered = "UNREGISTERED-value-abc123"
            self.assertEqual(scrub_text(unregistered), unregistered)
        finally:
            a_store()  # restore the registry other tests rely on

    def test_a_store_error_never_carries_the_value(self) -> None:
        from opencsi.auth.store import CredentialStoreError

        # Constructing a credential registers its value, which is the mechanism
        # that makes scrubbing work for a bare opaque string. Done here first so
        # this asserts the production path rather than the pattern list alone.
        a_store()
        for message in (
            GITCODE_ACCESS,
            f"cannot read {GITCODE_ACCESS}",
            f"token={OPENCSI_TOKEN}",
        ):
            error = CredentialStoreError(message)
            self._assert_clean(str(error), repr(error))

    def test_login_status_json_is_clean(self) -> None:
        store = a_store()
        code, out, err = self._with_store(store, ["login", "--status", "--json"])
        self._assert_clean(out, err)

    def test_login_status_text_is_clean(self) -> None:
        code, out, err = self._with_store(a_store(), ["login", "--status"])
        self._assert_clean(out, err)

    def test_doctor_output_is_clean(self) -> None:
        code, out, err = self._with_store(a_store(), ["doctor"])
        self._assert_clean(out, err)

    def test_doctor_json_is_clean(self) -> None:
        code, out, err = self._with_store(a_store(), ["doctor", "--json"])
        self._assert_clean(out, err)

    def test_logout_output_is_clean(self) -> None:
        code, out, err = self._with_store(a_store(), ["logout"])
        self._assert_clean(out, err)

    def test_logout_json_is_clean(self) -> None:
        code, out, err = self._with_store(a_store(), ["logout", "--json"])
        self._assert_clean(out, err)

    def test_usage_json_is_clean(self) -> None:
        """A command that would print rows, with the network stubbed out."""
        code, out, err = self._with_store(a_store(), ["usage", "--json"])
        self._assert_clean(out, err)

    def _with_store(self, store, argv):
        """Run the CLI with the real store replaced, at the seam that opens it."""
        from opencsi.auth import windows_store

        with mock.patch.object(
            windows_store, "open_default_store", return_value=store
        ):
            return run_cli(argv)


class TrayTooltipSafetyTest(unittest.TestCase):
    """The tray's tooltip is a surface too, and it is on screen permanently.

    The guarantee here is stronger than "the strings do not currently contain a
    token": ``MonitorSnapshot`` has **no** credential field at all, so the tooltip
    and menu are secret-free by construction and cannot become unsafe without a
    type change. That is asserted directly, because it is the property worth
    protecting -- a test that only grepped today's output would pass the day
    someone added the field and fail only once something was rendered into it.
    """

    def _snapshot(self):
        from opencsi.tray.presenter import MonitorSnapshot, MonitorState

        return MonitorSnapshot(
            state=MonitorState.OK,
            total_tokens=1200,
            requests=42,
            adoption_rate=0.25,
            fetched_at=__import__("datetime").datetime.now(),
        )

    def test_the_snapshot_has_no_credential_field(self) -> None:
        """The structural guarantee. Everything else follows from it."""
        import dataclasses

        fields = {f.name for f in dataclasses.fields(self._snapshot())}
        forbidden = {
            "token",
            "cookie",
            "access_token",
            "refresh_token",
            "virtual_key",
            "virtualKey",
            "xauth_token",
            "credential",
        }
        self.assertEqual(
            fields & forbidden, set(), f"the snapshot gained a credential field: {fields}"
        )

    def test_the_tooltip_never_contains_a_token(self) -> None:
        from opencsi.tray.presenter import tooltip_for

        for secret in SECRETS:
            self.assertNotIn(secret, tooltip_for(self._snapshot()))

    def test_the_headline_never_contains_a_token(self) -> None:
        from opencsi.tray.presenter import headline_for

        for secret in SECRETS:
            self.assertNotIn(secret, headline_for(self._snapshot()))

    def test_the_status_text_never_contains_a_token(self) -> None:
        from opencsi.tray.presenter import status_text

        for secret in SECRETS:
            self.assertNotIn(secret, status_text(self._snapshot()))

    def test_the_serialised_form_is_safe(self) -> None:
        """`as_dict()` is what crosses into logs and any IPC."""
        import json

        blob = json.dumps(self._snapshot().as_dict())
        for secret in SECRETS:
            self.assertNotIn(secret, blob)


if __name__ == "__main__":
    unittest.main()
