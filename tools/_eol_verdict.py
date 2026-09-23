"""Separate a real line-ending CONVERSION from an edit that keeps CRLF.

The raw CRLF count is not enough to tell these apart:

  probe_consent_menu.py  53 -> 0    every line changed ending  = CONVERSION
  login.py             1215 -> 1224 nine new lines, all CRLF  = edit only
  test_cli_session.py   557 -> 653  96 new lines, all CRLF    = edit only

The distinguishing question is whether the lines that already existed kept their
ending. If they did, the file's convention is unchanged and a later normalisation
is still pending; if they did not, the commit rewrote the whole file, which is
what makes `git blame` useless.

This compares the parent and child blobs line by line: every line present in the
parent must appear in the child with the same ending.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CASES = [
    ("b8cac4c~1", "b8cac4c", "tools/probe_consent_menu.py"),
    ("baa1f3a~1", "baa1f3a", "src/opencsi/cli/login.py"),
    ("baa1f3a~1", "baa1f3a", "tests/test_cli_session.py"),
    ("ddd7724~1", "ddd7724", "src/opencsi/auth/http_oauth.py"),
    ("adb0437~1", "adb0437", "src/opencsi/auth/http_oauth.py"),
    ("ddd7724~1", "ddd7724", "src/opencsi/auth/session.py"),
    ("ceb09a1~1", "ceb09a1", "src/opencsi/auth/session.py"),
    ("ddd7724~1", "ddd7724", "src/opencsi/tray/startup.py"),
    ("b2dcf98~1", "b2dcf98", "src/opencsi/tray/startup.py"),
]


def blob(ref: str) -> bytes:
    return subprocess.run(
        ["git", "show", ref], capture_output=True, cwd=ROOT, check=True
    ).stdout


def endings(data: bytes) -> tuple[int, int]:
    lines = data.split(b"\n")
    crlf = sum(1 for line in lines if line.endswith(b"\r"))
    return len(lines), crlf


print(f"{'file':<40} {'parent':<14} {'child':<14} verdict")
print("-" * 88)
for parent, child, name in CASES:
    p = blob(f"{parent}:{name}")
    c = blob(f"{child}:{name}")
    pl, pc = endings(p)
    cl, cc = endings(c)

    # Did the lines that existed before keep their ending?
    p_set = set(p.split(b"\n"))
    c_set = set(c.split(b"\n"))
    lost = [line for line in p_set if line not in c_set]
    lost_with_cr = [line for line in lost if line.endswith(b"\r")]
    lost_without_cr = [line for line in lost if not line.endswith(b"\r")]

    if pc > 0 and cc == 0:
        verdict = "CONVERSION (all CRLF stripped)"
    elif pc > 0 and lost_with_cr and not lost_without_cr:
        verdict = "CONVERSION (CRLF lines rewritten)"
    elif cc > pc and pc > 0:
        verdict = "edit only (kept CRLF)"
    elif pc == 0 and cc > 0:
        verdict = "RE-INTRODUCED CRLF"
    elif pc == 0 and cc == 0:
        verdict = "clean (LF throughout)"
    else:
        verdict = f"changed ({pc} -> {cc})"

    print(
        f"{name:<40} {f'{pl}L/{pc}c':<14} {f'{cl}L/{cc}c':<14} {verdict}"
    )
