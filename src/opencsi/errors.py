"""Exception hierarchy and CLI exit codes.

Every failure mode the tool can produce maps to exactly one exception class and
one documented process exit code. Nothing falls through to a bare ``exit 1``.

Exit code table
---------------
==== ==========================================
Code Meaning
==== ==========================================
0    success
2    invalid arguments / client misconfiguration
10   CDP endpoint unavailable
11   no usable browser target
12   not logged in (no openCsiTool token cookie)
13   session expired (cookie present but rejected)
20   permission denied
30   network error
31   server error (HTTP 5xx)
32   business API error (HTTP 200, code != 200)
33   GitCode QR protocol error (unexpected response shape)
34   GitCode authenticated but the openCsiTool session is not established
==== ==========================================

Codes 0/2/10/11/12/13/20/30/31 come from the project specification.
Code 32 is an addition: an HTTP 200 carrying a business-level failure
(``{"code": 500, "message": ...}``) is neither a network nor a transport
server error, and conflating it with either would mislead scripts.

Code 34 is the same kind of addition, and the reason this module now has a
*partial success* code at all. ``opencsi login --qr`` used to exit 0 the moment
GitCode accepted the scan, which claimed an openCsiTool session that did not
exist: the very next ``opencsi usage`` would still fail. Exiting 0 was wrong in
both directions -- it told a script the login had worked, and it gave a human
nothing to act on. GitCode success and openCsiTool success are two different
facts, so they now get two different exit codes.
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CDP_UNAVAILABLE = 10
EXIT_NO_BROWSER_TARGET = 11
EXIT_NOT_LOGGED_IN = 12
EXIT_SESSION_EXPIRED = 13
EXIT_PERMISSION_DENIED = 20
EXIT_NETWORK_ERROR = 30
EXIT_SERVER_ERROR = 31
EXIT_BUSINESS_ERROR = 32
#: The GitCode QR endpoints answered in a shape this client does not recognise.
#: Distinct from ``EXIT_SERVER_ERROR`` on purpose: a 5xx from openCsiTool and a
#: malformed body from GitCode are different failures with different fixes, and
#: code 31 previously meant both. That is the same conflation that motivated
#: ``EXIT_BUSINESS_ERROR`` above.
EXIT_QR_PROTOCOL = 33
#: GitCode authentication succeeded, but the openCsiTool session it is supposed
#: to lead to was not established. This is a *partial* success, and it needs its
#: own code precisely so it cannot be mistaken for either outcome: 0 would claim
#: ``opencsi usage`` now works (it does not), and any of the failure codes would
#: throw away the real progress the user made by scanning.
EXIT_OPENCSITOOL_PENDING = 34
#: The session was established and verified, but could not be stored durably, so
#: the *next* process will not have it. This is the failure mode that made the
#: original browserless login useless, and it must never be reported as a plain
#: success: exit 0 tells a script "you are signed in from now on", and here that
#: is false the moment this process ends.
#:
#: Distinct from ``EXIT_NOT_LOGGED_IN`` because the user is, right now, signed in
#: -- the fault is in persistence, not in authentication, and the two need
#: different fixes.
EXIT_NOT_PERSISTED = 35
#: Ctrl-C. Documented in the README alongside the codes above, and now named so
#: every site that reports an interrupt says the same thing rather than
#: repeating a literal and drifting apart.
EXIT_INTERRUPTED = 130


class OpenCsiError(Exception):
    """Base class for every error raised by this package.

    Attributes
    ----------
    code:
        Stable machine-readable identifier, suitable for JSON output and for
        scripts that should not parse prose.
    exit_code:
        Process exit status the CLI uses for this failure.
    hint:
        Optional actionable next step for the user. Shown by the CLI and by
        ``opencsi doctor``. Must never contain credentials.
    http_status:
        The HTTP status that produced this error, when there was one. ``None``
        for purely local failures (no credential, bad arguments).
    """

    code = "ERROR"
    exit_code = 1
    hint: str | None = None

    #: Set by the client/transport when the failure came from an HTTP response.
    http_status: int | None = None

    def __init__(
        self,
        message: str = "",
        *,
        hint: str | None = None,
        http_status: int | None = None,
    ) -> None:
        # Scrub defensively: an exception message is one of the easiest places
        # for a secret to escape into a log file or a bug report.
        from .redaction import scrub_text

        super().__init__(scrub_text(message))
        if hint is not None:
            self.hint = hint
        if http_status is not None:
            self.http_status = http_status

    def as_dict(self) -> dict[str, object]:
        """Serialisable form for ``--json`` output. Never includes secrets."""
        out: dict[str, object] = {"error": self.code, "message": str(self)}
        if self.hint:
            out["hint"] = self.hint
        if self.http_status is not None:
            out["http_status"] = self.http_status
        return out


# ── usage / configuration ─────────────────────────────────────────────────
class UsageError(OpenCsiError):
    """Invalid arguments or contradictory options."""

    code = "INVALID_ARGUMENTS"
    exit_code = EXIT_USAGE


class ConfigError(OpenCsiError):
    """Bad configuration (for example a malformed OPENCSI_CDP_URL)."""

    code = "INVALID_CONFIGURATION"
    exit_code = EXIT_USAGE


class BadAuthHeaderError(OpenCsiError):
    """The server rejected an ``Authorization`` header.

    openCsiTool authenticates exclusively by cookie. Sending a bearer token
    yields ``401 Invalid Authorization``. Seeing this error means something in
    the request path added an Authorization header; it must be removed.
    """

    code = "BAD_AUTH_HEADER"
    exit_code = EXIT_USAGE


# ── authentication / browser ──────────────────────────────────────────────
class CdpUnavailableError(OpenCsiError):
    """No reachable Chrome/Edge DevTools endpoint was found."""

    code = "CDP_UNAVAILABLE"
    exit_code = EXIT_CDP_UNAVAILABLE
    hint = (
        "No Chrome/Edge remote debugging port was found. Start a browser with "
        "remote debugging enabled (see README 'Browser preparation'), then retry."
    )


class NoBrowserTargetError(OpenCsiError):
    """The DevTools endpoint is up but exposes no usable page target."""

    code = "NO_BROWSER_TARGET"
    exit_code = EXIT_NO_BROWSER_TARGET
    hint = "Open a tab in that browser (for example https://opencsitool.com/myTools) and retry."


class CookieNotFoundError(OpenCsiError):
    """CDP is reachable but holds no openCsiTool token cookie."""

    code = "OPENCSITOOL_NOT_LOGGED_IN"
    exit_code = EXIT_NOT_LOGGED_IN
    hint = (
        "Open https://opencsitool.com/myTools in that browser and complete the "
        "GitCode login, then retry."
    )


class SessionExpiredError(OpenCsiError):
    """A cookie was supplied but the server rejected it (HTTP 401)."""

    code = "SESSION_EXPIRED"
    exit_code = EXIT_SESSION_EXPIRED
    hint = (
        "The openCsiTool session expired (cookie lifetime is about 1 hour). "
        "Open https://opencsitool.com/myTools in the browser and sign in again."
    )


# ── transport / API ───────────────────────────────────────────────────────
class PermissionDeniedError(OpenCsiError):
    """HTTP 403: the signed-in user is not allowed to perform this action."""

    code = "PERMISSION_DENIED"
    exit_code = EXIT_PERMISSION_DENIED


class NetworkError(OpenCsiError):
    """The request could not complete (DNS, TLS, timeout, connection reset)."""

    code = "NETWORK_ERROR"
    exit_code = EXIT_NETWORK_ERROR


class MissingParamError(OpenCsiError):
    """HTTP 400: the server reported a missing or invalid parameter."""

    code = "MISSING_PARAMETER"
    exit_code = EXIT_USAGE


class ServerError(OpenCsiError):
    """HTTP 5xx, or 502/503/504 after exhausting retries."""

    code = "SERVER_ERROR"
    exit_code = EXIT_SERVER_ERROR


class BusinessApiError(OpenCsiError):
    """HTTP 200 whose body reports a business failure (``code != 200``)."""

    code = "BUSINESS_API_ERROR"
    exit_code = EXIT_BUSINESS_ERROR


class ContractDriftError(OpenCsiError):
    """A response no longer matches the verified contract."""

    code = "CONTRACT_DRIFT"
    exit_code = EXIT_SERVER_ERROR


def exit_code_for(exc: BaseException) -> int:
    """Map any exception to a documented process exit code."""
    if isinstance(exc, OpenCsiError):
        return exc.exit_code
    if isinstance(exc, KeyboardInterrupt):
        return EXIT_INTERRUPTED
    return 1
