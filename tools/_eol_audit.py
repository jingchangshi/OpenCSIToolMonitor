"""Did any of my commits convert line endings in a file I only meant to edit?

The probe commit normalised probe_consent_menu.py (53 CRLF -> 0) as a side effect
of the edit. That is the exact failure `.gitattributes` was added to prevent, so
this checks every commit since the baseline for the same thing: a file whose CRLF
count changed while the change was supposed to be a small edit.

A whole-file conversion is not cosmetic here -- it makes `git blame` useless for
the commit that fixed a real bug.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "0637051"
INTENTIONAL = (".bat", ".cmd", ".ps1")


def git_bytes(*args: str) -> bytes:
    return subprocess.run(
        ["git", *args], capture_output=True, cwd=ROOT, check=True
    ).stdout


def git_text(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, cwd=ROOT, check=True,
        encoding="utf-8", errors="replace",
    ).stdout


commits = git_text("log", "--format=%h %s", f"{BASELINE}..HEAD").splitlines()
print(f"commits since baseline: {len(commits)}")
print()

conversions = []
for line in commits:
    sha = line.split()[0]
    subject = line.split(" ", 1)[1] if " " in line else ""
    files = [f for f in git_text("show", "--name-only", "--format=", sha).split("\n") if f]
    for name in files:
        if name.endswith(INTENTIONAL):
            continue
        try:
            parent = git_bytes("show", f"{sha}~1:{name}")
        except subprocess.CalledProcessError:
            continue  # added in this commit
        try:
            child = git_bytes("show", f"{sha}:{name}")
        except subprocess.CalledProcessError:
            continue  # deleted in this commit
        before = parent.count(b"\r\n")
        after = child.count(b"\r\n")
        if before != after:
            conversions.append((sha, subject[:52], name, before, after))

if conversions:
    print("LINE-ENDING CONVERSIONS:")
    for sha, subject, name, before, after in conversions:
        print(f"  {sha} {subject}")
        print(f"      {name}: CRLF {before} -> {after}")
else:
    print("no line-ending conversions in any commit since the baseline")
