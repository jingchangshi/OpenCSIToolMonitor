"""Chrome/Edge credential provider over the standard DevTools Protocol.

This module owns *all* browser knowledge in the project. It reads the
openCsiTool session cookie from an already-running, already-signed-in browser
and hands the raw value to the client. It never drives the browser, never
navigates, and never performs a write.

Discovery order (project brief §10)
-----------------------------------
1. An explicit endpoint (``--cdp`` / constructor argument).
2. ``OPENCSI_CDP_URL``.
3. Well-known localhost ports: 9222, 9223, 9224.
4. ``DevToolsActivePort`` files written by Chrome/Edge/Chromium/Brave.

Endpoint resolution (project brief §7)
--------------------------------------
``/json/version`` is the normal way to obtain the browser WebSocket URL, but
**Chrome 147+ disables the ``/json/*`` HTTP endpoints on the default
user-data-dir** (it answers ``404`` with an empty body, and the WebSocket
upgrade on the browser path then hangs). Verified on Chrome 153 in this
environment. When that happens we fall back to the WebSocket path that Chrome
itself recorded in ``DevToolsActivePort``.

A dedicated automation profile started with ``--remote-debugging-port`` behaves
normally and serves ``/json/version`` as documented.
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..errors import (
    CdpUnavailableError,
    CookieNotFoundError,
    NoBrowserTargetError,
    NetworkError,
    OpenCsiError,
)
from ..redaction import register_secret, scrub_text
from ..ws import CdpConnection, WebSocketError
from .base import CredentialStatus, remaining_seconds

#: Ports probed when nothing more specific is configured.
DEFAULT_PORTS: tuple[int, ...] = (9222, 9223, 9224)

#: The cookie we need, and the domains that may carry it.
COOKIE_NAME = "token"
COOKIE_DOMAINS: tuple[str, ...] = ("opencsitool.com", ".opencsitool.com")

#: How long a discovered cookie is reused before being re-read from the browser.
#: Deliberately short: the cookie itself lives about an hour, and re-reading is
#: cheap, so we stay close to the browser's own state.
DEFAULT_TTL = 60.0

#: Re-read this many seconds before expiry rather than waiting for a 401.
EXPIRY_MARGIN = 30.0

_HTTP_PROBE_TIMEOUT = 3.0
_WS_TIMEOUT = 15.0


# ── endpoint description ──────────────────────────────────────────────────
@dataclass(frozen=True)
class CdpEndpoint:
    """A resolved DevTools endpoint.

    Either ``ws_url`` (browser-level WebSocket) or ``http_base`` is set, and
    ``ws_path`` may carry a browser path recovered from ``DevToolsActivePort``.
    """

    host: str = "127.0.0.1"
    port: int = 9222
    scheme: str = "http"
    ws_url: str | None = None
    ws_path: str | None = None
    source: str = "unknown"
    browser: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def http_base(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"

    @property
    def ws_scheme(self) -> str:
        return "wss" if self.scheme == "https" else "ws"

    def browser_ws_url(self) -> str | None:
        """Browser-level WebSocket URL, if one is known."""
        if self.ws_url:
            return self.ws_url
        if self.ws_path:
            return f"{self.ws_scheme}://{self.host}:{self.port}{self.ws_path}"
        return None

    def __str__(self) -> str:
        # Port-derived sources restate the URL; only keep the source when it
        # adds information (marker file, explicit URL).
        if self.source and not self.source.startswith(f"port {self.port}"):
            return f"{self.http_base} (source: {self.source})"
        return self.http_base


# ── browser profile locations ─────────────────────────────────────────────
def _profile_dirs() -> list[Path]:
    """Well-known ``User Data`` directories that may hold ``DevToolsActivePort``.

    We only *read* the small marker file; user profiles are never modified.
    """
    home = Path.home()
    candidates: list[Path] = []
    env = os.environ

    if os.name == "nt":
        local = env.get("LOCALAPPDATA")
        if local:
            base = Path(local)
            candidates += [
                base / "Google" / "Chrome" / "User Data",
                base / "Microsoft" / "Edge" / "User Data",
                base / "Chromium" / "User Data",
                base / "BraveSoftware" / "Brave-Browser" / "User Data",
            ]
    elif os.uname().sysname == "Darwin":  # pragma: no cover - macOS
        candidates += [
            home / "Library" / "Application Support" / "Google" / "Chrome",
            home / "Library" / "Application Support" / "Microsoft Edge",
            home / "Library" / "Application Support" / "Chromium",
            home / "Library" / "Application Support" / "BraveSoftware" / "Brave-Browser",
        ]
    else:  # pragma: no cover - Linux
        cfg = Path(env.get("XDG_CONFIG_HOME", home / ".config"))
        candidates += [
            cfg / "google-chrome",
            cfg / "microsoft-edge",
            cfg / "chromium",
            cfg / "BraveSoftware" / "Brave-Browser",
        ]

    out: list[Path] = []
    for c in candidates:
        try:
            if c.is_dir():
                out.append(c)
        except OSError:
            continue
    return out


def read_devtools_active_port(profile: Path) -> tuple[int, str] | None:
    """Read ``(port, ws_path)`` from a profile's ``DevToolsActivePort`` file."""
    marker = profile / "DevToolsActivePort"
    try:
        text = marker.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    try:
        port = int(lines[0])
    except ValueError:
        return None
    ws_path = lines[1] if len(lines) > 1 else ""
    return port, ws_path


def _devtools_active_port_endpoints() -> list[CdpEndpoint]:
    """Endpoints recovered from ``DevToolsActivePort`` marker files."""
    out: list[CdpEndpoint] = []
    for profile in _profile_dirs():
        found = read_devtools_active_port(profile)
        if not found:
            continue
        port, ws_path = found
        if not ws_path:
            continue
        out.append(
            CdpEndpoint(
                port=port,
                ws_path=ws_path,
                source=f"DevToolsActivePort:{profile.name}",
            )
        )
    return out


# ── HTTP probing ──────────────────────────────────────────────────────────
def _http_get_json(url: str, timeout: float = _HTTP_PROBE_TIMEOUT) -> Any:
    """GET ``url`` and parse JSON, raising :class:`NetworkError` on failure.

    The error carries the HTTP status so callers can special-case 404/403.
    """
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        # HTTPError is itself a response object holding an open socket; closing
        # it avoids leaking a connection on every 404/403 probe.
        try:
            exc.close()
        except Exception:
            pass
        err = NetworkError(f"HTTP {exc.code} from {url}")
        err.http_status = exc.code  # type: ignore[attr-defined]
        raise err from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise NetworkError(f"{type(exc).__name__} contacting {url}") from exc
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        raise NetworkError(f"non-JSON response from {url}") from exc


def probe_http_endpoint(http_base: str, timeout: float = _HTTP_PROBE_TIMEOUT) -> dict[str, Any] | None:
    """Return ``/json/version`` metadata if ``http_base`` is a DevTools server."""
    try:
        data = _http_get_json(f"{http_base.rstrip('/')}/json/version", timeout=timeout)
    except NetworkError:
        return None
    if isinstance(data, dict) and data.get("webSocketDebuggerUrl"):
        return data
    return None


def _tcp_open(host: str, port: int, timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _looks_like_devtools(host: str, port: int) -> bool:
    """Cheap check that something CDP-ish is listening.

    ``/json/version`` may legitimately 404 on Chrome 147+, so a bare TCP accept
    combined with either a JSON reply or a ``DevToolsActivePort`` match counts.
    A completely unrelated service that merely accepts a connection is rejected
    because it neither answers ``/json/version`` nor appears in a marker file.
    """
    if not _tcp_open(host, port):
        return False
    base = f"http://{host}:{port}"
    try:
        data = _http_get_json(f"{base}/json/version", timeout=_HTTP_PROBE_TIMEOUT)
        if isinstance(data, dict) and data.get("webSocketDebuggerUrl"):
            return True
    except NetworkError as exc:
        status = getattr(exc, "http_status", None)
        if status == 403:
            # DevTools is there but wants the "Allow remote debugging" prompt.
            return True
        if status != 404:
            return False
    # 404 path: only trust it if a marker file points at this port.
    for profile in _profile_dirs():
        found = read_devtools_active_port(profile)
        if found and found[0] == port:
            return True
    return False


def discover_cdp_endpoint(
    explicit: str | None = None,
    *,
    env_var: str = "OPENCSI_CDP_URL",
    ports: Sequence[int] = DEFAULT_PORTS,
    host: str = "127.0.0.1",
    probe: bool = True,
) -> CdpEndpoint:
    """Resolve a DevTools endpoint using the documented precedence.

    ``explicit`` may be an ``http(s)://`` base or a ``ws(s)://`` URL. Returns a
    :class:`CdpEndpoint`; raises :class:`CdpUnavailableError` when nothing is
    reachable.
    """
    tried: list[str] = []

    def from_url(value: str, source: str) -> CdpEndpoint | None:
        value = value.strip()
        if not value:
            return None
        if value.startswith(("ws://", "wss://")):
            rest = value.split("://", 1)[1]
            authority = rest.split("/", 1)[0]
            path = "/" + rest.split("/", 1)[1] if "/" in rest else ""
            h, _, p = authority.partition(":")
            try:
                port = int(p) if p else (443 if value.startswith("wss") else 80)
            except ValueError:
                return None
            return CdpEndpoint(
                host=h or host,
                port=port,
                scheme="https" if value.startswith("wss") else "http",
                ws_url=value,
                source=source,
            )
        if "://" not in value:
            value = f"http://{value}"
        scheme, _, rest = value.partition("://")
        authority = rest.split("/", 1)[0]
        h, _, p = authority.partition(":")
        try:
            port = int(p) if p else (443 if scheme == "https" else 80)
        except ValueError:
            return None
        return CdpEndpoint(host=h or host, port=port, scheme=scheme, source=source)

    # 1. explicit argument
    if explicit:
        endpoint = from_url(explicit, "argument")
        if endpoint is None:
            raise CdpUnavailableError(f"could not parse CDP endpoint {explicit!r}")
        if endpoint.ws_url:
            return endpoint
        if not probe or _looks_like_devtools(endpoint.host, endpoint.port):
            meta = probe_http_endpoint(endpoint.http_base)
            if meta:
                return CdpEndpoint(
                    host=endpoint.host,
                    port=endpoint.port,
                    scheme=endpoint.scheme,
                    ws_url=meta.get("webSocketDebuggerUrl"),
                    source="argument",
                    browser=meta.get("Browser"),
                    metadata=meta,
                )
            # Chrome 147+ default profile: recover the path from the marker file.
            for candidate in _devtools_active_port_endpoints():
                if candidate.port == endpoint.port:
                    return CdpEndpoint(
                        host=endpoint.host,
                        port=endpoint.port,
                        scheme=endpoint.scheme,
                        ws_path=candidate.ws_path,
                        source="argument+DevToolsActivePort",
                    )
        tried.append(f"{explicit} (explicit)")
        raise CdpUnavailableError(
            "the CDP endpoint given on the command line is not reachable: " + ", ".join(tried)
        )

    # 2. environment variable
    env_value = os.environ.get(env_var)
    if env_value:
        try:
            return discover_cdp_endpoint(env_value, ports=ports, host=host, probe=probe)
        except CdpUnavailableError:
            tried.append(f"{env_value} ({env_var})")

    # 3. well-known ports
    for port in ports:
        if not probe or _looks_like_devtools(host, port):
            base = f"http://{host}:{port}"
            meta = probe_http_endpoint(base)
            if meta:
                return CdpEndpoint(
                    host=host,
                    port=port,
                    ws_url=meta.get("webSocketDebuggerUrl"),
                    source=f"port {port}",
                    browser=meta.get("Browser"),
                    metadata=meta,
                )
            for candidate in _devtools_active_port_endpoints():
                if candidate.port == port:
                    return CdpEndpoint(
                        host=host,
                        port=port,
                        ws_path=candidate.ws_path,
                        source=f"port {port} + DevToolsActivePort",
                    )
        tried.append(f"{host}:{port}")

    # 4. marker files alone (a non-default port)
    for candidate in _devtools_active_port_endpoints():
        if not probe or _tcp_open(candidate.host, candidate.port):
            return candidate
        tried.append(f"{candidate.host}:{candidate.port} (marker)")

    raise CdpUnavailableError(
        "no Chrome/Edge DevTools endpoint found. Tried: "
        + ", ".join(tried)
        + ". Start a browser with --remote-debugging-port=9222, or pass --cdp."
    )


# ── cookie selection ──────────────────────────────────────────────────────
def select_token_cookie(
    cookies: Iterable[Mapping[str, Any]],
    *,
    now: float | None = None,
    name: str = COOKIE_NAME,
    domains: Sequence[str] = COOKIE_DOMAINS,
) -> Mapping[str, Any] | None:
    """Pick the best openCsiTool session cookie from a CDP cookie list.

    Selection rules (project brief §38):

    1. ``name`` must match.
    2. ``domain`` must be one of ``domains`` (exact match, case-insensitive).
    3. The value must be non-empty.
    4. Non-expired cookies are preferred over expired ones.
    5. Among equals, a longer remaining lifetime wins, then an exact domain
       over a dot-prefixed one, then a longer value.

    Returns the chosen cookie mapping, or ``None``.
    """
    now = time.time() if now is None else now
    wanted = {d.lower() for d in domains}

    candidates: list[tuple[tuple[int, float, int, int], Mapping[str, Any]]] = []
    for cookie in cookies:
        if not isinstance(cookie, Mapping):
            continue
        if str(cookie.get("name", "")) != name:
            continue
        domain = str(cookie.get("domain", "")).lower()
        if domain not in wanted:
            continue
        value = cookie.get("value")
        if not isinstance(value, str) or not value:
            continue

        expires = cookie.get("expires")
        try:
            expires_f = float(expires) if expires not in (None, "", 0) else 0.0
        except (TypeError, ValueError):
            expires_f = 0.0

        # A session cookie (no expiry) is treated as long-lived.
        if expires_f <= 0:
            not_expired = 1
            remaining = float("inf")
        elif expires_f > now:
            not_expired = 1
            remaining = expires_f - now
        else:
            not_expired = 0
            remaining = 0.0

        exact_domain = 1 if not domain.startswith(".") else 0
        # ``inf`` cannot be compared with ``-`` ordering across tuples safely,
        # so clamp for the sort key.
        sort_remaining = remaining if remaining != float("inf") else 1e18
        key = (not_expired, sort_remaining, exact_domain, len(value))
        candidates.append((key, cookie))

    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return candidates[0][1]


# ── provider ──────────────────────────────────────────────────────────────
class CdpCookieProvider:
    """Read the openCsiTool session cookie from a running Chrome/Edge.

    Parameters
    ----------
    cdp_url:
        Explicit endpoint. When omitted, discovery follows the documented order.
    ttl:
        Seconds a successfully read cookie is reused before being re-read.
    ports:
        Ports probed during discovery.
    """

    name = "cdp"

    def __init__(
        self,
        cdp_url: str | None = None,
        *,
        ttl: float = DEFAULT_TTL,
        ports: Sequence[int] | None = None,
        timeout: float = _WS_TIMEOUT,
        discover: bool = True,
    ) -> None:
        self._explicit = cdp_url
        self._ttl = ttl
        # ``None`` and an empty sequence both mean "use the defaults"; a caller
        # that parsed an optional CLI flag should not have to special-case it.
        self._ports = tuple(ports) if ports else DEFAULT_PORTS
        self._timeout = timeout
        self._discover = discover

        self._token: str | None = None
        self._expires_at: float | None = None
        self._read_at: float = 0.0
        #: Whether ``_token`` was handed over by a renewer rather than read from
        #: the browser. See :meth:`holds_remembered_token`.
        self._remembered: bool = False
        self._endpoint: CdpEndpoint | None = None
        self._last_error: str | None = None
        self._last_detail: str | None = None
        self._last_hint: str | None = None
        self._browser: str | None = None

    # -- public API -------------------------------------------------------
    def get_token(self) -> str | None:
        """Return the cookie value, re-reading when the cache is stale."""
        now = time.time()
        if self._token and self._fresh(now):
            return self._token
        if self._token and self._expires_at and self._expires_at - now <= EXPIRY_MARGIN:
            # Close to expiry: refresh proactively instead of waiting for a 401.
            return self.refresh()
        if self._token and now - self._read_at < self._ttl:
            return self._token
        try:
            return self.refresh()
        except (CdpUnavailableError, CookieNotFoundError):
            # Fall back to a still-valid cached value if we have one.
            if self._token and self._expires_at and self._expires_at > now:
                return self._token
            raise

    def invalidate(self) -> None:
        """Drop the cached value so the next call re-reads from the browser.

        Unlike a manual provider, this is *not* permanent: the browser remains
        the source of truth, so a retry has a real chance of succeeding.
        """
        self._token = None
        self._expires_at = None
        self._read_at = 0.0
        self._remembered = False

    def peek_token(self) -> str | None:
        """The currently cached value, **without** re-reading the browser.

        Exists so a caller can answer "has the token changed?" without that
        question perturbing the answer. ``get_token()`` is allowed to fetch, so
        using it for a before/after comparison would compare a fresh read
        against itself and always report "unchanged".

        Returns ``None`` when nothing is cached; it never raises for a missing
        browser, because it never touches one.
        """
        return self._token

    def holds_remembered_token(self) -> bool:
        """Whether the cached value came from something other than the browser.

        This is what lets :class:`~opencsi.auth.session.SessionManager` tell the
        two renewal mechanisms apart. After a successful renewal it must drop the
        cached value *only* when the browser is the place the new secret landed
        -- a browser-driven renewal writes the cookie into the browser, so the
        cache is stale and the next read must go and fetch it. A browserless
        renewal has already handed the value over, so dropping it destroys the
        only copy that exists.

        Comparing the token before and after would almost always give the right
        answer, but not always: a browserless renewal that returns the *same*
        value the cache already held is indistinguishable from a browser-driven
        one by value alone, and that is exactly the case where dropping the cache
        would be wrong. Recording the origin is exact where a comparison is only
        usually right.

        Cleared by :meth:`refresh`, because a browser read supersedes it.
        """
        return self._remembered

    def remember_token(self, token: str, *, expires_in: float | None = None) -> None:
        """Cache a token obtained by something other than the browser.

        This exists for the browserless renewal path
        (:mod:`opencsi.auth.http_oauth`). That flow obtains a real openCsiTool
        session over plain HTTP, so the browser it was read *from* knows nothing
        about the new cookie -- without this, the very next ``get_token()`` would
        re-read the browser and hand back the old, expiring value, and the
        renewal would look like it had not happened.

        The cached value is treated exactly like one read from the browser: it
        expires on the same schedule and ``refresh()`` still overrides it. The
        provider stays a *cache over a browser*, not a credential store -- nothing
        is written to disk and the browser remains the fallback source.
        """
        if not token:
            return
        register_secret(token)
        self._token = token
        self._remembered = True
        self._read_at = time.time()
        self._expires_at = (
            self._read_at + float(expires_in)
            if isinstance(expires_in, (int, float)) and expires_in > 0
            else None
        )
        self._last_error = None
        self._last_detail = None
        self._last_hint = None

    def read_all_cookies(self, *, timeout: float | None = None) -> list[Mapping[str, Any]]:
        """Every cookie in the browser, for callers that need more than ``token``.

        The browserless OAuth flow needs the *GitCode* session cookies, not the
        openCsiTool one, so it cannot go through :meth:`get_token`. This exposes
        the same read without duplicating the endpoint discovery, the page/browser
        WebSocket fallback, or the error classification.

        It deliberately does **not** reuse :meth:`_read_cookies`. That method
        filters to ``https://opencsitool.com/`` because its one caller wants a
        single cookie, and the filtered page read returns as soon as it has
        anything -- so it answers "1 cookie" on a browser that holds hundreds, and
        a GitCode lookup through it would always come back empty. The
        ``Network.getCookies`` fallback inside it is unreachable for the same
        reason. Hence a separate walk that asks for the whole store.

        Returns an empty list rather than raising when the browser cannot be
        reached: the caller is a capability probe as often as it is a fetch, and
        "no cookies" and "no browser" lead to the same next step. Use
        :meth:`last_error_code` to tell them apart when it matters.

        No value is logged. Values are returned because the caller has to send
        them, which is the same contract :meth:`get_token` already has.
        """
        del timeout  # the connection's own timeout governs the read
        try:
            endpoint = self._endpoint or discover_cdp_endpoint(
                self._explicit, ports=self._ports, probe=self._discover
            )
        except OpenCsiError as exc:
            self._note_failure(exc)
            return []
        self._endpoint = endpoint

        errors: list[str] = []

        # Browser-domain store first: it is the only call that returns cookies
        # for *every* origin in one shot, with no target attachment.
        browser_ws = endpoint.browser_ws_url()
        if browser_ws:
            try:
                cookies = self._all_cookies_via_browser(browser_ws)
                if cookies:
                    return cookies
            except (WebSocketError, NetworkError, CdpUnavailableError) as exc:
                errors.append(f"browser socket: {type(exc).__name__}")

        # Then a page socket, asked *unfiltered*. Chrome scopes the answer to the
        # page's own origin only when ``urls`` is given, so omitting it is what
        # makes this a whole-store read rather than a one-origin one.
        page_ws = self._page_ws_url(endpoint)
        if page_ws:
            try:
                cookies = self._all_cookies_via_page(page_ws)
                if cookies:
                    return cookies
            except (WebSocketError, NetworkError, CdpUnavailableError) as exc:
                errors.append(f"page socket: {type(exc).__name__}")

        if errors:
            self._last_error = CdpUnavailableError.code
            self._last_detail = "could not read the browser cookie store (" + "; ".join(errors) + ")"
            self._last_hint = self._upgrade_hint(endpoint)
        return []

    def install_token(
        self,
        token: str,
        *,
        domain: str = ".opencsitool.com",
        expires_in: float | None = None,
        timeout: float | None = None,
    ) -> bool:
        """Write a session cookie into the browser. Returns whether it landed.

        Why this exists
        ---------------
        This project's hard rule is that a credential is never written to disk --
        the browser *is* the credential store, which is what makes
        ``opencsi usage`` work as a separate process with no shared state.

        A browserless renewal breaks that arrangement in one specific way: it
        mints a real session over plain HTTP, so the browser it read the GitCode
        credential from never learns the new cookie. Without this method the token
        exists only in the memory of the process that renewed, so
        ``opencsi login --renew`` succeeds and the next ``opencsi usage`` fails --
        a partial success reported as a complete one.

        The fix has to be the browser, not a file: writing the cookie to disk
        would violate the rule above and would put a live credential somewhere the
        project promised never to put one.

        Scope of the write
        ------------------
        Exactly one cookie, named ``token``, on ``.opencsitool.com``. Nothing else
        is touched, no existing cookie is deleted, and the value is never logged.
        The cookie is marked ``HttpOnly`` and ``Secure`` because the one being
        replaced is, and a renewal that quietly downgraded those flags would be a
        security regression dressed up as a fix.

        Returns ``False`` rather than raising when the browser cannot be reached.
        The caller has already succeeded at the thing it was asked to do; failing
        to *persist* is worth reporting, but it must not turn a real renewal into
        an exception.
        """
        if not token:
            return False

        try:
            endpoint = self._endpoint or discover_cdp_endpoint(
                self._explicit, ports=self._ports, probe=self._discover
            )
        except OpenCsiError as exc:
            self._note_failure(exc)
            return False
        self._endpoint = endpoint

        browser_ws = endpoint.browser_ws_url()
        if not browser_ws:
            self._last_error = CdpUnavailableError.code
            self._last_detail = "the DevTools endpoint exposed no browser-level socket"
            return False

        params: dict[str, Any] = {
            "name": COOKIE_NAME,
            "value": token,
            "domain": domain,
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "sameSite": "Lax",
        }
        if isinstance(expires_in, (int, float)) and expires_in > 0:
            # CDP wants an absolute epoch, not a relative lifetime -- passing the
            # relative number would set an expiry in 1970 and the cookie would be
            # dropped immediately, which looks exactly like a rejected write.
            params["expires"] = time.time() + float(expires_in)

        try:
            with CdpConnection(browser_ws, timeout=timeout or self._timeout) as conn:
                conn.call("Storage.setCookies", {"cookies": [params]}, timeout=timeout or self._timeout)
        except (WebSocketError, NetworkError, CdpUnavailableError, OpenCsiError) as exc:
            self._last_error = getattr(exc, "code", type(exc).__name__)
            self._last_detail = scrub_text(str(exc))[:200]
            return False

        # Verify by reading it back rather than trusting the call's return. A
        # rejected write is silent here -- ``Storage.setCookies`` answers ``{}``
        # either way -- so an unverified write would let the caller claim
        # persistence that did not happen.
        try:
            cookies = self._all_cookies_via_browser(browser_ws)
        except (WebSocketError, NetworkError, CdpUnavailableError):
            return False
        return any(
            str(c.get("name")) == COOKIE_NAME
            and str(c.get("value") or "") == token
            and domain.lstrip(".") in str(c.get("domain") or "")
            for c in cookies
        )

    def _all_cookies_via_browser(self, ws_url: str) -> list[Mapping[str, Any]]:
        """Whole cookie store over a browser-level connection."""
        with CdpConnection(ws_url, timeout=self._timeout) as conn:
            version = conn.call("Browser.getVersion", timeout=self._timeout)
            self._browser = str(version.get("product") or "") or None
            result = conn.call("Storage.getCookies", timeout=self._timeout)
            cookies = result.get("cookies")
            return [c for c in cookies if isinstance(c, Mapping)] if isinstance(cookies, list) else []

    def _all_cookies_via_page(self, ws_url: str) -> list[Mapping[str, Any]]:
        """Whole cookie store over a page-level connection.

        ``Network.getCookies`` with no ``urls`` argument. Note this still only
        reaches the *browser process's* store, not a different profile's -- which
        is the correct boundary, and why the caller has to be pointed at the right
        endpoint rather than this reaching across.
        """
        with CdpConnection(ws_url, timeout=self._timeout) as conn:
            try:
                conn.call("Network.enable", timeout=self._timeout)
            except WebSocketError:
                pass  # getCookies works without it on most builds
            result = conn.call("Network.getCookies", timeout=self._timeout)
            cookies = result.get("cookies")
            return [c for c in cookies if isinstance(c, Mapping)] if isinstance(cookies, list) else []

    def _note_failure(self, exc: OpenCsiError) -> None:
        """Record a failure's code, scrubbed detail and hint in one place."""
        self._last_error = exc.code
        self._last_detail = scrub_text(str(exc))[:300] or exc.code
        self._last_hint = exc.hint

    def refresh(self) -> str | None:
        """Force a re-read from the browser, bypassing the TTL."""
        try:
            cookies, endpoint = self._read_cookies()
        except OpenCsiError as exc:
            # Remember the actionable hint so `doctor` can print the *real*
            # cause (refused handshake, no target, ...) rather than guessing.
            self._last_error = exc.code
            self._last_detail = scrub_text(str(exc))[:300] or exc.code
            self._last_hint = exc.hint
            raise
        self._endpoint = endpoint
        cookie = select_token_cookie(cookies)
        if cookie is None:
            self._last_error = CookieNotFoundError.code
            self._last_detail = (
                f"the browser holds {len(cookies)} cookie(s) for other sites, "
                "but no openCsiTool 'token'"
            )
            self._last_hint = CookieNotFoundError.hint
            raise CookieNotFoundError(
                "CDP is reachable but the browser holds no openCsiTool "
                "'token' cookie for opencsitool.com."
            )

        value = str(cookie.get("value") or "")
        if not value:
            self._last_error = CookieNotFoundError.code
            self._last_detail = "the openCsiTool token cookie is present but empty"
            self._last_hint = CookieNotFoundError.hint
            raise CookieNotFoundError("the openCsiTool token cookie is empty")

        register_secret(value)
        self._token = value
        # A browser read supersedes any value handed over by a renewer: the
        # browser is the source of truth, so this is no longer "remembered".
        self._remembered = False
        expires = cookie.get("expires")
        try:
            expires_f = float(expires) if expires not in (None, "", 0) else 0.0
        except (TypeError, ValueError):
            expires_f = 0.0
        self._expires_at = expires_f if expires_f > 0 else None
        self._read_at = time.time()
        self._last_error = None
        self._last_detail = None
        self._last_hint = None
        return value

    def status(self) -> CredentialStatus:
        """Redacted status; never exposes the cookie value."""
        if self._token is None:
            available = False
            try:
                self.refresh()
                available = self._token is not None
            except Exception as exc:
                # Keep a stable machine-readable code plus a scrubbed one-liner.
                # Neither can contain the cookie: no error message in this
                # package embeds it.
                self._last_error = getattr(exc, "code", type(exc).__name__)
                self._last_detail = scrub_text(str(exc))[:300] or self._last_error
        else:
            available = True

        return CredentialStatus(
            available=available,
            source=self.name,
            expires_at=self._expires_at,
            expires_in=remaining_seconds(self._expires_at),
            domain="opencsitool.com" if available else None,
            http_only=True if available else None,
            secure=True if available else None,
            cookie_count=1 if available else 0,
            detail=self._last_detail or self._last_error,
        )

    @property
    def last_hint(self) -> str | None:
        """Actionable next step from the most recent failure, if any.

        Exposed so ``doctor`` reports the true cause (a refused WebSocket
        handshake, say) instead of inferring one from the symptom.
        """
        return self._last_hint

    @property
    def last_error_code(self) -> str | None:
        """Machine-readable code for the most recent failure, if any.

        Lets a caller map the failure to its documented exit status without
        guessing from the symptom. A refused DevTools handshake and an empty
        cookie jar both surface as "no credential", but they need different
        fixes and different exit codes.
        """
        return self._last_error

    # -- diagnostics ------------------------------------------------------
    @property
    def endpoint(self) -> CdpEndpoint | None:
        return self._endpoint

    @property
    def browser(self) -> str | None:
        return self._browser or (self._endpoint.browser if self._endpoint else None)

    def describe_endpoint(self) -> str:
        """Human-readable endpoint description (no secrets)."""
        if self._endpoint is None:
            return "not connected"
        return str(self._endpoint)

    def probe_endpoint(self) -> CdpEndpoint:
        """Resolve (but do not use) the endpoint. Useful for ``doctor``."""
        endpoint = self._endpoint or discover_cdp_endpoint(
            self._explicit, ports=self._ports, probe=self._discover
        )
        self._endpoint = endpoint
        return endpoint

    # -- internals --------------------------------------------------------
    def _fresh(self, now: float) -> bool:
        if not self._token:
            return False
        if self._expires_at is not None and self._expires_at - now <= EXPIRY_MARGIN:
            return False
        return (now - self._read_at) < self._ttl

    def _read_cookies(self) -> tuple[list[Mapping[str, Any]], CdpEndpoint]:
        endpoint = self._endpoint or discover_cdp_endpoint(
            self._explicit, ports=self._ports, probe=self._discover
        )
        self._endpoint = endpoint

        errors: list[str] = []
        # A strategy that completes its CDP calls without raising has told us
        # something real -- possibly "this browser holds no such cookie". That
        # is a different situation from "the socket never worked", and the two
        # must not be reported with the same message: the first means "sign in",
        # the second means "your browser needs restarting with a flag".
        connected = False

        # Strategy A: page-level WebSocket from /json/list.
        # Works on dedicated profiles and needs no session attach.
        page_ws = self._page_ws_url(endpoint)
        if page_ws:
            try:
                cookies = self._cookies_via_page(page_ws)
                connected = True
                if cookies:
                    return cookies, endpoint
            except (WebSocketError, NetworkError, CdpUnavailableError) as exc:
                errors.append(f"page socket: {type(exc).__name__}")

        # Strategy B: browser-level WebSocket, then attach to a page.
        browser_ws = endpoint.browser_ws_url()
        if browser_ws:
            try:
                cookies = self._cookies_via_browser(browser_ws)
                connected = True
                if cookies:
                    return cookies, endpoint
            except (WebSocketError, NetworkError, CdpUnavailableError) as exc:
                errors.append(f"browser socket: {type(exc).__name__}")

        if connected:
            # The DevTools connection works; there is simply no cookie to hand
            # back. Return the empty list and let refresh() raise the accurate
            # CookieNotFoundError with its "sign in" hint.
            return [], endpoint

        if errors:
            raise CdpUnavailableError(
                "the DevTools endpoint at "
                + str(endpoint)
                + " answered, but its WebSocket could not be used ("
                + "; ".join(errors)
                + ")",
                hint=self._upgrade_hint(endpoint),
            )
        raise CdpUnavailableError(
            "the DevTools endpoint at "
            + str(endpoint)
            + " exposed no usable WebSocket URL",
            hint=self._upgrade_hint(endpoint),
        )

    def _upgrade_hint(self, endpoint: CdpEndpoint) -> str:
        """Actionable next step when a *reachable* endpoint refuses the upgrade.

        Chrome 147+ stops serving ``/json/*`` and refuses the browser-level
        WebSocket upgrade when remote debugging was enabled interactively on the
        **default** user-data-dir (the ``chrome://inspect`` toggle). The port is
        open and ``DevToolsActivePort`` is present, so discovery succeeds and
        the failure only shows up here. The reliable fix is a dedicated profile
        started with an explicit ``--remote-debugging-port``.
        """
        return (
            f"Port {endpoint.port} is open but the DevTools WebSocket handshake "
            "was refused. Chrome 147+ blocks remote debugging on the default "
            "profile when it was enabled from chrome://inspect. Close that "
            "browser and start a dedicated-profile instance instead: "
            'chrome.exe --remote-debugging-port=9222 '
            '"--user-data-dir=%LOCALAPPDATA%\\opencsi-cdp-profile" '
            "https://opencsitool.com/myTools  -- then sign in once in that "
            "window. See README 'Browser preparation'."
        )

    def _page_ws_url(self, endpoint: CdpEndpoint) -> str | None:
        """Find a page target's WebSocket URL via ``/json/list``."""
        try:
            targets = _http_get_json(f"{endpoint.http_base}/json/list", timeout=_HTTP_PROBE_TIMEOUT)
        except NetworkError:
            return None
        if not isinstance(targets, list):
            return None
        pages = [
            t
            for t in targets
            if isinstance(t, Mapping)
            and t.get("type") == "page"
            and t.get("webSocketDebuggerUrl")
        ]
        if not pages:
            return None
        preferred = [t for t in pages if "opencsitool.com" in str(t.get("url") or "")]
        chosen = (preferred or pages)[0]
        return str(chosen["webSocketDebuggerUrl"])

    def _cookies_via_page(self, ws_url: str) -> list[Mapping[str, Any]]:
        """Read cookies through a page-level CDP connection."""
        with CdpConnection(ws_url, timeout=self._timeout) as conn:
            try:
                conn.call("Network.enable")
            except WebSocketError:
                pass  # getCookies works without it on most builds
            result = conn.call(
                "Network.getCookies",
                {"urls": ["https://opencsitool.com/"]},
                timeout=self._timeout,
            )
            cookies = result.get("cookies")
            if isinstance(cookies, list) and cookies:
                return [c for c in cookies if isinstance(c, Mapping)]
            # Some builds only honour an unfiltered request.
            result = conn.call("Network.getCookies", timeout=self._timeout)
            cookies = result.get("cookies")
            return [c for c in cookies if isinstance(c, Mapping)] if isinstance(cookies, list) else []

    def _cookies_via_browser(self, ws_url: str) -> list[Mapping[str, Any]]:
        """Read cookies through a browser-level connection.

        Prefers ``Storage.getCookies`` (browser domain, no session required),
        then falls back to attaching to a page and using ``Network.getCookies``.
        """
        with CdpConnection(ws_url, timeout=self._timeout) as conn:
            version = conn.call("Browser.getVersion", timeout=self._timeout)
            self._browser = str(version.get("product") or "") or None

            # Browser-domain cookie store: no target attachment needed.
            try:
                result = conn.call("Storage.getCookies", timeout=self._timeout)
                cookies = result.get("cookies")
                if isinstance(cookies, list) and cookies:
                    return [c for c in cookies if isinstance(c, Mapping)]
            except WebSocketError:
                pass

            # Fall back to a flattened session on a page target.
            targets = conn.call("Target.getTargets", timeout=self._timeout)
            infos = targets.get("targetInfos")
            if not isinstance(infos, list):
                raise NoBrowserTargetError("DevTools returned no target list")
            pages = [
                t
                for t in infos
                if isinstance(t, Mapping) and t.get("type") == "page"
            ]
            if not pages:
                raise NoBrowserTargetError(
                    "the browser has no open page tab to attach to"
                )
            preferred = [t for t in pages if "opencsitool.com" in str(t.get("url") or "")]
            target = (preferred or pages)[0]

            attached = conn.call(
                "Target.attachToTarget",
                {"targetId": target.get("targetId"), "flatten": True},
                timeout=self._timeout,
            )
            session_id = attached.get("sessionId")
            if not session_id:
                raise NoBrowserTargetError("could not attach to a browser target")

            try:
                conn.call("Network.enable", session_id=session_id, timeout=self._timeout)
            except WebSocketError:
                pass
            result = conn.call(
                "Network.getCookies",
                {"urls": ["https://opencsitool.com/"]},
                session_id=session_id,
                timeout=self._timeout,
            )
            cookies = result.get("cookies")
            return [c for c in cookies if isinstance(c, Mapping)] if isinstance(cookies, list) else []

    def __repr__(self) -> str:
        return (
            f"CdpCookieProvider(endpoint={self._endpoint!r}, "
            f"has_token={self._token is not None}, token=<redacted>)"
        )
