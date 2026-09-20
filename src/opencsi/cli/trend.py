"""``opencsi trend`` -- the ``tokenTrend`` series.

``startDate``/``endDate`` are the *only* parameters that change this data; every
other command ignores them (report §18).
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta

from ..aggregation import aggregate_trend, aggregate_trend_detail, display_name_for
from ..errors import UsageError
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
    window = parser.add_argument_group("window (aliases)")
    window.add_argument(
        "--days",
        type=int,
        metavar="N",
        help="shortcut for the last N days, ending today",
    )
    window.add_argument(
        "--from",
        dest="from_date",
        metavar="YYYY-MM-DD",
        help="alias for --start-date",
    )
    window.add_argument(
        "--to",
        dest="to_date",
        metavar="YYYY-MM-DD",
        help="alias for --end-date",
    )
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


def resolve_window(args: argparse.Namespace, *, today: date | None = None) -> tuple[str | None, str | None]:
    """Combine ``--start-date``/``--end-date`` with the ``--days``/``--from``/``--to`` aliases.

    The aliases exist because "the last 7 days" is what people actually want to
    ask, and computing that by hand is easy to get wrong by a day. ``--days``
    includes today, so ``--days 7`` is today plus the six before it.

    Mixing ``--days`` with an explicit date is rejected rather than silently
    preferring one: the two can contradict, and guessing would produce a window
    the user did not ask for.
    """
    start = getattr(args, "start_date", None)
    end = getattr(args, "end_date", None)
    from_date = getattr(args, "from_date", None)
    to_date = getattr(args, "to_date", None)
    days = getattr(args, "days", None)

    if from_date:
        if start:
            raise UsageError("--from and --start-date are the same option; give one")
        start = from_date
    if to_date:
        if end:
            raise UsageError("--to and --end-date are the same option; give one")
        end = to_date

    if days is not None:
        if days <= 0:
            raise UsageError("--days must be a positive number of days")
        if start or end:
            raise UsageError(
                "--days cannot be combined with an explicit date range; "
                "use one or the other"
            )
        anchor = today or date.today()
        # Inclusive of today, so --days 1 is today alone.
        end = anchor.isoformat()
        start = (anchor - timedelta(days=days - 1)).isoformat()

    # Re-validate through the shared checker so the ordering and shape rules
    # stay in exactly one place.
    args.start_date = start
    args.end_date = end
    return validated_dates(args)


def run(ctx: CliContext) -> int:
    start, end = resolve_window(ctx.args)
    client = ctx.make_client()
    snapshot = client.get_my_tools(start, end)

    points = snapshot.token_trend
    by_model = aggregate_trend(points, by_model=True)
    by_date = aggregate_trend(points, by_model=False)
    detail = aggregate_trend_detail(points, by_model=ctx.args.group_by == BY_MODEL)

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
                "key": bucket.key,
                "display_name": (
                    display_name_for(bucket.key, prices)
                    if ctx.args.group_by == BY_MODEL
                    else bucket.key
                ),
                "tokens": bucket.tokens,
                "prompt_tokens": bucket.prompt_tokens,
                "completion_tokens": bucket.completion_tokens,
                "share": round(bucket.tokens / total, 6) if total else 0.0,
            }
            for bucket in detail
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
            ["Key", "Display name", "Tokens", "Prompt", "Completion", "Share"],
            aligns=["left", "left", "right", "right", "right", "right"],
        )
        for bucket in sorted(detail, key=lambda b: -b.tokens):
            table.add(
                bucket.key,
                display_name_for(bucket.key, prices)
                if ctx.args.group_by == BY_MODEL
                else bucket.key,
                format_count(bucket.tokens),
                format_count(bucket.prompt_tokens),
                format_count(bucket.completion_tokens),
                format_percent(bucket.tokens / total) if total else "-",
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
