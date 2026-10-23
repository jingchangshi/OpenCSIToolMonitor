"""GitCode refresh-token lifecycle.

Why this module exists
----------------------
A stored GitCode credential has two expiries, and they are an order of magnitude
apart:

* the **openCsiTool** session cookie -- about one hour, renewed over plain HTTP
  from the GitCode credential. Solved, and covered by
  :mod:`opencsi.auth.http_oauth`.
* the **GitCode access token** itself -- the QR login's own credential. When it
  dies, either the refresh token extends the login or the user scans a QR code
  again. That difference is why the stored credential carries a
  ``refresh_token`` at all.

Which endpoint the QR credential actually belongs to
----------------------------------------------------
The endpoint below was **not** guessed, and the first attempt was wrong in a way
worth recording. ``POST https://gitcode.com/oauth/token`` with
``grant_type=refresh_token`` is a *real* endpoint -- a bogus ``grant_type`` made
it enumerate its own supported values -- but it serves GitCode's **OAuth
application** token system (the ``authorization_code`` grant a third-party app
exchanges). A credential issued by the WeChat-QR login is refused there with
HTTP 400 ``refresh_token不存在或已过期`` -- the same answer a fabricated token
gets -- because it simply does not exist in that system. Three independent lines
of evidence establish the family the QR credential *does* belong to:

1. **GitCode's own frontend** (current production bundle) renews its token with::

       POST https://web-api.gitcode.com/uc/api/v1/user/token/refresh
       Authorization: Bearer <access_token>
       Content-Type: application/x-www-form-urlencoded
       body: refresh_token=<refresh_token>

   and on 200 stores the returned ``access_token`` / ``refresh_token`` over the
   old ones. It never calls ``gitcode.com/oauth/token`` anywhere in the bundle.
2. A no-credential probe of that endpoint answers a fabricated token with its
   own error family -- HTTP 400 ``error_code 400004 REFRESH_TOKEN_ERROR`` --
   distinct from the generic ``BAD_REQUEST`` the OAuth-application endpoint
   produces.
3. The QR login's tokens are used exactly the way that endpoint expects: the
   frontend stores them in ``localStorage`` and sends them as
   ``Authorization: Bearer`` against ``web-api.gitcode.com``.

The QR flow therefore mints a **GitCode account token**, and that is the system
this module talks to.

What is *not* claimed: that a live ``A1/R1 -> A2/R2`` exchange has been observed
on a real account. It has not -- the exchange rotates the refresh token, so
running it is an explicit opt-in (``tools/probe_gitcode_token_refresh.py``).
Everything up to that request's shape is verified; the exchange itself is the
acceptance step that remains.

Failure semantics
-----------------
Four outcomes, deliberately not collapsed into one. ``except Exception ->
LOGIN_REQUIRED`` is the shape this module exists to avoid: it turns a wifi
dropout into "scan the QR code again", which is both wrong and expensive for the
user.

The 4xx split is the part error classification gets wrong most easily, so it is
explicit: **only** the endpoint's own credential-refusal answer (401, or 400
carrying ``error_code 400004 REFRESH_TOKEN_ERROR``) means the refresh token is
dead. Any *other* 4xx is a shape this code does not recognise -- a changed API,
a WAF page, a proxy -- and is reported as a protocol error rather than as "sign
in again", because re-authenticating does not fix a parser.
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

#: The account-token refresh endpoint. On ``web-api.gitcode.com`` -- the same
#: host the QR login itself runs on -- with the ``/uc`` prefix the frontend's
#: axios interceptor adds. See the module docstring for why this is *not*
#: ``gitcode.com/oauth/token``: that host serves the OAuth-application token
#: system, which the QR credential is not part of.
GITCODE_TOKEN_URL = "https://web-api.gitcode.com/uc/api/v1/user/token/refresh"

#: The endpoint's own refusal code for an unusable token. Observed on the wire
#: against a fabricated value; treated as the one 400 shape that means "this
#: refresh token is dead" rather than "this request is malformed".
REFRESH_TOKEN_ERROR_CODE = 400004

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT = 20.0

#: Refresh this long before the access token actually expires -- *when the
#: expiry is known*. The QR response carries no ``expires_in``, so for a
#: QR-issued credential the expiry is normally unknown and the policy that
#: applies is the demand-driven one described on :meth:`GitCodeTokenRefresher.
#: needs_refresh`, not a margin.
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
    only on the endpoint's own credential-refusal answer -- 401, or 400 with
    ``error_code 400004`` -- never on a network failure or an unrecognised
    response.
    """

    NETWORK_ERROR = "NETWORK_ERROR"
    """The request did not reach GitCode, was throttled, or GitCode is unwell.

    429 and 5xx land here on purpose: both say "try again later" and neither
    says anything about the credential.
    """

    PROTOCOL_ERROR = "PROTOCOL_ERROR"
    """GitCode answered in a shape this code does not understand.

    Kept separate from :attr:`LOGIN_REQUIRED` on purpose: a changed response
    shape, an unexpected 4xx, or a WAF interstitial is a bug in this tool or an
    environment problem, and reporting it as "sign in again" would have the user
    re-authenticate forever without ever fixing it.
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


def _refusal_code(body: str) -> int | None:
    """The endpoint's ``error_code`` from an error body, when parseable."""
    try:
        decoded = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(decoded, dict):
        return None
    code = decoded.get("error_code")
    if isinstance(code, bool) or not isinstance(code, int):
        return None
    return code


def _classify_http_error(status: int, body: str) -> tuple[RefreshStatus, str]:
    """Map a transport failure onto a status, without guessing.

    The split that matters is "GitCode refused *this credential*" versus
    everything else:

    * **401**, or a **400** carrying the endpoint's own ``400004
      REFRESH_TOKEN_ERROR`` code, means the refresh token was looked at and
      rejected -- expired, revoked, or already rotated. ``LOGIN_REQUIRED`` is
      correct, and it is reached *only* here.
    * **any other 4xx** is a response this code does not recognise: a changed
      API, an unfamiliar validation message, a WAF interstitial. Reported as a
      protocol error. The earlier behaviour -- every 4xx to LOGIN_REQUIRED --
      sent users to re-scan for problems a re-scan cannot fix, and the real
      endpoint's answer to a *bogus* token shows the refusal has a specific,
      recognisable shape, so anything else is not a refusal.
    * **429** is throttling: try later, the credential is fine. Grouped with
      the network-class failures rather than given its own status, because the
      caller's correct action is identical.
    * **5xx** means GitCode is unwell -- same handling.
    * **status 0** means no response was received at all -- DNS, TLS, timeout,
      a refused connection. Also a network failure, and not a statement about
      the credential. This case is easy to get wrong: 0 is not a 4xx, so a naive
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
    if status == 429:
        return RefreshStatus.NETWORK_ERROR, (
            f"GitCode throttled the refresh (HTTP 429); try again later: {detail}"
        )
    if status == 418:
        # CloudWAF's interstitial page. The credential was not looked at, so
        # this must not read as "sign in again" -- and the remedy is headers,
        # not a QR scan (see the Origin/Referer note in GitCodeTokenRefresher).
        return RefreshStatus.PROTOCOL_ERROR, (
            "GitCode's edge WAF (CloudWAF) intercepted the request (HTTP 418) "
            f"before the credential was evaluated: {detail}"
        )
    if status == 401:
        return RefreshStatus.LOGIN_REQUIRED, (
            f"GitCode refused the refresh token (HTTP 401): {detail}"
        )
    if 400 <= status < 500:
        if _refusal_code(body) == REFRESH_TOKEN_ERROR_CODE:
            return RefreshStatus.LOGIN_REQUIRED, (
                f"GitCode refused the refresh token (HTTP {status}): {detail}"
            )
        return RefreshStatus.PROTOCOL_ERROR, (
            f"GitCode answered HTTP {status} with an unrecognised body: {detail}"
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
        return "GitCode account-token refresh over plain HTTP"

    def needs_refresh(
        self, credential: object, *, margin: float = DEFAULT_REFRESH_MARGIN
    ) -> bool:
        """Whether the credential is close enough to expiry to refresh now.

        An **unknown** expiry is *not* refreshed proactively. That is the
        demand-driven behaviour GitCode's own client implements -- it refreshes
        when an API call comes back 401 and then retries the call, and never
        refreshes on a schedule -- and it is the evidence-backed choice here too.
        The QR login response carries no ``expires_in``, so "unknown" is the
        normal state of a QR credential; refreshing it on every renewal cycle
        would spend one request per hour against a credential whose state the
        response does not report, for a problem that the demand-driven retry in
        :class:`RefreshingGitCodeRenewer` handles at the moment it actually
        exists. A *known* expiry inside ``margin`` is refreshed proactively, and
        ``force=True`` refreshes regardless -- probes and explicit user actions
        still get a round trip on request.
        """
        expires_at = getattr(credential, "access_expires_at", None)
        if expires_at is None:
            return False
        return (expires_at - time.time()) <= margin

    def refresh(self, credential: object, *, force: bool = False) -> RefreshResult:
        """Attempt a refresh. Never raises; always reports.

        ``force`` skips the expiry check -- for the recovery retry in
        :class:`RefreshingGitCodeRenewer` and for the probe, which need the
        round trip to happen regardless of the clock.
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

        status, body = self._post(credential, refresh_token)
        self._last_status = status

        if status != 200:
            outcome, detail = _classify_http_error(status, body)
            return RefreshResult(outcome, detail=detail)

        return self._parse(body, sent_refresh_token=refresh_token)

    def _post(self, credential: object, refresh_token: str) -> tuple[int, str]:
        """POST and return ``(status, body)``; HTTP errors are returned, not raised.

        ``(0, "")`` means the request never produced a response -- DNS, TLS,
        timeout, a refused connection. The caller maps that to a network failure.

        The request shape is the frontend's, verbatim in every field that could
        matter: Bearer credential in the ``Authorization`` header, the refresh
        token as the urlencoded body, JSON accepted. Unlike the OAuth-application
        endpoint, nothing travels in the query string -- which also keeps the
        refresh token out of any URL a proxy could log.
        """
        access_token = getattr(credential, "access_token", None)
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            # Huawei CloudWAF answered HTTP 418 to a header-minimal POST during
            # the 2026-10-24 audit (the credential was never evaluated); the
            # same request with these two headers got the clean API answer.
            # That is the same pattern docs/gitcode-qr-protocol.md §9.4 records
            # for the QR flow, and the browser always sends them on a
            # cross-site POST, so they cost nothing and unblock the endpoint.
            "Origin": "https://gitcode.com",
            "Referer": "https://gitcode.com/",
        }
        if isinstance(access_token, str) and access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        request = urllib.request.Request(
            self._token_url,
            method="POST",
            headers=headers,
            data=urllib.parse.urlencode({"refresh_token": refresh_token}).encode("ascii"),
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
            # Never the exception's own text: an error message could echo parts
            # of the request, and the request body carries the refresh token.
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
        if isinstance(decoded, dict) and isinstance(decoded.get("data"), dict):
            # The frontend reads the fields straight off the body; an envelope
            # is accepted too so a future wrapper does not become a "dead
            # credential" for users who never needed to re-scan.
            decoded = decoded["data"]
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

    The proactive gate
    ------------------
    The pre-renewal refresh runs **only** when the GitCode access token's expiry
    is *known* and near. ``StoredGitCodeRefresher`` already decides that
    (``refresh(force=False)`` against ``DEFAULT_REFRESH_MARGIN``), and this class
    must not second-guess it into running every time: an openCsiTool session
    lives about an hour, and a QR-issued GitCode credential carries no recorded
    expiry, so an unconditional pre-refresh would spend a request every hour on
    a credential whose state the answer does not report.

    Failure policy
    --------------
    The refresh result never ends the renewal on its own. The storage token and the
    GitCode access token are separate credentials, and refusing to extend one says
    nothing about whether the other still works -- only the OAuth leg can answer
    that, so it is always attempted:

    * ``REFRESHED`` / ``NOT_NEEDED`` -- proceed with the current credential;
    * ``NETWORK_ERROR`` -- proceed. A briefly unreachable GitCode must not become a
      QR scan the user did not need;
    * ``PROTOCOL_ERROR`` -- proceed, carrying the detail. A response this code does
      not understand is a bug in this tool, and refusing to renew would hide it
      behind an authentication error;
    * ``LOGIN_REQUIRED`` / ``NO_REFRESH_TOKEN`` -- proceed as well, and report
      ``LOGIN_REQUIRED`` only if the OAuth leg *also* fails. A live run proved the
      original ordering wrong: it returned LOGIN_REQUIRED while the stored session
      had 56 minutes of life left, because a refused GitCode refresh was treated as
      final.

    The demand-driven recovery
    --------------------------
    When the OAuth leg fails with ``LOGIN_REQUIRED`` -- which is what an expired
    GitCode access token looks like: ``checkOrAuthorize`` answers 401 and the
    session probe agrees -- and the pre-refresh did not just run, the refresh is
    attempted once here and the renewal retried on success. This is the behaviour
    GitCode's own frontend implements (an API call comes back 401, the client
    refreshes the token, the call is retried), and it is the moment the refresh
    is actually needed rather than a scheduled guess. One attempt per renewal:
    a refresh token the server refuses is not asked for twice, and a refresh that
    succeeded but still left the leg failing is reported with both facts.
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
        refresh_failed_hard = False
        refreshed_this_attempt = False
        if callable(refresh):
            try:
                result = refresh(force=False)
            except Exception as exc:  # noqa: BLE001 - a refresh fault must not abort renewal
                result = RefreshResult(
                    RefreshStatus.PROTOCOL_ERROR,
                    detail=f"the GitCode refresh raised {type(exc).__name__}",
                )
            self.last_refresh = result
            refresh_failed_hard = result.needs_login
            refreshed_this_attempt = result.status is RefreshStatus.REFRESHED

        outcome = self._renewer.renew(timeout=timeout, before=before)

        recovery_ran = False
        if (
            outcome.status is RenewalStatus.LOGIN_REQUIRED
            and callable(refresh)
            and not refreshed_this_attempt
            and not refresh_failed_hard
        ):
            # The GitCode session the OAuth leg saw is gone and the proactive
            # refresh had no reason to run. This is the moment a refresh is
            # actually called for: force one, and retry the leg exactly once if
            # a new credential landed.
            try:
                recovery = refresh(force=True)
            except Exception as exc:  # noqa: BLE001 - a refresh fault must not abort renewal
                recovery = RefreshResult(
                    RefreshStatus.PROTOCOL_ERROR,
                    detail=f"the GitCode refresh raised {type(exc).__name__}",
                )
            self.last_refresh = recovery
            recovery_ran = True
            if recovery.status is RefreshStatus.REFRESHED:
                outcome = self._renewer.renew(timeout=timeout, before=before)

        if outcome.status is not RenewalStatus.LOGIN_REQUIRED:
            return outcome

        refused = self.last_refresh is not None and self.last_refresh.needs_login
        if refused:
            # Both halves are gone: the session could not be renewed *and* the
            # GitCode credential cannot be extended. Only now is a scan the honest
            # answer -- and both details are included, because "sign in again"
            # without saying why each half failed is the kind of message that
            # produces a support ticket instead of a fix.
            detail = (
                "the openCsiTool session could not be renewed and the GitCode "
                f"credential can no longer be refreshed "
                f"({self.last_refresh.status.value if self.last_refresh else '?'})"
            )
            if outcome.detail:
                detail = f"{outcome.detail}; {detail}"
            if self.last_refresh and self.last_refresh.detail:
                detail = f"{detail}: {self.last_refresh.detail}"
            return RenewalResult(RenewalStatus.LOGIN_REQUIRED, detail=detail)

        if recovery_ran and self.last_refresh is not None and self.last_refresh.ok:
            # A refresh succeeded but the leg still refused. The leg's own
            # detail stands; the note is appended so the report carries both
            # facts rather than implying nothing was tried.
            note = (
                "a refreshed GitCode credential was obtained, but the openCsiTool "
                "session was still refused"
            )
            return RenewalResult(
                RenewalStatus.LOGIN_REQUIRED,
                detail=f"{outcome.detail or 'the session was refused'}; {note}",
            )

        return outcome

    def describe(self) -> str:
        return (
            "refresh the GitCode credential if it is near expiry, renew the "
            "openCsiTool session over HTTP, and on a refused GitCode session "
            "refresh once and retry"
        )

    def __repr__(self) -> str:
        return "RefreshingGitCodeRenewer(refresher=stored-gitcode-refresher, renewer=http-oauth)"
