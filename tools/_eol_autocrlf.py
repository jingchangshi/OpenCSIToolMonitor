"""Why did `text=auto eol=lf` fail to keep CRLF out, with the repo's real config?

The history shows the pin being defeated:

  ddd7724  normalised http_oauth.py / session.py / startup.py  (CRLF -> 0)
  adb0437  http_oauth.py re-introduced CRLF                     (0 -> 928)
  ceb09a1  session.py re-introduced CRLF                        (0 -> 694)
  b2dcf98  startup.py re-introduced CRLF                        (0 -> 327)

The earlier mechanism run used a temp repo whose `core.autocrlf` defaulted to
true. The real repository sets `core.autocrlf=false` in `.git/config`, and that
is the variable that differs. So this repeats the two decisive cases with
`core.autocrlf=false`, and adds the case that matches the history: a file whose
*index entry already holds CRLF* is edited again.

It also checks `--renormalize`, because if that is the only thing that works, the
fix has to be a one-time renormalisation commit rather than a config change.
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


def crlf_in(cwd: Path, ref: str) -> int:
    data = subprocess.run(["git", "show", ref], capture_output=True, cwd=cwd).stdout
    return data.count(b"\r\n")


def setup(cwd: Path, autocrlf: str) -> None:
    run(cwd, "init", "-q")
    run(cwd, "config", "user.email", "t@example.com")
    run(cwd, "config", "user.name", "t")
    run(cwd, "config", "core.autocrlf", autocrlf)
    (cwd / ".gitattributes").write_bytes(b"* text=auto eol=lf\n")


for autocrlf in ("false", "true"):
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        setup(repo, autocrlf)
        print(f"=== core.autocrlf={autocrlf} ===")

        # Case A: brand-new CRLF file, attribute already in place.
        (repo / "new.py").write_bytes(b"a\r\nb\r\n")
        run(repo, "add", "new.py")
        print(f"  A new CRLF file, git add          -> index CRLF {crlf_in(repo, ':new.py')}")

        # Case B: CRLF already in the index (committed while text was disabled),
        # then edited again with the attribute restored. This is the history.
        (repo / ".gitattributes").write_bytes(b"* -text\n")
        run(repo, "add", ".gitattributes")
        (repo / "old.py").write_bytes(b"x\r\ny\r\n")
        run(repo, "add", "old.py")
        run(repo, "commit", "-q", "-m", "crlf")
        (repo / ".gitattributes").write_bytes(b"* text=auto eol=lf\n")
        run(repo, "add", ".gitattributes")
        run(repo, "commit", "-q", "-m", "pin")
        (repo / "old.py").write_bytes(b"x\r\ny\r\nz\r\n")
        run(repo, "add", "old.py")
        print(f"  B existing CRLF entry, edited+add -> index CRLF {crlf_in(repo, ':old.py')}")

        # Case C: does --renormalize fix it?
        run(repo, "add", "--renormalize", "old.py")
        print(f"  C after add --renormalize         -> index CRLF {crlf_in(repo, ':old.py')}")

        # Case D: what does a fresh checkout produce?
        run(repo, "commit", "-q", "-m", "after-renorm")
        out = Path(tmp) / "clone"
        subprocess.run(
            ["git", "clone", "-q", str(repo), str(out)], capture_output=True
        )
        wt = (out / "old.py").read_bytes()
        print(f"  D fresh clone working tree CRLF   -> {wt.count(b'\r\n')}")
        print()
