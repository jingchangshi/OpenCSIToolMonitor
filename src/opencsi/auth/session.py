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

    @property
    def ok(self) -> bool:
        return self.status is LoginStatus.SUCCEEDED

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "status": self.status.value,
            "ok": self.ok,
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
        if result.renewed:
            # The new value is a different secret; make sure the client cannot
            # keep using the old one from its own transport state.
            self.credentials.invalidate()
        return result

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
