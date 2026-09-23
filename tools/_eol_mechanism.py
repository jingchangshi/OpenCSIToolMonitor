"""Does .gitattributes' `text=auto eol=lf` actually keep CRLF out of the index?

The commit history says it does not:

  ddd7724  build: pin line endings...   http_oauth.py  CRLF 928 -> 0   (normalised)
  adb0437  renew: persist the minted... http_oauth.py  CRLF   0 -> 928 (re-introduced!)
  ddd7724                               session.py     694 -> 0
  ceb09a1  renew: stop throwing away... session.py       0 -> 694
  ddd7724                               startup.py     327 -> 0
  b2dcf98  tray: report which context... startup.py      0 -> 327

So the pin was added, three files were normalised, and then the next edit of each
put the CRLF straight back. That is the whole point of the attribute failing.

This reproduces it in a throwaway repository rather than by reasoning about git's
documentation, so the mechanism is measured. Three questions:

  1. With `text=auto eol=lf`, does `git add` of a CRLF file store LF?
  2. Does `core.autocrlf=false` change that?
  3. Does `git add --renormalize` fix an index that already holds CRLF?
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


def run(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, cwd=cwd,
        encoding="utf-8", errors="replace",
    ).stdout


def blob_crlf(repo: Path, ref: str) -> int:
    data = subprocess.run(
        ["git", "show", ref], capture_output=True, cwd=repo
    ).stdout
    return data.count(b"\r\n")


with tempfile.TemporaryDirectory() as tmp:
    repo = Path(tmp)
    run(repo, "init", "-q")
    run(repo, "config", "user.email", "t@example.com")
    run(repo, "config", "user.name", "t")

    (repo / ".gitattributes").write_bytes(b"* text=auto eol=lf\n")
    run(repo, "add", ".gitattributes")
    run(repo, "commit", "-q", "-m", "pin")

    print("case 1: add a CRLF file WITH the attribute already present")
    (repo / "a.py").write_bytes(b"line1\r\nline2\r\n")
    run(repo, "add", "a.py")
    print(f"   index blob CRLF: {blob_crlf(repo, ':a.py')}  (0 = attribute worked)")

    run(repo, "commit", "-q", "-m", "a")
    print(f"   committed blob CRLF: {blob_crlf(repo, 'HEAD:a.py')}")

    print()
    print("case 2: an index entry that already holds CRLF, then edited")
    # Force CRLF into the index by disabling the attribute temporarily.
    (repo / ".gitattributes").write_bytes(b"* -text\n")
    run(repo, "add", ".gitattributes")
    (repo / "b.py").write_bytes(b"x\r\ny\r\n")
    run(repo, "add", "b.py")
    print(f"   b.py in index with text disabled: CRLF {blob_crlf(repo, ':b.py')}")
    run(repo, "commit", "-q", "-m", "b-crlf")

    # Now restore the attribute and edit the file, as the later commits did.
    (repo / ".gitattributes").write_bytes(b"* text=auto eol=lf\n")
    run(repo, "add", ".gitattributes")
    run(repo, "commit", "-q", "-m", "repin")
    (repo / "b.py").write_bytes(b"x\r\ny\r\nz\r\n")
    run(repo, "add", "b.py")
    print(f"   after edit+add WITH attribute restored: CRLF {blob_crlf(repo, ':b.py')}")
    print("   -> if this is non-zero, a normal `git add` does NOT renormalise")
    print("      an existing index entry, which is exactly the history above")

    print()
    print("case 3: does --renormalize fix it?")
    run(repo, "add", "--renormalize", "b.py")
    print(f"   after git add --renormalize: CRLF {blob_crlf(repo, ':b.py')}")

    print()
    print("case 4: core.autocrlf setting in effect")
    print(f"   core.autocrlf = {run(repo, 'config', 'core.autocrlf').strip() or '(unset)'}")
    print(f"   check-attr b.py: {run(repo, 'check-attr', 'text', 'eol', '--', 'b.py').strip()}")
