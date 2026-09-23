"""Durable credential storage: the abstraction, and the rules it must satisfy.

Why this module exists
----------------------
``HttpOAuthRenewer`` proved that the whole OAuth leg works over plain HTTP with no
browser engine. That was the protocol question, and it is settled. What remained
was a *lifetime* question, and it is the reason this module exists:

.. code-block::

    Process A:  login --qr -> GitCode credential -> openCsiTool token
                ...both live in this process's memory, and die with it.
    Process B:  usage -> no credential anywhere -> "not signed in"

A browserless login that only works in the process that performed it is not a
finished tool. The credential has to outlive the process, the terminal and the
machine's reboot, and it has to do so without being written anywhere readable.

What a store must never do
--------------------------
The prohibited shapes are named so a reviewer can check them off:

* no plaintext ``credentials.json`` -- an unencrypted file next to the config
  would be readable by anything running as the user, including a backup agent;
* no ``.env``, no plaintext registry value, no pickle, no unencrypted SQLite;
* no plaintext temporary file on the way to an encrypted one. Serialisation
  happens in memory and only the *encrypted* bytes are ever written, because a
  temp file is exactly the artifact a crash leaves behind.

On Windows the implementation is DPAPI (:mod:`opencsi.auth.windows_store`),
scoped to the current user. On other platforms there is deliberately **no
fallback store**: a silent plaintext fallback would be worse than an honest
"unsupported", because it would store a live credential in the clear on a
machine whose owner believed it was encrypted.

What this module holds
----------------------
The data model (:class:`CredentialBundle` and the two credential records), the
:class:`CredentialStore` protocol, and one in-memory implementation for tests.
Nothing here reads or writes a file; that is the platform store's job.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Protocol, runtime_checkable

from ..redaction import MASK, register_secret

#: Bumped when the on-disk shape changes incompatibly. The store refuses to
#: guess at a version it does not know rather than misreading it: a credential
#: file from the future is more likely to be a different tool's than a newer
#: build of this one.
STORE_VERSION = 1


@dataclass(frozen=True, repr=False)
class StoredGitCodeCredential:
    """A GitCode credential, as obtained from a QR login.

    This is the *upstream* credential -- the one GitCode issues. It is what makes
    a later renewal possible without user interaction, so it is the single most
    valuable secret this tool holds and the reason the store is encrypted.

    ``repr`` is suppressed at the class level rather than overridden per field,
    because the failure mode being guarded against is an *accidental* one: a
    ``print(bundle)`` in a debugger, an f-string in a traceback, a dataclass
    repr landing in a log line. ``repr=False`` on the dataclass makes those
    unrepresentable instead of relying on the reader to remember.
    """

    access_token: str
    refresh_token: str | None = None
    username: str | None = None
    #: Epoch seconds. ``None`` means "not known", which is different from
    #: "expired" -- the QR flow does not always report an expiry.
    access_expires_at: float | None = None
    refresh_expires_at: float | None = None

    def __repr__(self) -> str:
        return (
            f"StoredGitCodeCredential(username={self.username!r}, "
            f"access_token={MASK}, refresh_token={MASK}, "
            f"access_expires_at={self.access_expires_at!r})"
        )

    __str__ = __repr__

    def __post_init__(self) -> None:
        # Register with the redaction registry so these values are masked even
        # if they reach a log line through some path this module does not own.
        register_secret(self.access_token)
        register_secret(self.refresh_token)

    @property
    def access_expired(self) -> bool:
        """Whether the access token is known to be past its expiry."""
        if self.access_expires_at is None:
            return False
        return self.access_expires_at <= time.time()

    @property
    def access_remaining(self) -> float | None:
        """Seconds of access-token life left, or ``None`` when unknown."""
        if self.access_expires_at is None:
            return None
        return self.access_expires_at - time.time()

    @property
    def has_refresh_token(self) -> bool:
        """Whether a silent refresh is even possible."""
        return bool(self.refresh_token)

    def as_persisted(self) -> dict[str, Any]:
        """The plaintext mapping to serialise. Callers must encrypt it."""
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "username": self.username,
            "access_expires_at": self.access_expires_at,
            "refresh_expires_at": self.refresh_expires_at,
        }

    @classmethod
    def from_persisted(cls, data: Mapping[str, Any]) -> "StoredGitCodeCredential":
        """Rebuild from a decoded mapping, ignoring unknown keys.

        Unknown keys are dropped rather than rejected so that a file written by
        a slightly newer build still loads. A *missing* ``access_token`` is a
        different matter and is rejected, because a credential without one is
        not a credential.
        """
        access = data.get("access_token")
        if not isinstance(access, str) or not access:
            raise ValueError("stored GitCode credential has no access_token")
        return cls(
            access_token=access,
            refresh_token=_optional_str(data.get("refresh_token")),
            username=_optional_str(data.get("username")),
            access_expires_at=_optional_float(data.get("access_expires_at")),
            refresh_expires_at=_optional_float(data.get("refresh_expires_at")),
        )


@dataclass(frozen=True, repr=False)
class StoredOpenCsiCredential:
    """The openCsiTool session, as a bare cookie value plus its expiry.

    Only the value is kept -- not the whole cookie record. The cookie's domain,
    path and flags are constants of the service, so storing them would be
    storing our own assumptions rather than a credential, and a stale copy of
    them would be worse than recomputing them.
    """

    token: str
    expires_at: float | None = None

    def __repr__(self) -> str:
        return f"StoredOpenCsiCredential(token={MASK}, expires_at={self.expires_at!r})"

    __str__ = __repr__

    def __post_init__(self) -> None:
        register_secret(self.token)

    @property
    def expired(self) -> bool:
        if self.expires_at is None:
            return False
        return self.expires_at <= time.time()

    @property
    def remaining(self) -> float | None:
        if self.expires_at is None:
            return None
        return self.expires_at - time.time()

    def expires_in(self) -> float | None:
        """Seconds of life left, matching ``CredentialStatus.expires_in``."""
        return self.remaining

    def as_persisted(self) -> dict[str, Any]:
        return {"token": self.token, "expires_at": self.expires_at}

    @classmethod
    def from_persisted(cls, data: Mapping[str, Any]) -> "StoredOpenCsiCredential":
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise ValueError("stored openCsiTool credential has no token")
        return cls(
            token=token,
            expires_at=_optional_float(data.get("expires_at")),
        )


@dataclass(frozen=True, repr=False)
class CredentialBundle:
    """Everything in the store, as one immutable value.

    Both halves are optional and independently so. A user who completed a QR scan
    but whose openCsiTool leg needed a consent click has a GitCode credential and
    no session; a user who pasted a cookie by hand has the opposite. Collapsing
    either case into "no credentials" would discard the half that still works --
    and for the GitCode half, discarding it means asking for another scan.
    """

    gitcode: StoredGitCodeCredential | None = None
    opencsi: StoredOpenCsiCredential | None = None

    def __repr__(self) -> str:
        return (
            f"CredentialBundle(gitcode={'set' if self.gitcode else 'absent'}, "
            f"opencsi={'set' if self.opencsi else 'absent'})"
        )

    __str__ = __repr__

    @property
    def empty(self) -> bool:
        return self.gitcode is None and self.opencsi is None

    def as_persisted(self) -> dict[str, Any]:
        """The mapping to serialise, versioned. Callers must encrypt it."""
        out: dict[str, Any] = {"version": STORE_VERSION}
        if self.gitcode is not None:
            out["gitcode"] = self.gitcode.as_persisted()
        if self.opencsi is not None:
            out["opencsi"] = self.opencsi.as_persisted()
        return out

    @classmethod
    def from_persisted(cls, data: Mapping[str, Any]) -> "CredentialBundle":
        """Rebuild from a decoded mapping.

        A half that fails to parse is dropped rather than failing the whole
        bundle. That is deliberate: a GitCode record that a newer build wrote in
        an unreadable shape must not cost the user their *session*, which is the
        half that keeps the CLI working right now.
        """
        version = data.get("version")
        if version is not None and version != STORE_VERSION:
            raise ValueError(f"unsupported credential store version: {version!r}")

        gitcode = None
        raw_gitcode = data.get("gitcode")
        if isinstance(raw_gitcode, Mapping):
            try:
                gitcode = StoredGitCodeCredential.from_persisted(raw_gitcode)
            except ValueError:
                gitcode = None

        opencsi = None
        raw_opencsi = data.get("opencsi")
        if isinstance(raw_opencsi, Mapping):
            try:
                opencsi = StoredOpenCsiCredential.from_persisted(raw_opencsi)
            except ValueError:
                opencsi = None

        return cls(gitcode=gitcode, opencsi=opencsi)

    def with_gitcode(self, credential: StoredGitCodeCredential | None) -> "CredentialBundle":
        return replace(self, gitcode=credential)

    def with_opencsi(self, credential: StoredOpenCsiCredential | None) -> "CredentialBundle":
        return replace(self, opencsi=credential)


@dataclass(frozen=True)
class CredentialStoreStatus:
    """Redacted description of a store. Safe to print, log and serialise.

    There is deliberately no field that could hold a credential. ``path`` is
    included because "where is it stored?" is the first question a user asks,
    and the *location* is not a secret -- the contents are.
    """

    available: bool
    backend: str
    path: str | None = None
    has_gitcode: bool = False
    has_opencsi: bool = False
    opencsi_expires_at: float | None = None
    gitcode_username: str | None = None
    detail: str | None = None

    @property
    def empty(self) -> bool:
        return not (self.has_gitcode or self.has_opencsi)

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "available": self.available,
            "backend": self.backend,
        }
        if self.path:
            out["path"] = self.path
        out["has_gitcode_credential"] = self.has_gitcode
        out["has_opencsi_session"] = self.has_opencsi
        if self.opencsi_expires_at is not None:
            out["opencsi_expires_at_epoch"] = round(self.opencsi_expires_at, 1)
        if self.gitcode_username:
            out["gitcode_username"] = self.gitcode_username
        if self.detail:
            out["detail"] = self.detail
        return out


class CredentialStoreError(Exception):
    """The store exists but could not be used.

    Carries no credential material. The message is scrubbed on construction,
    which catches two different things:

    * a **registered** value -- anything a
      :class:`~opencsi.auth.store.StoredOpenCsiCredential` or
      :class:`~opencsi.auth.store.StoredGitCodeCredential` has been built from,
      and any token the renewal flow has seen;
    * a **recognisable** value -- the patterns in
      :func:`opencsi.redaction.scrub_text`, which catch credential-shaped text
      even when nothing registered it.

    The second matters more than it looks. The store is the one place that
    handles a credential it did not itself create and has not yet registered,
    so "nothing registered it" is the normal case here, not the exception.
    A bare opaque string with no surrounding ``token=`` context is not matched
    by any pattern -- which is a real limit, and the reason the store's own
    code never interpolates a value into a message in the first place.
    """

    def __init__(self, message: str) -> None:
        from ..redaction import scrub_text

        super().__init__(scrub_text(message))


@runtime_checkable
class CredentialStore(Protocol):
    """A durable, encrypted home for credentials.

    The split between :meth:`save_gitcode` and :meth:`save_opencsi` is not
    stylistic. The two halves are written at different moments by different
    flows -- a QR scan writes GitCode first and the openCsiTool session second --
    and a single ``save(bundle)`` would make the second write overwrite the first
    unless every caller remembered to load-then-merge. Making the partial writes
    the default removes the opportunity to forget.
    """

    name: str

    def load(self) -> CredentialBundle:
        """Read everything. Raises :class:`CredentialStoreError` if unreadable."""

    def save_gitcode(self, credential: StoredGitCodeCredential) -> None:
        """Persist the GitCode credential, leaving the openCsiTool half alone."""

    def save_opencsi(self, credential: StoredOpenCsiCredential) -> None:
        """Persist the openCsiTool session, leaving the GitCode half alone."""

    def clear_gitcode(self) -> None:
        """Remove the GitCode credential only."""

    def clear_opencsi(self) -> None:
        """Remove the openCsiTool session only.

        Used when the session is known to be dead and the upstream credential
        may still be good -- so ``logout`` is not the right verb for it.
        """

    def clear_all(self) -> None:
        """Remove every credential this store holds."""

    def status(self) -> CredentialStoreStatus:
        """Redacted description, for ``doctor`` and ``login --status``."""


class MemoryCredentialStore:
    """An in-process store, for tests and for ``--no-persist`` runs.

    It exists so that a test can exercise the whole login and renewal path
    without touching a user's real credential file, and so that the behaviour of
    "persist" and "do not persist" differ only in which store is passed in.

    It is **not** a fallback for a platform whose secure store is unavailable.
    Storing a live credential in process memory is not durable, and using this
    where DPAPI failed would silently produce a session that dies with the
    process -- the exact bug this architecture exists to fix.
    """

    name = "memory"

    def __init__(self, bundle: CredentialBundle | None = None) -> None:
        self._bundle = bundle or CredentialBundle()
        #: Counts writes, so a test can assert that a no-op was a no-op.
        self.writes = 0

    def load(self) -> CredentialBundle:
        return self._bundle

    def save_gitcode(self, credential: StoredGitCodeCredential) -> None:
        self._bundle = self._bundle.with_gitcode(credential)
        self.writes += 1

    def save_opencsi(self, credential: StoredOpenCsiCredential) -> None:
        self._bundle = self._bundle.with_opencsi(credential)
        self.writes += 1

    def clear_gitcode(self) -> None:
        self._bundle = self._bundle.with_gitcode(None)
        self.writes += 1

    def clear_opencsi(self) -> None:
        self._bundle = self._bundle.with_opencsi(None)
        self.writes += 1

    def clear_all(self) -> None:
        self._bundle = CredentialBundle()
        self.writes += 1

    def status(self) -> CredentialStoreStatus:
        bundle = self._bundle
        return CredentialStoreStatus(
            available=True,
            backend=self.name,
            path=None,
            has_gitcode=bundle.gitcode is not None,
            has_opencsi=bundle.opencsi is not None,
            opencsi_expires_at=(
                bundle.opencsi.expires_at if bundle.opencsi is not None else None
            ),
            gitcode_username=(
                bundle.gitcode.username if bundle.gitcode is not None else None
            ),
        )

    def __repr__(self) -> str:
        return f"MemoryCredentialStore({self._bundle!r})"


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None
