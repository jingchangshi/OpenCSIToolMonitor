"""``opencsi logs`` -- the llmgateway call log for the bound employee.

This endpoint lives on a different service path
(``/opencsitool/llmgateway/rest/v1/users/{employeeId}/call-logs``) and needs the
employee id, so the client resolves the session first.
"""

from __future__ import annotations

import argparse

from ..formatting import Table, format_int, format_relative_seconds, or_dash, section
from .context import CliContext, add_common_options, add_date_options, validated_dates

#: Field names the page shows, in preference order, when present.
_INTERESTING = (
    "id",
    "requestId",
    "requestType",
    "model",
    "modelName",
    "status",
    "totalTokens",
    "promptTokens",
    "completionTokens",
    "cost",
    "createTime",
    "requestTime",
)


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "logs",
        help="show recent LLM gateway call logs",
        description=(
            "Show the LLM gateway call log for the signed-in employee. The log "
            "is often empty for accounts that only use bundled tools."
        ),
    )
    add_common_options(parser)
    add_date_options(parser)
    parser.add_argument("--page", type=int, default=1, help="page number (default: 1)")
    parser.add_argument(
        "--page-size",
        type=int,
        default=20,
        help="rows per page (default: 20)",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="print every field of each record instead of the summary columns",
    )
    parser.set_defaults(handler=run)


def run(ctx: CliContext) -> int:
    start, end = validated_dates(ctx.args)
    client = ctx.make_client()
    result = client.get_call_logs(
        page=ctx.args.page,
        page_size=ctx.args.page_size,
        start_date=start,
        end_date=end,
    )

    records = result["list"]
    payload = {
        "total": result["total"],
        "page": result["page"],
        "page_size": result["pageSize"],
        "count": len(records),
        "records": records,
    }

    def render() -> None:
        ctx.out(section("LLM gateway call logs"))
        ctx.out(
            f"page {result['page']}  size {result['pageSize']}  "
            f"total {format_int(result['total'])}  shown {len(records)}"
        )
        if not records:
            ctx.blank()
            ctx.out("No call log records.")
            ctx.err(
                "note: an empty log is normal when usage is billed through a "
                "bundled tool rather than the LLM gateway."
            )
            return
        ctx.blank()
        if ctx.args.raw:
            keys: list[str] = []
            for record in records:
                for key in record:
                    if key not in keys:
                        keys.append(key)
            table = Table(list(keys))
            for record in records:
                table.add(*(or_dash(record.get(k)) for k in keys))
            ctx.table(table)
            return

        columns = [k for k in _INTERESTING if any(k in r for r in records)]
        if not columns:
            columns = list(records[0].keys())[:6]
        table = Table(columns)
        for record in records:
            table.add(*(or_dash(record.get(k)) for k in columns))
        ctx.table(table)

    ctx.emit(payload, render)
    return 0
