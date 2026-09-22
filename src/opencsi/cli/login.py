"""``opencsi login`` -- establish, inspect and renew a session.

Four modes, in order of how much user involvement they need:

* **``--status``** -- report the session and credential lifetime without
  changing anything.
* **``--renew``** -- run one silent OAuth renewal and report the structured
  outcome. This is the manual trigger for the automatic path, and the entry
  point used to verify renewal for real without waiting ~58 minutes.
* **default (browser)** -- open the openCsiTool page in the browser you already
  use, wait for the ``token`` cookie to appear over DevTools, then validate it.
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
import os
import time

from ..auth import (
    BrowserOAuthRenewer,
    CdpCookieProvider,
    ManualCookieProvider,
    RenewalStatus,
    SessionManager,
)
from ..errors import (
    EXIT_INTERRUPTED,
    EXIT_NETWORK_ERROR,
    EXIT_QR_PROTOCOL,
    EXIT_SESSION_EXPIRED,
    EXIT_USAGE,
    OpenCsiError,
    UsageError,
    exit_code_for,
)
from ..formatting import format_relative_seconds, render_kv, section
from ..redaction import register_secret
from .context import CliContext, add_common_options

LOGIN_URL = "https://opencsitool.com/myTools"
POLL_INTERVAL = 2.0

#: Renewal outcomes that mean "a human is needed after all", and the exit code
#: to report for each. Derived from the *status*, never from parsing prose.
_RENEWAL_EXIT = {
    RenewalStatus.RENEWED: 0,
    RenewalStatus.ALREADY_VALID: 0,
    RenewalStatus.LOGIN_REQUIRED: EXIT_SESSION_EXPIRED,
    RenewalStatus.CDP_UNAVAILABLE: 10,
    RenewalStatus.OAUTH_FAILED: EXIT_SESSION_EXPIRED,
    RenewalStatus.TIMEOUT: EXIT_NETWORK_ERROR,
    RenewalStatus.UNSUPPORTED: EXIT_USAGE,
}


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "login",
        help="open the login page, inspect the session, or renew it silently",
        description=(
            "Establish, inspect or renew an openCsiTool session. Without a mode "
            "flag this opens the login page in your browser and waits for the "
            "session cookie. --status reports the current session; --renew "
            "re-runs the GitCode OAuth flow in a background browser tab without "
            "asking you anything. --manual reads a cookie from stdin (the value "
            "is never stored on disk)."
        ),
    )
    add_common_options(parser)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--status",
        action="store_true",
        help="report the current session and credential lifetime; change nothing",
    )
    mode.add_argument(
        "--renew",
        action="store_true",
        help=(
            "re-run GitCode OAuth in a background tab to renew an expiring "
            "session, then report the structured outcome"
        ),
    )
    mode.add_argument(
        "--browser",
        action="store_true",
        help="(default) open the login page and wait for a session",
    )
    mode.add_argument(
        "--qr",
        action="store_true",
        help=(
            "sign in to GitCode by scanning a WeChat code, with no browser; "
            "the code is saved as an image to scan (see "
            "docs/gitcode-qr-protocol.md)"
        ),
    )
    mode.add_argument(
        "--manual",
        action="store_true",
        help="read the cookie value from stdin instead of the browser",
    )
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
        "--qr-wait",
        type=float,
        default=180.0,
        metavar="SECONDS",
        help="how long to wait for a QR scan before giving up (default: 180)",
    )
    parser.set_defaults(handler=run)


def run(ctx: CliContext) -> int:
    if ctx.args.manual:
        return _manual(ctx)
    if ctx.args.status:
        return _status(ctx)
    if ctx.args.renew:
        return _renew(ctx)
    if ctx.args.qr:
        return _qr(ctx)
    return _browser(ctx)


# ── QR mode ───────────────────────────────────────────────────────────────
def _qr(ctx: CliContext) -> int:
    """Sign in by scanning a WeChat code, without a browser.

    This is the *interactive* half of the new authentication architecture. It
    obtains a GitCode session over plain HTTP -- verified reproducible against
    the real protocol (docs/gitcode-qr-protocol.md).

    Two honest limits, both established by decoding a real code rather than by
    assuming:

    * GitCode's ``qrcode`` field is a **WeChat mini-program code**, not a QR
      code. Its dots are finer than a terminal cell, so a terminal rendering
      cannot be scanned. The code is therefore written to a file at full
      resolution and the user is pointed at it; the terminal drawing is a
      preview so they can see it loaded.
    * openCsiTool's own ``token`` cookie is issued by its OAuth callback, which
      needs a browser session. A GitCode session alone does not mint it.

    So this command's success criterion is "GitCode signed in", and it says
    plainly what still needs the browser afterwards.
    """
    from ..auth.gitcode_qr import GitCodeQrAuthenticator, QrLoginStatus, QrStatus
    from ..auth.qr_render import decode_payload_image, render_payload, write_image

    authenticator = GitCodeQrAuthenticator(
        api_base=os.environ.get("OPENCSI_GITCODE_API", "https://web-api.gitcode.com"),
        source=os.environ.get("OPENCSI_QR_SOURCE", "toolbar_login"),
        max_wait=float(getattr(ctx.args, "qr_wait", 180.0) or 180.0),
        use_proxy=not bool(getattr(ctx.args, "no_proxy", False)),
    )

    rendered: dict[str, object] = {}

    def on_challenge(challenge) -> None:
        result = render_payload(challenge.image)
        # Write the full-resolution image separately: it is the only rendering
        # that can actually be scanned, and `render_payload` may have returned a
        # preview.
        path = None
        raw = decode_payload_image(challenge.image)
        if raw is not None:
            path = write_image(raw)
        rendered["mode"] = result.mode
        rendered["path"] = path or result.path
        rendered["scannable"] = bool(path) or result.scannable
        rendered["detail"] = result.detail
        if ctx.json:
            return

        ctx.err("")
        ctx.err("Sign in to GitCode by scanning this with WeChat (微信扫一扫):")
        ctx.err("")
        if result.mode == "unicode":
            # Drawn on stderr with the rest of the interactive block. Splitting
            # the picture onto stdout and its caption onto stderr makes the two
            # interleave unpredictably once either stream is a pipe, and a
            # caption that arrives before its picture is worse than useless.
            for line in result.text.splitlines():
                ctx.err(line)
        if rendered["path"]:
            ctx.err("")
            ctx.err(f"Open this file and scan it with WeChat: {rendered['path']}")
            if not result.scannable:
                ctx.err(
                    "Scan the image, not the terminal drawing above: this is a "
                    "WeChat mini-program code, and its dots are finer than a "
                    "terminal cell."
                )
        elif result.mode == "none":
            ctx.err(f"error: could not display the code. {result.detail or ''}")
        ctx.err("")

    def on_state(state) -> None:
        if ctx.json:
            return
        message = {
            QrStatus.WAITING: "Waiting for scan...",
            QrStatus.SCAN: "Scanned. Confirm on your phone...",
            QrStatus.LOGIN: "Login confirmed. Completing GitCode sign-in...",
            QrStatus.TIMEOUT: "The code expired; issuing a new one...",
        }.get(state)
        if message:
            ctx.err(message)

    ctx.err("Requesting a GitCode login code...")
    result = authenticator.login(
        timeout=float(getattr(ctx.args, "qr_wait", 180.0) or 180.0),
        on_challenge=on_challenge,
        on_state=on_state,
    )

    cookies = authenticator.session_cookies()
    payload = {
        "ok": result.ok,
        "qr_login": result.as_dict(),
        "display": {
            "mode": rendered.get("mode"),
            "path": rendered.get("path"),
            "scannable": rendered.get("scannable", False),
            "note": rendered.get("detail"),
        },
        "gitcode_session_cookies": sorted(cookies),
        "openscitool_session": {
            "established": False,
            "next_step": (
                "openCsiTool's token cookie is issued by its own OAuth callback, "
                "which needs a browser session. Run 'opencsi login' (or "
                "'opencsi login --renew') with a browser signed in to GitCode."
            ),
        },
    }

    def render() -> None:
        if result.ok:
            ctx.err("")
            ctx.err("GitCode sign-in complete.")
            if result.username:
                ctx.out(f"GitCode user: {result.username}")
            if result.is_new_user is not None:
                ctx.out(f"New GitCode account: {'yes' if result.is_new_user else 'no'}")
            if cookies:
                ctx.out("GitCode session cookies received: " + ", ".join(sorted(cookies)))
            ctx.blank()
            ctx.out(
                "Next: openCsiTool's session cookie comes from its own OAuth "
                "callback, so run 'opencsi login' in a browser signed in to "
                "GitCode. 'opencsi login --renew' will then keep it alive "
                "without further prompts."
            )
        else:
            ctx.err("")
            ctx.err(f"error: QR sign-in did not complete ({result.status.value}).")
            if result.detail:
                ctx.err(f"       {result.detail}")

    ctx.emit(payload, render)

    if result.ok:
        return 0
    return {
        QrLoginStatus.CANCELLED: EXIT_USAGE,
        QrLoginStatus.EXPIRED: EXIT_SESSION_EXPIRED,
        QrLoginStatus.TIMEOUT: EXIT_SESSION_EXPIRED,
        QrLoginStatus.NETWORK_ERROR: EXIT_NETWORK_ERROR,
        QrLoginStatus.PROTOCOL_ERROR: EXIT_QR_PROTOCOL,
    }.get(result.status, 1)


# ── status mode ───────────────────────────────────────────────────────────
def _status(ctx: CliContext) -> int:
    """Report the session without changing it.

    ``--no-renew`` is forced on: a status check that silently performed an OAuth
    round-trip would be a surprising side effect, and would also hide the very
    expiry the user asked about.

    That means ``session.renewer`` is ``None`` here *by construction*, so it
    cannot be used to answer "can this session renew itself?" -- reading it was a
    real bug: it made ``--status`` print "unavailable" on a machine where
    ``doctor`` reported renewal working. The capability is probed separately,
    through the same helper ``doctor`` uses, so the two commands agree.
    """
    from ..auth.oauth_browser import renewal_capability

    provider = ctx.make_provider()
    client = ctx.make_client(provider=provider, renew=False)
    cred = provider.status()

    identity = None
    error: OpenCsiError | None = None
    try:
        identity = client.login_or_restore_session(refresh=True)
    except OpenCsiError as exc:
        error = exc

    capability = renewal_capability(provider)
    session = client.session
    payload = {
        "ok": identity is not None,
        "credential": cred.as_dict(),
        "renewal": {
            "available": capability.available,
            "reason": capability.reason,
            "needs_renewal": session.needs_renewal(),
            "margin_seconds": session.renew_margin,
            "last": session.last_renewal.as_dict() if session.last_renewal else None,
        },
        "session": (
            {
                "user_name": identity.user_name,
                "employee_id": identity.employee_id,
            }
            if identity
            else None
        ),
    }
    if error is not None:
        payload["error"] = error.as_dict()

    def render() -> None:
        ctx.out(section("Session"))
        ctx.out(render_kv([("Status", "OK" if identity else "FAILED")]))
        if identity:
            ctx.out(
                render_kv(
                    [
                        ("User", identity.display_name),
                        ("Employee", identity.employee_id),
                    ]
                )
            )
        ctx.blank()
        ctx.out(
            render_kv(
                [
                    ("Credential source", cred.source),
                    ("Cookie lifetime left", format_relative_seconds(cred.expires_in)),
                    (
                        "Silent renewal",
                        "available" if capability.available else "unavailable",
                    ),
                ]
            )
        )
        if not capability.available:
            # Say *why*, not just "unavailable": the reason is the difference
            # between a fixable misconfiguration and a credential that has no
            # upstream session to renew against.
            ctx.out(f"  {capability.reason}")
        if capability.available and session.needs_renewal():
            ctx.out(
                "The session is inside the renewal margin; the next request will "
                "renew it silently."
            )
        if error is not None:
            ctx.err(f"error: {error}")
            if error.hint:
                ctx.err(f"       -> {error.hint}")

    ctx.emit(payload, render)
    return 0 if identity is not None else exit_code_for(error)


# ── renew mode ────────────────────────────────────────────────────────────
def _renew(ctx: CliContext) -> int:
    """Run one silent renewal, on demand.

    This exists so the automatic path can be exercised deliberately -- waiting
    ~58 minutes for a cookie to expire is not a workable way to verify renewal,
    and forcing expiry by tampering with the server's cookie would prove
    nothing about the real flow.
    """
    provider = ctx.make_provider()
    if not isinstance(provider, CdpCookieProvider):
        raise UsageError(
            "--renew needs a browser-backed credential; a manually supplied "
            "token has no GitCode SSO session to renew against"
        )

    base_url = ctx.args.base_url or "https://opencsitool.com"
    renewer = ctx.make_renewer(provider, base_url=base_url)
    if renewer is None:
        raise UsageError(
            "--renew needs a browser-backed credential; a manually supplied "
            "token has no GitCode SSO session to renew against"
        )
    session = SessionManager(provider, renewer=renewer)

    # Read the current credential first so the before/after comparison has a
    # real "before", and so the report can show what the expiry was.
    before = provider.status()
    result = session.renew(force=True)
    evidence = getattr(renewer, "last_evidence", None)

    # A renewal that reports success is only credible if the server agrees, so
    # verify with a real request rather than trusting the cookie's presence.
    verified = False
    verify_error: OpenCsiError | None = None
    if result.renewed:
        client = ctx.make_client(provider=ctx.make_provider(), renew=False)
        try:
            client.login_or_restore_session(refresh=True)
            verified = True
        except OpenCsiError as exc:
            verify_error = exc

    payload = {
        "ok": result.ok and (verified or not result.renewed),
        "renewal": result.as_dict(),
        "before": before.as_dict(),
        "verified": verified,
        "evidence": evidence.as_dict() if evidence else None,
    }
    if verify_error is not None:
        payload["verify_error"] = verify_error.as_dict()

    def render() -> None:
        ctx.out(section("Session renewal"))
        ctx.out(render_kv([("Outcome", result.status.value)]))
        if result.detail:
            ctx.out(render_kv([("Detail", result.detail)]))
        rows = [("Cookie lifetime before", format_relative_seconds(before.expires_in))]
        if result.expires_in is not None:
            rows.append(
                ("Cookie lifetime after", format_relative_seconds(result.expires_in))
            )
        ctx.out(render_kv(rows))
        if result.renewed:
            ctx.out(
                render_kv(
                    [
                        ("Token changed", "yes" if result.token_changed else "no"),
                        ("Server accepted it", "yes" if verified else "no"),
                    ]
                )
            )
        if not result.ok:
            ctx.blank()
            if result.requires_interaction:
                ctx.err(
                    "error: the GitCode SSO session is gone, so silent renewal "
                    "cannot help."
                )
                ctx.err(f"       -> sign in at {LOGIN_URL}")
            else:
                ctx.err(f"error: renewal did not succeed ({result.status.value}).")
                if result.detail:
                    ctx.err(f"       {result.detail}")
        if verify_error is not None:
            ctx.err(f"error: the new cookie was rejected: {verify_error}")

    ctx.emit(payload, render)
    if not result.ok:
        return _RENEWAL_EXIT.get(result.status, 1)
    if result.renewed and not verified:
        return EXIT_SESSION_EXPIRED
    return 0


# ── browser mode ──────────────────────────────────────────────────────────
def _open_a_readable_browser(ctx: CliContext) -> None:
    """Start a browser whose session this tool can read, and say what happened.

    Reports every outcome explicitly. The failure this replaces was silent: the
    command claimed to have opened a browser, the user signed in, and nothing
    worked -- with no message connecting the two. A launch that cannot produce a
    readable browser must say so *before* the user spends a minute signing in.

    A launch failure is not fatal to the command: the wait loop below still runs,
    so a user who already has a correctly-started browser open is unaffected.
    """
    from ..auth.browser_launch import BrowserLaunchStatus, launch_debug_browser

    result = launch_debug_browser(LOGIN_URL)

    if result.status is BrowserLaunchStatus.LAUNCHED:
        ctx.err(
            f"started {result.browser} with DevTools on port {result.port} "
            f"({LOGIN_URL})"
        )
        ctx.err("       sign in there, then leave the window open.")
    elif result.status is BrowserLaunchStatus.ALREADY_RUNNING:
        ctx.err(f"using the browser already listening on port {result.port}")
        ctx.err("       sign in there, then leave the window open.")
    elif result.status is BrowserLaunchStatus.NO_BROWSER_FOUND:
        ctx.err("could not start a browser automatically: none was found.")
        ctx.err(
            f"       open {LOGIN_URL} in Chrome or Edge started with "
            f"--remote-debugging-port={result.port}, then sign in."
        )
    else:
        ctx.err("could not start a browser the tool can read from.")
        if result.detail:
            ctx.err(f"       {result.detail}")
        ctx.err(f"       {LOGIN_URL}")


def _browser(ctx: CliContext) -> int:
    client = ctx.make_client(provider=ctx.make_provider(), renew=False)
    provider = client.credentials
    assert isinstance(provider, CdpCookieProvider)

    if not ctx.args.no_browser:
        # Start a browser the tool can actually read from, rather than handing
        # the URL to the OS default browser.
        #
        # This used to be ``webbrowser.open(LOGIN_URL)``, which starts the
        # user's *default* browser with **no** ``--remote-debugging-port``. The
        # cookie it then wrote was invisible to this tool, so the command
        # printed "sign in there, then leave the tab open" and then failed with
        # the very same "no DevTools endpoint" error it started with. Telling
        # someone to sign in is only honest if signing in can work.
        _open_a_readable_browser(ctx)

    deadline = time.time() + max(0.0, ctx.args.wait)
    attempts = 0
    last_error: str | None = None
    last_exc: OpenCsiError | None = None

    while True:
        attempts += 1
        try:
            provider.refresh()
            identity = client.login_or_restore_session(refresh=True)
        except OpenCsiError as exc:
            last_error = str(exc)
            last_exc = exc
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
    # Exit with the cause, not a fixed 12. Reporting "not logged in" when no
    # DevTools endpoint was ever reachable sends the user to sign in inside a
    # browser that is not running -- the same mistake `status` and `doctor`
    # originally made (objective §30).
    return exit_code_for(last_exc)


# ── manual mode ───────────────────────────────────────────────────────────
def _manual(ctx: CliContext) -> int:
    if not ctx.stdin_is_tty and ctx.json:
        raise UsageError("--manual --json cannot read a cookie from a pipe")

    try:
        value = getpass.getpass("openCsiTool 'token' cookie value (input hidden): ")
    except KeyboardInterrupt:
        # Interrupted, not mistyped. This used to return 2 -- the usage code --
        # which made Ctrl+C here exit differently from Ctrl+C anywhere else in
        # the CLI (app.py returns EXIT_INTERRUPTED), and told the user their
        # arguments were wrong when they had just pressed a key. Same class of
        # mistake as the fixed exit codes above: derive the code from what
        # actually happened.
        ctx.err("interrupted.")
        return EXIT_INTERRUPTED
    except EOFError:
        # Nothing supplied at all: a closed stdin is a call-shape problem, so
        # the usage code is the honest one here.
        ctx.err("error: no cookie value supplied.")
        return EXIT_USAGE

    value = value.strip()
    if not value:
        ctx.err("error: empty cookie value.")
        return EXIT_USAGE

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
