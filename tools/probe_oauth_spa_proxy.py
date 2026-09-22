"""Measure why a headless Chrome cannot reach opencsitool.com on this machine.

Silent renewal drives a background browser tab to ``opencsitool.com``. If the
browser cannot reach that host, renewal fails before OAuth is even involved --
so this probe isolates *network reachability* from everything the rest of the
investigation covers, and pins the cause to a specific flag.

The measurement, per configuration:

1. launch a headless Chrome on its own port against a given profile directory,
2. attach over CDP, enable ``Network``, and navigate a background tab to a URL,
3. record the final ``location`` and, for the main document, the
   ``Network.loadingFailed`` ``errorText`` -- Chrome's own net error string,
   which is the difference between "the host refused" and "a proxy failed",
4. tear the browser down and move to the next configuration.

Configurations are compared so the *fix* is identified rather than guessed at:
no flag (system proxy inherited), ``--no-proxy-server``, and
``--proxy-bypass-list=<host>``.

It changes nothing on the machine: the system proxy is only ever *read* (via
``netsh winhttp show proxy`` and the WinINET registry values) and never written,
the browser profile is the caller's, and no source file is touched. The browser
processes it starts are its own and are stopped again before it returns.

LIVE / NETWORK / read-only / writes nothing
-------------------------------------------
LIVE and NETWORK: it navigates a real browser to real hosts. read-only: it
performs GET navigations only, calls no authenticated endpoint and no business
API. writes nothing: it does not alter proxy settings, the registry, or any
repository file; the only side effect is the throwaway browser it starts and
stops.

Usage
-----
    python tools/probe_oauth_spa_proxy.py
    python tools/probe_oauth_spa_proxy.py --profile "%LOCALAPPDATA%\\OpenCSI\\auth-test-profile"
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, "src")

from opencsi.auth.cdp import discover_cdp_endpoint  # noqa: E402
from opencsi.ws import CdpConnection  # noqa: E402

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

#: Hosts that matter to the two halves of the flow. ``opencsitool.com`` is where
#: the OAuth entry and callback live; the GitCode hosts serve the authorize SPA
#: and its backend API.
HOSTS = (
    "https://opencsitool.com/myTools",
    "https://gitcode.com/",
    "https://web-api.gitcode.com/",
)

#: ``label -> extra Chrome flags``. The first entry is the control: whatever
#: proxy the operating system hands the browser, with no override.
#:
#: The last two use a deliberately dead proxy (port 1) to answer the question
#: the recommendation turns on: does ``--proxy-bypass-list`` merely *add* an
#: exception while the proxy is still used for everything else, or does it
#: quietly disable proxying altogether? If the bypass list works as advertised,
#: ``opencsitool.com`` succeeds while the other hosts fail through the dead
#: proxy -- which is what makes it safe on a machine that genuinely needs a
#: proxy for other traffic.
CONFIGS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("baseline (system proxy inherited)", ()),
    ("--no-proxy-server", ("--no-proxy-server",)),
    ("--proxy-bypass-list=opencsitool.com", ("--proxy-bypass-list=opencsitool.com",)),
    (
        "dead proxy, no bypass (control)",
        ("--proxy-server=http://127.0.0.1:1",),
    ),
    (
        "dead proxy + bypass opencsitool.com",
        (
            "--proxy-server=http://127.0.0.1:1",
            "--proxy-bypass-list=opencsitool.com",
        ),
    ),
)


def configs_with_system_proxy(proxy_server: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Add a configuration that names the system proxy explicitly.

    The recommendation turns on whether ``--proxy-bypass-list`` is honoured when
    the proxy comes from the operating system. Testing that needs a variant that
    spells the *same* proxy out on the command line, so the only difference from
    the bypass-only configuration is where the proxy address came from.
    """
    if not proxy_server:
        return CONFIGS
    return CONFIGS + (
        (
            "system proxy named explicitly + bypass",
            (
                f"--proxy-server=http://{proxy_server}",
                "--proxy-bypass-list=opencsitool.com",
            ),
        ),
    )


def system_proxy() -> dict[str, Any]:
    """Read the machine's proxy configuration. Read-only; never writes."""
    info: dict[str, Any] = {}
    try:
        result = subprocess.run(
            ["netsh", "winhttp", "show", "proxy"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        info["winhttp"] = " ".join(result.stdout.split())
    except Exception as exc:  # noqa: BLE001 - a probe must not raise
        info["winhttp"] = f"<{type(exc).__name__}>"
    try:
        import winreg  # noqa: PLC0415 - Windows-only, imported where used

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            for name in ("ProxyEnable", "ProxyServer", "ProxyOverride", "AutoConfigURL"):
                try:
                    info[name] = winreg.QueryValueEx(key, name)[0]
                except FileNotFoundError:
                    info[name] = None
    except Exception as exc:  # noqa: BLE001
        info["wininet"] = f"<{type(exc).__name__}>"
    return info


def host_of(url: str) -> str:
    rest = url.split("://", 1)[-1]
    return rest.split("/", 1)[0].split(":", 1)[0].lower()


def launch(port: int, profile: Path, flags: tuple[str, ...]) -> subprocess.Popen[bytes]:
    args = [
        CHROME,
        "--headless=new",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "--noerrdialogs",
        *flags,
        "about:blank",
    ]
    return subprocess.Popen(
        args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def wait_for_cdp(port: int, *, seconds: float = 20.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            endpoint = discover_cdp_endpoint(f"http://127.0.0.1:{port}", probe=True)
            if endpoint.browser_ws_url():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    return False


def probe_url(conn: CdpConnection, url: str, *, settle: float = 6.0) -> dict[str, Any]:
    """Navigate a background tab and report where it landed and why it failed."""
    created = conn.call(
        "Target.createTarget", {"url": "about:blank", "background": True}, timeout=15.0
    )
    target_id = str(created.get("targetId") or "")
    session_id = str(
        conn.call(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}, timeout=15.0
        ).get("sessionId")
        or ""
    )
    for domain in ("Network.enable", "Page.enable"):
        try:
            conn.call(domain, {}, session_id=session_id, timeout=15.0)
        except Exception:  # noqa: BLE001
            pass

    # Every configuration shares one profile, so a successful navigation in an
    # earlier configuration can be replayed from the HTTP cache in a later one
    # and read as "reachable" even though the network path is broken. Disabling
    # the cache per tab is what makes the configurations comparable; without it
    # the dead-proxy control reported opencsitool.com as reachable.
    try:
        conn.call(
            "Network.setCacheDisabled",
            {"cacheDisabled": True},
            session_id=session_id,
            timeout=15.0,
        )
    except Exception:  # noqa: BLE001
        pass

    navigate_error = ""
    try:
        conn.call("Page.navigate", {"url": url}, session_id=session_id, timeout=20.0)
    except Exception as exc:  # noqa: BLE001
        # A navigation that never acknowledges still produced a Network event
        # stream, and that stream is the measurement. Recording the timeout and
        # carrying on keeps one flaky navigation from discarding the whole run.
        navigate_error = type(exc).__name__

    failures: list[dict[str, Any]] = []
    deadline = time.monotonic() + settle
    while time.monotonic() < deadline:
        try:
            event = conn.poll_event(timeout=0.5)
        except Exception:  # noqa: BLE001
            break
        if not isinstance(event, Mapping):
            continue
        if str(event.get("method")) == "Network.loadingFailed":
            params = event.get("params") or {}
            # Only the main document's failure explains the navigation outcome;
            # a failed favicon is noise.
            if str(params.get("type") or "") == "Document":
                failures.append(
                    {
                        "errorText": params.get("errorText"),
                        "canceled": params.get("canceled"),
                        "blockedReason": params.get("blockedReason"),
                    }
                )

    try:
        final = conn.call(
            "Runtime.evaluate",
            {"expression": "location.href", "returnByValue": True},
            session_id=session_id,
            timeout=15.0,
        )
        final_url = str((final.get("result") or {}).get("value") or "")
    except Exception:  # noqa: BLE001
        final_url = ""

    try:
        conn.call("Target.closeTarget", {"targetId": target_id}, timeout=10.0)
    except Exception:  # noqa: BLE001
        pass

    return {
        "url": url,
        "final": final_url,
        "final_host": host_of(final_url) if "://" in final_url else final_url,
        "reachable": "chrome-error" not in final_url and bool(final_url),
        "failures": failures,
        "navigate_error": navigate_error,
    }


def stop(process: subprocess.Popen[bytes], profile: Path) -> None:
    """Stop a browser and every child still holding its profile lock.

    ``terminate()`` on the launcher is not enough: Chrome's renderer and utility
    processes are separate images that outlive it briefly, and while any of them
    holds ``SingletonLock`` the next configuration cannot start against the same
    ``--user-data-dir``. Killing by command line is what actually releases it,
    which is why the profile path is a parameter here rather than assumed.
    """
    try:
        process.terminate()
        process.wait(timeout=15)
    except Exception:  # noqa: BLE001
        try:
            process.kill()
        except Exception:  # noqa: BLE001
            pass

    marker = str(profile).replace("'", "''")
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{marker}*' }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    try:
        subprocess.run(
            ["pwsh", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=40,
        )
    except Exception:  # noqa: BLE001
        pass

    # The lock file is removed asynchronously by the exiting processes; poll
    # rather than sleeping a fixed amount, so a fast release is not punished.
    lock = profile / "SingletonLock"
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if not lock.exists():
            return
        time.sleep(0.5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9341)
    parser.add_argument(
        "--profile",
        default=os.path.join(
            os.environ.get("LOCALAPPDATA", ""), "OpenCSI", "auth-test-profile"
        ),
    )
    parser.add_argument("--json", default="")
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="navigate each host this many times per configuration, to expose flakiness",
    )
    parser.add_argument(
        "--only", default="", help="run only configurations whose label contains this text"
    )
    args = parser.parse_args()

    profile = Path(args.profile)
    out: list[str] = []
    report: dict[str, Any] = {"proxy": system_proxy(), "configs": {}}

    def emit(line: str = "") -> None:
        print(line)
        out.append(line)

    emit("Chrome reachability vs. the system proxy")
    emit("=" * 72)
    emit(f"profile : {profile}")
    emit(f"chrome  : {CHROME}")
    emit("")
    emit("system proxy (read-only):")
    for key, value in report["proxy"].items():
        emit(f"  {key:14s}: {value}")
    emit("")

    configs = configs_with_system_proxy(str(report["proxy"].get("ProxyServer") or ""))
    for index, (label, flags) in enumerate(configs):
        if args.only and args.only.lower() not in label.lower():
            continue
        port = args.port + index
        emit("-" * 72)
        emit(f"config: {label}")
        emit(f"  flags: {' '.join(flags) if flags else '(none)'}")

        process = launch(port, profile, flags)
        try:
            if not wait_for_cdp(port):
                emit("  could not reach CDP; skipping")
                report["configs"][label] = {"error": "cdp-unavailable"}
                continue

            endpoint = discover_cdp_endpoint(f"http://127.0.0.1:{port}", probe=True)
            results: list[dict[str, Any]] = []
            with CdpConnection(endpoint.browser_ws_url(), timeout=20.0) as conn:
                for url in HOSTS:
                    for _ in range(max(1, args.repeat)):
                        result = probe_url(conn, url)
                        results.append(result)
                        verdict = "OK" if result["reachable"] else "UNREACHABLE"
                        emit(f"  {verdict:12s} {url}")
                        emit(f"               -> {result['final_host']}")
                        if result["navigate_error"]:
                            emit(f"               navigate ack: {result['navigate_error']}")
                        for failure in result["failures"]:
                            emit(
                                f"               errorText={failure['errorText']} "
                                f"canceled={failure['canceled']} "
                                f"blocked={failure['blockedReason']}"
                            )
            report["configs"][label] = {"flags": list(flags), "results": results}
        finally:
            stop(process, profile)

    emit("")
    emit("=" * 72)
    emit("summary")
    for label, entry in report["configs"].items():
        if "results" not in entry:
            emit(f"  {label:38s} {entry.get('error')}")
            continue
        cells = []
        for result in entry["results"]:
            mark = "ok" if result["reachable"] else "FAIL"
            cells.append(f"{host_of(result['url'])}={mark}")
        emit(f"  {label:38s} {'  '.join(cells)}")

    if args.json:
        Path(args.json).write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        emit(f"\nwritten: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
