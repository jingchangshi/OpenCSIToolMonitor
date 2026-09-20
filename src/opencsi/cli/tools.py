"""``opencsi tools`` -- the granted AI tool accounts (``requestList``)."""

from __future__ import annotations

import argparse

from ..formatting import Table, format_count, format_datetime, section
from .context import CliContext, add_common_options, add_date_options, validated_dates


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "tools",
        help="list granted AI tool accounts",
        description=(
            "List every AI tool account granted to the signed-in user, with "
            "status, token usage and the masked virtual key."
        ),
    )
    add_common_options(parser)
    add_date_options(parser)
    parser.add_argument(
        "--active-only",
        action="store_true",
        help="show only accounts with status 使用中",
    )
    parser.set_defaults(handler=run)


def run(ctx: CliContext) -> int:
    start, end = validated_dates(ctx.args)
    client = ctx.make_client()
    snapshot = client.get_my_tools(start, end)

    grants = snapshot.grants
    if ctx.args.active_only:
        grants = tuple(g for g in grants if g.is_active)

    payload = {
        "count": len(grants),
        "active": sum(1 for g in grants if g.is_active),
        "tools": [
            {
                "id": g.id,
                "application_number": g.application_number,
                "request_type": g.request_type,
                "ai_tool_name": g.ai_tool_name,
                "status": g.status,
                "status_text": g.status_text,
                "account_name": g.account_name,
                "virtual_key_masked": g.virtual_key_masked,
                "token_usage": g.token_usage,
                "request_count": g.request_count,
                "pr_count": g.pr_count,
                "added_lines": g.added_lines_count,
                "generated_lines": g.generated_code_lines,
                "adopted_lines": g.adopted_code_lines,
                "issue_date": g.issue_date,
                "create_time": g.create_time,
                "last_used_date": g.last_used_date,
            }
            for g in grants
        ],
    }

    def render() -> None:
        if not grants:
            ctx.out("No tool accounts matched.")
            return
        table = Table(
            ["ID", "Request No.", "Type", "Account", "Status", "Tokens", "Key"],
            aligns=["right", "left", "left", "left", "left", "right", "left"],
        )
        for g in grants:
            table.add(
                g.id,
                g.application_number,
                g.request_type,
                g.account_name,
                g.status_text,
                format_count(g.token_usage),
                g.virtual_key_masked,
            )
        ctx.table(table)
        ctx.blank()
        ctx.out(
            f"{len(grants)} account(s): "
            f"{sum(1 for g in grants if g.is_active)} 使用中, "
            f"{sum(1 for g in grants if not g.is_active)} 已失效"
        )

    ctx.emit(payload, render)
    return 0
