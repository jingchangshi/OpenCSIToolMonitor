"""``opencsi status`` -- session health and the headline numbers.

Answers "can this tool talk to openCsiTool right now, and what does it say?"
The default output is deliberately short: a user should not have to understand
CDP, cookies or REST to read it (objective §46). Internal identifiers (userId,
accountId, organization UUID) are hidden unless ``--verbose``, because they are
noise in normal use and identifying in a screenshot or a shared terminal.
"""

from __future__ import annotations

import argparse

from ..errors import OpenCsiError, exit_code_for
from ..formatting import (
    format_int,
    format_percent,
    format_relative_seconds,
    format_server_time,
    render_kv,
    section,
)
from .context import CliContext, add_common_options


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "status",
        help="show session and credential status",
        description=(
            "Verify that a usable openCsiTool session exists and report who is "
            "signed in, plus the headline usage numbers. Performs two "
            "authenticated requests (getUserInfo, personalQueueStatus)."
        ),
    )
    add_common_options(parser)
    parser.add_argument(
        "--no-summary",
        action="store_true",
        help="skip the usage summary and only check the session (one request)",
    )
    parser.set_defaults(handler=run)


def run(ctx: CliContext) -> int:
    client = ctx.make_client()
    provider = client.credentials

    # Credential status first: it is the difference between "no browser" and
    # "browser present but not signed in", and the two need different fixes.
    cred = provider.status()

    session_error: OpenCsiError | None = None
    try:
        identity = client.login_or_restore_session()
        session_ok = True
    except OpenCsiError as exc:
        identity = None
        session_ok = False
        session_error = exc

    # The summary is best-effort: a session that works but a summary that fails
    # should still report the session, not collapse into one failure.
    snapshot = None
    summary_error: OpenCsiError | None = None
    if session_ok and not ctx.args.no_summary:
        try:
            snapshot = client.get_my_tools()
        except OpenCsiError as exc:
            summary_error = exc

    payload: dict[str, object] = {
        "ok": session_ok,
        "session": "ok" if session_ok else "failed",
        "credential": cred.as_dict(),
        "identity": (
            {
                "user_name": identity.user_name,
                "employee_id": identity.employee_id,
                "role_view_name": identity.role_view_name,
                **(
                    {
                        "user_id": identity.user_id,
                        "account_id": identity.account_id,
                        "account_login": identity.account_login,
                        "organization_id": identity.organization_id,
                        "organization_name": identity.organization_name,
                        "roles": list(identity.roles),
                    }
                    if ctx.verbose
                    else {}
                ),
            }
            if identity
            else None
        ),
        "base_url": client.base_url,
    }
    if snapshot is not None:
        payload["summary"] = {
            "total_tokens": snapshot.total_tokens,
            "request_count": snapshot.total_request_count,
            "pr_count": snapshot.pr_count,
            "added_lines": snapshot.added_lines_count,
            "generated_lines": snapshot.generated_code_lines,
            "adopted_lines": snapshot.adopted_code_lines,
            "adoption_rate": round(snapshot.adoption_rate, 6),
            "active_tools": len(snapshot.active_grants),
            "expired_tools": len(snapshot.expired_grants),
            "data_fresh_time": snapshot.sync_status.data_fresh_time,
            "fetched_at": (
                snapshot.fetched_at.isoformat() if snapshot.fetched_at else None
            ),
        }
    if session_error is not None:
        payload["error"] = str(session_error)
        payload["error_code"] = session_error.code
    if summary_error is not None:
        payload["summary_error"] = str(summary_error)
        payload["summary_error_code"] = summary_error.code

    def render() -> None:
        ctx.out(section("openCsiTool"))
        ctx.out(render_kv([("Session", "OK" if session_ok else "FAILED")]))
        if identity:
            ctx.out(
                render_kv(
                    [
                        ("User", identity.display_name),
                        ("Employee", identity.employee_id),
                        ("Role", identity.role_view_name or "-"),
                    ]
                )
            )
        if snapshot is not None:
            ctx.out(
                render_kv(
                    [
                        (
                            "Data updated",
                            format_server_time(snapshot.sync_status.data_fresh_time),
                        ),
                    ]
                )
            )
            ctx.blank()
            ctx.out(
                render_kv(
                    [
                        ("Tokens", format_int(snapshot.total_tokens)),
                        ("Requests", format_int(snapshot.total_request_count)),
                        ("PRs", format_int(snapshot.pr_count)),
                        ("Added lines", format_int(snapshot.added_lines_count)),
                        ("AI generated", format_int(snapshot.generated_code_lines)),
                        ("AI adopted", format_int(snapshot.adopted_code_lines)),
                        ("Adoption rate", format_percent(snapshot.adoption_rate)),
                    ]
                )
            )
            ctx.blank()
            ctx.out(
                render_kv(
                    [
                        ("Active tools", str(len(snapshot.active_grants))),
                        ("Expired tools", str(len(snapshot.expired_grants))),
                    ]
                )
            )
        elif summary_error is not None:
            ctx.err(f"note: usage summary unavailable: {summary_error}")

        if ctx.verbose:
            # Only here do the internal identifiers appear.
            ctx.blank()
            ctx.out(section("Diagnostics (--verbose)"))
            rows = [
                ("API origin", client.base_url),
                ("Credential source", cred.source),
                ("Credential available", "yes" if cred.available else "no"),
                ("Cookie lifetime left", format_relative_seconds(cred.expires_in)),
            ]
            if identity:
                rows += [
                    ("User ID", identity.user_id),
                    ("Account ID", identity.account_id),
                    ("Login", identity.account_login),
                    ("Organization", identity.organization_name or "-"),
                    ("Organization ID", identity.organization_id),
                    ("Roles", ", ".join(identity.roles) or "-"),
                ]
            ctx.out(render_kv(rows))
            if cred.detail and not session_error:
                ctx.err(f"note: {cred.detail}")
        if not session_ok and session_error is not None:
            ctx.blank()
            ctx.err(f"error: {session_error}")
            if session_error.hint:
                ctx.err(f"       -> {session_error.hint}")

    ctx.emit(payload, render)
    if session_ok:
        return 0
    if ctx.json:
        # JSON consumers must not have to parse prose to learn the outcome.
        return 1
    # Propagate the real cause. Hardcoding "not signed in" here told a user
    # with no reachable DevTools port to go and sign in, which cannot help.
    return exit_code_for(session_error)
