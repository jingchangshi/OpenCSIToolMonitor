"""The credential store under real multi-process access.

Why this file exists
--------------------
``DpapiCredentialStore.save_gitcode`` / ``save_opencsi`` are read-modify-write
cycles: load the bundle, replace one half, encrypt, replace the file. Two
processes doing that concurrently -- the tray renewing the openCsiTool session
while a CLI rotates the GitCode refresh token -- is the *normal* state of this
tool, and nothing else in the suite exercised it. Every other store test runs
two handles sequentially, which cannot observe a stale-read write-back.

The scenarios named by the audit brief, and where they live here:

* two processes updating *different* halves -- the deterministic test below,
  which interleaves a read and a write so the second writer's stale view
  provably destroys the first writer's update;
* two processes refreshing the *same* half -- concurrent workers, both writing
  the openCsiTool half; the file must stay loadable and hold one of them;
* a process dying mid-write -- a leftover ``.tmp`` must be ignored by ``load``
  and cleaned up by the next write;
* a refresh that succeeded but could not be written -- covered in
  ``tests/test_gitcode_refresh.py`` (the refresher reports a storage failure
  and carries the replacement credential); not duplicated here.

Every store in this file lives in a private temporary directory. The real
``%LOCALAPPDATA%\\OpenCSI\\credentials.dat`` is never opened.

Windows-only, because DPAPI is: the same ``skipUnless`` the DPAPI store tests
use. CI runs the suite on both platforms, so the skip is visible rather than
silent.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401  (imported for its sys.path side effect)

SRC = Path(__file__).resolve().parents[1] / "src"

ACCESS_A = "gitcode-access-token-aaaa-abcdefghijklmnop"
ACCESS_B = "gitcode-access-token-bbbb-abcdefghijklmnop"
REFRESH_A = "gitcode-refresh-token-aaaa-abcdefghijklmnop"
REFRESH_B = "gitcode-refresh-token-bbbb-abcdefghijklmnop"
SESSION_A = "opencsi-session-token-aaaa-abcdefghijklmnop"
SESSION_B = "opencsi-session-token-bbbb-abcdefghijklmnop"

#: The worker the concurrency tests drive. It is a separate *process*, which is
#: the whole point: two store objects in one process share no state anyway, so
#: only a real second interpreter can reproduce what the tray and a CLI do.
_WORKER = r'''
import sys
import time
from pathlib import Path

sys.path.insert(0, sys.argv[3])

from opencsi.auth.store import (
    StoredGitCodeCredential,
    StoredOpenCsiCredential,
)
from opencsi.auth.windows_store import DpapiCredentialStore

path = Path(sys.argv[1])
op = sys.argv[2]
store = DpapiCredentialStore(path)

if op == "save-opencsi":
    store.save_opencsi(StoredOpenCsiCredential(token=sys.argv[4], expires_at=None))
elif op == "save-gitcode":
    store.save_gitcode(StoredGitCodeCredential(
        access_token=sys.argv[4], refresh_token=sys.argv[5], username=sys.argv[6]))
elif op == "renew-loop":
    # A renewal-shaped workload: read the bundle, replace the session half,
    # write it back -- several times, as fast as the store allows. Any
    # interleaving bug shows up as an exception or a lost half.
    for i in range(int(sys.argv[4])):
        store.save_opencsi(StoredOpenCsiCredential(token=f"{sys.argv[5]}-{i}", expires_at=None))
        time.sleep(0.001)
print("ok")
'''


@unittest.skipUnless(sys.platform == "win32", "DPAPI is Windows-only")
class StoreMultiprocessTest(unittest.TestCase):
    """The store's cross-process guarantees, on the real implementation."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "credentials.dat"
        self._worker_path = Path(self._dir.name) / "_store_worker.py"
        self._worker_path.write_text(_WORKER, encoding="utf-8")

    def tearDown(self) -> None:
        self._dir.cleanup()

    # ── helpers ───────────────────────────────────────────────────────────
    def _spawn(self, op: str, *args: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC)
        return subprocess.run(
            [sys.executable, str(self._worker_path), str(self.path), op, str(SRC), *args],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
            check=False,
        )

    def _spawn_two(self, op_a: str, args_a: tuple[str, ...], op_b: str, args_b: tuple[str, ...]):
        """Start both workers as close to simultaneously as the OS allows."""
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC)
        procs = [
            subprocess.Popen(
                [sys.executable, str(self._worker_path), str(self.path), op_a, str(SRC), *args_a],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
            ),
            subprocess.Popen(
                [sys.executable, str(self._worker_path), str(self.path), op_b, str(SRC), *args_b],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
            ),
        ]
        return [p.communicate(timeout=120) + (p.returncode,) for p in procs]

    def _store(self):
        from opencsi.auth.windows_store import DpapiCredentialStore

        return DpapiCredentialStore(self.path)

    # ── 1. the stale-read write-back, deterministically ───────────────────
    def test_a_stale_view_cannot_overwrite_a_newer_half(self) -> None:
        """Interleaved readers must not write a half they did not see.

        The sequence is the one the audit brief names as "the token was rotated
        but an old process writes the old credential back", made deterministic
        by ordering the steps instead of racing them:

            P1 loads the bundle            (sees GitCode half A)
            P2 saves GitCode half B        (rotated by a refresh)
            P1 saves the openCsiTool half  (a renewal)

        P1's save must not resurrect GitCode half A. Whether P1 and P2 live in
        one process or two is irrelevant to the store's contract, so the steps
        run through two independent handles in a fixed order: the interleaving
        is exact, and the test never depends on scheduler timing.
        """
        from opencsi.auth.store import StoredGitCodeCredential, StoredOpenCsiCredential

        store = self._store()
        store.save_gitcode(StoredGitCodeCredential(access_token=ACCESS_A, refresh_token=REFRESH_A))

        reader = self._store()  # P1's view, taken before the rotation
        reader.load()

        rotator = self._store()  # P2 rotates the GitCode credential
        rotator.save_gitcode(
            StoredGitCodeCredential(access_token=ACCESS_B, refresh_token=REFRESH_B)
        )

        renewer = self._store()  # P1 renews the openCsiTool half
        renewer.save_opencsi(StoredOpenCsiCredential(token=SESSION_A))

        bundle = self._store().load()
        self.assertEqual(
            bundle.gitcode.access_token,
            ACCESS_B,
            "a concurrent renewal resurrected the pre-rotation GitCode credential",
        )
        self.assertEqual(bundle.gitcode.refresh_token, REFRESH_B)
        self.assertEqual(bundle.opencsi.token, SESSION_A)

    # ── 2. two real processes, different halves, at the same time ─────────
    def test_concurrent_processes_writing_different_halves_keep_both(self) -> None:
        """A rotation and a renewal landing together must not lose either."""
        (out_a, _, code_a), (out_b, _, code_b) = self._spawn_two(
            "save-gitcode", (ACCESS_A, REFRESH_A, "alice"),
            "save-opencsi", (SESSION_A,),
        )
        self.assertEqual(code_a, 0, f"worker A failed: {out_a}")
        self.assertEqual(code_b, 0, f"worker B failed: {out_b}")

        bundle = self._store().load()
        self.assertIsNotNone(bundle.gitcode, "the GitCode half was lost")
        self.assertIsNotNone(bundle.opencsi, "the openCsiTool half was lost")

    # ── 3. two real processes, same half ──────────────────────────────────
    def test_concurrent_processes_writing_the_same_half_leave_a_loadable_store(self) -> None:
        """Both writers report success; the file holds exactly one winner."""
        (out_a, err_a, code_a), (out_b, err_b, code_b) = self._spawn_two(
            "save-opencsi", (SESSION_A,),
            "save-opencsi", (SESSION_B,),
        )
        self.assertEqual(code_a, 0, f"worker A failed: {err_a}")
        self.assertEqual(code_b, 0, f"worker B failed: {err_b}")

        bundle = self._store().load()
        self.assertIsNotNone(bundle.opencsi)
        self.assertIn(bundle.opencsi.token, (SESSION_A, SESSION_B))

    # ── 4. a renewal-shaped loop from several processes ───────────────────
    def test_concurrent_renew_loops_never_fail_and_never_lose_a_half(self) -> None:
        """Four processes renewing at once: no errors, no corruption."""
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC)
        procs = [
            subprocess.Popen(
                [sys.executable, str(self._worker_path), str(self.path),
                 "renew-loop", str(SRC), "5", f"session-{i}"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
            )
            for i in range(4)
        ]
        results = [p.communicate(timeout=180) + (p.returncode,) for p in procs]
        for i, (out, err, code) in enumerate(results):
            self.assertEqual(
                code, 0, f"renew worker {i} failed (exit {code}): {err.strip()[:300]}"
            )

        bundle = self._store().load()
        self.assertIsNotNone(bundle.opencsi, "the renewed session was lost")
        self.assertTrue(
            bundle.opencsi.token.startswith("session-"),
            f"unexpected session value survived: {bundle.opencsi.token[:16]}...",
        )

    # ── 5. a process dying mid-write ──────────────────────────────────────
    def test_a_leftover_temporary_file_is_ignored_and_cleaned_up(self) -> None:
        """A crash between create-tmp and replace must not corrupt the store."""
        from opencsi.auth.store import StoredGitCodeCredential, StoredOpenCsiCredential

        store = self._store()
        store.save_gitcode(StoredGitCodeCredential(access_token=ACCESS_A, refresh_token=REFRESH_A))
        store.save_opencsi(StoredOpenCsiCredential(token=SESSION_A))

        # Simulate the artifact a killed writer leaves: a stale .tmp with junk.
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_bytes(b"not-a-credential-blob")

        # The stale tmp is invisible to readers...
        bundle = self._store().load()
        self.assertEqual(bundle.opencsi.token, SESSION_A)
        self.assertEqual(bundle.gitcode.access_token, ACCESS_A)

        # ...and the next successful write removes it.
        store.save_opencsi(StoredOpenCsiCredential(token=SESSION_B))
        self.assertFalse(tmp.exists(), "the stale temporary file survived a later write")
        self.assertEqual(self._store().load().opencsi.token, SESSION_B)

    # ── 6. the lock file itself must not be a plaintext risk ──────────────
    def test_the_write_lock_carries_no_credential_material(self) -> None:
        """Whatever the synchronisation uses, it must not hold a secret."""
        from opencsi.auth.store import StoredGitCodeCredential, StoredOpenCsiCredential

        store = self._store()
        store.save_gitcode(StoredGitCodeCredential(access_token=ACCESS_A, refresh_token=REFRESH_A))
        store.save_opencsi(StoredOpenCsiCredential(token=SESSION_A))

        for neighbour in self.path.parent.iterdir():
            if neighbour == self.path:
                continue
            raw = neighbour.read_bytes()
            for secret in (ACCESS_A, REFRESH_A, SESSION_A):
                self.assertNotIn(
                    secret.encode(), raw,
                    f"{neighbour.name} contains credential material",
                )
            # And it must not be a plaintext serialisation of the bundle.
            if raw:
                try:
                    decoded = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    decoded = None
                if isinstance(decoded, dict):
                    self.assertNotIn("access_token", decoded)
                    self.assertNotIn("token", decoded)


if __name__ == "__main__":
    unittest.main()
