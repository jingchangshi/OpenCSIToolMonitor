"""``opencsi logout`` -- clear the locally stored credentials.

What this does, and what it deliberately does not
--------------------------------------------------
It removes this machine's encrypted credential file. It does **not** call any
remote revoke endpoint, and that is a decision rather than an omission:

* revoking server-side is destructive in a way the user cannot undo from here --
  it invalidates sessions on their other machines, which is not what "log out on
  this laptop" means;
* it needs a valid session to authenticate the revoke, which is exactly what the
  user may no longer have;
* a tool that reaches the network when asked to forget something locally is a
  tool whose "log out" cannot be used offline or with confidence.

If remote revocation is ever wanted it belongs behind an explicit flag that says
so, not folded into this verb.

Scope
-----
Two halves are stored and they have different lifetimes, so the default is to
clear the **session** only:

* the openCsiTool session (about an hour) is the thing "log out" normally means;
* the GitCode credential (15 days, plus its refresh token) is the upstream
  identity, and discarding it means the next sign-in needs a WeChat scan.

``--forget-gitcode`` clears the second as well, and ``--all`` is the same thing
stated plainly. The default keeps the cheap-to-keep half, because a user who
wants their session gone today almost never wants to re-scan tomorrow.

It is worth being precise about what this does *not* protect against: clearing
the file does not invalidate the cookie, which remains valid on the server until
it expires. If a credential is believed compromised, the remedy is to revoke it
where it was issued -- this command is about leaving no credential on this disk.
"""

from __future__ import annotations

import argparse

from ..auth.store import CredentialStoreError
from ..errors import ConfigError, EXIT_OK
from ..formatting import render_kv, section
from .context import ENV_NO_STORE, CliContext, add_common_options


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "logout",
        help="clear the credentials stored on this machine",
        description=(
            "Remove the locally stored credential file. By default only the "
            "openCsiTool session is cleared and the GitCode credential is kept, "
            "so signing in again does not need another QR scan. Nothing is "
            "revoked on the server; see the module docstring."
        ),
    )
    add_common_options(parser)
    parser.add_argument(
        "--forget-gitcode",
        action="store_true",
        help=(
            "also remove the stored GitCode credential, so the next sign-in "
            "requires a new QR scan"
        ),
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="forget_all",
        help="remove everything, including the GitCode credential",
    )
    parser.set_defaults(handler=run)


def run(ctx: CliContext) -> int:
    import os

    from ..auth.windows_store import open_default_store

    if getattr(ctx.args, "no_store", False) or os.environ.get(ENV_NO_STORE):
        raise ConfigError(
            "--no-store means there is no stored credential to clear; "
            "run without it to remove the stored credentials"
        )

    store = open_default_store()
    if store is None:
        # Not an error. The user asked to remove something that cannot exist on
        # this platform, and the honest answer is that there is nothing to
        # remove -- not a failure exit code for a request that is already true.
        _report(ctx, cleared=False, detail="no secure credential store on this platform")
        return EXIT_OK

    forget_gitcode = bool(
        getattr(ctx.args, "forget_gitcode", False) or getattr(ctx.args, "forget_all", False)
    )
    path = getattr(store, "path", None)

    try:
        if forget_gitcode:
            store.clear_all()
            cleared = "everything"
        else:
            store.clear_opencsi()
            cleared = "the openCsiTool session"
    except CredentialStoreError as exc:
        # A store that cannot be written is reported, with its own non-zero
        # status: claiming the credentials are gone when the file is still there
        # would be the worst possible answer to this particular question.
        ctx.err(f"error: the credentials could not be cleared: {exc}")
        ctx.emit_json({"ok": False, "detail": str(exc)})
        return 2

    _report(
        ctx,
        cleared=True,
        detail=cleared,
        path=str(path) if path is not None else None,
        kept_gitcode=not forget_gitcode,
    )
    return EXIT_OK


def _report(
    ctx: CliContext,
    *,
    cleared: bool,
    detail: str,
    path: str | None = None,
    kept_gitcode: bool = False,
) -> None:
    """Say what happened, in both output modes.

    The text form names ``opencsi login --qr`` when the GitCode credential was
    kept, because that is the command that will work without a scan -- and a user
    who has just logged out is the user most likely to want to know which one it
    is.
    """
    if not cleared:
        ctx.emit(
            {"ok": True, "cleared": False, "detail": detail},
            lambda: ctx.out("local credentials cleared (nothing was stored)"),
        )
        return

    payload: dict[str, object] = {
        "ok": True,
        "cleared": True,
        "cleared_scope": detail,
        "gitcode_credential_kept": kept_gitcode,
    }
    if path:
        payload["store_path"] = path

    def render() -> None:
        ctx.out("local credentials cleared")
        ctx.out(render_kv([("Cleared", detail)]))
        if path:
            ctx.out(render_kv([("Store", path)]))
        if kept_gitcode:
            ctx.blank()
            ctx.out(
                "The GitCode credential was kept, so signing back in needs no QR "
                "scan:"
            )
            ctx.out("    opencsi login --renew")
            ctx.out("To remove it as well: opencsi logout --all")

    ctx.emit(payload, render)
