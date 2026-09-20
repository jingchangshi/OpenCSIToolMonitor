"""Manual credential provider.

Intended for tests and for advanced users whose browser cannot be reached.

The value is held in memory only. The CLI never accepts a token on the command
line (that would place it in process argv and shell history); it reads from
stdin via :func:`getpass` instead. See ``opencsi login --manual``.
"""

from __future__ import annotations

import time

from ..redaction import register_secret
from .base import CredentialStatus, remaining_seconds


class ManualCookieProvider:
    """Hold a cookie value supplied directly by the caller.

    ``invalidate()`` clears the value permanently, because there is no
    underlying source to re-read. This is the documented difference from
    :class:`~opencsi.auth.cdp.CdpCookieProvider`, whose ``invalidate()`` merely
    drops a cache.
    """

    name = "manual"

    def __init__(self, token: str | None = None, *, expires_at: float | None = None) -> None:
        self._token: str | None = None
        self._expires_at = expires_at
        self._read_at = time.time()
        if token:
            self.set_token(token)

    def set_token(self, token: str, *, expires_at: float | None = None) -> None:
        """Install a token, registering it for redaction."""
        if not token:
            raise ValueError("token must be a non-empty string")
        register_secret(token)
        self._token = token
        self._read_at = time.time()
        if expires_at is not None:
            self._expires_at = expires_at

    def get_token(self) -> str | None:
        return self._token

    def invalidate(self) -> None:
        """Clear the token. Permanent: there is no source to re-read."""
        self._token = None

    def refresh(self) -> str | None:
        """No-op re-read: the manual provider cannot obtain a new token."""
        return self._token

    def status(self) -> CredentialStatus:
        return CredentialStatus(
            available=self._token is not None,
            source=self.name,
            expires_at=self._expires_at,
            expires_in=remaining_seconds(self._expires_at),
            domain="opencsitool.com" if self._token else None,
            http_only=True if self._token else None,
            secure=True if self._token else None,
            cookie_count=1 if self._token else 0,
            detail=None if self._token else "no token supplied",
        )

    def __repr__(self) -> str:
        return (
            f"ManualCookieProvider(has_token={self._token is not None}, token=<redacted>)"
        )
