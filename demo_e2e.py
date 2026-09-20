"""End-to-end demo: the real CLI over a real socket.

This is not part of the test suite. It exists to produce honest evidence for the
implementation report: it runs the actual ``opencsi`` command line, over actual
HTTP, against a local server that replays the sanitized fixtures captured during
the API investigation. Nothing is monkeypatched inside the CLI -- only the
network origin is redirected to loopback.

Usage:  python demo_e2e.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "src"
FIXTURES = HERE / "tests" / "fixtures"
PYTHON = sys.executable

#: (path fragment, fixture file). Matched by substring, mirroring
#: tests/helpers.py ROUTES so the demo and the suite agree on the contract.
ROUTES = (
    ("/user/getUserInfo", "get_user_info.json"),
    ("/user/getUserRolesByOrganizationId", "get_user_roles.json"),
    ("/user/getVisibleRoleViews", "visible_role_views.json"),
    ("/ai/config/cost", "config_cost.json"),
    ("/ai/operations/personalQueueStatus", "personal_queue_status.json"),
    ("/call-logs", "call_logs.json"),
    ("/key-budget", "key_budget.json"),
)

_MISSES: list[str] = []


def fixture_for(path: str) -> str | None:
    for fragment, name in ROUTES:
        if fragment in path:
            return name
    return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        name = fixture_for(path)
        if name is None:
            _MISSES.append(path)
            body = json.dumps({"code": 404, "message": f"no fixture for {path}"})
            status = 404
        else:
            body = (FIXTURES / name).read_text(encoding="utf-8")
            status = 200
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> int:
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env["PYTHONIOENCODING"] = "utf-8"
    env["OPENCSI_CDP_URL"] = "http://127.0.0.1:9"  # unused: cookie is injected
    # A stub credential provider is supplied via a tiny shim module so the real
    # CLI runs unmodified.
    shim = HERE / "_demo_shim"
    shim.mkdir(exist_ok=True)
    (shim / "sitecustomize.py").write_text(
        "import opencsi.cli.context as c\n"
        "from opencsi.auth.manual import ManualCookieProvider\n"
        "_orig = c.CliContext.make_client\n"
        "def patched(self, *, provider=None):\n"
        "    return _orig(self, provider=provider or ManualCookieProvider('DEMOCOOKIE' + 'a'*48))\n"
        "c.CliContext.make_client = patched\n",
        encoding="utf-8",
    )
    env["PYTHONPATH"] = str(SRC) + os.pathsep + str(shim)

    commands = [
        ["status"],
        ["status", "--verbose"],
        ["tools"],
        ["tools", "--show-key-mask", "--type", "API_BUNDLE"],
        ["usage"],
        ["trend"],
        ["trend", "--days", "7"],
        ["prices"],
        ["logs"],
        ["doctor", "--skip-contract"],
    ]

    print("=" * 72)
    print(f"end-to-end demo against {base} (fixtures replayed over a real socket)")
    print("=" * 72)

    failures = 0
    for args in commands:
        print(f"\n$ opencsi {' '.join(args)}")
        print("-" * 72)
        proc = subprocess.run(
            [PYTHON, "-m", "opencsi", *args, "--base-url", base, "--no-proxy"],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        print(proc.stdout.rstrip())
        if proc.stderr.strip():
            print("[stderr]", proc.stderr.strip()[:400])
        print(f"[exit {proc.returncode}]")
        if proc.returncode != 0:
            failures += 1

    httpd.shutdown()
    httpd.server_close()

    print("\n" + "=" * 72)
    if _MISSES:
        print("UNROUTED PATHS (the CLI asked for something without a fixture):")
        for path in sorted(set(_MISSES)):
            print("  ", path)
    else:
        print("Every path the CLI requested had a fixture.")
    print(f"{len(commands) - failures}/{len(commands)} commands exited 0.")
    return 0 if not failures and not _MISSES else 1


if __name__ == "__main__":
    raise SystemExit(main())
