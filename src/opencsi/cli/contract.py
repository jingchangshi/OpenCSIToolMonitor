"""``opencsi contract-check`` -- verify the upstream API still matches.

Read-only structural check: it asserts HTTP status, envelope shape, required
fields and types, and deliberately ignores dynamic business values so that
normal data drift never fails it (report §49-50). Run it after an openCsiTool
release to learn whether this tool needs updating, rather than discovering it
through a confusing error.
"""

from __future__ import annotations

import argparse

from ..errors import OpenCsiError
from ..formatting import section
from .context import CliContext, add_common_options


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "contract-check",
        help="verify the live API still matches the verified contract",
        description=(
            "Compare the live responses against the contract recorded during "
            "the API investigation. Read-only; performs GET requests only."
        ),
    )
    add_common_options(parser)
    parser.set_defaults(handler=run)


def run(ctx: CliContext) -> int:
    client = ctx.make_client()
    try:
        result = client.contract_check()
    except OpenCsiError as exc:
        payload = {"ok": False, "error": exc.as_dict()}
        if ctx.json:
            ctx.emit_json(payload)
        else:
            ctx.err(f"error: {exc}")
            if exc.hint:
                ctx.err(f"       -> {exc.hint}")
        return exc.exit_code

    checks = result["checks"]
    payload = {
        "ok": result["ok"],
        "passed": sum(1 for c in checks if c["ok"]),
        "failed": sum(1 for c in checks if not c["ok"]),
        "checks": checks,
    }

    def render() -> None:
        ctx.out(section("API contract check"))
        for check in checks:
            mark = "[ok]  " if check["ok"] else "[FAIL]"
            detail = check.get("detail") or ""
            ctx.out(f"{mark} {check['check']}: {detail}")
        ctx.blank()
        ctx.out(f"{payload['passed']} passed, {payload['failed']} failed.")

    ctx.emit(payload, render)
    return 0 if result["ok"] else 1
