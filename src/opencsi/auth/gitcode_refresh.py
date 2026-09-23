"""GitCode refresh-token lifecycle.

Why this module exists
----------------------
A stored GitCode credential has two expiries, and they are an order of magnitude
apart:

* the **openCsiTool** session cookie -- about one hour, renewed over plain HTTP
  from the GitCode credential. Solved, and covered by
  :mod:`opencsi.auth.http_oauth`.
* the **GitCode access token** itself -- documented at ``expires_in = 1296000``,
  which is 15 days. Until this module existed, that expiry was terminal: the
  refresh token was stored and then never used, so the tool would work for a
  fortnight and then require another WeChat scan.

That is the difference between a credential lifetime measured in weeks and one
measured in months, and it is why the stored credential carries a
``refresh_token`` at all.

The endpoint, established by probing rather than guessing
---------------------------------------------------------
``POST https://gitcode.com/oauth/token`` with ``grant_type=refresh_token``.

The evidence, and it is worth recording because the endpoint is not where one
would first look: sending a bogus ``grant_type`` makes the server enumerate its
own supported values --

    grant_type必须为以下值：'authorization_code','refresh_token'

-- and a bogus ``refresh_token`` returns an endpoint-specific
``refresh_token不存在或已过期``. Both were reproduced against production
(``tools/probe_gitcode_refresh.py``).

**The host is ``gitcode.com``, not ``web-api.gitcode.com``.** Those are different
services: ``checkOrAuthorize`` lives on the API host, the token endpoint does not.
Pointing this at the API host would 404 rather than explain itself, which is
exactly the kind of wrong guess that costs an afternoon.

Only two parameters are required -- ``grant_type`` and ``refresh_token``.
``client_id``, ``client_secret`` and ``redirect_uri`` are conditional on
``authorization_code`` and are not sent here.

Failure semantics
-----------------
Four outcomes, deliberately not collapsed into one. ``except Exception ->
LOGIN_REQUIRED`` is the shape this module exists to avoid: it turns a wifi dropout
into "scan the QR code again", which is both wrong and expensive for the user.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from enum import Enum

from ..redaction import scrub_text

#: The token endpoint. On ``gitcode.com`` -- see the module docstring.
GITCODE_TOKEN_URL = "https://gitcode.com/oauth/token"

#: The documented lifetime of an access token: 1296000 s = exactly 15 days.
#: Recorded because the *point* of the refresh token is a lifetime an order of
#: magnitude longer than the session cookie's hour, and this number is the claim
#: being relied on.
GITCODE_ACCESS_TOKEN_SECONDS = 1296000

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT = 20.0

#: Refresh this long before the access token actually expires. A day is generous
#: for something measured in weeks, and it means a machine that was asleep for a
#: fortnight still refreshes on wake rather than after its first failed request.
DEFAULT_REFRESH_MARGIN = 86400.0


class RefreshStatus(str, Enum):
    """What a refresh attempt concluded. Compared by identity, never by string."""

    REFRESHED = "REFRESHED"
    """A new access token was obtained and should replace the stored one."""

    NOT_NEEDED = "NOT_NEEDED"
    """The access token has more life left than the margin allows for."""

    NO_REFRESH_TOKEN = "NO_REFRESH_TOKEN"
    """Nothing to refresh with. The user must sign in again."""

    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    """GitCode refused the refresh token: expired, revoked, or already rotated.

    This is the one outcome that genuinely needs a QR scan, and it is reached
    only on a 4xx that says so -- not on a network failure.
    """

    NETWORK_ERROR = "NETWORK_ERROR"
    """The request did not reach GitCode, or the reply did not arrive."""

    PROTOCOL_ERROR = "PROTOCOL_ERROR"
    """GitCode answered 200 with a body this code does not understand.

    Kept separate from :attr:`LOGIN_REQUIRED` on purpose: a changed response
    shape is a bug in this tool, and reporting it as "sign in again" would have
    the user re-authenticate forever without ever fixing it.
    """


@dataclass(frozen=True, repr=False)
class RefreshResult:
    """A refresh outcome, secret-free.

    The new credentials are carried in :attr:`credential`, which is a
    :class:`~opencsi.auth.store.StoredGitCodeCredential` and therefore redacts
    itself. There is deliberately no ``access_token`` field: a result object with
    a token field is a result object that eventually gets logged.
    """

    status: RefreshStatus
    #: The replacement credential, present only when ``status`` is REFRESHED.
    credential: object | None = None
    #: Seconds of life the new access token has, when the server said.
    expires_in: float | None = None
    #: Whether the refresh token itself changed. Reported because it decides
    #: whether the stored copy is still usable.
    rotated: bool = False
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in (RefreshStatus.REFRESHED, RefreshStatus.NOT_NEEDED)

    @property
    def needs_login(self) -> bool:
        return self.status in (
            RefreshStatus.LOGIN_REQUIRED,
            RefreshStatus.NO_REFRESH_TOKEN,
        )

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "status": self.status.value,
            "ok": self.ok,
            "requires_login": self.needs_login,
        }
        if self.expires_in is not None:
            out["expires_in_seconds"] = round(self.expires_in, 1)
        if self.rotated:
            out["refresh_token_rotated"] = True
        if self.detail:
            out["detail"] = self.detail
        return out

    def __repr__(self) -> str:
        return (
            f"RefreshResult(status={self.status.value}, rotated={self.rotated}, "
            f"expires_in={self.expires_in!r})"
        )


def _classify_http_error(status: int, body: str) -> tuple[RefreshStatus, str]:
    """Map a transport failure onto a status, without guessing.

    The split that matters is 4xx versus everything else:

    * a **4xx** means GitCode rejected *this refresh token*. It is expired,
      revoked, or has already been rotated away -- all of which need a new sign
      in. ``LOGIN_REQUIRED`` is correct.
    * a **5xx** means GitCode is unwell. The refresh token is probably fine, and
      telling the user to scan again would be both wrong and, a minute later,
      visibly wrong. Reported as a network-class failure instead.
    * **status 0** means no response was received at all -- DNS, TLS, timeout, a
      refused connection. Also a network failure, and not a statement about the
      credential. This case is easy to get wrong: 0 is not a 4xx, so a naive
      ``if 400 <= status < 500 ... else PROTOCOL_ERROR`` reports a wifi dropout
      as a changed API response.

    The body is scrubbed before it is quoted: an error response is one of the
    few places a token can arrive without being asked for.
    """
    if status == 0:
        return (
            RefreshStatus.NETWORK_ERROR,
            f"the request did not reach GitCode ({scrub_text(body) or 'no response'})",
        )
    detail = scrub_text(body)[:200] if body else f"HTTP {status}"
    if 500 <= status < 600:
        return RefreshStatus.NETWORK_ERROR, f"GitCode returned HTTP {status}: {detail}"
    if 400 <= status < 500:
        return RefreshStatus.LOGIN_REQUIRED, (
            f"GitCode refused the refresh token (HTTP {status}): {detail}"
        )
    return RefreshStatus.PROTOCOL_ERROR, f"unexpected HTTP {status}: {detail}"


class GitCodeTokenRefresher:
    """Exchange a GitCode refresh token for a new access token.

    Deliberately does **not** touch the store. It is given a credential and
    returns a decision; persisting the replacement belongs to the caller, so that
    a refresh performed for a diagnostic cannot silently rewrite the user's stored
    credentials as a side effect.
    """

    name = "gitcode-refresh"

    def __init__(
        self,
        *,
        token_url: str = GITCODE_TOKEN_URL,
        timeout: float = DEFAULT_TIMEOUT,
        use_proxy: bool = False,
    ) -> None:
        self._token_url = token_url
        self._timeout = timeout
        #: ``ProxyHandler({})`` by default, matching
        #: :class:`~opencsi.auth.http_oauth.HttpOAuthRenewer`: the system proxy on
        #: a real machine was observed failing TLS for the openCsiTool host.
        self._use_proxy = use_proxy
        self._last_status: int | None = None

    @property
    def last_status(self) -> int | None:
        """The HTTP status of the last attempt, for diagnostics."""
        return self._last_status

    def describe(self) -> str:
        return "GitCode refresh_token grant over plain HTTP"

    def needs_refresh(
        self, credential: object, *, margin: float = DEFAULT_REFRESH_MARGIN
    ) -> bool:
        """Whether the credential is close enough to expiry to refresh now.

        An **unknown** expiry is treated as needing a refresh. That is the
        opposite of the choice :class:`~opencsi.auth.session.SessionManager`
        makes for the session cookie, and deliberately so: a session cookie with
        no ``expires`` never expires, whereas an access token with no recorded
        expiry is one whose expiry this tool failed to record -- and refreshing
        costs one request where not refreshing costs a scan.
        """
        expires_at = getattr(credential, "access_expires_at", None)
        if expires_at is None:
            return True
        return (expires_at - time.time()) <= margin

    def refresh(self, credential: object, *, force: bool = False) -> RefreshResult:
        """Attempt a refresh. Never raises; always reports.

        ``force`` skips the expiry check, for ``opencsi login --refresh-gitcode``
        and for the probe, which need the round trip to happen regardless of the
        clock.
        """
        refresh_token = getattr(credential, "refresh_token", None)
        if not refresh_token:
            return RefreshResult(
                RefreshStatus.NO_REFRESH_TOKEN,
                detail=(
                    "no GitCode refresh token is stored, so the access token "
                    "cannot be extended"
                ),
            )

        if not force and not self.needs_refresh(credential):
            remaining = getattr(credential, "access_remaining", None)
            return RefreshResult(
                RefreshStatus.NOT_NEEDED,
                expires_in=remaining,
                detail="the GitCode access token still has life left",
            )

        status, body = self._post(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )
        self._last_status = status

        if status != 200:
            outcome, detail = _classify_http_error(status, body)
            return RefreshResult(outcome, detail=detail)

        return self._parse(body, sent_refresh_token=refresh_token)

    def _post(self, params: dict[str, str]) -> tuple[int, str]:
        """POST and return ``(status, body)``; HTTP errors are returned, not raised.

        ``(0, "")`` means the request never produced a response -- DNS, TLS,
        timeout, a refused connection. The caller maps that to a network failure.
        """
        query = urllib.parse.urlencode(params)
        request = urllib.request.Request(
            f"{self._token_url}?{query}",
            method="POST",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            data=b"",
        )
        if self._use_proxy:
            opener = urllib.request.build_opener()
        else:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=self._timeout) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            # Never the exception's own text: a URL can appear in it, and a URL
            # built from this call contains the refresh token in its query.
            return 0, type(exc).__name__

    def _parse(self, body: str, *, sent_refresh_token: str) -> RefreshResult:
        """Turn a 200 response into a result, or a protocol error."""
        from .store import StoredGitCodeCredential

        try:
            decoded = json.loads(body)
        except json.JSONDecodeError:
            return RefreshResult(
                RefreshStatus.PROTOCOL_ERROR,
                detail="GitCode returned 200 with a body that is not JSON",
            )
        if not isinstance(decoded, dict):
            return RefreshResult(
                RefreshStatus.PROTOCOL_ERROR,
                detail="GitCode returned 200 with a JSON body that is not an object",
            )

        access_token = decoded.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            # A 200 with no access_token is the shape a changed API would take.
            # Reported as a protocol error, never as "sign in again": the user
            # re-authenticating would not fix a parser.
            return RefreshResult(
                RefreshStatus.PROTOCOL_ERROR,
                detail=(
                    "GitCode returned 200 but no access_token; the response "
                    "shape has changed"
                ),
            )

        expires_in = decoded.get("expires_in")
        expires_at = None
        if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
            expires_at = time.time() + float(expires_in)
            expires_in = float(expires_in)
        else:
            expires_in = None

        # Rotation: GitCode does not document whether it reissues the refresh
        # token. Treating it as rotating is correct under both behaviours --
        # overwriting with a fresh value is right when it rotated, and harmless
        # when the server echoed the same one -- so the decision does not depend
        # on the answer. It is still *reported*, because a caller that finds the
        # value unchanged should be able to see that rather than infer it.
        new_refresh = decoded.get("refresh_token")
        if isinstance(new_refresh, str) and new_refresh:
            rotated = new_refresh != sent_refresh_token
            refresh_token = new_refresh
        else:
            rotated = False
            refresh_token = sent_refresh_token

        return RefreshResult(
            RefreshStatus.REFRESHED,
            credential=StoredGitCodeCredential(
                access_token=access_token,
                refresh_token=refresh_token,
                # The token endpoint does not return a username. It is filled in
                # by StoredGitCodeRefresher from the credential being replaced,
                # because dropping it would make `login --status` forget who is
                # signed in after the first refresh.
                username=None,
                access_expires_at=expires_at,
            ),
            expires_in=expires_in,
            rotated=rotated,
        )


class StoredGitCodeRefresher:
    """Refresh the stored GitCode credential and write the replacement back.

    The thin layer that turns :class:`GitCodeTokenRefresher`'s decision into a
    store write, kept separate so the exchange itself has no side effects and can
    be probed safely.

    Rotation is the reason the write-back is not optional. If GitCode issues a new
    refresh token and the old one is kept, the *next* refresh fails and the user
    is sent for a QR scan they did not need -- a failure that appears a fortnight
    after the change that caused it, which is the worst possible time to debug it.
    """

    name = "stored-gitcode-refresher"

    def __init__(self, store: object, *, refresher: GitCodeTokenRefresher | None = None) -> None:
        self._store = store
        self._refresher = refresher or GitCodeTokenRefresher()

    def refresh(self, *, force: bool = False) -> RefreshResult:
        """Refresh and persist. Never raises; always reports."""
        from .store import CredentialStoreError

        try:
            bundle = self._store.load()  # type: ignore[attr-defined]
        except CredentialStoreError as exc:
            return RefreshResult(RefreshStatus.PROTOCOL_ERROR, detail=str(exc))

        credential = bundle.gitcode
        if credential is None:
            return RefreshResult(
                RefreshStatus.NO_REFRESH_TOKEN,
                detail="no GitCode credential is stored",
            )

        result = self._refresher.refresh(credential, force=force)
        if result.status is not RefreshStatus.REFRESHED or result.credential is None:
            return result

        replacement = result.credential
        # Carry the username across: the token endpoint does not return one, and
        # losing it would make `login --status` forget who is signed in after the
        # first refresh.
        if getattr(replacement, "username", None) is None and credential.username:
            from dataclasses import replace as _replace

            replacement = _replace(replacement, username=credential.username)

        try:
            self._store.save_gitcode(replacement)  # type: ignore[attr-defined]
        except CredentialStoreError as exc:
            # The refresh succeeded but could not be stored. Reported as a
            # storage failure rather than a refresh failure: the new token works
            # now, and the problem is that the next process will not have it.
            return RefreshResult(
                RefreshStatus.PROTOCOL_ERROR,
                credential=replacement,
                expires_in=result.expires_in,
                rotated=result.rotated,
                detail=f"the refreshed credential could not be stored: {exc}",
            )
        return result

    def describe(self) -> str:
        return "refresh the stored GitCode credential and persist the replacement"


class RefreshingGitCodeRenewer:
    """Refresh an expiring GitCode credential, then run the openCsiTool renewal.

    Why the ordering lives here
    ---------------------------
    ``HttpOAuthRenewer`` knows how to turn a GitCode credential into an
    openCsiTool session. ``StoredGitCodeRefresher`` knows how to keep the GitCode
    credential itself alive. Neither should learn about the other:
    ``SessionManager`` is explicitly about the openCsiTool session, and putting a
    GitCode concern inside it would make the session layer responsible for a
    credential it never reads.

    So the two are composed here, in a class whose only job is the order.

    The gate that matters
    ---------------------
    The refresh runs **only** when the GitCode access token is actually near
    expiry. ``StoredGitCodeRefresher`` already decides that (``refresh(force=
    False)`` against ``DEFAULT_REFRESH_MARGIN``), and this class must not
    second-guess it into running every time: an openCsiTool session lives about an
    hour while a GitCode token lives fifteen days, so an unconditional refresh
    would spend a fortnight-long credential roughly 360 times for nothing -- and
    with rotation, would replace a perfectly good refresh token every hour.

    Failure policy
    --------------
    Only a genuine refusal stops the renewal:

    * ``REFRESHED`` / ``NOT_NEEDED`` -- proceed;
    * ``NETWORK_ERROR`` -- proceed. The stored access token may still be entirely
      valid, and "GitCode was briefly unreachable" must not become "sign in
      again", which is a QR scan the user did not need;
    * ``LOGIN_REQUIRED`` / ``NO_REFRESH_TOKEN`` -- stop and report, because there
      is genuinely no credential left to spend;
    * ``PROTOCOL_ERROR`` -- proceed, but carry the detail through. A response this
      code does not understand is a bug in this tool, and refusing to renew would
      hide it behind an authentication error.
    """

    name = "stored-oauth"

    def __init__(self, refresher: object, renewer: object) -> None:
        self._refresher = refresher
        self._renewer = renewer
        #: The refresh outcome, for diagnostics. Never holds a token: a
        #: ``RefreshResult`` redacts itself and carries no token field.
        self.last_refresh: RefreshResult | None = None

    @property
    def last_persisted(self) -> object:
        """Forward the inner renewer's persistence verdict.

        Reported so a caller can distinguish "the server issued a session" from
        "the next process will be able to read it" -- the distinction this whole
        round exists to keep.
        """
        return getattr(self._renewer, "last_persisted", None)

    @property
    def last_trace(self) -> object:
        return getattr(self._renewer, "last_trace", None)

    def can_renew(self) -> bool:
        can = getattr(self._renewer, "can_renew", None)
        return bool(can()) if callable(can) else True

    def renew(self, *, timeout: float | None = None, before: object = None):
        """Refresh if needed, then renew. Never raises; always reports."""
        from .session import RenewalResult, RenewalStatus

        refresh = getattr(self._refresher, "refresh", None)
        if callable(refresh):
            try:
                result = refresh(force=False)
            except Exception as exc:  # noqa: BLE001 - a refresh fault must not abort renewal
                self.last_refresh = RefreshResult(
                    RefreshStatus.PROTOCOL_ERROR,
                    detail=f"the GitCode refresh raised {type(exc).__name__}",
                )
            else:
                self.last_refresh = result
                if result.needs_login:
                    return RenewalResult(
                        RenewalStatus.LOGIN_REQUIRED,
                        detail=(
                            "the GitCode credential can no longer be refreshed "
                            f"({result.status.value}); sign in again with "
                            "'opencsi login --qr'"
                        ),
                    )
                # NETWORK_ERROR and PROTOCOL_ERROR deliberately fall through: the
                # stored access token may still work, and finding out is exactly
                # what the renewal attempt below does.

        return self._renewer.renew(timeout=timeout, before=before)

    def describe(self) -> str:
        return (
            "refresh the GitCode credential if it is near expiry, then renew the "
            "openCsiTool session over HTTP"
        )

    def __repr__(self) -> str:
        return "RefreshingGitCodeRenewer(refresher=stored-gitcode-refresher, renewer=http-oauth)"
