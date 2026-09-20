"""``opencsi login`` -- establish and verify a session.

Two modes:

* **default** -- open the openCsiTool page in the browser you already use, wait
  for the ``token`` cookie to appear over DevTools, then validate it.
* **``--manual``** -- read a cookie value from *stdin* via :func:`getpass` for
  browsers the tool cannot reach.

Security notes that shape this command:

* A cookie is **never** accepted as a command line argument. Anything passed on
  the command line lands in process argv, shell history and process listings.
* A cookie is **never** written to disk. There is no credential file to leak or
  to forget to delete. The value lives for the duration of the process only.
"""

from __future__ import annotations

import argparse
import getpass
import time
import webbrowser

from ..auth import CdpCookieProvider, ManualCookieProvider
from ..errors import OpenCsiError, UsageError
from ..formatting import format_relative_seconds, render_kv, section
from ..redaction import register_secret
from .context import CliContext, add_common_options

LOGIN_URL = "https://opencsitool.com/myTools"
POLL_INTERVAL = 2.0


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "login",
        help="open the login page and verify the resulting session",
        description=(
            "Open openCsiTool in your browser, wait for the session cookie, and "
            "verify it. Use --manual to paste a cookie value on stdin instead "
            "(the value is never stored on disk)."
        ),
    )
    add_common_options(parser)
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="do not open a browser; only wait for an existing session",
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="how long to wait for the cookie to appear (default: 0 = check once)",
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="read the cookie value from stdin instead of the browser",
    )
    parser.set_defaults(handler=run)


def run(ctx: CliContext) -> int:
    if ctx.args.manual:
        return _manual(ctx)
    return _browser(ctx)


# ── browser mode ──────────────────────────────────────────────────────────
def _browser(ctx: CliContext) -> int:
    client = ctx.make_client(provider=CdpCookieProvider(ports=ctx.args.ports or None))
    provider = client.credentials
    assert isinstance(provider, CdpCookieProvider)

    if not ctx.args.no_browser:
        try:
            opened = webbrowser.open(LOGIN_URL)
        except Exception:
            opened = False
        if not opened:
            ctx.err(
                f"could not open a browser automatically; open {LOGIN_URL} "
                "manually and sign in."
            )
        else:
            ctx.err(f"opened {LOGIN_URL} -- sign in there, then leave the tab open.")

    deadline = time.time() + max(0.0, ctx.args.wait)
    attempts = 0
    last_error: str | None = None

    while True:
        attempts += 1
        try:
            provider.refresh()
            identity = client.login_or_restore_session(refresh=True)
        except OpenCsiError as exc:
            last_error = str(exc)
            identity = None

        if identity is not None:
            return _report(ctx, client, identity, attempts)

        if time.time() >= deadline:
            break
        time.sleep(POLL_INTERVAL)

    ctx.err(f"error: no valid openCsiTool session after {attempts} attempt(s).")
    if last_error:
        ctx.err(f"       {last_error}")
    ctx.err(f"       open {LOGIN_URL} in a browser with remote debugging enabled,")
    ctx.err("       sign in, keep the tab open, then run 'opencsi doctor'.")
    return 12


# ── manual mode ───────────────────────────────────────────────────────────
def _manual(ctx: CliContext) -> int:
    if not ctx.stdin_is_tty and ctx.json:
        raise UsageError("--manual --json cannot read a cookie from a pipe")

    try:
        value = getpass.getpass("openCsiTool 'token' cookie value (input hidden): ")
    except (EOFError, KeyboardInterrupt):
        ctx.err("error: no cookie value supplied.")
        return 2

    value = value.strip()
    if not value:
        ctx.err("error: empty cookie value.")
        return 2

    # Register before anything else can touch it, so it is redacted even if a
    # later error path stringifies it.
    register_secret(value)

    client = ctx.make_client(provider=ManualCookieProvider(value))
    try:
        identity = client.login_or_restore_session()
    except OpenCsiError as exc:
        ctx.err(f"error: {exc}")
        if exc.hint:
            ctx.err(f"       -> {exc.hint}")
        return exc.exit_code

    ctx.err(
        "note: the value was held in memory only and has already been discarded "
        "with this process. Nothing was written to disk."
    )
    return _report(ctx, client, identity, 1)


# ── shared reporting ──────────────────────────────────────────────────────
def _report(ctx: CliContext, client, identity, attempts: int) -> int:
    status = client.credentials.status()
    payload = {
        "ok": True,
        "attempts": attempts,
        "identity": {
            "user_id": identity.user_id,
            "employee_id": identity.employee_id,
            "user_name": identity.user_name,
            "account_login": identity.account_login,
            "organization_name": identity.organization_name,
        },
        "credential": status.as_dict(),
    }

    def render() -> None:
        ctx.out(section("Signed in"))
        ctx.out(
            render_kv(
                [
                    ("Display name", identity.display_name),
                    ("Login", identity.account_login),
                    ("Employee ID", identity.employee_id),
                    ("Organization", identity.organization_name),
                    ("Credential source", status.source),
                    ("Cookie lifetime left", format_relative_seconds(status.expires_in)),
                ]
            )
        )
        ctx.blank()
        ctx.out("Session is valid. Try: opencsi usage")

    ctx.emit(payload, render)
    return 0
