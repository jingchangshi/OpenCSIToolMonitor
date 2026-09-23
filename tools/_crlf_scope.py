"""Find every tracked file still committed with CRLF, now that .gitattributes pins LF.

`.gitattributes` pins `* text=auto eol=lf`, so any file committed with CRLF
before it existed stays CRLF in the index until something rewrites it. My probe
edit happened to normalise probe_consent_menu.py and git showed it as a 53-line
whole-file rewrite -- which is exactly the symptom `.gitattributes`' own comment
says makes `git blame` useless.

So this measures the real scope: which tracked files still carry CRLF in the
committed blob, despite the attribute saying they must not.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Extensions that .gitattributes declares CRLF on purpose.
INTENTIONAL_CRLF = (".bat", ".cmd", ".ps1")


def git_bytes(*args: str) -> bytes:
    return subprocess.run(
        ["git", *args], capture_output=True, cwd=ROOT, check=True
    ).stdout


names = [n for n in git_bytes("ls-files").decode("utf-8", "replace").split("\n") if n]

offenders = []
for name in names:
    if name.endswith(INTENTIONAL_CRLF):
        continue
    blob = git_bytes("show", f"HEAD:{name}")
    crlf = blob.count(b"\r\n")
    if crlf:
        offenders.append((name, crlf, len(blob)))

print(f"tracked files: {len(names)}")
print(f"files still committed with CRLF (excluding intentional .bat/.cmd/.ps1): {len(offenders)}")
print()
for name, crlf, size in sorted(offenders):
    print(f"  {name:<52} {crlf:>5} CRLF  ({size} bytes)")

print()
# Is the attribute actually being applied to them?
if offenders:
    sample = offenders[0][0]
    check = subprocess.run(
        ["git", "check-attr", "text", "eol", "--", sample],
        capture_output=True, text=True, cwd=ROOT, encoding="utf-8", errors="replace",
    ).stdout.strip()
    print(f"attributes for {sample}:")
    print(" ", check)
    print()
    print("Interpretation: the attribute applies to *future* writes. A blob already")
    print("in history keeps its CRLF until a commit rewrites the file, which is why")
    print("the first touch of each of these produces a whole-file diff.")
