"""``opencsi trend`` -- the ``tokenTrend`` series.

``startDate``/``endDate`` are the *only* parameters that change this data; every
other command ignores them (report §18).
"""

from __future__ import annotations

import argparse

from ..aggregation import aggregate_trend, display_name_for
from ..formatting import (
    Table,
    format_count,
    format_int,
    format_percent,
    section,
)
from .context import CliContext, add_common_options, add_date_options, validated_dates

#: ``--group-by`` choices.
BY_MODEL = "model"
BY_DATE = "date"


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "trend",
        help="show the token trend series",
        description=(
            "Show token consumption over time. The date window filters this "
            "series only; it does not change the tool-account list."
        ),
    )
    add_common_options(parser)
    add_date_options(parser)
    parser.add_argument(
        "--group-by",
        choices=(BY_MODEL, BY_DATE),
        default=BY_MODEL,
        help="aggregate by model (default) or by date",
    )
    parser.add_argument(
        "--by-day",
        action="store_true",
        help="also print a per-day breakdown",
    )
    parser.set_defaults(handler=run)


def run(ctx: CliContext) -> int:
    start, end = validated_dates(ctx.args)
    client = ctx.make_client()
    snapshot = client.get_my_tools(start, end)

    points = snapshot.token_trend
    by_model = aggregate_trend(points, by_model=True)
    by_date = aggregate_trend(points, by_model=False)

    try:
        prices = client.get_model_prices()
    except Exception:
        # Labels are cosmetic here; a price-list failure must not hide the trend.
        prices = ()

    primary = by_model if ctx.args.group_by == BY_MODEL else by_date
    total = sum(primary.values())

    payload: dict[str, object] = {
        "group_by": ctx.args.group_by,
        "date_range": {"start": start, "end": end},
        "total_tokens": total,
        "series": [
            {
                "key": key,
                "display_name": (
                    display_name_for(key, prices) if ctx.args.group_by == BY_MODEL else key
                ),
                "tokens": tokens,
                "share": round(tokens / total, 6) if total else 0.0,
            }
            for key, tokens in sorted(primary.items(), key=lambda kv: -kv[1])
        ],
        "distinct_dates": len(snapshot.trend_dates),
        "distinct_models": len(snapshot.trend_models),
        "sample_count": len(points),
    }
    if ctx.args.by_day:
        payload["by_day"] = [
            {"date": date, "tokens": tokens}
            for date, tokens in sorted(by_date.items())
        ]

    def render() -> None:
        if not points:
            ctx.out("No trend data for this window.")
            return

        ctx.out(section(f"Token trend by {ctx.args.group_by}"))
        table = Table(
            ["Key", "Display name", "Tokens", "Share"],
            aligns=["left", "left", "right", "right"],
        )
        for key, tokens in sorted(primary.items(), key=lambda kv: -kv[1]):
            table.add(
                key,
                display_name_for(key, prices) if ctx.args.group_by == BY_MODEL else key,
                format_count(tokens),
                format_percent(tokens / total) if total else "-",
            )
        ctx.table(table)

        ctx.blank()
        ctx.out(
            f"{len(points)} sample(s) across {len(snapshot.trend_dates)} date(s) "
            f"and {len(snapshot.trend_models)} model(s); "
            f"series total {format_count(total)} tokens."
        )

        if ctx.args.by_day:
            ctx.blank()
            ctx.out(section("By day"))
            day_table = Table(["Date", "Tokens"], aligns=["left", "right"])
            for date, tokens in sorted(by_date.items()):
                day_table.add(date, format_count(tokens))
            ctx.table(day_table)

    ctx.emit(payload, render)
    return 0
