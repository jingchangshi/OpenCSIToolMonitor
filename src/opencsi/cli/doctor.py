"""``opencsi doctor`` -- end-to-end diagnostics.

Answers, in order, the four questions that actually go wrong:

1. Is a DevTools endpoint reachable, and where?
2. Does that endpoint expose a usable target and a token cookie?
3. Does the openCsiTool API accept the session?
4. Does every endpoint this tool uses still match the verified contract?

Each check is independent: one failure does not stop the rest, because the
whole point is to show the user *where* the chain broke.
"""

from __future__ import annotations

import argparse
import os
import platform
import sys

from ..errors import (
    EXIT_CDP_UNAVAILABLE,
    EXIT_NETWORK_ERROR,
    EXIT_NO_BROWSER_TARGET,
    EXIT_NOT_LOGGED_IN,
    EXIT_PERMISSION_DENIED,
    EXIT_SERVER_ERROR,
    EXIT_SESSION_EXPIRED,
    OpenCsiError,
)
from ..formatting import format_relative_seconds, render_kv, section, yes_no
from ..version import USER_AGENT, __version__
from .context import CliContext, add_common_options

OK = "ok"
WARN = "warn"
FAIL = "fail"

_MARK = {OK: "[ok]  ", WARN: "[warn]", FAIL: "[FAIL]"}

#: Provider error codes mapped to their documented process exit status. Kept
#: here rather than inferred from the check name, because one check can fail for
#: several unrelated reasons that need different exit codes.
_CODE_EXIT = {
    "CDP_UNAVAILABLE": EXIT_CDP_UNAVAILABLE,
    "NO_BROWSER_TARGET": EXIT_NO_BROWSER_TARGET,
    "OPENCSITOOL_NOT_LOGGED_IN": EXIT_NOT_LOGGED_IN,
    "SESSION_EXPIRED": EXIT_SESSION_EXPIRED,
    "PERMISSION_DENIED": EXIT_PERMISSION_DENIED,
    "NETWORK_ERROR": EXIT_NETWORK_ERROR,
    "SERVER_ERROR": EXIT_SERVER_ERROR,
}


def _code_to_exit(code: str | None) -> int | None:
    """Map a provider error code to its exit status, if it is one we know."""
    return _CODE_EXIT.get(code or "")


def _credential_hint(provider, endpoint_ok: bool) -> str:
    """Actionable next step for a missing credential.

    The provider's own hint is preferred: it knows *why* the read failed (a
    refused WebSocket handshake is a different problem from an empty cookie jar,
    and they need different fixes). The fallbacks below only run when the
    provider has no specific advice to give.
    """
    own_hint = getattr(provider, "last_hint", None)
    if own_hint:
        return str(own_hint)
    if endpoint_ok:
        return (
            "the browser endpoint is reachable but holds no openCsiTool token "
            "cookie. Open https://opencsitool.com/myTools in that browser and "
            "sign in, then retry."
        )
    if getattr(provider, "name", "") == "cdp":
        return (
            "no browser session could be read. See README 'Browser "
            "preparation' for starting a browser with remote debugging."
        )
    return "supply a credential, then retry."


def _record_renewal(record, provider, credential_ok: bool) -> None:
    """Report whether an expiring session can renew itself (objective §54).

    This is the check that answers "will I have to sign in again in an hour?".
    A readable credential with no renewer is a *warning*, not a failure: the tool
    works, it just cannot save the user the next login.

    The verdict comes from ``renewal_capability``, which ``login --status`` also
    uses -- the two commands reported this independently once and disagreed, with
    ``--status`` claiming renewal was unavailable on a machine where it worked.
    """
    if not credential_ok:
        record(
            "silent renewal",
            WARN,
            "not attempted: no credential available",
            "resolve the credential check above first",
        )
        return

    if os.environ.get("OPENCSI_NO_RENEW"):
        record(
            "silent renewal",
            WARN,
            "disabled by configuration",
            "unset OPENCSI_NO_RENEW to enable it",
        )
        return

    from ..auth.oauth_browser import renewal_capability

    capability = renewal_capability(provider)
    if capability.available and not capability.caveated:
        record("silent renewal", OK, capability.reason)
    elif capability.available:
        # The machinery is reachable but something about it needs saying -- most
        # often that the browser holds no GitCode SSO cookie, so the round-trip
        # will park on a sign-in. Reporting OK here is the exact defect this
        # check was rewritten to stop: `doctor` said health, the troubleshooting
        # guide said that line meant recovery, and the next renewal asked the
        # user to approve an authorization. The reason string carries the
        # detail; WARN is what makes it visible.
        record(
            "silent renewal",
            WARN,
            capability.reason,
            "renewal is possible but not currently unattended",
        )
    else:
        record(
            "silent renewal",
            WARN,
            capability.reason,
            "silent renewal needs a reachable browser-level DevTools endpoint",
        )


def _record_tray(record) -> None:
    """Report whether the Windows tray can run here (objective §54).

    Deliberately informational: the tray is an optional extra, and a machine
    without it is a perfectly good place to run the CLI.

    Uses the tray's own availability check rather than re-testing the imports
    here, so doctor and the tray can never disagree about what is missing.
    """
    if os.name != "nt":
        record("tray support", WARN, "the tray is Windows-only", None)
        return

    from ..tray import tray_available

    available, reason = tray_available()
    if available:
        record("tray support", OK, "pystray and Pillow installed")
    else:
        record(
            "tray support",
            WARN,
            reason or "unavailable",
            'install the extra: pip install "opencsi[tray]"',
        )

    # Whether it starts at sign-in is a separate question from whether it can
    # run, and it is the one a user forgets they answered.
    from ..tray.startup import StartupManager

    status = StartupManager().status()
    if status.detail and not status.enabled:
        record("tray startup", WARN, status.detail, None)
    elif status.enabled:
        record("tray startup", OK, "starts at sign-in")
    else:
        record(
            "tray startup",
            WARN,
            "does not start at sign-in",
            "enable it with: opencsi tray --install-startup",
        )


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "doctor",
        help="diagnose the credential and API chain",
        description=(
            "Run an ordered diagnostic over browser endpoint, cookie, session "
            "and API contract, then print an actionable summary."
        ),
    )
    add_common_options(parser)
    parser.add_argument(
        "--skip-contract",
        action="store_true",
        help="skip the live API contract check (fewer requests)",
    )
    parser.set_defaults(handler=run)


def run(ctx: CliContext) -> int:
    checks: list[dict[str, object]] = []
    #: Exit codes carried by the checks, most specific first. Deriving the
    #: process status from the *check name* was wrong: a refused DevTools
    #: handshake was reported as 12 ("not signed in") when the user needed to
    #: restart their browser, and 12 is the advice they would have followed.
    codes: list[int] = []

    def record(
        name: str,
        status: str,
        detail: str = "",
        hint: str | None = None,
        exit_code: int | None = None,
    ) -> None:
        entry: dict[str, object] = {"check": name, "status": status, "detail": detail}
        if hint:
            entry["hint"] = hint
        if exit_code is not None and status != OK:
            codes.append(exit_code)
        checks.append(entry)

    # ── 1. environment ────────────────────────────────────────────────────
    record(
        "python",
        OK,
        f"{platform.python_version()} on {platform.system()} {platform.release()}",
    )
    if sys.version_info < (3, 10):
        record(
            "python version",
            FAIL,
            f"{platform.python_version()} is older than the supported 3.10",
            "install Python 3.10 or newer",
        )

    # ── 2. DevTools endpoint ──────────────────────────────────────────────
    # ``renew=False``: a diagnostic must *report* an expired session, not
    # silently repair it. Otherwise doctor would always say "session ok" and the
    # user could never see the problem they ran doctor to find.
    client = ctx.make_client(renew=False)
    provider = client.credentials
    endpoint_ok = False
    try:
        probe = getattr(provider, "probe_endpoint", None)
        endpoint = probe() if callable(probe) else None
        if endpoint is None:
            record("devtools endpoint", WARN, "provider does not use CDP")
        else:
            endpoint_ok = True
            record("devtools endpoint", OK, str(endpoint))
            browser = getattr(provider, "browser", None)
            if browser:
                record("browser", OK, str(browser))
    except OpenCsiError as exc:
        record("devtools endpoint", FAIL, str(exc), exc.hint, exc.exit_code)

    # ── 3. credential ─────────────────────────────────────────────────────
    credential_ok = False
    try:
        status = provider.status()
        credential_ok = bool(status.available)
        detail = f"source={status.source}"
        if status.expires_in is not None:
            detail += f", expires in {format_relative_seconds(status.expires_in)}"
        if not credential_ok and status.detail:
            # Surface the real cause (refused handshake, empty cookie jar, ...)
            # instead of a bare "source=cdp" that tells the user nothing.
            detail += f", {status.detail}"
        record(
            "credential",
            OK if credential_ok else FAIL,
            detail,
            None if credential_ok else _credential_hint(provider, endpoint_ok),
            None if credential_ok else _code_to_exit(getattr(provider, "last_error_code", None)),
        )
        if status.expiring_soon:
            record(
                "credential lifetime",
                WARN,
                f"only {format_relative_seconds(status.expires_in)} left",
                "the tool refreshes automatically on a 401, but re-login soon",
            )
    except OpenCsiError as exc:
        record("credential", FAIL, str(exc), exc.hint, exc.exit_code)

    # ── 4. session ────────────────────────────────────────────────────────
    identity = None
    if not credential_ok:
        # One root cause, one failure. Reporting the session as failed too would
        # make a missing cookie look like two independent problems.
        record(
            "session",
            WARN,
            "not attempted: no credential available",
            "resolve the credential check above first",
        )
    else:
        try:
            identity = client.login_or_restore_session()
            record(
                "session",
                OK,
                f"{identity.display_name} (employeeId={identity.employee_id})",
            )
        except OpenCsiError as exc:
            record("session", FAIL, str(exc), exc.hint, exc.exit_code)

    # ── 5. silent renewal capability ──────────────────────────────────────
    # Reported separately from the credential because they fail for different
    # reasons and need different fixes: a readable cookie with no renewer means
    # the user will be asked to sign in again in ~an hour, which is exactly the
    # problem this project exists to remove.
    _record_renewal(record, provider, credential_ok)

    # ── 6. tray prerequisites ─────────────────────────────────────────────
    _record_tray(record)

    # ── 7. per-endpoint API checks ────────────────────────────────────────
    # The objective (§28) asks for named rows -- getUserInfo,
    # personalQueueStatus, model prices -- rather than one collapsed "contract"
    # line, because the whole value of a diagnosis is knowing *which* call is
    # broken. `contract_check` already produces exactly that granularity, so its
    # sub-checks are surfaced individually instead of being joined into a string.
    if identity is not None and not ctx.args.skip_contract:
        try:
            result = client.contract_check()
            for sub in result["checks"]:
                record(
                    sub["check"],
                    OK if sub["ok"] else FAIL,
                    sub.get("detail") or "",
                    None
                    if sub["ok"]
                    else "the upstream API changed shape; re-run the investigation",
                )
        except OpenCsiError as exc:
            record("api contract", FAIL, str(exc), exc.hint, exc.exit_code)

    failures = [c for c in checks if c["status"] == FAIL]
    warnings = [c for c in checks if c["status"] == WARN]

    payload = {
        "ok": not failures,
        "version": __version__,
        "user_agent": USER_AGENT,
        "base_url": client.base_url,
        "checks": checks,
        "summary": {
            "ok": sum(1 for c in checks if c["status"] == OK),
            "warn": len(warnings),
            "fail": len(failures),
        },
    }

    def render() -> None:
        ctx.out(section(f"opencsi doctor ({__version__})"))
        ctx.out(render_kv([("API origin", client.base_url), ("User-Agent", USER_AGENT)]))
        ctx.blank()
        for check in checks:
            mark = _MARK.get(str(check["status"]), "[????]")
            ctx.out(f"{mark} {check['check']}: {check['detail']}")
            hint = check.get("hint")
            if hint and check["status"] != OK:
                # Unlike an incidental warning elsewhere, a doctor hint *is* the
                # answer this command exists to produce, so it goes to stdout --
                # `opencsi doctor > report.txt` must not lose the fix.
                ctx.out(f"       -> {hint}")
        ctx.blank()
        if failures:
            ctx.out(
                f"{len(failures)} check(s) failed, {len(warnings)} warning(s)."
            )
        elif warnings:
            ctx.out(f"All checks passed, {len(warnings)} warning(s).")
        else:
            ctx.out(f"All {len(checks)} checks passed.")

    ctx.emit(payload, render)

    if failures:
        # Exit with the most specific code the checks actually reported, so a
        # script can branch on the real cause rather than on the check's name.
        if codes:
            return codes[0]
        return 1
    return 0
