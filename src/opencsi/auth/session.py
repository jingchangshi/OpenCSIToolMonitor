"""Session lifecycle: credential reload, session renewal, interactive login.

Why this module exists
----------------------
The original design had one verb -- ``CredentialProvider.refresh()`` -- and used
it for two different jobs:

* **credential reload** -- re-read the *same* source, hoping it now holds a
  newer value (the browser may have been refreshed by the site itself);
* **session renewal** -- cause a *new* session to be issued, by re-running the
  OAuth dance.

Those are not the same operation, and conflating them produced the one-hour
login bug: when the openCsiTool ``token`` cookie expires, the browser's copy
expires too, so re-reading the browser returns the same dead value. ``refresh()``
therefore cannot extend a session, yet the 401 path treated it as if it could.

The three semantics are now separate protocols:

.. code-block::

                        SessionManager
                             │
              ┌──────────────┼───────────────┐
              │              │               │
              ▼              ▼               ▼
     CredentialProvider  SessionRenewer  InteractiveAuthenticator
              │              │               │
              ▼              ▼               ▼
         read cookie     renew session    user login

:class:`SessionManager` owns the *policy* (when to do which), and delegates the
*mechanism* to the three collaborators. The client talks only to the manager.

Nothing in this module logs, prints or serialises a credential. Every result
type is deliberately secret-free: :class:`RenewalResult` and :class:`LoginResult`
carry a status enum and a redacted detail string, never a token.
"""

from __future__ import annotations

import inspect
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol, runtime_checkable

from ..errors import OpenCsiError
from .base import CredentialProvider, CredentialStatus


def _accepts_keyword(func: Callable[..., Any], name: str) -> bool:
    """Whether ``func`` can be called with keyword ``name``.

    Used to support both the documented renewer signature and a narrower
    third-party one without catching a ``TypeError`` raised from *inside* the
    call, which would silently swallow a real bug.
    """
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return False
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if parameter.name == name and parameter.kind in (
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            return True
    return False

log = logging.getLogger("opencsi.session")

#: Renew when the credential has less than this much life left. Five minutes is
#: long enough that a slow OAuth round-trip finishes before the cookie dies, and
#: short enough that a renewal happens at most once per session lifetime -- so
#: the tool does not re-run OAuth on every single query.
DEFAULT_RENEW_MARGIN = 300.0

#: How long a successful renewal suppresses further renewal attempts. Guards
#: against a server that hands back a cookie which *looks* about to expire
#: (or a clock skew) turning every request into an OAuth round-trip.
RENEW_COOLDOWN = 60.0

#: Bounded retry budget for a single renewal attempt, so a broken OAuth flow
#: cannot spin. The manager never loops beyond this.
MAX_RENEW_ATTEMPTS = 2


class RenewalStatus(str, Enum):
    """Outcome of a renewal attempt. Compared by identity, never by string."""

    RENEWED = "RENEWED"
    """A new, later-expiring credential was obtained and installed."""

    ALREADY_VALID = "ALREADY_VALID"
    """The current credential is still valid; nothing needed doing."""

    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    """Renewal needs the user: the upstream SSO session is gone."""

    CONSENT_REQUIRED = "CONSENT_REQUIRED"
    """Renewal needs the user: GitCode is showing an approval form.

    Distinct from :attr:`LOGIN_REQUIRED` because the user action is different
    and much smaller. The SSO session is alive -- GitCode knows who they are and
    has rendered "授权 OpenCsitool S <user>" -- so nothing needs signing in; one
    click on the consent page completes the grant. Reporting that as a login
    problem sends the user to re-authenticate when they are already
    authenticated, and reporting it as a timeout (which is what the renewer used
    to do) tells them the browser is slow when it is in fact idle and waiting
    for them.
    """

    CDP_UNAVAILABLE = "CDP_UNAVAILABLE"
    """The browser needed for silent renewal is not reachable."""

    OAUTH_FAILED = "OAUTH_FAILED"
    """The OAuth round-trip ran but did not yield a usable credential."""

    TIMEOUT = "TIMEOUT"
    """The OAuth round-trip did not finish inside its budget."""

    UNSUPPORTED = "UNSUPPORTED"
    """No renewer is configured (for example a manual credential)."""


class LoginStatus(str, Enum):
    """Outcome of an interactive login attempt."""

    SUCCEEDED = "SUCCEEDED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    UNSUPPORTED = "UNSUPPORTED"


class LoginStage(str, Enum):
    """How far a login got, when "did it work?" is not a yes/no question.

    A QR login has two independent halves and they can disagree:

    .. code-block::

        GitCode accepts the scan   ──►  GITCODE_AUTHENTICATED
                  │
                  ▼
        openCsiTool mints its own ``token`` cookie  ──►  OPENCSITOOL_AUTHENTICATED

    The first half is under this tool's control and works over plain HTTP. The
    second half runs through openCsiTool's own OAuth callback, which needs a
    GitCode *browser* session, so it can legitimately fail while the first half
    succeeded.

    ``GITCODE_AUTHENTICATED`` therefore means "real, verified progress, and
    something still has to happen". Collapsing it into either ``True`` or
    ``False`` is what produced the defect this enum exists to fix: the CLI
    reported exit 0 (a lie to scripts) while printing "run opencsi login in a
    browser" (a contradiction to humans).
    """

    #: Nothing usable was obtained.
    NONE = "NONE"
    #: GitCode knows who the user is; openCsiTool does not.
    GITCODE_AUTHENTICATED = "GITCODE_AUTHENTICATED"
    #: The openCsiTool session exists and ``getUserInfo`` succeeded.
    OPENCSITOOL_AUTHENTICATED = "OPENCSITOOL_AUTHENTICATED"
    #: openCsiTool authentication was attempted and did not complete. Distinct
    #: from :attr:`GITCODE_AUTHENTICATED` because it records that the second leg
    #: was *tried* and why it stopped -- a consent page is not a missing session.
    OPENCSITOOL_PENDING = "OPENCSITOOL_PENDING"

    @property
    def is_complete(self) -> bool:
        """Whether the whole login finished, which is the only exit-0 case."""
        return self is LoginStage.OPENCSITOOL_AUTHENTICATED


@dataclass(frozen=True)
class RenewalResult:
    """Structured renewal outcome.

    Deliberately secret-free: there is no field that could hold a token. Callers
    branch on ``status``; ``detail`` is a redacted one-liner for humans only and
    must never be parsed for control flow.
    """

    status: RenewalStatus
    renewed: bool = False
    requires_interaction: bool = False
    detail: str | None = None
    #: True when the credential value actually changed, not merely re-read.
    token_changed: bool = False
    #: Seconds of life the new credential has, when known.
    expires_in: float | None = None

    @property
    def ok(self) -> bool:
        return self.status in (RenewalStatus.RENEWED, RenewalStatus.ALREADY_VALID)

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "status": self.status.value,
            "renewed": self.renewed,
            "requires_interaction": self.requires_interaction,
        }
        if self.token_changed:
            out["token_changed"] = True
        if self.expires_in is not None:
            out["expires_in_seconds"] = round(self.expires_in, 1)
        if self.detail:
            out["detail"] = self.detail
        return out


@dataclass(frozen=True)
class LoginResult:
    """Structured interactive-login outcome (secret-free)."""

    status: LoginStatus
    detail: str | None = None
    method: str | None = None
    requires_interaction: bool = True
    #: How far the login actually got. Defaults to the coarse reading of
    #: ``status`` so an authenticator written before this field existed keeps
    #: behaving exactly as it did.
    stage: LoginStage = LoginStage.NONE

    @property
    def ok(self) -> bool:
        return self.status is LoginStatus.SUCCEEDED

    @property
    def complete(self) -> bool:
        """Whether the *whole* login finished.

        Deliberately stricter than :attr:`ok`: an authenticator can report
        ``SUCCEEDED`` for its own half of the flow while the session the caller
        needs is still missing.
        """
        return self.stage.is_complete

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "status": self.status.value,
            "ok": self.ok,
            "stage": self.stage.value,
            "complete": self.complete,
        }
        if self.method:
            out["method"] = self.method
        if self.detail:
            out["detail"] = self.detail
        return out


@runtime_checkable
class SessionRenewer(Protocol):
    """Cause a *new* session to be issued without user interaction.

    This is the operation the old ``CredentialProvider.refresh()`` was mistaken
    for. An implementation must prove that the credential actually changed --
    re-reading the same expired value is not renewal.

    ``before`` is how the renewer learns the value to compare against. Without
    it a renewer can only observe "a cookie exists", which is exactly the
    evidence that is *not* sufficient: the old, expired cookie also exists.
    """

    name: str

    def renew(
        self,
        *,
        timeout: float | None = None,
        before: CredentialProvider | None = None,
    ) -> RenewalResult:
        """Attempt renewal and return a structured, secret-free result."""

    def can_renew(self) -> bool:
        """Cheap pre-flight: is silent renewal plausible right now?"""

    def describe(self) -> str:
        """Short human-readable description of the renewal mechanism."""


@runtime_checkable
class InteractiveAuthenticator(Protocol):
    """Obtain a session with the user's involvement (browser, QR, paste)."""

    name: str

    def login(self, *, timeout: float | None = None) -> LoginResult:
        """Run an interactive login and return a structured result."""

    def describe(self) -> str:
        """Short human-readable description of the login mechanism."""


#: Renewal statuses that mean "this mechanism cannot help here, so trying the
#: next one is worthwhile" rather than "renewal failed". The distinction is the
#: whole reason a fallback chain exists.
#:
#: ``LOGIN_REQUIRED`` is deliberately **absent**: if the upstream SSO session is
#: gone, a second mechanism reading the same session cannot conjure one, and
#: trying anyway would turn one honest failure into two slow ones.
_FALLBACK_WORTH_TRYING = frozenset(
    {
        RenewalStatus.CDP_UNAVAILABLE,
        RenewalStatus.UNSUPPORTED,
        RenewalStatus.OAUTH_FAILED,
    }
)


class FallbackRenewer:
    """Try several renewal mechanisms in order, stopping at the first that works.

    Why this exists rather than one renewer
    ---------------------------------------
    There are two mechanisms now, and they cover different ground:

    * :class:`~opencsi.auth.http_oauth.HttpOAuthRenewer` -- plain HTTP, no
      browser engine. Covers renewal and repeat authorization, which is the
      overwhelming majority of what a long-running monitor does.
    * :class:`~opencsi.auth.oauth_browser.BrowserOAuthRenewer` -- drives a real
      browser. Covers the case HTTP cannot: a first-time consent that has to be
      confirmed, and any environment where the GitCode session is only reachable
      inside a browser profile.

    Putting the ordering here rather than in each caller means it is decided once
    and is testable without a browser or a network.

    What it will *not* do
    ---------------------
    It does not retry a mechanism whose answer was final. ``CONSENT_REQUIRED``
    and ``LOGIN_REQUIRED`` stop the chain immediately: both mean a human is
    needed, and neither is something the next mechanism can fix. Falling through
    on those would delay a message the user needs, and could mask a genuine
    consent requirement behind a misleading "renewal failed".
    """

    def __init__(self, renewers: "list[Any]", *, name: str = "fallback") -> None:
        self._renewers = [r for r in renewers if r is not None]
        self.name = name
        #: Which mechanism actually produced the reported outcome, so a caller
        #: can say *how* the session was renewed instead of only that it was.
        self.last_mechanism: str | None = None

    def can_renew(self) -> bool:
        """Whether *any* mechanism is plausible. Cheap and non-committal."""
        return any(_can(r) for r in self._renewers)

    def renew(
        self,
        *,
        timeout: float | None = None,
        before: CredentialProvider | None = None,
    ) -> RenewalResult:
        """Try each mechanism in order. Never raises; always reports."""
        if not self._renewers:
            return RenewalResult(
                RenewalStatus.UNSUPPORTED,
                detail="no renewal mechanism is available",
            )

        attempts: list[str] = []
        last: RenewalResult | None = None

        for renewer in self._renewers:
            label = getattr(renewer, "name", type(renewer).__name__)
            if not _can(renewer):
                attempts.append(f"{label}: unavailable")
                continue

            try:
                result = renewer.renew(timeout=timeout, before=before)
            except OpenCsiError as exc:
                attempts.append(f"{label}: {exc.code}")
                last = RenewalResult(
                    RenewalStatus.OAUTH_FAILED, detail=scrub_text(str(exc))[:200]
                )
                continue
            except Exception as exc:  # noqa: BLE001 - a mechanism must not crash the chain
                attempts.append(f"{label}: {type(exc).__name__}")
                last = RenewalResult(
                    RenewalStatus.OAUTH_FAILED,
                    detail=f"{label} failed ({type(exc).__name__})",
                )
                continue

            attempts.append(f"{label}: {result.status.value}")
            self.last_mechanism = label
            last = result

            if result.ok:
                # A later mechanism can only do worse than a success, so stop.
                return result
            if result.status not in _FALLBACK_WORTH_TRYING:
                # A final answer: a human is needed, or the session is gone.
                return result

        if last is None:
            return RenewalResult(
                RenewalStatus.UNSUPPORTED,
                detail="no renewal mechanism could be attempted",
            )
        # Every mechanism either was unavailable or failed in a way the next one
        # might have fixed. Report the last real attempt, annotated with what was
        # tried, so the user sees the chain rather than only its tail.
        return RenewalResult(
            last.status,
            renewed=last.renewed,
            requires_interaction=last.requires_interaction,
            token_changed=last.token_changed,
            expires_in=last.expires_in,
            detail=(
                (last.detail + " ") if last.detail else ""
            )
            + f"(tried: {'; '.join(attempts)})",
        )

    def describe(self) -> str:
        inner = ", ".join(
            getattr(r, "name", type(r).__name__) for r in self._renewers
        )
        return f"renewal chain: {inner}"

    def __repr__(self) -> str:
        return f"FallbackRenewer({[getattr(r, 'name', '?') for r in self._renewers]})"


def _can(renewer: Any) -> bool:
    """``can_renew()`` that cannot itself raise.

    A capability probe that throws would abort the chain for a mechanism that
    might have worked, which is the opposite of what a probe is for.
    """
    probe = getattr(renewer, "can_renew", None)
    if not callable(probe):
        return True
    try:
        return bool(probe())
    except Exception:  # noqa: BLE001 - a probe must never raise
        return False


@dataclass
class SessionManager:
    """Owns credential *policy*; delegates credential *mechanism*.

    Responsibilities:

    * decide **when** a credential needs attention (``needs_renewal``);
    * run **reload then renew** in the right order on a 401, bounded;
    * expose a secret-free view of the whole authentication state for the CLI
      and the tray.

    It never reads a cookie itself: :attr:`credentials` remains the only reader,
    which keeps the "one place touches the secret" property intact.
    """

    credentials: CredentialProvider
    renewer: SessionRenewer | None = None
    authenticator: InteractiveAuthenticator | None = None
    renew_margin: float = DEFAULT_RENEW_MARGIN
    renew_cooldown: float = RENEW_COOLDOWN

    #: Last renewal outcome, for diagnostics. Secret-free by construction.
    last_renewal: RenewalResult | None = None
    _last_renew_at: float = field(default=0.0, repr=False)
    _renew_attempts: int = field(default=0, repr=False)

    # ── observation ───────────────────────────────────────────────────────
    def status(self) -> CredentialStatus:
        """Current credential status (delegates; never raises for 'absent')."""
        return self.credentials.status()

    def needs_renewal(self, *, margin: float | None = None) -> bool:
        """Whether the credential is expired or inside the renewal margin.

        An unknown expiry is treated as *not* needing renewal: the credential
        may be a session cookie with no ``expires`` at all, and renewing on
        every call would be worse than waiting for a 401 that may never come.
        """
        status = self.credentials.status()
        if not status.available:
            return False
        if status.expires_in is None:
            return False
        return status.expires_in <= (self.renew_margin if margin is None else margin)

    def describe(self) -> str:
        """One-line, secret-free description of the authentication setup."""
        parts = [f"credential={self.credentials.name}"]
        parts.append(
            f"renewer={self.renewer.name}" if self.renewer is not None else "renewer=none"
        )
        parts.append(
            f"authenticator={self.authenticator.name}"
            if self.authenticator is not None
            else "authenticator=none"
        )
        return ", ".join(parts)

    # ── policy ────────────────────────────────────────────────────────────
    def ensure_valid(self) -> RenewalResult:
        """Renew proactively when the credential is close to expiry.

        This is the *scheduled* path, used before a normal API call. It returns
        ``ALREADY_VALID`` without touching the network when there is time left,
        which is what keeps a five-minute refresh loop from re-running OAuth.
        """
        if not self.needs_renewal():
            return RenewalResult(RenewalStatus.ALREADY_VALID)
        return self.renew()

    def renew(self, *, force: bool = False) -> RenewalResult:
        """Attempt silent session renewal, respecting the cooldown.

        ``force`` bypasses the cooldown (used by ``opencsi login --renew`` and
        the tray's explicit "Renew session" action).
        """
        if self.renewer is None:
            return RenewalResult(
                RenewalStatus.UNSUPPORTED,
                requires_interaction=True,
                detail=(
                    f"the {self.credentials.name} credential cannot renew itself; "
                    "sign in again"
                ),
            )

        now = time.time()
        if not force and (now - self._last_renew_at) < self.renew_cooldown:
            # A renewal ran very recently. Do not hammer OAuth: report what it
            # said rather than starting another round-trip.
            if self.last_renewal is not None:
                return self.last_renewal
            return RenewalResult(
                RenewalStatus.ALREADY_VALID,
                detail="a renewal ran moments ago; not repeating it",
            )

        self._renew_attempts += 1
        result = self._invoke_renewer()
        self._last_renew_at = time.time()
        self.last_renewal = result
        if result.renewed and not self._provider_holds_new_value():
            # The new secret landed somewhere the provider has not seen, which
            # means the browser has it. Dropping the cache is what forces the next
            # read to go and fetch it.
            #
            # This guard is load-bearing, and its absence was a real bug. The
            # browserless renewer obtains the session over plain HTTP and hands
            # the value straight to the provider, so the *browser* it read the
            # GitCode credential from never learns the new cookie. Unconditionally
            # invalidating threw away the only copy that existed: the renewal had
            # genuinely succeeded, and the very next ``get_token()`` went to the
            # browser, found nothing, and reported "the new cookie was rejected".
            # The source run passed only because it happened to reuse the
            # provider; the frozen build exposed it.
            self.credentials.invalidate()
        return result

    def _provider_holds_new_value(self) -> bool:
        """Whether the provider already holds what the renewer just produced.

        Asks the provider directly when it can answer exactly
        (``holds_remembered_token``), because a value comparison is only usually
        right: a browserless renewal returning the *same* value the cache already
        held looks identical to a browser-driven one by value alone, and that is
        precisely the case where dropping the cache would be wrong.

        Falls back to a before/after identity comparison for providers that
        cannot answer -- a manual provider, or a future one. There the comparison
        is safe: those providers do not read from a browser, so invalidating them
        cannot lose a value that only the renewer knows about.
        """
        try:
            exact = getattr(self.credentials, "holds_remembered_token", None)
            if callable(exact):
                return bool(exact())
        except Exception:  # noqa: BLE001 - introspection must not break renewal
            pass
        return self._current_token_identity() is not None

    def _invoke_renewer(self) -> RenewalResult:
        """Call the renewer, tolerating both the narrow and wide signatures.

        ``before`` is what lets a renewer prove the token actually changed, so
        it is always offered. A renewer written against the narrower
        ``renew(*, timeout)`` signature is still supported rather than crashing
        the caller -- and its result is trusted less, because it could not have
        made that comparison.
        """
        renewer = self.renewer
        assert renewer is not None
        try:
            accepts_before = _accepts_keyword(renewer.renew, "before")
        except Exception:  # noqa: BLE001 - introspection is best-effort
            accepts_before = False
        try:
            if accepts_before:
                return renewer.renew(before=self.credentials)
            return renewer.renew()
        except OpenCsiError as exc:
            return RenewalResult(
                RenewalStatus.OAUTH_FAILED,
                requires_interaction=False,
                detail=f"{exc.code}: {exc}",
            )
        except Exception as exc:  # noqa: BLE001 - never let renewal crash a caller
            return RenewalResult(
                RenewalStatus.OAUTH_FAILED,
                detail=f"unexpected {type(exc).__name__} during renewal",
            )

    def reload_then_renew(self) -> RenewalResult:
        """The 401 recovery sequence: cheap reload first, renewal second.

        Order matters. A reload is nearly free and occasionally sufficient (the
        site itself may have rotated the cookie in the browser). Only when the
        reload yields nothing new do we pay for a full OAuth round-trip.
        """
        before = self._current_token_identity()
        try:
            self.credentials.invalidate()
            reloaded = self.credentials.refresh()
        except OpenCsiError as exc:
            log.debug("credential reload failed (%s); trying renewal", exc.code)
            reloaded = None

        if reloaded and self._current_token_identity() != before:
            return RenewalResult(
                RenewalStatus.RENEWED,
                renewed=True,
                token_changed=True,
                expires_in=self._expires_in(),
                detail="the browser held a newer cookie; no OAuth round-trip needed",
            )

        return self.renew(force=True)

    # ── interactive ───────────────────────────────────────────────────────
    def login(self, *, timeout: float | None = None) -> LoginResult:
        """Run the configured interactive login, if any."""
        if self.authenticator is None:
            return LoginResult(
                LoginStatus.UNSUPPORTED,
                detail="no interactive authenticator is configured",
            )
        try:
            return self.authenticator.login(timeout=timeout)
        except OpenCsiError as exc:
            return LoginResult(LoginStatus.FAILED, detail=f"{exc.code}: {exc}")
        except Exception as exc:  # noqa: BLE001
            return LoginResult(
                LoginStatus.FAILED, detail=f"unexpected {type(exc).__name__} during login"
            )

    # ── internals ─────────────────────────────────────────────────────────
    def _current_token_identity(self) -> object:
        """A value that changes when the token changes, without holding it.

        Used only for equality comparison, so the raw token never escapes into
        a local that could be logged.

        ``peek_token()`` is preferred over ``get_token()`` when the provider
        offers it. That is not a micro-optimisation: ``get_token()`` is allowed
        to go and *fetch* a fresh value, so using it here would compare the new
        credential against itself and report "nothing changed" for exactly the
        reload this method exists to detect.
        """
        try:
            peek = getattr(self.credentials, "peek_token", None)
            token = peek() if callable(peek) else self.credentials.get_token()
        except OpenCsiError:
            return None
        if not token:
            return None
        import hashlib

        return (
            len(token),
            hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()[:16],
        )

    def _expires_in(self) -> float | None:
        try:
            return self.credentials.status().expires_in
        except Exception:  # noqa: BLE001 - status is advisory
            return None
