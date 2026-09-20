"""Credential provider protocol and status object.

A provider answers exactly one question -- "what cookie should I send?" -- and
exposes a redacted status for diagnostics. It never returns a ``Secret`` to the
client for storage, and :class:`CredentialStatus` deliberately has no field for
the token value.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class CredentialStatus:
    """Redacted description of a provider's current state.

    There is intentionally no ``token`` field: this object is safe to print,
    log and serialise. Callers that need the raw value call ``get_token()``.
    """

    available: bool
    source: str
    expires_at: float | None = None
    expires_in: float | None = None
    domain: str | None = None
    http_only: bool | None = None
    secure: bool | None = None
    cookie_count: int = 0
    detail: str | None = None

    @property
    def expired(self) -> bool:
        return self.expires_in is not None and self.expires_in <= 0

    @property
    def expiring_soon(self) -> bool:
        """True when the credential has under 60 seconds of life left."""
        return self.expires_in is not None and 0 < self.expires_in <= 60

    def as_dict(self) -> dict[str, object]:
        """Serialisable, secret-free representation."""
        out: dict[str, object] = {
            "available": self.available,
            "source": self.source,
        }
        if self.domain is not None:
            out["domain"] = self.domain
        if self.http_only is not None:
            out["http_only"] = self.http_only
        if self.secure is not None:
            out["secure"] = self.secure
        if self.expires_in is not None:
            out["expires_in_seconds"] = round(self.expires_in, 1)
        if self.expires_at is not None:
            out["expires_at_epoch"] = round(self.expires_at, 1)
        if self.cookie_count:
            out["cookie_count"] = self.cookie_count
        if self.detail:
            out["detail"] = self.detail
        return out


@runtime_checkable
class CredentialProvider(Protocol):
    """Source of the openCsiTool session cookie.

    Implementations must guarantee:

    * ``get_token()`` returns the raw cookie value, or ``None`` when no
      credential is configured at all.
    * ``get_token()`` may instead raise an :class:`~opencsi.errors.OpenCsiError`
      subclass when a credential *source* exists but could not be used (a
      browser that refuses the DevTools upgrade, a browser holding no
      openCsiTool cookie). Raising is preferred over returning ``None`` in that
      case, because the exception carries a machine-readable code, an
      actionable hint, and a distinct exit status -- whereas ``None`` collapses
      every cause into "not signed in". The client propagates these unchanged.
    * ``invalidate()`` discards any cached value so the *next* ``get_token()``
      re-reads from the underlying source. It must not permanently poison a
      re-readable source such as a browser.
    * No method ever logs, prints or otherwise exposes the token.
    """

    name: str

    def get_token(self) -> str | None:
        """Return the current cookie value, ``None``, or raise ``OpenCsiError``."""

    def invalidate(self) -> None:
        """Drop the cached value so the next call re-reads the source."""

    def refresh(self) -> str | None:
        """Force a re-read, bypassing any TTL-based reuse."""

    def status(self) -> CredentialStatus:
        """Return a redacted status for diagnostics."""


def remaining_seconds(expires_at: float | None) -> float | None:
    """Seconds until ``expires_at`` (may be negative); ``None`` if unknown."""
    if expires_at is None:
        return None
    return expires_at - time.time()
