"""Silent OAuth session renewal over the DevTools Protocol.

The problem this solves
-----------------------
The openCsiTool ``token`` cookie lives about 58 minutes. When it expires, the
*browser's* copy expires too, so re-reading the browser
(:meth:`CdpCookieProvider.refresh`) returns the same dead value. Reload is not
renewal.

What does work: the GitCode SSO session in the dedicated browser profile usually
outlives the openCsiTool cookie by a long way. So navigating a *background* tab
to the OAuth entry point makes GitCode re-issue an authorization code
automatically, openCsiTool exchanges it, and a fresh ``token`` cookie lands in
the browser. The user sees nothing.

    openCsiTool token nearing expiry
              │
              ▼
    Target.createTarget (background tab)
              │
              ▼
    Page.navigate  /rest/v1/oauth2/authorization/gitcode
              │
              ├─ GitCode SSO still valid → 302 → callback → new token cookie
              └─ GitCode SSO gone        → login page → LOGIN_REQUIRED
              │
              ▼
    Network.getCookies → compare with the old value
              │
              ▼
    Target.closeTarget

Design constraints
------------------
* **Pure CDP.** No DOM scraping, no selectors, no Playwright/Selenium. The
  browser is navigated and its cookie jar is read; nothing is clicked.
* **Never disturb the user.** Renewal runs in a *new background target*, not in
  a tab the user is looking at, and the target is closed afterwards. The
  foreground tab is never navigated.
* **Proof, not hope.** ``Page.loadEventFired`` is not success. A renewal counts
  only when the cookie value actually changed *and* its expiry moved later.
  The caller additionally validates the session with ``getUserInfo``.
* **Bounded.** Every wait has a deadline; the whole attempt has a timeout.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote

from ..errors import CdpUnavailableError, OpenCsiError
from ..redaction import register_secret, scrub_text
from ..ws import CdpConnection, WebSocketError
from .base import CredentialProvider, CredentialStatus
from .cdp import (
    COOKIE_DOMAINS,
    COOKIE_NAME,
    CdpCookieProvider,
    CdpEndpoint,
    discover_cdp_endpoint,
    select_token_cookie,
)
from .session import RenewalResult, RenewalStatus

log = logging.getLogger("opencsi.auth.oauth")

#: The OAuth entry point. A GET here redirects to GitCode's authorize endpoint;
#: when the GitCode SSO session is alive it redirects straight back to the
#: openCsiTool callback, which sets a new ``token`` cookie.
OAUTH_PATH = "/opencsitool/rest/v1/oauth2/authorization/gitcode"

#: Where to land afterwards. The SPA's own default; keeps the tab meaningful if
#: a user ever does look at it.
OAUTH_REDIRECT = "/myTools"

#: How long to wait for the redirect chain to finish and the cookie to settle.
DEFAULT_TIMEOUT = 45.0

#: Poll cadence while waiting for the cookie to change.
_POLL_INTERVAL = 0.5

#: Give the callback a moment to write the cookie after the load event.
_SETTLE_DELAY = 1.5

#: A host that must never be treated as "still on GitCode": once we are back
#: here, the OAuth round-trip has completed.
_APP_HOST = "opencsitool.com"
_LOGIN_HOST = "gitcode.com"


@dataclass(frozen=True)
class RenewalEvidence:
    """What the renewal actually observed (secret-free)."""

    navigated_url_host: str
    final_url_host: str
    landed_on_login_page: bool
    token_changed: bool
    expiry_extended: bool
    old_expires_in: float | None
    new_expires_in: float | None
    detail: str | None = None

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "navigated_host": self.navigated_url_host,
            "final_host": self.final_url_host,
            "landed_on_login_page": self.landed_on_login_page,
            "token_changed": self.token_changed,
            "expiry_extended": self.expiry_extended,
        }
        if self.old_expires_in is not None:
            out["old_expires_in_seconds"] = round(self.old_expires_in, 1)
        if self.new_expires_in is not None:
            out["new_expires_in_seconds"] = round(self.new_expires_in, 1)
        if self.detail:
            out["detail"] = self.detail
        return out


def _host_of(url: str) -> str:
    """Host of ``url`` without parsing a query string into a log line."""
    if not url:
        return ""
    rest = url.split("://", 1)[-1]
    authority = rest.split("/", 1)[0]
    return authority.split(":", 1)[0].split("@")[-1].lower()


def _path_of(url: str) -> str:
    """Path of ``url``, with the query string dropped.

    The query is deliberately discarded rather than returned: on the OAuth
    callback it carries ``code`` and ``state``, which are secrets.
    """
    if not url:
        return ""
    rest = url.split("://", 1)[-1]
    if "/" not in rest:
        return "/"
    return "/" + rest.split("/", 1)[1].split("?", 1)[0].split("#", 1)[0]


#: Path prefixes that mean "GitCode is asking the human to authenticate".
#: Matching the *host* alone would be wrong: ``gitcode.com/oauth/authorize`` is
#: the authorize endpoint (which a live SSO session sails straight through),
#: whereas ``gitcode.com/-/oauth/login`` and ``gitcode.com/login`` are the pages
#: that only appear when the SSO session is gone.
_LOGIN_PATH_PREFIXES = ("/login", "/-/oauth/login", "/-/login")


class BrowserOAuthRenewer:
    """Renew the openCsiTool session by re-running OAuth in a background tab.

    Parameters
    ----------
    cdp_url:
        Explicit DevTools endpoint; otherwise discovery follows the same order
        as :class:`~opencsi.auth.cdp.CdpCookieProvider`.
    base_url:
        The openCsiTool origin to build the OAuth URL from.
    timeout:
        Total budget for one renewal attempt.
    ports:
        Ports probed during discovery.
    """

    name = "browser-oauth"

    def __init__(
        self,
        cdp_url: str | None = None,
        *,
        base_url: str = "https://opencsitool.com",
        timeout: float = DEFAULT_TIMEOUT,
        ports: Sequence[int] | None = None,
        connect_timeout: float = 15.0,
        poll_interval: float = _POLL_INTERVAL,
        settle_delay: float = _SETTLE_DELAY,
    ) -> None:
        self._explicit = cdp_url
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._ports = tuple(ports) if ports else None
        self._connect_timeout = connect_timeout
        # Poll cadence and the post-load settle wait are injectable so the test
        # suite does not have to spend real seconds per renewal. Production
        # values are the module constants.
        self._poll_interval = poll_interval
        self._settle_delay = settle_delay
        self._endpoint: CdpEndpoint | None = None
        self._last_evidence: RenewalEvidence | None = None

    # ── public API ────────────────────────────────────────────────────────
    def oauth_url(self) -> str:
        """The URL whose navigation triggers the OAuth round-trip."""
        return (
            f"{self._base_url}{OAUTH_PATH}"
            f"?redirect={quote(OAUTH_REDIRECT, safe='')}"
        )

    def can_renew(self) -> bool:
        """Whether a DevTools endpoint is reachable right now."""
        try:
            self._resolve_endpoint()
            return True
        except OpenCsiError:
            return False

    def describe(self) -> str:
        return "silent GitCode OAuth in a background browser tab (CDP)"

    @property
    def last_evidence(self) -> RenewalEvidence | None:
        return self._last_evidence

    def renew(
        self,
        *,
        timeout: float | None = None,
        before: CredentialProvider | None = None,
    ) -> RenewalResult:
        """Run one silent renewal attempt.

        ``before`` supplies the credential value to compare against; the caller
        normally passes its own provider so the comparison is against the exact
        value that was in use when the 401 happened.
        """
        budget = self._timeout if timeout is None else timeout
        deadline = time.monotonic() + max(1.0, budget)

        old_status = self._safe_status(before)
        old_token = self._safe_token(before)

        try:
            endpoint = self._resolve_endpoint()
        except OpenCsiError as exc:
            self._last_evidence = None
            return RenewalResult(
                RenewalStatus.CDP_UNAVAILABLE,
                detail=scrub_text(str(exc))[:300],
            )

        browser_ws = endpoint.browser_ws_url()
        if not browser_ws:
            return RenewalResult(
                RenewalStatus.CDP_UNAVAILABLE,
                detail=(
                    "the DevTools endpoint exposes no browser-level WebSocket, "
                    "which silent renewal needs to create a background tab"
                ),
            )
        if "/devtools/page/" in browser_ws:
            # A page-level socket cannot call Target.createTarget. Saying so
            # plainly beats a WebSocket error from an unsupported method.
            return RenewalResult(
                RenewalStatus.CDP_UNAVAILABLE,
                detail=(
                    "the configured CDP URL is a page-level socket; silent "
                    "renewal needs the browser-level WebSocket (from "
                    "/json/version) so it can open a background tab"
                ),
            )

        target_id: str | None = None
        try:
            with CdpConnection(browser_ws, timeout=self._connect_timeout) as conn:
                target_id = self._create_background_target(conn)
                try:
                    final_host, final_path, timed_out = self._drive_oauth(
                        conn, target_id, deadline
                    )
                    cookies = self._read_cookies(conn)
                finally:
                    self._close_target(conn, target_id)
                    target_id = None
        except (WebSocketError, OpenCsiError) as exc:
            return RenewalResult(
                RenewalStatus.OAUTH_FAILED,
                detail=f"{type(exc).__name__}: {scrub_text(str(exc))[:240]}",
            )

        if timed_out:
            # The redirect chain never settled. Whatever cookie is in the jar is
            # the *old* one -- reporting ALREADY_VALID here would tell the caller
            # its session is fine when nothing was actually verified.
            return RenewalResult(
                RenewalStatus.TIMEOUT,
                detail=(
                    "the OAuth round-trip did not finish inside the budget; the "
                    "browser may be slow or GitCode may be unreachable"
                ),
            )

        return self._evaluate(final_host, final_path, cookies, old_token, old_status)

    # ── target lifecycle ──────────────────────────────────────────────────
    def _create_background_target(self, conn: CdpConnection) -> str:
        """Create a background tab and return its target id.

        ``background: true`` asks Chromium not to focus the new tab, so the
        user's current page keeps focus and stays visible. If a build ignores
        the flag we still never navigate the *existing* tab, so the worst case
        is a new tab appearing -- not the user's page being hijacked.
        """
        result = conn.call(
            "Target.createTarget",
            {"url": "about:blank", "background": True},
            timeout=self._connect_timeout,
        )
        target_id = result.get("targetId")
        if not target_id:
            raise CdpUnavailableError(
                "DevTools created no target for silent renewal"
            )
        return str(target_id)

    @staticmethod
    def _close_target(conn: CdpConnection, target_id: str | None) -> None:
        """Best-effort close; a failure here must not fail the renewal."""
        if not target_id:
            return
        try:
            conn.call("Target.closeTarget", {"targetId": target_id}, timeout=5.0)
        except Exception:  # noqa: BLE001 - cleanup is never fatal
            log.debug("could not close the renewal target; it will be GC'd")

    def _drive_oauth(
        self, conn: CdpConnection, target_id: str, deadline: float
    ) -> tuple[str, str, bool]:
        """Navigate the background tab and wait for the redirect chain.

        Returns ``(final_host, final_path, timed_out)``. The full URL is
        deliberately not returned: on the OAuth callback it carries ``code`` and
        ``state``, which are secrets.
        """
        attached = conn.call(
            "Target.attachToTarget",
            {"targetId": target_id, "flatten": True},
            timeout=self._connect_timeout,
        )
        session_id = attached.get("sessionId")
        if not session_id:
            raise CdpUnavailableError("could not attach to the renewal target")
        session_id = str(session_id)

        try:
            conn.call("Page.enable", session_id=session_id, timeout=self._connect_timeout)
        except WebSocketError:
            pass  # navigation still works; we just lose the load event

        url = self.oauth_url()
        conn.call(
            "Page.navigate",
            {"url": url},
            session_id=session_id,
            timeout=self._connect_timeout,
        )

        final_host = ""
        final_path = ""
        timed_out = True
        while time.monotonic() < deadline:
            time.sleep(self._poll_interval)
            final_host, final_path, landed_on_login = self._current_location(conn, session_id)
            if landed_on_login:
                # GitCode is asking the user to authenticate: stop early rather
                # than burning the whole budget.
                timed_out = False
                break
            if final_host == _APP_HOST:
                # We are back on the app; the callback has run. Give the
                # Set-Cookie a moment to be committed before reading it.
                time.sleep(self._settle_delay)
                final_host, final_path, _ = self._current_location(conn, session_id)
                timed_out = False
                break

        return final_host, final_path, timed_out

    def _current_location(
        self, conn: CdpConnection, session_id: str
    ) -> tuple[str, str, bool]:
        """Return ``(host, path, landed_on_login_page)`` for the renewal tab."""
        try:
            result = conn.call(
                "Runtime.evaluate",
                {"expression": "location.href", "returnByValue": True},
                session_id=session_id,
                timeout=self._connect_timeout,
            )
        except WebSocketError:
            return "", "", False
        value = (result.get("result") or {}).get("value")
        if not isinstance(value, str):
            return "", "", False
        host = _host_of(value)
        path = _path_of(value)
        return host, path, self._looks_like_login_page(host, path)

    @staticmethod
    def _looks_like_login_page(host: str, path: str = "") -> bool:
        """Whether the tab is sitting on a GitCode *login* page.

        Only the host and path shape are considered -- never page content, which
        could contain a QR ticket. The distinction matters: a tab parked on
        ``gitcode.com/oauth/authorize`` may simply still be redirecting, but a
        tab on ``gitcode.com/login`` means GitCode wants a human and waiting
        longer cannot help.
        """
        if not host.endswith(_LOGIN_HOST):
            return False
        return any(path.startswith(prefix) for prefix in _LOGIN_PATH_PREFIXES)

    # ── cookie handling ───────────────────────────────────────────────────
    def _read_cookies(self, conn: CdpConnection) -> list[Mapping[str, Any]]:
        """Read the openCsiTool cookie from the browser-level store."""
        try:
            result = conn.call("Storage.getCookies", timeout=self._connect_timeout)
            cookies = result.get("cookies")
            if isinstance(cookies, list) and cookies:
                return [c for c in cookies if isinstance(c, Mapping)]
        except WebSocketError:
            pass

        # Fall back to a page session on any open tab.
        targets = conn.call("Target.getTargets", timeout=self._connect_timeout)
        for info in targets.get("targetInfos") or []:
            if not isinstance(info, Mapping) or info.get("type") != "page":
                continue
            try:
                attached = conn.call(
                    "Target.attachToTarget",
                    {"targetId": info.get("targetId"), "flatten": True},
                    timeout=self._connect_timeout,
                )
                session_id = attached.get("sessionId")
                if not session_id:
                    continue
                result = conn.call(
                    "Network.getCookies",
                    {"urls": [f"{self._base_url}/"]},
                    session_id=str(session_id),
                    timeout=self._connect_timeout,
                )
                cookies = result.get("cookies")
                if isinstance(cookies, list) and cookies:
                    return [c for c in cookies if isinstance(c, Mapping)]
            except (WebSocketError, OpenCsiError):
                continue
        return []

    # ── verification ──────────────────────────────────────────────────────
    def _evaluate(
        self,
        final_host: str,
        final_path: str,
        cookies: Sequence[Mapping[str, Any]],
        old_token: str | None,
        old_status: CredentialStatus | None,
    ) -> RenewalResult:
        """Decide the outcome from observed state, not from hope."""
        landed_on_login = self._looks_like_login_page(final_host, final_path)
        cookie = select_token_cookie(cookies)

        if landed_on_login:
            # GitCode sent us to a login page. This is checked *before* the
            # cookie, because the jar still holds the old (expired) cookie in
            # this case -- and treating "a cookie exists" as success would
            # report a dead session as healthy, which is precisely the class of
            # mistake this module exists to prevent.
            evidence = RenewalEvidence(
                navigated_url_host=_APP_HOST,
                final_url_host=final_host,
                landed_on_login_page=True,
                token_changed=False,
                expiry_extended=False,
                old_expires_in=old_status.expires_in if old_status else None,
                new_expires_in=None,
                detail=(
                    "GitCode asked for a login, so the long-lived SSO session "
                    "is gone; the user must sign in again"
                ),
            )
            self._last_evidence = evidence
            return RenewalResult(
                RenewalStatus.LOGIN_REQUIRED,
                requires_interaction=True,
                detail=evidence.detail,
            )

        if cookie is None:
            self._last_evidence = None
            return RenewalResult(
                RenewalStatus.OAUTH_FAILED,
                detail=(
                    "the OAuth round-trip finished but the browser still holds no "
                    "openCsiTool token cookie"
                ),
            )

        new_token = str(cookie.get("value") or "")
        if not new_token:
            return RenewalResult(
                RenewalStatus.OAUTH_FAILED,
                detail="the OAuth round-trip produced an empty token cookie",
            )

        new_expires_at = self._expiry(cookie)
        new_expires_in = (
            new_expires_at - time.time() if new_expires_at is not None else None
        )
        old_expires_in = old_status.expires_in if old_status is not None else None

        token_changed = bool(old_token) and new_token != old_token
        # A server-issued cookie whose expiry moved later is proof of a *new*
        # session even if the value happened to repeat.
        expiry_extended = (
            new_expires_at is not None
            and old_status is not None
            and old_status.expires_at is not None
            and new_expires_at > old_status.expires_at + 1.0
        )

        evidence = RenewalEvidence(
            navigated_url_host=_APP_HOST,
            final_url_host=final_host,
            landed_on_login_page=self._looks_like_login_page(final_host, final_path),
            token_changed=token_changed,
            expiry_extended=expiry_extended,
            old_expires_in=old_expires_in,
            new_expires_in=new_expires_in,
        )
        self._last_evidence = evidence

        if not token_changed and not expiry_extended:
            # The navigation completed but nothing about the session moved.
            # Reporting success here would be the exact "loadEventFired means
            # authenticated" mistake this module exists to avoid.
            return RenewalResult(
                RenewalStatus.ALREADY_VALID,
                detail=(
                    "the OAuth round-trip returned the same cookie with no later "
                    "expiry; the session was not extended"
                ),
                expires_in=new_expires_in,
            )

        # Register the new value for redaction immediately: it is now live and
        # may appear in a later error message.
        register_secret(new_token)
        return RenewalResult(
            RenewalStatus.RENEWED,
            renewed=True,
            token_changed=token_changed,
            expires_in=new_expires_in,
            detail=(
                "a new openCsiTool token cookie was issued by the GitCode "
                "OAuth round-trip"
            ),
        )

    @staticmethod
    def _expiry(cookie: Mapping[str, Any]) -> float | None:
        try:
            value = float(cookie.get("expires") or 0.0)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    # ── helpers ───────────────────────────────────────────────────────────
    def _resolve_endpoint(self) -> CdpEndpoint:
        if self._endpoint is None:
            self._endpoint = discover_cdp_endpoint(
                self._explicit, ports=self._ports or (9222, 9223, 9224)
            )
        return self._endpoint

    @staticmethod
    def _safe_token(provider: CredentialProvider | None) -> str | None:
        """The caller's current token, without perturbing it.

        ``peek_token()`` is used when available: ``get_token()`` may re-read the
        browser, and a before/after comparison must not itself change the
        "before".
        """
        if provider is None:
            return None
        try:
            peek = getattr(provider, "peek_token", None)
            return peek() if callable(peek) else provider.get_token()
        except OpenCsiError:
            return None

    @staticmethod
    def _safe_status(provider: CredentialProvider | None) -> CredentialStatus | None:
        if provider is None:
            return None
        try:
            return provider.status()
        except Exception:  # noqa: BLE001 - status is advisory
            return None

    def __repr__(self) -> str:
        return (
            f"BrowserOAuthRenewer(base_url={self._base_url!r}, "
            f"timeout={self._timeout!r}, token=<redacted>)"
        )


def make_cdp_renewer(
    provider: CdpCookieProvider,
    *,
    base_url: str = "https://opencsitool.com",
    timeout: float = DEFAULT_TIMEOUT,
) -> BrowserOAuthRenewer:
    """Build a renewer that shares ``provider``'s endpoint choice.

    Keeping the endpoint decision in one place means ``--cdp`` and
    ``$OPENCSI_CDP_URL`` cannot make the reader and the renewer disagree about
    which browser they are talking to.
    """
    return BrowserOAuthRenewer(
        getattr(provider, "_explicit", None),
        base_url=base_url,
        timeout=timeout,
        ports=getattr(provider, "_ports", None),
    )
