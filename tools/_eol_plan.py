"""What would `git add --renormalize .` change, and is it safe?

The mechanism is now measured: `text=auto eol=lf` applies when a file is *newly
added*, but git does not renormalise an index entry that already holds CRLF. So
the pin has been half-working since ddd7724, and the next edit of any of the 21
affected files produces a whole-file diff -- which is the exact harm that commit
was written to prevent. It already happened once, in my own probe commit.

Renormalising is a one-time fix, but it rewrites 21 files, one of which is a PNG
that happens to contain a CRLF byte pair in its compressed data. A `text=auto`
rule misapplied to a binary would corrupt it irreversibly, so this checks the
blob hashes before deciding.

This performs the renormalisation in the *index* and reports what changed, then
restores the index. Nothing is committed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, cwd=ROOT,
        encoding="utf-8", errors="replace",
    ).stdout


def git_bytes(*args: str) -> bytes:
    return subprocess.run(["git", *args], capture_output=True, cwd=ROOT).stdout


def hashes() -> dict[str, str]:
    """Every staged/committed blob hash, so a rewrite is visible."""
    out = git("ls-files", "-s")
    result = {}
    for line in out.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            result[parts[1]] = parts[0].split()[1]
    return result


before = hashes()
png_before = git_bytes("show", "HEAD:tests/fixtures/gitcode_login_code.png")

# Stage everything with renormalisation.
git("add", "--renormalize", ".")
after = hashes()

changed = [name for name in sorted(before) if before[name] != after.get(name)]
print(f"files whose blob hash would change: {len(changed)}")
print()
for name in changed:
    print(f"  {name}")

print()
png_after = git_bytes("show", ":tests/fixtures/gitcode_login_code.png")
print("binary fixture check:")
print(f"  PNG identical: {png_before == png_after}")
print(f"  PNG bytes: {len(png_before)} -> {len(png_after)}")

# Restore the index to match HEAD.
git("reset", "-q")
restored = hashes()
print()
print(f"index restored to HEAD: {restored == before}")
