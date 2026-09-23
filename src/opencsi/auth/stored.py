"""Credential providers and sources backed by the durable store.

This is the module that makes the store *useful*. It answers the two questions
the rest of the system asks:

* "what openCsiTool cookie should I send?" --
  :class:`StoredOpenCsiCredentialProvider`
* "what GitCode credential can renew it?" --
  :class:`StoredGitCodeCredentialSource`

The interface-compatibility decision
------------------------------------
:class:`~opencsi.auth.http_oauth.HttpOAuthRenewer` already exists, already works,
and already proved the browserless flow. It reads its GitCode credential through
``source.read_all_cookies()`` and writes the minted session back through
``provider.remember_token()``.

Rather than rewrite that module against a new abstraction -- which would be a
large change to working, measured code -- the classes below present a
*stored* credential in the cookie-shaped records the existing interface already
consumes. The whole existing renewal path then works unchanged, over a store that
persists.

The cost is honest and worth writing down: the record shape is a compatibility
shim, not a model of what a GitCode credential is. When the source/sink is
refactored later, :meth:`StoredGitCodeCredentialSource.read_all_cookies` is the
single method that disappears. Until then correctness and verifiability beat
abstraction purity, which is the trade this round deliberately makes.
"""

from __future__ import annotations

import time
from typing import Any, Mapping

from ..errors import OpenCsiError
from ..redaction import register_secret
from .base import CredentialStatus
from .http_oauth import GITCODE_SESSION_COOKIES
from .store import (
    CredentialStore,
    CredentialStoreError,
    StoredGitCodeCredential,
    StoredOpenCsiCredential,
)

#: The cookie-shaped names the browserless flow reads out of a source. These are
#: the same names a real GitCode profile carries, so a stored credential and a
#: browser-read one are indistinguishable to the renewer.
ACCESS_COOKIE = "GITCODE_ACCESS_TOKEN"
REFRESH_COOKIE = "GITCODE_REFRESH_TOKEN"
USERNAME_COOKIE = "GitCodeUserName"

#: The domain the shimmed records claim. Matches what a real profile reports,
#: because ``_read_gitcode_session`` filters on ``gitcode.com`` appearing in the
#: domain -- a synthetic value like ``stored`` would be silently dropped and the
#: renewal would report "no GitCode session" with no indication why.
GITCODE_DOMAIN = ".gitcode.com"

#: Treat a session as needing attention this long before it actually expires.
#: Mirrors the session manager's own margin so a store-backed provider does not
#: report "valid" for a credential the manager is about to renew.
DEFAULT_EXPIRY_MARGIN = 60.0


def _record(name: str, value: str) -> dict[str, Any]:
    """One cookie-shaped record, in the shape ``read_all_cookies`` returns."""
    register_secret(value)
    return {
        "name": name,
        "value": value,
        "domain": GITCODE_DOMAIN,
        "path": "/",
        "secure": True,
        "httpOnly": True,
        # 0 rather than a real timestamp: a session cookie. The expiry that
        # matters is carried on the credential itself, and inventing a cookie
        # ``expires`` here would make the shim look like it knew something the
        # record does not.
        "expires": 0,
    }


class StoredGitCodeCredentialSource:
    """Serve a stored GitCode credential to the browserless OAuth flow.

    Read-only by design. It never writes to the store: the renewal flow's job is
    to mint an openCsiTool session, and the GitCode credential is only *consumed*
    to do that. Persisting a rotated refresh token is
    :mod:`opencsi.auth.gitcode_refresh`'s job, on its own schedule, so that a
    renewal cannot silently overwrite the upstream credential as a side effect.
    """

    name = "stored-gitcode"

    def __init__(self, store: CredentialStore) -> None:
        self._store = store
        #: Set when ``load`` failed, so a caller can report *why* rather than
        #: only that no credential was found. A broken store and a store with
        #: nothing in it are different problems with different fixes.
        self.last_error: str | None = None

    def _credential(self) -> StoredGitCodeCredential | None:
        try:
            bundle = self._store.load()
        except CredentialStoreError as exc:
            self.last_error = str(exc)
            return None
        self.last_error = None
        return bundle.gitcode

    def read_all_cookies(self, *, timeout: float | None = None) -> list[Mapping[str, Any]]:
        """The GitCode credential as cookie records, or an empty list.

        An empty list -- not an exception -- when there is nothing stored. The
        renewer treats "no records" as "this source cannot help", which is what
        lets :class:`~opencsi.auth.session.FallbackRenewer` move on to a browser.
        Raising here would abort the chain instead.
        """
        del timeout  # the store is local; there is no I/O budget to honour
        credential = self._credential()
        if credential is None:
            return []

        records: list[dict[str, Any]] = [_record(ACCESS_COOKIE, credential.access_token)]
        if credential.refresh_token:
            records.append(_record(REFRESH_COOKIE, credential.refresh_token))
        if credential.username:
            # The username is not a secret and is not registered. It is carried
            # because a real GitCode profile carries it and one probe showed the
            # OAuth leg sending it; omitting it would make this source subtly
            # different from the one that was measured.
            records.append(
                {
                    "name": USERNAME_COOKIE,
                    "value": credential.username,
                    "domain": GITCODE_DOMAIN,
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                    "expires": 0,
                }
            )
        return records

    @property
    def cookie_names(self) -> tuple[str, ...]:
        return tuple(sorted(str(r["name"]) for r in self.read_all_cookies()))

    def status(self) -> CredentialStatus:
        """Redacted status, so ``doctor`` can report this source."""
        credential = self._credential()
        if credential is None:
            return CredentialStatus(
                available=False,
                source=self.name,
                detail=self.last_error or "no GitCode credential is stored",
            )
        return CredentialStatus(
            available=True,
            source=self.name,
            expires_at=credential.access_expires_at,
            expires_in=credential.access_remaining,
            domain=GITCODE_DOMAIN,
            cookie_count=len(self.read_all_cookies()),
            detail=(
                f"gitcode user {credential.username}"
                if credential.username
                else None
            ),
        )

    def __repr__(self) -> str:
        return f"StoredGitCodeCredentialSource({self._store!r})"


class StoredOpenCsiCredentialProvider:
    """The openCsiTool session, read from -- and written to -- the durable store.

    Implements :class:`~opencsi.auth.base.CredentialProvider`, so it drops into
    :class:`~opencsi.auth.session.SessionManager` and
    :class:`~opencsi.client.OpenCsiToolClient` unchanged. That is the point: the
    client has no idea whether its cookie came from a browser or a DPAPI blob.

    ``name`` is ``"secure-store"`` rather than ``"dpapi"`` so the reported source
    is true on a platform where the store is something else, and so that a
    diagnostic does not imply a Windows-only implementation.
    """

    name = "secure-store"

    def __init__(
        self,
        store: CredentialStore,
        *,
        ttl: float = 5.0,
        expiry_margin: float = DEFAULT_EXPIRY_MARGIN,
    ) -> None:
        self._store = store
        self._ttl = ttl
        self._expiry_margin = expiry_margin
        #: A short cache. The store is local and cheap, but ``get_token()`` is
        #: called per request and re-decrypting the file each time is pointless
        #: work; the TTL is small enough that a credential written by another
        #: process is picked up almost immediately.
        self._cached: StoredOpenCsiCredential | None = None
        self._read_at = 0.0
        self._last_error: str | None = None

    # ── reading ───────────────────────────────────────────────────────────
    def _load(self, *, force: bool = False) -> StoredOpenCsiCredential | None:
        now = time.time()
        if not force and self._cached is not None and (now - self._read_at) < self._ttl:
            return self._cached
        try:
            bundle = self._store.load()
        except CredentialStoreError as exc:
            # A broken store must not read as "signed in". Surface it as an
            # OpenCsiError from get_token so the exit code says storage failed
            # rather than "not signed in", which is a different user action.
            self._last_error = str(exc)
            self._cached = None
            return None
        self._last_error = None
        self._cached = bundle.opencsi
        self._read_at = now
        return self._cached

    def get_token(self) -> str | None:
        """The stored session value, or ``None`` when there is none.

        ``None`` -- not an exception -- when the store is simply empty, matching
        the provider contract's "no credential is configured at all". A store
        that exists but cannot be *read* raises instead, because that is a
        distinct failure with a distinct remedy.
        """
        credential = self._load()
        if credential is None:
            if self._last_error:
                raise OpenCsiError(
                    f"the credential store is unreadable: {self._last_error}"
                )
            return None
        return credential.token

    def peek_token(self) -> str | None:
        """The cached value without re-reading the store.

        ``SessionManager`` uses this for before/after comparisons; going to the
        store would perturb the answer it is trying to measure.
        """
        return self._cached.token if self._cached is not None else None

    def invalidate(self) -> None:
        """Drop the **cache** only. Never deletes the stored credential.

        This is the distinction the docstring in :mod:`opencsi.auth.session`
        warns about: a single 401 means *this request* was rejected, not that the
        user's credential is worthless. Deleting the persistent copy on a 401
        would sign the user out permanently because of one transient rejection --
        and would also destroy the GitCode half that could have renewed it.
        """
        self._cached = None
        self._read_at = 0.0

    def refresh(self) -> str | None:
        """Re-read from the store, bypassing the cache."""
        credential = self._load(force=True)
        return credential.token if credential is not None else None

    def holds_remembered_token(self) -> bool:
        """Whether the current value came from a renewer rather than the store.

        Always ``True`` once a value is cached, and this is load-bearing.
        ``SessionManager.renew()`` invalidates the provider when a renewal
        succeeds but the provider does not hold the new value -- the rule that
        lets it re-read a *browser* that the renewer wrote into. For a store the
        renewer writes through ``remember_token``, which persists and caches, so
        the value is already held and invalidating would be pure loss.
        """
        return self._cached is not None

    def remember_token(self, token: str, *, expires_in: float | None = None) -> None:
        """Persist a renewed session, then cache it.

        Persist first: if the write fails, the caller must learn that the new
        session will not survive this process. Caching first and writing second
        would make a failed persistence invisible until the next run -- which is
        the exact class of bug this round exists to eliminate.
        """
        if not token:
            return
        register_secret(token)
        expires_at = time.time() + expires_in if expires_in is not None else None
        credential = StoredOpenCsiCredential(token=token, expires_at=expires_at)
        self._store.save_opencsi(credential)
        self._cached = credential
        self._read_at = time.time()

    def install_token(self, token: str, *, expires_in: float | None = None) -> None:
        """Alias for :meth:`remember_token`, for callers that use that name.

        ``HttpOAuthRenewer`` looks for ``install_token`` when writing a session
        *back* into a source; ``CdpCookieProvider`` exposes both names. Providing
        both keeps the renewer's existing branching correct without editing it.
        """
        self.remember_token(token, expires_in=expires_in)

    # ── diagnostics ───────────────────────────────────────────────────────
    def status(self) -> CredentialStatus:
        """Redacted status. Never raises: diagnostics must report a broken store."""
        credential = self._load()
        if credential is None:
            return CredentialStatus(
                available=False,
                source=self.name,
                detail=self._last_error or "no openCsiTool session is stored",
            )
        return CredentialStatus(
            available=True,
            source=self.name,
            expires_at=credential.expires_at,
            expires_in=credential.remaining,
            domain="opencsitool.com",
            http_only=True,
            secure=True,
            cookie_count=1,
            detail=(
                "expiring soon"
                if credential.remaining is not None
                and credential.remaining <= self._expiry_margin
                else None
            ),
        )

    def __repr__(self) -> str:
        return f"StoredOpenCsiCredentialProvider({self._store!r})"


class CompositeCredentialProvider:
    """Try several providers in order; the first usable credential wins.

    Why this exists
    ---------------
    The migration this round performs runs *alongside* the old browser path for a
    while: a user who is already signed in through a browser profile has no stored
    credential yet, and must keep working rather than being told to sign in again.
    A composite makes that a property of one small class instead of a rule each
    caller has to remember.

    The distinction it must not blur
    --------------------------------
    "This source has nothing" and "this source exists but is broken" are not the
    same, and only the first should fall through:

    * a provider returning ``None``/empty -- **fall through**, the next source may
      have a credential;
    * a provider raising :class:`~opencsi.errors.OpenCsiError` -- **stop and
      report**, because a browser whose DevTools endpoint refuses the upgrade is
      a diagnosable problem, and silently trying the next source would replace a
      specific message with a vague "not signed in".

    That rule is why the loop remembers the first exception and re-raises it when
    no provider produced a credential: the user sees the real cause, not the
    absence of a fallback.
    """

    def __init__(self, providers: "list[Any]", *, name: str = "composite") -> None:
        self._providers = [p for p in providers if p is not None]
        self.name = name
        #: Which provider supplied the value, for diagnostics.
        self.last_source: str | None = None

    def get_token(self) -> str | None:
        first_error: OpenCsiError | None = None
        for provider in self._providers:
            label = getattr(provider, "name", type(provider).__name__)
            try:
                token = provider.get_token()
            except OpenCsiError as exc:
                if first_error is None:
                    first_error = exc
                continue
            if token:
                self.last_source = label
                return token
        if first_error is not None:
            # Nothing worked, and at least one source reported a real problem.
            # Raising that is more useful than returning None, which the client
            # would render as "not signed in" -- sending the user to sign in
            # again when the actual fault was elsewhere.
            raise first_error
        return None

    def invalidate(self) -> None:
        for provider in self._providers:
            try:
                provider.invalidate()
            except Exception:  # noqa: BLE001 - one provider must not block the rest
                continue

    def refresh(self) -> str | None:
        for provider in self._providers:
            try:
                token = provider.refresh()
            except OpenCsiError:
                continue
            if token:
                self.last_source = getattr(provider, "name", "?")
                return token
        return None

    def status(self) -> CredentialStatus:
        """Report the **first available** provider's status.

        Deliberately not a merge: a merged status would claim an availability
        that no single source has, and its expiry would be meaningless.
        """
        fallback: CredentialStatus | None = None
        for provider in self._providers:
            try:
                status = provider.status()
            except Exception:  # noqa: BLE001 - status is advisory
                continue
            if status.available:
                return status
            if fallback is None:
                fallback = status
        return fallback or CredentialStatus(
            available=False, source=self.name, detail="no credential source is available"
        )

    def describe(self) -> str:
        inner = ", ".join(
            getattr(p, "name", type(p).__name__) for p in self._providers
        )
        return f"credential sources in order: {inner}"

    def __repr__(self) -> str:
        return f"CompositeCredentialProvider({[getattr(p, 'name', '?') for p in self._providers]})"
