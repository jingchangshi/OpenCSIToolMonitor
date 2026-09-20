"""``opencsi usage`` -- the "Personal data overview" card, plus cost estimate.

This is the headline command: tokens, requests, PRs, generated/adopted lines and
the per-tool token split, exactly as the web page renders them.
"""

from __future__ import annotations

import argparse

from ..aggregation import display_name_for, summarise
from ..formatting import (
    Table,
    format_count,
    format_datetime,
    format_int,
    format_money,
    format_percent,
    format_price,
    render_kv,
    section,
)
from .context import CliContext, add_common_options, add_date_options, validated_dates


def _trend_window(snapshot) -> str:  # noqa: ANN001 - MyToolsSnapshot
    """Describe the trend window in words when no explicit dates were given."""
    if snapshot.start_date and snapshot.end_date:
        return f"{snapshot.start_date} .. {snapshot.end_date}"
    if snapshot.start_date or snapshot.end_date:
        return f"{snapshot.start_date or '?'} .. {snapshot.end_date or '?'}"
    return "all dates (server default)"


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "usage",
        help="show the personal data overview (tokens, PRs, lines)",
        description=(
            "Show the personal data overview card: total tokens and requests, "
            "PR count, generated and adopted code lines, and the per-tool token "
            "split. Optionally estimate spend from the published price list."
        ),
    )
    add_common_options(parser)
    add_date_options(parser)
    parser.add_argument(
        "--cost",
        action="store_true",
        help="also estimate spend using the ai/config/cost price list",
    )
    parser.set_defaults(handler=run)


def run(ctx: CliContext) -> int:
    start, end = validated_dates(ctx.args)
    client = ctx.make_client()
    snapshot = client.get_my_tools(start, end)
    summary = summarise(snapshot)

    estimate = None
    prices = ()
    if ctx.args.cost:
        prices = client.get_model_prices()
        estimate = client.estimate_cost(summary.tokens_by_request_type)

    payload: dict[str, object] = {
        "summary": summary.as_dict(),
        "sync_status": {
            "data_fresh_time": snapshot.sync_status.data_fresh_time,
            "etl_time": snapshot.sync_status.etl_time,
            "replication_time": snapshot.sync_status.replication_time,
        },
        "fetched_at": snapshot.fetched_at.isoformat() if snapshot.fetched_at else None,
        "date_range": {"start": snapshot.start_date, "end": snapshot.end_date},
    }
    if snapshot.token_budget is not None:
        payload["token_budget"] = {
            "exists": snapshot.token_budget.exists,
            "max_budget": snapshot.token_budget.max_budget,
            "spend": snapshot.token_budget.spend,
            "budget_duration": snapshot.token_budget.budget_duration,
        }
    if estimate is not None:
        payload["cost_estimate"] = estimate.as_dict()

    def render() -> None:
        ctx.out(section("Personal data overview"))
        ctx.out(
            render_kv(
                [
                    ("Total tokens", format_count(summary.total_tokens)),
                    ("Total tokens (exact)", format_int(summary.total_tokens)),
                    ("Total requests", format_count(summary.total_request_count)),
                    ("Pull requests", format_int(summary.pr_count)),
                    ("Added lines", format_count(summary.added_lines_count)),
                    ("Generated code lines", format_count(summary.generated_code_lines)),
                    ("Adopted code lines", format_int(summary.adopted_code_lines)),
                    (
                        "Adoption rate",
                        # Show the underlying fraction: a bare percentage that
                        # restates itself reads like a bug.
                        f"{format_percent(summary.adoption_rate)} "
                        f"({format_int(summary.adopted_code_lines)}"
                        f"/{format_int(summary.generated_code_lines)})",
                    ),
                    (
                        "Tool accounts",
                        f"{summary.total_tools} "
                        f"({summary.active_tools} 使用中 / {summary.expired_tools} 已失效)",
                    ),
                ]
            )
        )

        if summary.tokens_by_request_type:
            ctx.blank()
            ctx.out(section("Tokens by tool"))
            table = Table(
                ["Type", "Display name", "Tokens", "Share"],
                aligns=["left", "left", "right", "right"],
            )
            total = summary.total_tokens or sum(summary.tokens_by_request_type.values())
            for request_type, tokens in sorted(
                summary.tokens_by_request_type.items(), key=lambda kv: -kv[1]
            ):
                table.add(
                    request_type,
                    display_name_for(request_type, prices),
                    format_count(tokens),
                    format_percent(tokens / total) if total else "-",
                )
            ctx.table(table)

        if estimate is not None:
            ctx.blank()
            ctx.out(section("Estimated cost"))
            table = Table(
                ["Type", "Bill", "Tokens", "Unit price", "Est. cost"],
                aligns=["left", "left", "right", "right", "right"],
            )
            for line in estimate.lines:
                table.add(
                    # The column is labelled "Type", so show the request type.
                    # The friendly display name would be a different value in
                    # the same column (e.g. "Trae" for "TRAE"), which reads as
                    # an inconsistency against the "Tokens by tool" table above.
                    line.request_type,
                    line.bill_type,
                    format_count(line.tokens),
                    format_price(line.unit_price),
                    format_money(line.estimated_cost, currency=estimate.currency),
                )
            ctx.table(table)
            ctx.blank()
            ctx.out(
                render_kv(
                    [
                        ("Token-based spend", format_money(estimate.token_cost, currency=estimate.currency)),
                        ("Flat monthly fees", format_money(estimate.flat_cost, currency=estimate.currency)),
                        ("Total", format_money(estimate.total_cost, currency=estimate.currency)),
                    ]
                )
            )
            ctx.err(
                "note: estimated from the published price list; the server-side "
                "billing record is authoritative."
            )

        ctx.blank()
        ctx.out(section("Data freshness"))
        ctx.out(
            render_kv(
                [
                    ("Data fresh time (server)", snapshot.sync_status.data_fresh_time),
                    ("ETL time", snapshot.sync_status.etl_time),
                    ("Replication time", snapshot.sync_status.replication_time),
                    ("Fetched at (this run)", format_datetime(snapshot.fetched_at)),
                    ("Trend window", _trend_window(snapshot)),
                ]
            )
        )

    ctx.emit(payload, render)
    return 0
