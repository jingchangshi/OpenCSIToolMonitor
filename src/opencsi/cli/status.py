"""``opencsi status`` -- session and credential health.

Answers "can this tool talk to openCsiTool right now?" without fetching the
whole snapshot, so it is cheap enough to run before a script.
"""

from __future__ import annotations

import argparse

from ..errors import OpenCsiError, exit_code_for
from ..formatting import format_datetime, format_relative_seconds, render_kv, section
from .context import CliContext, add_common_options


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "status",
        help="show session and credential status",
        description=(
            "Verify that a usable openCsiTool session exists and report who is "
            "signed in. Performs one authenticated request (getUserInfo)."
        ),
    )
    add_common_options(parser)
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

    payload = {
        "ok": session_ok,
        "credential": cred.as_dict(),
        "identity": (
            {
                "user_id": identity.user_id,
                "employee_id": identity.employee_id,
                "user_name": identity.user_name,
                "account_login": identity.account_login,
                "organization_name": identity.organization_name,
                "role_view_name": identity.role_view_name,
                "roles": list(identity.roles),
            }
            if identity
            else None
        ),
        "base_url": client.base_url,
    }
    if session_error is not None:
        payload["error"] = str(session_error)
        payload["error_code"] = session_error.code

    def render() -> None:
        ctx.out(section("openCsiTool session"))
        ctx.out(
            render_kv(
                [
                    ("API origin", client.base_url),
                    ("Credential source", cred.source),
                    ("Credential available", "yes" if cred.available else "no"),
                    ("Cookie lifetime left", format_relative_seconds(cred.expires_in)),
                    ("Session valid", "yes" if session_ok else "no"),
                ]
            )
        )
        if cred.detail and not session_error:
            # Only when it adds information: a failed session request already
            # reports the same underlying cause, and printing both duplicated
            # the line.
            ctx.err(f"note: {cred.detail}")
        if identity:
            ctx.blank()
            ctx.out(section("Signed in as"))
            ctx.out(
                render_kv(
                    [
                        ("Display name", identity.display_name),
                        ("Login", identity.account_login),
                        ("Employee ID", identity.employee_id),
                        ("User ID", identity.user_id),
                        ("Organization", identity.organization_name),
                        ("Role view", identity.role_view_name),
                        ("Roles", ", ".join(identity.roles) or "-"),
                    ]
                )
            )
        elif session_error:
            ctx.blank()
            ctx.err(f"error: {session_error}")

    ctx.emit(payload, render)
    if session_ok:
        return 0
    if ctx.json:
        # JSON consumers must not have to parse prose to learn the outcome.
        return 1
    # Propagate the real cause. Hardcoding "not signed in" here told a user
    # with no reachable DevTools port to go and sign in, which cannot help.
    return exit_code_for(session_error)
