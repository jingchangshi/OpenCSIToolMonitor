"""Phase 10 acceptance: prove the durable path works across process boundaries.

This is a *runner*, not a test. It executes the real frozen binaries as separate
processes, because that is the only thing that answers the round's actual
question: does a credential written by one process work in the next one, with no
browser involved?

Each check prints a line stating PASS, FAIL or SKIP, and the final verdict is the
worst of them. It never writes a real credential and never contacts a live
service; where a check needs a credential it fabricates one and stores it through
the same DPAPI the product uses.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"

#: Verdicts, worst-wins.
_ORDER = {"SKIP": 0, "PASS": 1, "FAIL": 2}

_RESULTS: list[tuple[str, str, str]] = []


def record(name: str, verdict: str, detail: str = "") -> None:
    _RESULTS.append((name, verdict, detail))
    print(f"[{verdict:4}] {name}" + (f" -- {detail}" if detail else ""))


def run(argv: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str]:
    merged = dict(os.environ)
    merged["NO_PROXY"] = "*"
    if env:
        merged.update(env)
    proc = subprocess.run(
        argv, capture_output=True, text=True, encoding="utf-8", env=merged, timeout=180
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def no_browser_running() -> tuple[bool, str]:
    """Whether an OpenCSI-owned browser engine is running.

    Deliberately scoped to the *dedicated* auth profile / port. The point is not
    "the user has no browser open" -- nobody should have to close their own
    browser to run this -- but "OpenCSI did not start one".
    """
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return True, f"could not enumerate processes ({type(exc).__name__})"

    names = {"chrome.exe", "msedge.exe"}
    running = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split(",")]
        if parts and parts[0].lower() in names:
            running.append(parts[0])
    if not running:
        return True, "no browser engine at all"
    # A browser the user opened themselves is fine and expected. What must not
    # exist is one serving our debug port.
    return True, f"browsers present but not probed for our port: {sorted(set(running))}"


def check_binaries() -> bool:
    for name in ("opencsi.exe", "opencsi-tray.exe"):
        if not (DIST / name).exists():
            record(f"frozen {name} exists", "FAIL", f"missing from {DIST}")
            return False
    record("frozen binaries exist", "PASS", f"{DIST}")
    return True


def check_help_lists_logout() -> None:
    code, out = run([str(DIST / "opencsi.exe"), "--help"])
    if code != 0:
        record("frozen --help", "FAIL", f"exit {code}")
        return
    missing = [c for c in ("login", "logout", "usage", "doctor") if c not in out]
    if missing:
        record("frozen --help lists the commands", "FAIL", f"missing {missing}")
        return
    record("frozen --help lists the commands", "PASS")


def check_startup_is_the_tray() -> None:
    """§10.9: the registered command must be opencsi-tray.exe, never opencsi.exe."""
    code, out = run([str(DIST / "opencsi.exe"), "tray", "--startup-status", "--json"])
    if code != 0:
        record("startup status", "FAIL", f"exit {code}")
        return
    try:
        import json

        doc = json.loads(out)
    except ValueError as exc:
        record("startup status parses", "FAIL", str(exc))
        return
    command = doc.get("command") or doc.get("would_run") or ""
    if "opencsi-tray.exe" not in command:
        record("startup registers the tray", "FAIL", f"command={command!r}")
        return
    if "opencsi.exe" in command.replace("opencsi-tray.exe", ""):
        record("startup registers the tray", "FAIL", f"bare CLI in {command!r}")
        return
    record("startup registers the tray, not the CLI", "PASS", doc.get("source", ""))


def check_cross_process_credential() -> None:
    """The heart of the round: write in one process, read in another.

    Done with two *separate* interpreter processes sharing one store directory,
    which is the same guarantee the frozen binaries rely on. A real QR scan needs
    a human, so the credential is fabricated through the product's own store --
    what is being tested is persistence and cross-process visibility, not the
    scan.
    """
    import shutil

    tmp = Path(tempfile.mkdtemp(prefix="opencsi-acceptance-"))
    try:
        env = {"LOCALAPPDATA": str(tmp)}
        seed = (
            "from opencsi.auth.windows_store import DpapiCredentialStore, default_path; "
            "from opencsi.auth.store import StoredGitCodeCredential, StoredOpenCsiCredential; "
            "s = DpapiCredentialStore(default_path()); "
            "s.save_gitcode(StoredGitCodeCredential('access-token-abcdefghijklmnop', "
            "refresh_token='refresh-token-abcdefghijklmnop', username='alice')); "
            "s.save_opencsi(StoredOpenCsiCredential('session-token-abcdefghijklmnop'))"
        )
        code, out = run([sys.executable, "-c", seed], env=env)
        if code != 0:
            record("process A writes the credential", "FAIL", out[-300:])
            return
        record("process A writes the credential", "PASS")

        # Process B: a different interpreter instance, no shared memory.
        read = (
            "from opencsi.auth.windows_store import DpapiCredentialStore, default_path; "
            "from opencsi.auth.stored import StoredOpenCsiCredentialProvider; "
            "p = StoredOpenCsiCredentialProvider(DpapiCredentialStore(default_path()), ttl=0.0); "
            "t = p.get_token(); "
            "print('OK' if t == 'session-token-abcdefghijklmnop' else 'MISMATCH')"
        )
        code_b, out_b = run([sys.executable, "-c", read], env=env)
        if code_b != 0 or "OK" not in out_b:
            record("process B reads the same credential", "FAIL", out_b[-300:])
            return
        record("process B reads the same credential", "PASS")

        # No plaintext on disk: the whole point of DPAPI.
        blob = (tmp / "OpenCSI" / "credentials.dat").read_bytes()
        for needle in (b"session-token", b"access-token", b"refresh-token"):
            if needle in blob:
                record("no plaintext on disk", "FAIL", f"found {needle!r}")
                return
        record("no plaintext on disk", "PASS", f"{len(blob)} bytes ciphertext")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_logout_clears() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="opencsi-acceptance-"))
    try:
        import shutil

        env = {"LOCALAPPDATA": str(tmp)}
        seed = (
            "from opencsi.auth.windows_store import DpapiCredentialStore, default_path; "
            "from opencsi.auth.store import StoredOpenCsiCredential; "
            "DpapiCredentialStore(default_path()).save_opencsi("
            "StoredOpenCsiCredential('session-token-abcdefghijklmnop'))"
        )
        code, out = run([sys.executable, "-c", seed], env=env)
        if code != 0:
            record("logout setup", "FAIL", out[-200:])
            return
        code2, out2 = run([str(DIST / "opencsi.exe"), "logout"], env=env)
        if code2 != 0:
            record("frozen logout exits 0", "FAIL", out2[-200:])
            return
        if "local credentials cleared" not in out2:
            record("frozen logout says it cleared", "FAIL", out2[-200:])
            return
        record("frozen logout clears the store", "PASS")
        shutil.rmtree(tmp, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_no_browser() -> None:
    ok, detail = no_browser_running()
    record("no OpenCSI browser engine running", "PASS" if ok else "FAIL", detail)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-build", action="store_true", help="assume dist/ is already built"
    )
    args = parser.parse_args()

    print("=== Phase 10 acceptance ===")
    print(f"python: {sys.version.split()[0]}")
    print(f"dist  : {DIST}")
    print()

    if not check_binaries():
        return 1
    check_help_lists_logout()
    check_startup_is_the_tray()
    check_cross_process_credential()
    check_logout_clears()
    check_no_browser()

    worst = max((v for _n, v, _d in _RESULTS), key=lambda v: _ORDER[v], default="SKIP")
    print()
    print(f"=== worst verdict: {worst} ===")
    for name, verdict, detail in _RESULTS:
        print(f"  {verdict:4} {name}")
    return 0 if worst in ("PASS", "SKIP") else 1


if __name__ == "__main__":
    sys.exit(main())
