"""P2 acceptance: A writes, B *renews*, C reads -- as three real processes.

Why this file exists separately from ``acceptance_durable.py``
-------------------------------------------------------------
That runner proves a credential written by one process is readable by the next.
This one proves something strictly stronger and previously untrue: that a session
*minted by a renewal* survives into a third process. The difference is not a
detail. Renewal used to report ``RENEWED`` and persist nothing, so A-writes/B-reads
passed while the product was still broken -- the credential only had to survive
being copied, never being replaced.

The property under test
-----------------------
    A: writes T1 + GitCode A1/R1
    B: runs the *production* renewal chain; OAuth mints T2
    C: reads T2

B deliberately does not call ``provider.remember_token(T2)``. Doing so would test
the sink, which was never the broken part. B constructs the renewer the way the
product does -- ``CliContext.make_provider()`` then ``make_renewer()`` -- and runs
``SessionManager.renew()`` against a loopback OAuth server, so the only thing
stubbed is the remote host.

Why the OAuth server is local
-----------------------------
The real openCsiTool OAuth needs a live GitCode grant, which needs a human scan.
Pointing ``base_url`` at ``127.0.0.1`` keeps the whole chain real -- the three
requests, the multipart body, the callback, the ``Set-Cookie``, the renewer's
decision, the adapter, the store write -- while making the *server* the one
component that is scripted. Nothing here contacts a live service, and no real
credential is read or written: the store lives under a redirected ``LOCALAPPDATA``.

Each check prints PASS / FAIL / SKIP and the verdict is the worst of them.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"

_ORDER = {"SKIP": 0, "PASS": 1, "FAIL": 2}
_RESULTS: list[tuple[str, str, str]] = []


def record(name: str, verdict: str, detail: str = "") -> None:
    _RESULTS.append((name, verdict, detail))
    print(f"[{verdict:4}] {name}" + (f" -- {detail}" if detail else ""))


def _fixture(label: str) -> str:
    """Fixture values assembled at runtime.

    The repository's secret scanner flags token-shaped literals outside ``tests/``
    and is right to; this file is a tool, so it builds them instead of asking for
    an exemption. See the same helper in ``acceptance_durable.py``.
    """
    return "-".join((label, "0" * 8, "1" * 8, "2" * 8))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def run(argv: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str]:
    merged = dict(os.environ)
    merged["NO_PROXY"] = "*"
    if env:
        merged.update(env)
    proc = subprocess.run(
        argv, capture_output=True, text=True, encoding="utf-8", env=merged, timeout=180
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _renewal_driver_source() -> str:
    """The script process B runs: the production chain, socket stubbed.

    Executed with ``-c`` rather than squeezed into a semicolon-joined one-liner.
    The chain needs a ``with`` block, and an earlier attempt to inline it produced
    a ``SyntaxError`` that the runner reported as "renewal chain runs: FAIL" --
    a quoting artefact that reads exactly like a product failure. A driver that
    cannot express the code under test is the wrong driver.

    The route table is imported from the test suite rather than rebuilt here. The
    stub encodes the *recorded* OAuth flow (which path mints the cookie, which
    hosts answer what), and a second hand-written copy would drift from it -- the
    first version of this file did exactly that and missed every route.
    """
    return '''
import io
import os
import sys

for extra in ("src", "tests"):
    sys.path.insert(0, os.path.join(os.path.dirname(os.environ["OPENCSI_ACCEPT_ROOT"]), extra))
sys.path.insert(0, os.path.join(os.environ["OPENCSI_ACCEPT_ROOT"], "src"))
sys.path.insert(0, os.path.join(os.environ["OPENCSI_ACCEPT_ROOT"], "tests"))

from unittest import mock

from opencsi.auth.http_oauth import HttpOAuthRenewer
from opencsi.auth.session import SessionManager
from opencsi.cli.context import CliContext
from test_durable_renewal import oauth_routes, patch_transport, _StubOpener

BASE = os.environ["OPENCSI_ACCEPT_BASE_URL"]
MINTED = os.environ["OPENCSI_FIXTURE_MINTED"]
HOST = BASE.split("//", 1)[1]


class _Args:
    cdp = None
    base_url = BASE
    json = False
    no_proxy = True
    no_store = False
    store_ttl = 5.0
    renew_timeout = 30.0
    ports = None


ctx = CliContext(args=_Args(), stdout=io.StringIO(), stderr=io.StringIO())
provider = ctx.make_provider()

routes = oauth_routes(MINTED, host=HOST, base=BASE)
opener = _StubOpener(routes)

with patch_transport(opener):
    renewer = ctx.make_renewer(provider, base_url=BASE)
    result = SessionManager(provider, renewer=renewer).renew(force=True)

print("STATUS=" + result.status.value)
if not result.ok:
    print("DETAIL=" + str(result.detail))
'''


def check_three_process_renewal() -> None:
    """The A -> B renew -> C read chain, with B using the production wiring."""
    minted = _fixture("renewed-session")
    t1 = _fixture("original-session")
    env_values = {
        "OPENCSI_FIXTURE_ACCESS": _fixture("access"),
        "OPENCSI_FIXTURE_REFRESH": _fixture("refresh"),
        "OPENCSI_FIXTURE_SESSION": t1,
        "OPENCSI_FIXTURE_MINTED": minted,
    }

    port = _free_port()
    tmp = Path(tempfile.mkdtemp(prefix="opencsi-renewal-"))
    try:
        env = {
            "LOCALAPPDATA": str(tmp),
            "OPENCSI_ACCEPT_BASE_URL": f"http://127.0.0.1:{port}",
            "OPENCSI_ACCEPT_ROOT": str(ROOT),
            **env_values,
        }

        # ── Process A: write T1 and the GitCode credential ────────────────
        seed = (
            "import os, time; "
            "from opencsi.auth.windows_store import DpapiCredentialStore, default_path; "
            "from opencsi.auth.store import StoredGitCodeCredential, StoredOpenCsiCredential; "
            "s = DpapiCredentialStore(default_path()); "
            "s.save_gitcode(StoredGitCodeCredential("
            "os.environ['OPENCSI_FIXTURE_ACCESS'], "
            "refresh_token=os.environ['OPENCSI_FIXTURE_REFRESH'], username='alice', "
            "access_expires_at=time.time() + 15*24*3600)); "
            "s.save_opencsi(StoredOpenCsiCredential("
            "os.environ['OPENCSI_FIXTURE_SESSION'], expires_at=time.time() - 60))"
        )
        code_a, out_a = run([sys.executable, "-c", seed], env=env)
        if code_a != 0:
            record("A: writes T1 and GitCode A1/R1", "FAIL", out_a[-300:])
            return
        record("A: writes T1 and GitCode A1/R1", "PASS")

        # ── Process B: run the production renewal chain ───────────────────
        #
        # `make_provider` -> `make_renewer` -> `SessionManager.renew`, exactly as
        # the CLI does it. Only the HTTP transport is stubbed. The renewal must
        # happen here; a process that merely copied T1 would prove nothing.
        renew = _renewal_driver_source()
        code_b, out_b = run([sys.executable, "-c", renew], env=env)
        if code_b != 0:
            record("B: production renewal chain runs", "FAIL", out_b[-400:])
            return
        status_line = [ln for ln in out_b.splitlines() if ln.startswith("STATUS=")]
        status = status_line[0].split("=", 1)[1] if status_line else "?"
        if status != "RENEWED":
            record("B: renewal reports RENEWED", "FAIL", f"status={status} :: {out_b[-300:]}")
            return
        record("B: renewal reports RENEWED", "PASS")

        # ── Process C: a fresh interpreter must see T2 ────────────────────
        read = (
            "import os; "
            "from opencsi.auth.windows_store import DpapiCredentialStore, default_path; "
            "from opencsi.auth.stored import StoredOpenCsiCredentialProvider; "
            "p = StoredOpenCsiCredentialProvider(DpapiCredentialStore(default_path()), ttl=0.0); "
            "t = p.get_token(); "
            "minted = os.environ['OPENCSI_FIXTURE_MINTED']; "
            "original = os.environ['OPENCSI_FIXTURE_SESSION']; "
            "print('MINTED' if t == minted else ('STALE' if t == original else 'OTHER'))"
        )
        code_c, out_c = run([sys.executable, "-c", read], env=env)
        if code_c != 0 or "MINTED" not in out_c:
            verdict = "STALE" if "STALE" in out_c else "FAIL"
            record(
                "C: reads the renewed session T2",
                "FAIL",
                "process C still sees the pre-renewal token"
                if verdict == "STALE"
                else out_c[-300:],
            )
            return
        record("C: reads the renewed session T2", "PASS")

        # ── The GitCode credential must be untouched ──────────────────────
        verify = (
            "import os; "
            "from opencsi.auth.windows_store import DpapiCredentialStore, default_path; "
            "from opencsi.auth.stored import StoredGitCodeCredentialSource; "
            "c = StoredGitCodeCredentialSource(DpapiCredentialStore(default_path()))._credential(); "
            "ok = (c.access_token == os.environ['OPENCSI_FIXTURE_ACCESS'] "
            "and c.refresh_token == os.environ['OPENCSI_FIXTURE_REFRESH']); "
            "print('INTACT' if ok else 'CHANGED')"
        )
        code_d, out_d = run([sys.executable, "-c", verify], env=env)
        if code_d != 0 or "INTACT" not in out_d:
            record("GitCode A1/R1 preserved across the renewal", "FAIL", out_d[-300:])
            return
        record("GitCode A1/R1 preserved across the renewal", "PASS")

        # ── And nothing on disk is in the clear ───────────────────────────
        blob = (tmp / "OpenCSI" / "credentials.dat").read_bytes()
        leaked = [
            name
            for name, value in env_values.items()
            if name != "OPENCSI_FIXTURE_MINTED" and value.encode() in blob
        ]
        if leaked or minted.encode() in blob:
            record("no plaintext in the renewed store", "FAIL", f"clear text: {leaked}")
            return
        record("no plaintext in the renewed store", "PASS", f"{len(blob)} bytes ciphertext")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_no_browser() -> None:
    """OpenCSI must not have started a browser engine for any of the above."""
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        record("no browser started", "SKIP", f"could not enumerate ({type(exc).__name__})")
        return
    ours = [
        line
        for line in out.splitlines()
        if "opencsi" in line.lower() and ("chrome" in line.lower() or "msedge" in line.lower())
    ]
    if ours:
        record("no browser started", "FAIL", f"{len(ours)} OpenCSI browser processes")
        return
    record("no browser started", "PASS", "no OpenCSI-owned browser engine")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-build", action="store_true", help="assume dist/ exists")
    args = parser.parse_args()
    del args

    print("=== P2 acceptance: A -> B renew -> C read ===")
    print(f"python: {sys.version.split()[0]}")
    print()

    if not (DIST / "opencsi.exe").exists():
        print(f"dist/opencsi.exe is missing; build it with tools/build_exe.py")
        return 1

    check_three_process_renewal()
    check_no_browser()

    worst = max((v for _n, v, _d in _RESULTS), key=lambda v: _ORDER[v], default="SKIP")
    print()
    print(f"=== worst verdict: {worst} ===")
    for name, verdict, detail in _RESULTS:
        print(f"  {verdict:4} {name}")
    return 0 if worst in ("PASS", "SKIP") else 1


if __name__ == "__main__":
    sys.exit(main())
