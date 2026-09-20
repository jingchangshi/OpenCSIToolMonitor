"""``opencsi prices`` -- the ``ai/config/cost`` price list.

The endpoint returns a bare JSON array (no ``code``/``data`` envelope), which is
why the client special-cases it. ``TOKEN`` rows are priced per million tokens;
``FLAT`` rows carry a monthly fee and must never be read as a token price.
"""

from __future__ import annotations

import argparse

from ..formatting import (
    Table,
    format_money,
    format_price,
    section,
)
from .context import CliContext, add_common_options


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "prices",
        help="show the model/tool price list",
        description=(
            "Show the published price list (ai/config/cost). TOKEN rows are "
            "billed per million tokens; FLAT rows are a monthly subscription."
        ),
    )
    add_common_options(parser)
    parser.add_argument(
        "--all",
        action="store_true",
        help="include disabled rows (default: enabled rows only)",
    )
    parser.add_argument(
        "--bill-type",
        choices=("TOKEN", "FLAT"),
        help="restrict output to one billing model",
    )
    parser.set_defaults(handler=run)


def run(ctx: CliContext) -> int:
    client = ctx.make_client()
    prices = client.get_model_prices()

    selected = tuple(prices)
    if not ctx.args.all:
        selected = tuple(p for p in selected if p.is_enabled)
    if ctx.args.bill_type:
        wanted = ctx.args.bill_type.upper()
        selected = tuple(p for p in selected if p.bill_type.upper() == wanted)

    payload = {
        "count": len(selected),
        "total_rows": len(prices),
        "enabled_rows": sum(1 for p in prices if p.is_enabled),
        "prices": [
            {
                "request_type": p.request_type,
                "display_name": p.display_name,
                "bill_type": p.bill_type,
                "enabled": p.is_enabled,
                "price_mode": p.price_mode,
                "blended_price": p.blended_price,
                "input_price": p.input_price,
                "output_price": p.output_price,
                "monthly_fee": p.monthly_fee,
                "remark": p.remark,
            }
            for p in selected
        ],
    }

    def render() -> None:
        if not selected:
            ctx.out("No price rows matched.")
            return
        table = Table(
            ["Request type", "Display name", "Bill", "Mode", "Blended", "In", "Out", "Monthly"],
            aligns=["left", "left", "left", "left", "right", "right", "right", "right"],
        )
        for p in sorted(selected, key=lambda x: (x.bill_type, x.request_type)):
            table.add(
                p.request_type,
                p.display_name or "-",
                p.bill_type or "-",
                p.price_mode or "-",
                format_price(p.blended_price),
                format_price(p.input_price),
                format_price(p.output_price),
                format_money(p.monthly_fee) if p.monthly_fee is not None else "-",
            )
        ctx.table(table)
        ctx.blank()
        ctx.out(
            f"{len(selected)} row(s) shown of {len(prices)} "
            f"({sum(1 for p in prices if p.is_enabled)} enabled)."
        )
        ctx.err(
            "note: 'Blended/In/Out' are per 1,000,000 tokens; 'Monthly' is a flat "
            "subscription fee, not a token price."
        )

    ctx.emit(payload, render)
    return 0
