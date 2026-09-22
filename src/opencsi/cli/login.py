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
from dataclasses import dataclass
from typing import Any

from ..auth import (
    BrowserOAuthRenewer,
    CdpCookieProvider,
    ManualCookieProvider,
    RenewalResult,
    RenewalStatus,
    SessionManager,
)
from ..auth.gitcode_bridge import BridgeResult, BridgeStatus
from ..auth.session import LoginStage
from ..client import BASE_URL
from ..errors import (
    EXIT_INTERRUPTED,
    EXIT_NETWORK_ERROR,
    EXIT_OPENCSITOOL_PENDING,
    EXIT_QR_PROTOCOL,
    EXIT_SESSION_EXPIRED,
    EXIT_USAGE,
    OpenCsiError,
    UsageError,
    exit_code_for,
)
from ..formatting import format_relative_seconds, render_kv, section
from ..redaction import register_secret, scrub_text
from .context import ENV_BASE_URL, ENV_CDP_URL, CliContext, add_common_options

LOGIN_URL = "https://opencsitool.com/myTools"
POLL_INTERVAL = 2.0

#: Renewal outcomes that mean "a human is needed after all", and the exit code
#: to report for each. Derived from the *status*, never from parsing prose.
_RENEWAL_EXIT = {
    RenewalStatus.RENEWED: 0,
    RenewalStatus.ALREADY_VALID: 0,
    RenewalStatus.LOGIN_REQUIRED: EXIT_SESSION_EXPIRED,
    RenewalStatus.CONSENT_REQUIRED: EXIT_SESSION_EXPIRED,
    RenewalStatus.CDP_UNAVAILABLE: 10,
    RenewalStatus.OAUTH_FAILED: EXIT_SESSION_EXPIRED,
    RenewalStatus.TIMEOUT: EXIT_NETWORK_ERROR,
    RenewalStatus.UNSUPPORTED: EXIT_USAGE,
}


def _qr_exit_codes() -> dict:
    """QR outcomes mapped to exit codes.

    Built by a function rather than a module-level literal because
    ``gitcode_qr`` is imported lazily elsewhere in this module, and importing it
    at module scope would pull the QR machinery into every ``opencsi``
    invocation -- including the tray's, which must stay cheap at sign-in. The
    mapping is still a single named source of truth, so a test can assert it is
    exhaustive the same way ``_RENEWAL_EXIT`` is.

    ``SUCCEEDED`` is deliberately absent. A successful QR login does not get its
    exit code from this map at all: it gets 0 only if the *second* half -- the
    openCsiTool session -- also completed, and
    :data:`~opencsi.errors.EXIT_OPENCSITOOL_PENDING` if it did not. Mapping
    ``SUCCEEDED`` here would re-introduce the defect this table was split to fix,
    by giving "GitCode said yes" the same code as "the login is done".
    """
    from ..auth.gitcode_qr import QrLoginStatus

    return {
        QrLoginStatus.CANCELLED: EXIT_USAGE,
        QrLoginStatus.EXPIRED: EXIT_SESSION_EXPIRED,
        QrLoginStatus.TIMEOUT: EXIT_SESSION_EXPIRED,
        QrLoginStatus.NETWORK_ERROR: EXIT_NETWORK_ERROR,
        QrLoginStatus.PROTOCOL_ERROR: EXIT_QR_PROTOCOL,
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
    """Sign in by scanning a WeChat code, then complete openCsiTool automatically.

    This command has two halves and it used to report only the first one.

    .. code-block::

        POST /qrcode/wechat_mini_program      ─┐
        GET  /qrcode/wechat_mini_program       ├─ half one: plain HTTP, no browser
        POST /oauth/login/qrcode/...          ─┘   -> GitCode credential
                    │
                    ▼
        plant that credential in a dedicated profile,
        then run openCsiTool's OAuth leg in a hidden engine
                    │
                    ▼
        GET /opencsitool/rest/v1/user/getUserInfo -> 200   <- half two

    Half one always worked. Half two is the part that decides whether
    ``opencsi usage`` will work afterwards, and the old code did not attempt it
    at all: it printed "run 'opencsi login' in a browser" and exited **0**. That
    exit code was the defect. It told a script the login had succeeded when the
    session it needs did not exist, and it told a human nothing was left to do
    when something was.

    Now half two is attempted, and the exit code says which half actually
    finished:

    ======  ==========================================================
    Code    Meaning
    ======  ==========================================================
    0       ``getUserInfo`` answered -- the whole login is done
    34      GitCode authenticated, openCsiTool did not (partial success)
    13      the QR expired, timed out, or was cancelled
    30/33   a network or protocol failure
    ======  ==========================================================

    Only code 0 is allowed to mean "try ``opencsi usage`` now".

    Two limits are established by measurement rather than assumption, and both
    are stated in the output rather than hidden:

    * GitCode's ``qrcode`` field is a **WeChat mini-program code**, not a QR
      code. Its dots are finer than a terminal cell, so a terminal rendering
      cannot be scanned; the full-resolution image is written to a file and the
      user is pointed at it.
    * the openCsiTool leg needs a browser *engine* (GitCode's authorize page is
      a client-rendered SPA), but not a browser *window*. Half two therefore
      runs in the hidden host from :mod:`opencsi.auth.auth_host`.
    """
    from ..auth.gitcode_qr import GitCodeQrAuthenticator, QrLoginStatus, QrStatus
    from ..auth.qr_render import decode_payload_image, render_payload, write_image

    qr_exit = _qr_exit_codes()

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
            # Try to open the viewer, and say so only if it worked. The file path
            # is printed either way, because an auto-open that silently fails
            # would leave the user with a caption and no picture -- and the whole
            # reason the image is written separately is that the terminal drawing
            # is not reliably scannable.
            if _open_image(rendered["path"]):
                ctx.err("(opened it in your image viewer)")
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

    if not result.ok:
        payload = {
            "ok": False,
            "stage": LoginStage.NONE.value,
            "complete": False,
            "qr_login": result.as_dict(),
            "display": {
                "mode": rendered.get("mode"),
                "path": rendered.get("path"),
                "scannable": rendered.get("scannable", False),
                "note": rendered.get("detail"),
            },
        }

        def render_failure() -> None:
            ctx.err("")
            ctx.err(f"error: QR sign-in did not complete ({result.status.value}).")
            if result.detail:
                ctx.err(f"       {result.detail}")

        ctx.emit(payload, render_failure)
        return qr_exit.get(result.status, 1)

    # ── half two: turn the GitCode credential into an openCsiTool session ──
    completion = _complete_qr_login(ctx, result)
    payload = _qr_payload(ctx, result, rendered, cookies, completion)

    def render_success() -> None:
        ctx.err("")
        ctx.err("GitCode sign-in complete.")
        if result.username:
            ctx.out(f"GitCode user: {result.username}")
        if result.is_new_user is not None:
            ctx.out(f"New GitCode account: {'yes' if result.is_new_user else 'no'}")
        if cookies:
            ctx.out("GitCode session cookies received: " + ", ".join(sorted(cookies)))
        _render_completion(ctx, completion)

    ctx.emit(payload, render_success)

    if completion.identity is not None:
        return 0
    return EXIT_OPENCSITOOL_PENDING


@dataclass
class _Completion:
    """How far the second half of a QR login got.

    A plain data holder rather than a tuple so every field is named at the point
    it is read: this is the structure the command's success semantics hang on,
    and ``(a, b, c, d)`` would make it easy to read the wrong one.
    """

    #: The verified openCsiTool identity, when ``getUserInfo`` answered.
    identity: Any = None
    #: Why the second half stopped, in the user's words.
    reason: str | None = None
    #: An actionable next step, when there is one.
    next_step: str | None = None
    #: The renewal outcome, for ``--json``.
    renewal: RenewalResult | None = None
    #: Which renewal mechanism produced the outcome.
    mechanism: str | None = None
    #: The OAuth step trace, secret-free, for ``--json``.
    trace: Any = None
    #: The GitCode cookie names the QR credential supplied (never their values).
    credential_cookies: tuple[str, ...] = ()
    #: Whether the credential was planted into a browser profile.
    #:
    #: Retained because the *browser* route still exists as a fallback, and a
    #: report that could not say which route ran would be unable to show that the
    #: browserless one is the one in use.
    bridged: bool = False
    #: The bridge outcome, when the browser route was taken.
    bridge: BridgeResult | None = None


def _complete_qr_login(ctx: CliContext, result) -> "_Completion":
    """Attempt the openCsiTool half of a QR login. Never raises.

    The QR flow authenticates with GitCode over plain HTTP, and the openCsiTool
    OAuth leg is *also* plain HTTP -- three ordinary requests, measured in
    ``tools/probe_oauth_browserless.py``. So the two halves join directly, and no
    browser is involved at any point.

    That is a change of approach, and worth stating plainly because the earlier
    design did the opposite. It planted the QR credential into a browser profile
    and then drove a hidden engine through GitCode's SPA, which worked but needed
    a browser install, a profile, a planted cookie and a cleanup step. All of that
    was a consequence of the belief that the OAuth leg required an engine. It does
    not, so none of it is needed.

    The browser route is kept as a fallback for the case HTTP cannot cover: a
    GitCode account whose application grant has never been approved. Then a human
    has to confirm the approval page once, and
    :attr:`RenewalStatus.CONSENT_REQUIRED` says so.

    The one thing that may set ``identity`` is ``getUserInfo`` agreeing. A flow
    that reports success without it is not treated as success anywhere here.
    """
    completion = _Completion()

    credentials = dict(result.credentials())
    if not credentials:
        completion.reason = (
            "GitCode signed in but returned no credential this tool can use to "
            "establish the openCsiTool session."
        )
        completion.next_step = (
            "run 'opencsi login' to sign in through a browser instead"
        )
        return completion

    # ── the browserless route ─────────────────────────────────────────────
    from ..auth.http_oauth import GitCodeCookieSource, HttpOAuthRenewer
    from ..auth.session import RenewalStatus

    source = GitCodeCookieSource(
        access_token=credentials.get("access_token"),
        refresh_token=credentials.get("refresh_token"),
        username=result.username,
    )
    completion.credential_cookies = source.cookie_names

    if not source.cookie_names:
        completion.reason = (
            "the QR login returned a credential, but not one of the cookies the "
            "GitCode session is made of, so it cannot be used."
        )
        completion.next_step = "run 'opencsi login --qr' again to get a fresh code"
        return completion

    base_url = (
        getattr(ctx.args, "base_url", None) or os.environ.get(ENV_BASE_URL) or BASE_URL
    )
    renewer = HttpOAuthRenewer(
        source,
        base_url=base_url,
        timeout=float(getattr(ctx.args, "renew_timeout", 45.0) or 45.0),
        use_proxy=not bool(getattr(ctx.args, "no_proxy", False)),
    )

    renewal = renewer.renew()
    completion.renewal = renewal
    completion.mechanism = renewer.name
    completion.trace = renewer.last_trace

    if renewal.status is RenewalStatus.RENEWED or renewal.status is RenewalStatus.ALREADY_VALID:
        token = source.get_token()
        if token:
            completion.identity = _verify_session(ctx, base_url, token)
        if completion.identity is None:
            completion.reason = (
                "the openCsiTool session cookie was issued, but the server did "
                "not accept it, so the login is not complete."
            )
            completion.next_step = "run 'opencsi login' to sign in through a browser"
        return completion

    if renewal.status is RenewalStatus.CONSENT_REQUIRED:
        # HTTP reached the authorization step and found no existing grant. The
        # approval page has to be confirmed by a human. This is the one case the
        # browser route exists for, so rather than reporting a dead end, the
        # engine is brought up and the user is asked to approve it -- and if that
        # cannot be done either, the reason says which of the two failed.
        ctx.err("")
        ctx.err(
            "GitCode has no existing authorisation for this application, so the "
            "approval page has to be confirmed once."
        )
        _run_oauth_completion(ctx, completion)
        if completion.identity is None and completion.reason is None:
            completion.reason = renewal.detail or (
                "the authorisation still needs to be approved"
            )
        if completion.identity is None and completion.next_step is None:
            completion.next_step = f"open {LOGIN_URL} and approve it once"
        return completion

    if renewal.status is RenewalStatus.LOGIN_REQUIRED:
        completion.reason = (
            "the GitCode credential from the QR login was not accepted, so the "
            "openCsiTool session cannot be established from it."
        )
        completion.next_step = "run 'opencsi login --qr' again to get a fresh code"
        return completion

    completion.reason = renewal.detail or "the openCsiTool OAuth step did not complete"
    completion.next_step = "run 'opencsi login' to sign in through a browser"
    return completion


def _verify_session(ctx: CliContext, base_url: str, token: str):
    """Confirm a freshly-issued session with ``getUserInfo``.

    The only accepted proof. A cookie that exists and a session the server
    honours are different facts, and the whole P0 defect was a report that
    conflated them.

    A failure to verify returns ``None`` rather than raising: an unverifiable
    session is not a verified one, and the caller's job is to say so rather than
    to propagate a transport error the user cannot act on.
    """
    from ..client import OpenCsiToolClient
    from ..transport import HttpTransport

    class _FixedToken:
        """Serves exactly one token and never re-reads a browser.

        The session was just minted by this process, so there is no browser to go
        back to and no reason to look for one.
        """

        name = "qr-issued"

        def __init__(self, value: str) -> None:
            self._value = value

        def get_token(self) -> str:
            return self._value

    try:
        transport = HttpTransport(
            base_url=base_url,
            timeout=float(getattr(ctx.args, "timeout", 15.0) or 15.0),
            use_proxy=not bool(getattr(ctx.args, "no_proxy", False)),
        )
        client = OpenCsiToolClient(_FixedToken(token), transport=transport)
        return client.login_or_restore_session(refresh=True)
    except Exception:  # noqa: BLE001 - an unverifiable session is not a verified one
        return None


def _gitcode_verification_enabled() -> bool:
    """Whether the bridge may ask GitCode to confirm the session.

    On by default, because an unverified bridge is exactly the kind of
    unconfirmed claim this objective exists to remove. It can be switched off for
    an offline environment, and doing so produces ``UNVERIFIED`` -- never
    ``BRIDGED``.
    """
    return os.environ.get("OPENCSI_SKIP_GITCODE_VERIFY", "").strip() not in ("1", "true", "yes")


def _run_oauth_completion(ctx: CliContext, completion: "_Completion") -> None:
    """Complete the OAuth leg through a browser, for the one case HTTP cannot.

    Reached only when the browserless flow reported ``CONSENT_REQUIRED``: the
    credential was accepted and the OAuth leg ran, but the account has no existing
    application grant, so the approval page has to be confirmed by a human. That
    is a decision this tool must not make on the user's behalf, so the browser is
    brought up and the user answers it.

    The engine is started hidden when the browser build supports it, because a
    window that appears unbidden during a CLI command is a surprise. It reports
    ``visible=True`` when headless is unavailable rather than claiming a window
    was never shown -- the ``--headless=new`` flag is silently ignored by some
    builds, and a caller that asked for hidden must not be told it got hidden.

    Everything here is reported, never raised: this runs inside a command whose
    whole contract is that it says what happened.
    """
    from ..auth.auth_host import AuthBrowserHost

    # Prefer the endpoint the user configured; otherwise bring up the hidden one.
    endpoint_url = ctx.args.cdp or os.environ.get(ENV_CDP_URL) or None
    host: AuthBrowserHost | None = None

    if endpoint_url is None:
        host = AuthBrowserHost()
        host_result = host.ensure_running()
        if not host_result.ok:
            completion.reason = (
                host_result.detail
                or "the browser engine needed to confirm the approval could not be started"
            )
            completion.next_step = (
                "open " + LOGIN_URL + " in a browser and approve the authorisation once"
            )
            return
        if host_result.visible:
            # Say it plainly. A caller that asked for a hidden engine and got a
            # window must not be told the browser is invisible.
            ctx.err(
                "note: this browser build has no headless mode, so the approval "
                "window was opened visibly."
            )
        endpoint_url = f"http://127.0.0.1:{host_result.port}"
        completion.bridged = True

    try:
        provider = CdpCookieProvider(cdp_url=endpoint_url)
        renewer = BrowserOAuthRenewer(provider, base_url=ctx.args.base_url or BASE_URL)
        session = SessionManager(provider, renewer=renewer)
        renewal = session.renew(force=True)
    except OpenCsiError as exc:
        completion.reason = scrub_text(str(exc))[:300]
        completion.next_step = exc.hint
        return
    except Exception as exc:  # noqa: BLE001 - the flow must report, not crash
        completion.reason = f"the OAuth round-trip failed ({type(exc).__name__})"
        return

    completion.renewal = renewal
    completion.mechanism = "browser-oauth"

    if not renewal.ok:
        completion.reason = _renewal_reason(renewal)
        completion.next_step = _renewal_next_step(renewal)
        return

    # The renewal says a cookie landed. That is not the same as the *session*
    # working, so it is confirmed with a real request -- the same standard the
    # rest of the project holds itself to.
    try:
        client = ctx.make_client(
            provider=CdpCookieProvider(cdp_url=endpoint_url), renew=False
        )
        completion.identity = client.login_or_restore_session(refresh=True)
    except OpenCsiError as exc:
        completion.reason = (
            "a new cookie was issued but openCsiTool did not accept it: "
            + scrub_text(str(exc))[:200]
        )
        completion.next_step = exc.hint or "run 'opencsi doctor' for the connection details"
        return

    if completion.identity is None:
        completion.reason = "a new cookie was issued but the session could not be confirmed"


def _renewal_reason(renewal: RenewalResult) -> str:
    """The renewal outcome, in the user's words. Never a token."""
    if renewal.status is RenewalStatus.CONSENT_REQUIRED:
        return (
            "GitCode is waiting for the OpenCsitool authorisation to be confirmed. "
            "This is not a sign-in problem: the GitCode session is alive."
        )
    if renewal.status is RenewalStatus.LOGIN_REQUIRED:
        return "the GitCode session did not carry over to the browser profile"
    if renewal.status is RenewalStatus.CDP_UNAVAILABLE:
        return "no browser engine was reachable to run the OAuth leg"
    return f"the OAuth round-trip did not complete ({renewal.status.value})"


def _renewal_next_step(renewal: RenewalResult) -> str | None:
    if renewal.status is RenewalStatus.CONSENT_REQUIRED:
        return f"open {LOGIN_URL} and approve it once; no sign-in is needed"
    if renewal.status is RenewalStatus.LOGIN_REQUIRED:
        return "run 'opencsi login' and sign in at " + LOGIN_URL
    return None


def _qr_payload(
    ctx: CliContext,
    result,
    rendered: dict,
    cookies: dict,
    completion: "_Completion",
) -> dict:
    """The ``--json`` document for a QR login, at whichever stage it reached."""
    established = completion.identity is not None
    # ``bridged`` used to be the signal for "the second half got somewhere". It
    # no longer is: the browserless route never bridges anything, so keying the
    # stage on it would report GITCODE_AUTHENTICATED for a login that had in fact
    # completed the OAuth round trip. The renewal outcome is the honest signal.
    reached_oauth = completion.renewal is not None
    stage = (
        LoginStage.OPENCSITOOL_AUTHENTICATED
        if established
        else LoginStage.OPENCSITOOL_PENDING
        if (reached_oauth or completion.bridged)
        else LoginStage.GITCODE_AUTHENTICATED
    )
    payload: dict[str, object] = {
        "ok": established,
        "complete": established,
        "stage": stage.value,
        "qr_login": result.as_dict(),
        "display": {
            "mode": rendered.get("mode"),
            "path": rendered.get("path"),
            "scannable": rendered.get("scannable", False),
            "note": rendered.get("detail"),
        },
        "gitcode_session_cookies": sorted(cookies),
        # Which route ran. The browserless one is the default now, and a report
        # that could not say so would be unable to show that no browser was used.
        "mechanism": completion.mechanism,
        "browser_used": completion.bridged,
        "bridge": completion.bridge.as_dict() if completion.bridge else None,
        "renewal": completion.renewal.as_dict() if completion.renewal else None,
        "oauth_trace": completion.trace.as_dict() if completion.trace else None,
        "openscitool_session": {
            "established": established,
            "user_name": getattr(completion.identity, "user_name", None),
            "employee_id": getattr(completion.identity, "employee_id", None),
            "reason": completion.reason,
            "next_step": completion.next_step,
        },
    }
    if established:
        payload["exit_code"] = 0
    else:
        payload["exit_code"] = EXIT_OPENCSITOOL_PENDING
    return payload


def _render_completion(ctx: CliContext, completion: "_Completion") -> None:
    """Say what the second half did, and what is left, without overclaiming."""
    if completion.identity is not None:
        ctx.blank()
        ctx.out("openCsiTool session established and verified.")
        ctx.out(
            render_kv(
                [
                    ("User", completion.identity.display_name),
                    ("Employee", completion.identity.employee_id),
                ]
            )
        )
        # Which route established it. Stated because "no browser was needed" is
        # the finding this whole path was rebuilt around, and a user who was told
        # for months that a browser engine was required deserves to see it.
        if completion.mechanism:
            ctx.out(render_kv([("Renewed via", completion.mechanism)]))
            if not completion.bridged:
                ctx.out(render_kv([("Browser", "not used")]))
        ctx.blank()
        ctx.out("Try: opencsi usage")
        return

    ctx.err("")
    ctx.err(
        "warning: GitCode sign-in succeeded, but the openCsiTool session was not "
        "established."
    )
    if completion.reason:
        ctx.err(f"       {completion.reason}")
    if completion.next_step:
        ctx.err(f"       -> {completion.next_step}")
    ctx.err(
        "       'opencsi usage' will not work until that session exists; this "
        f"command exits {EXIT_OPENCSITOOL_PENDING} rather than 0 to say so."
    )


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
    # Which mechanism actually ran. Reported because "renewal succeeded" is not
    # the whole answer once there is more than one way to do it: whether the
    # browser was needed is exactly the question this project spent a long time
    # getting wrong, and a report that omits it cannot settle it.
    mechanism = getattr(renewer, "last_mechanism", None) or getattr(renewer, "name", None)

    # A renewal that reports success is only credible if the server agrees, so
    # verify with a real request rather than trusting the cookie's presence.
    #
    # The *same* provider is reused, and that is load-bearing. The browserless
    # renewer obtains the session over HTTP, so the browser it read the GitCode
    # credential from has never heard of the new cookie -- it handed the token to
    # the provider through ``remember_token``. Building a fresh provider here
    # would re-read the browser, find no ``token``, and report a renewal that
    # demonstrably worked as "the new cookie was rejected". That is exactly what
    # it did before this line was changed, and it made the browserless path look
    # broken in the frozen build while the source run passed, because the source
    # run happened to reuse the provider.
    # A renewal that reports success is only credible if the server agrees, so
    # verify with a real request rather than trusting the cookie's presence.
    #
    # The *same* provider is reused, and that is load-bearing. The browserless
    # renewer obtains the session over HTTP, so the browser it read the GitCode
    # credential from has never heard of the new cookie -- it handed the token to
    # the provider through ``remember_token``. A fresh provider here would
    # re-read the browser, find no ``token``, and report a renewal that
    # demonstrably worked as "the new cookie was rejected". That is exactly what
    # happened before this line was changed. The source run passed only because
    # it happened to reuse the provider; the frozen build exposed it, which is
    # the argument for running the artifacts and not just the tree.
    verified = False
    verify_error: OpenCsiError | None = None
    if result.renewed:
        client = ctx.make_client(provider=provider, renew=False)
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
        "mechanism": mechanism,
        "evidence": evidence.as_dict() if evidence else None,
    }
    if verify_error is not None:
        payload["verify_error"] = verify_error.as_dict()

    def render() -> None:
        ctx.out(section("Session renewal"))
        ctx.out(render_kv([("Outcome", result.status.value)]))
        if mechanism:
            ctx.out(render_kv([("Mechanism", mechanism)]))
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
            if result.status is RenewalStatus.CONSENT_REQUIRED:
                # NOT the "SSO session is gone" message below. The user is still
                # signed in -- GitCode has rendered an approval page naming them
                # -- so telling them to sign in sends them to do work that cannot
                # fix anything. `requires_interaction` is true for both states,
                # which is exactly why it must not be used as a synonym for
                # "signed out".
                ctx.err(
                    "error: GitCode is waiting for the OpenCsitool approval to be "
                    "confirmed."
                )
                ctx.err(
                    "       -> open https://opencsitool.com/myTools and approve it; "
                    "no sign-in is needed."
                )
            elif result.requires_interaction:
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
def _open_image(path: str) -> bool:
    """Open an image in the OS viewer. Returns whether it was launched.

    Best-effort and quiet on failure: the caller prints the path regardless, so a
    machine with no viewer loses nothing. What must not happen is a claim that the
    image was opened when it was not -- hence the return value rather than a bare
    attempt.

    The default *browser* is deliberately not used, even though it would display a
    PNG. It is the wrong tool for a code the user is about to photograph with a
    phone, and on a machine where the browser is the broken part -- which is a
    state this login path is specifically offered in -- it would be a route that
    does not work.
    """
    import subprocess
    import sys

    try:
        if sys.platform == "win32":
            # ``os.startfile`` is the shell's own "open with the default app",
            # which is what a double-click does.
            os.startfile(path)  # noqa: S606 - the shell association is the point
            return True
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [opener, path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        return True
    except Exception:  # noqa: BLE001 - a missing viewer is not a login failure
        return False


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

    result = launch_debug_browser(
        LOGIN_URL, no_proxy=bool(getattr(ctx.args, "no_proxy", False))
    )

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
        # A system proxy that cannot reach opencsitool.com produces a page that
        # simply never loads -- indistinguishable from the site being down. The
        # flag that fixes it is not guessable, so it is named here.
        if not getattr(ctx.args, "no_proxy", False):
            ctx.err(
                "       if the page will not load, a system proxy may be blocking "
                "opencsitool.com; retry with --no-proxy."
            )


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
