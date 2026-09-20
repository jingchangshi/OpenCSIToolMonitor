"""``opencsi tools`` -- the granted AI tool accounts (``requestList``).

Filtering is entirely client-side. The server exposes no search endpoint for
``requestList``; it always returns the full list, so ``--type``/``--search``/
``--active`` narrow it locally rather than inventing a query the API does not
have.
"""

from __future__ import annotations

import argparse

from ..errors import UsageError
from ..formatting import Table, format_count, format_datetime, section
from .context import CliContext, add_common_options, add_date_options, validated_dates


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "tools",
        help="list granted AI tool accounts",
        description=(
            "List every AI tool account granted to the signed-in user, with "
            "status, token usage and the masked virtual key. Filtering is "
            "client-side."
        ),
    )
    add_common_options(parser)
    add_date_options(parser)
    parser.add_argument(
        "--active",
        "--active-only",
        dest="active_only",
        action="store_true",
        help="show only accounts with status 使用中",
    )
    parser.add_argument(
        "--type",
        dest="request_type",
        metavar="TYPE",
        help="only accounts of this requestType, e.g. API_BUNDLE (exact, case-insensitive)",
    )
    parser.add_argument(
        "--search",
        metavar="TEXT",
        help=(
            "only accounts whose account name, tool name, request number or "
            "request type contains TEXT (case-insensitive)"
        ),
    )
    parser.add_argument(
        "--show-key-mask",
        action="store_true",
        help=(
            "include the site's own masked virtual key (sk-xxxxxxxx****). The "
            "full key is never available to this tool."
        ),
    )
    parser.set_defaults(handler=run)


def _matches(grant, *, needle: str) -> bool:
    """Case-insensitive substring match over the fields a user would search."""
    haystack = " ".join(
        str(part or "")
        for part in (
            grant.account_name,
            grant.ai_tool_name,
            grant.application_number,
            grant.request_type,
            grant.status_text,
        )
    ).lower()
    return needle in haystack


def run(ctx: CliContext) -> int:
    start, end = validated_dates(ctx.args)
    client = ctx.make_client()
    snapshot = client.get_my_tools(start, end)

    grants = snapshot.grants
    total_before = len(grants)

    if ctx.args.active_only:
        grants = tuple(g for g in grants if g.is_active)

    if ctx.args.request_type:
        wanted = ctx.args.request_type.strip().lower()
        grants = tuple(g for g in grants if g.request_type.lower() == wanted)
        if not grants:
            available = sorted({g.request_type for g in snapshot.grants if g.request_type})
            raise UsageError(
                f"no account has requestType {ctx.args.request_type!r}; "
                f"available: {', '.join(available) if available else '(none)'}"
            )

    if ctx.args.search:
        needle = ctx.args.search.strip().lower()
        grants = tuple(g for g in grants if _matches(g, needle=needle))

    show_key = bool(ctx.args.show_key_mask)
    payload = {
        "count": len(grants),
        "total_accounts": total_before,
        "filtered": len(grants) != total_before,
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
                "token_usage": g.token_usage,
                "request_count": g.request_count,
                "pr_count": g.pr_count,
                "added_lines": g.added_lines_count,
                "generated_lines": g.generated_code_lines,
                "adopted_lines": g.adopted_code_lines,
                "issue_date": g.issue_date,
                "create_time": g.create_time,
                "last_used_date": g.last_used_date,
                **(
                    {"virtual_key_masked": g.virtual_key_masked}
                    if show_key
                    else {"has_virtual_key": g.has_virtual_key}
                ),
            }
            for g in grants
        ],
    }

    def render() -> None:
        if not grants:
            ctx.out("No tool accounts matched.")
            if total_before:
                ctx.err(
                    f"note: {total_before} account(s) exist; the filters excluded "
                    "all of them."
                )
            return
        headers = ["ID", "Request No.", "Type", "Account", "Status", "Tokens"]
        aligns = ["right", "left", "left", "left", "left", "right"]
        if show_key:
            headers.append("Key (masked)")
            aligns.append("left")
        table = Table(headers, aligns=aligns)
        for g in grants:
            row = [
                g.id,
                g.application_number,
                g.request_type,
                g.account_name,
                g.status_text,
                format_count(g.token_usage),
            ]
            if show_key:
                row.append(g.virtual_key_masked)
            table.add(*row)
        ctx.table(table)
        ctx.blank()
        ctx.out(
            f"{len(grants)} account(s): "
            f"{sum(1 for g in grants if g.is_active)} 使用中, "
            f"{sum(1 for g in grants if not g.is_active)} 已失效"
        )
        if not show_key:
            ctx.err(
                "note: virtual keys are hidden. Re-run with --show-key-mask to "
                "see the site's own masked form (the full key is never shown)."
            )

    ctx.emit(payload, render)
    return 0
