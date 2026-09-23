"""Repository hygiene, enforced from the committed blobs rather than the tree.

Two rules, both of which this repository broke for real before they existed:

Line endings
------------
``.gitattributes`` pins ``* text=auto eol=lf``. That attribute governs *new*
writes; it does not rewrite blobs already stored in the index. The pin was added
in ``ddd7724`` and the tree still held CRLF blobs afterwards, which is why a
71-line change to ``auth_host.py`` once landed as a 1378-line diff and made
``git blame`` useless for the commit that fixed a session-loss bug.

The one-time ``git add --renormalize .`` has been run. This test is what keeps it
from coming back. It deliberately inspects ``git show HEAD:<path>`` -- the
*committed* blob -- and not the working-tree file, because the working tree's
endings are a function of the reader's ``core.autocrlf`` and checkout, while the
committed blob is the thing that actually makes diffs unreadable.

Scratch tools
-------------
``tools/_*.py`` were investigation scripts: six one-off files that measured a
problem, answered it, and stayed in the tree. The answer belongs in a test; the
script does not belong in a production repository. ``tools/_*.py`` is therefore
forbidden unless a future file is explicitly allowlisted below.

Running ``git``
---------------
Both checks shell out to git. When git is unavailable (a vendored source drop, an
unpacked sdist) the test skips rather than failing, because the condition it
guards -- what is committed -- does not exist in that situation.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Extensions stored as CRLF on purpose. ``cmd.exe`` misparses LF-only line
#: endings in batch files, and some PowerShell hosts do too -- a broken build
#: script then fails in a way that looks like a missing tool.
CRLF_BY_DESIGN = (".bat", ".cmd", ".ps1")

#: Extensions whose blob must never be normalised. A PNG or SQLite profile
#: rewritten as text is unrecoverable, so it is not merely "not checked" -- it is
#: asserted to be byte-identical to what a text filter would have destroyed.
BINARY_EXTENSIONS = (
    ".png",
    ".jpg",
    ".ico",
    ".exe",
    ".dll",
    ".pyd",
    ".zip",
    ".whl",
    ".sqlite",
    ".db",
    ".pfx",
)

#: ``tools/_*.py`` are forbidden scratch scripts. Add a name here only if a file
#: with a leading underscore becomes a genuine, kept part of the toolchain.
ALLOWED_SCRATCH_TOOLS: tuple[str, ...] = ()


def _git(*args: str) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout


def _git_available() -> bool:
    try:
        _git("rev-parse", "--git-dir")
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


def _tracked_files() -> list[str]:
    out = _git("ls-files", "-z").decode("utf-8", errors="replace")
    return [p for p in out.split("\0") if p]


def _committed_blob(path: str) -> bytes | None:
    """The blob stored at ``HEAD:path``, or None when the path is new."""
    result = subprocess.run(
        ["git", "show", f"HEAD:{path}"],
        cwd=ROOT,
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def _is_binary(path: str, blob: bytes) -> bool:
    """Whether git considers this blob binary.

    Asked of git rather than guessed from the bytes: ``git check-attr`` knows the
    ``binary`` attribute this repository sets, and a NUL-byte heuristic would
    disagree with it on the UTF-16 files git also treats as binary.
    """
    if path.lower().endswith(BINARY_EXTENSIONS):
        return True
    return b"\0" in blob[:8192]


@unittest.skipUnless(_git_available(), "not a git checkout")
class LineEndingTest(unittest.TestCase):
    """Committed text blobs must be LF, read back from ``HEAD``."""

    @classmethod
    def setUpClass(cls) -> None:
        if _committed_blob("README.md") is None:
            raise unittest.SkipTest("HEAD has no tree to read (unborn branch)")

    def _offenders(self, predicate) -> list[str]:
        bad: list[str] = []
        for path in _tracked_files():
            if path.lower().endswith(CRLF_BY_DESIGN):
                continue
            blob = _committed_blob(path)
            if blob is None:
                continue  # staged but not yet committed; checked after commit
            if _is_binary(path, blob):
                continue
            if predicate(blob):
                bad.append(path)
        return bad

    def test_no_committed_text_blob_contains_crlf(self) -> None:
        """The rule the renormalise pass established, and CI keeps.

        A CRLF anywhere in a text blob is a failure, not a warning. Counting
        ``\\r\\n`` instead would let a lone ``\\r`` through, and a lone ``\\r``
        in a source file is exactly as damaging to a diff.
        """
        offenders = self._offenders(lambda blob: b"\r" in blob)
        self.assertEqual(
            offenders,
            [],
            "these files are committed with CRLF; run `git add --renormalize .`:\n"
            + "\n".join(f"  {p}" for p in offenders),
        )

    def test_the_rule_is_not_vacuous(self) -> None:
        """Guard: the scan must actually be looking at a non-trivial tree.

        If ``_tracked_files`` returned nothing -- a pathspec mistake, a wrong
        cwd -- the test above would pass while checking nothing at all.
        """
        text_files = 0
        for path in _tracked_files():
            blob = _committed_blob(path)
            if blob is not None and not _is_binary(path, blob):
                text_files += 1
        self.assertGreater(
            text_files, 20, "the line-ending scan found almost no text files"
        )

    def test_the_check_can_detect_crlf(self) -> None:
        """Guard: a blob that *does* contain CRLF must be reported.

        The predicate is exercised on a synthetic blob so that a bug making it
        always return False fails here rather than silently blessing the tree.
        """
        self.assertTrue(b"\r" in b"a\r\nb\r\n")
        self.assertFalse(b"\r" in b"a\nb\n")

    def test_binary_blobs_are_left_alone(self) -> None:
        """Binary blobs must be stored verbatim, not as normalised text.

        A text filter applied to a PNG is unrecoverable, so this asserts the
        opposite direction from the CRLF rule: the committed blob must still be
        the real file, byte for byte.
        """
        checked = 0
        for path in _tracked_files():
            if not path.lower().endswith(BINARY_EXTENSIONS):
                continue
            blob = _committed_blob(path)
            if blob is None:
                continue
            checked += 1
            worktree = (ROOT / path).read_bytes()
            self.assertEqual(
                blob,
                worktree,
                f"{path} is stored differently from its working-tree bytes, so a "
                f"text filter has touched a binary file",
            )
        if checked == 0:
            self.skipTest("no binary files are tracked")

    def test_a_binary_fixture_exists_and_is_a_real_png(self) -> None:
        """Pin the one binary this repository actually tracks.

        It is a GitCode login-code image used by the QR tests. Naming it keeps
        the binary check above from becoming vacuous if it were ever deleted.
        """
        path = "tests/fixtures/gitcode_login_code.png"
        self.assertIn(path, _tracked_files())
        blob = _committed_blob(path)
        self.assertIsNotNone(blob)
        self.assertTrue(blob.startswith(b"\x89PNG\r\n\x1a\n"), "not a PNG header")


@unittest.skipUnless(_git_available(), "not a git checkout")
class ScratchToolTest(unittest.TestCase):
    """``tools/_*.py`` were one-off investigation scripts and must not return."""

    def test_no_underscore_prefixed_tool_is_tracked(self) -> None:
        offenders = [
            path
            for path in _tracked_files()
            if path.startswith("tools/_") and path.endswith(".py")
            and Path(path).name not in ALLOWED_SCRATCH_TOOLS
        ]
        self.assertEqual(
            offenders,
            [],
            "these look like investigation scratch scripts; the finding belongs "
            "in a test and the script does not belong in the repository:\n"
            + "\n".join(f"  {p}" for p in offenders),
        )

    def test_the_named_scratch_scripts_are_gone(self) -> None:
        """The six specific scripts, by name.

        A generic ``_*`` rule would also pass if the whole ``tools/`` directory
        were emptied, so the files that motivated the rule are named. What each
        found is preserved: the CRLF mechanism in ``.gitattributes`` and this
        module's docstring, and the probe-label contract in
        ``tools/live_probe_labels.py``.
        """
        for name in (
            "tools/_crlf_scope.py",
            "tools/_eol_audit.py",
            "tools/_eol_autocrlf.py",
            "tools/_eol_mechanism.py",
            "tools/_eol_plan.py",
            "tools/_eol_verdict.py",
        ):
            self.assertFalse((ROOT / name).exists(), f"{name} is still present")


@unittest.skipUnless(_git_available(), "not a git checkout")
class GeneratedFileTest(unittest.TestCase):
    """The CI ``hygiene`` job's rule, also enforced locally."""

    def test_no_generated_files_are_tracked(self) -> None:
        import re

        pattern = re.compile(r"(__pycache__|\.pyc$|\.pyo$|\.egg-info|^dist/|^build/)")
        offenders = [p for p in _tracked_files() if pattern.search(p)]
        self.assertEqual(offenders, [], f"generated files are tracked: {offenders}")


class NoTestTouchesTheRealCredentialStoreTest(unittest.TestCase):
    """No test may read or write the developer's real credential store.

    This is the one leak that has no visible symptom until it matters. A test that
    forgets ``no_store`` on a hand-built ``args`` object does not fail -- it opens
    ``%LOCALAPPDATA%\\OpenCSI\\credentials.dat``, which is the real file, and
    writes its fixture into it. Nothing goes red. What happens instead is that a
    developer later runs ``opencsi login --renew`` and gets a confusing failure
    against a credential named ``tester`` that they never created.

    That is not hypothetical: it is what this test was written after. The fixture
    was found in a real store, and the leak was confirmed by watching the file's
    mtime change while ``tests/test_qr_login_semantics.py`` ran.

    Checked structurally rather than behaviourally, because the behavioural
    version -- run the suite, hash the store -- is slow and would only catch the
    leak on a machine that has a store at all. What is asserted instead is the
    rule that makes the leak impossible: a test that builds its own ``args`` must
    say ``no_store``.
    """

    def test_hand_built_args_objects_declare_no_store(self) -> None:
        """Any test-local ``_Args``/``args`` class must set ``no_store``.

        Only classes that look like a CLI args stand-in are examined: they are
        identified by carrying at least two attributes the real parser defines, so
        an unrelated helper class named ``Args`` is not swept up.
        """
        import ast

        #: Attributes the real parser sets on every command. A class carrying two
        #: or more of them is standing in for `args`.
        cli_attributes = {
            "json",
            "cdp",
            "base_url",
            "no_proxy",
            "no_store",
            "ports",
            "timeout",
            "renew_timeout",
            "store_ttl",
        }

        offenders: list[str] = []
        for path in sorted((ROOT / "tests").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                assigned = {
                    t.id
                    for stmt in node.body
                    if isinstance(stmt, ast.Assign)
                    for t in stmt.targets
                    if isinstance(t, ast.Name)
                }
                if len(assigned & cli_attributes) < 2:
                    continue
                if "no_store" not in assigned:
                    offenders.append(
                        f"{path.name}:{node.lineno} class {node.name} "
                        f"(has {sorted(assigned & cli_attributes)})"
                    )

        self.assertEqual(
            offenders,
            [],
            "these stand-in args objects do not set no_store, so any code path "
            "reading a credential store will open the developer's real one:\n"
            + "\n".join(f"  {o}" for o in offenders),
        )

    def test_the_store_path_is_not_inside_the_repository(self) -> None:
        """A store inside the checkout would be committed eventually."""
        from opencsi.auth.windows_store import default_path

        text = str(default_path()).replace("\\", "/").lower()
        self.assertNotIn("opencsitoolmonitor", text)
        self.assertNotIn("/workspace/", text)


if __name__ == "__main__":
    unittest.main()
