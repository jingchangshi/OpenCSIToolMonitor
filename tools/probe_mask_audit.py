"""Sweep every JSON-emitting command for unexpected over-masking.

Defect 14 was found because a documented numeric field came out as the string
"<redacted>". The same rule produced that, so the question worth asking is: where
*else* does a value that is plainly not a secret get masked?

This runs each read-only command with --json and reports every occurrence of the
mask, with its path, so each one can be judged rather than assumed. A mask is
correct when it stands where a credential would be, and wrong when it replaces a
number, a duration, a count or a boolean.

Read-only: no command here mutates anything, none is given --renew, and no cookie
value or token is printed -- only the paths at which the mask itself appears.
"""
#: labels: LIVE, GET_ONLY

from __future__ import annotations

import json
import subprocess
import sys

sys.path.insert(0, "src")

from opencsi.redaction import MASK  # noqa: E402

#: Commands that emit JSON without changing state.
COMMANDS: list[list[str]] = [
    ["doctor", "--json", "--skip-contract"],
    ["login", "--status", "--json"],
    ["tray", "--once", "--json"],
    ["tray", "--check", "--json"],
    ["usage", "--json"],
    ["status", "--json"],
    ["tools", "--json"],
    ["logs", "--json", "--page-size", "2"],
]


def walk(node, path: str, found: list[tuple[str, object]]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            walk(value, f"{path}.{key}", found)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            walk(value, f"{path}[{index}]", found)
    elif node == MASK:
        found.append((path, node))


def main() -> int:
    wrong: list[str] = []
    for command in COMMANDS:
        argv = [sys.executable, "-m", "opencsi", *command, "--no-proxy"]
        # The CLI writes UTF-8 regardless of the console code page (this machine
        # is GBK/936), so decoding with the locale codec corrupts Chinese output
        # and can raise before JSON is even parsed. Ask for UTF-8 explicitly and
        # replace rather than fail, so one odd byte cannot abort the audit.
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        label = " ".join(command)
        if proc.returncode != 0:
            print(f"{label:34s} exit={proc.returncode} (skipped)")
            continue
        if not proc.stdout:
            print(f"{label:34s} no output (skipped)")
            continue
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            print(f"{label:34s} not JSON (skipped)")
            continue

        found: list[tuple[str, object]] = []
        walk(payload, "$", found)
        if not found:
            print(f"{label:34s} no masks")
            continue
        for path, _ in found:
            # A mask under a key that names a credential is the intended case.
            leaf = path.rsplit(".", 1)[-1].lower()
            looks_like_secret = any(
                word in leaf
                for word in ("token", "cookie", "secret", "password", "key", "auth")
            )
            verdict = "ok" if looks_like_secret else "SUSPECT"
            if not looks_like_secret:
                wrong.append(f"{label}: {path}")
            print(f"{label:34s} {path:52s} {verdict}")

    print()
    if wrong:
        print("paths where a mask replaced something that is not a credential:")
        for item in wrong:
            print(f"  {item}")
        return 1
    print("every mask stands where a credential would be")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
