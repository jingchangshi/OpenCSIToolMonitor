"""HTTP transport built on :mod:`urllib`.

Replaces the earlier ``httpx``-based draft so the tool has no third-party
runtime dependency. The class is deliberately small: it knows about URLs,
cookies, JSON, timeouts and retry classification -- and nothing about
openCsiTool semantics.

Retry policy (project brief §60)
--------------------------------
Retried (transient): connection reset, timeout, and 502/503/504.
Never retried: 400, 401, 403, 404 and any other 4xx. Those are deterministic;
retrying them wastes the user's time and can look like credential probing.

The ``Authorization`` header is never set. openCsiTool authenticates by cookie
only, and sending a bearer token produces ``401 Invalid Authorization``.
"""

from __future__ import annotations

import json
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlencode, urlparse, urlunparse

from .errors import NetworkError, ServerError
from .redaction import scrub_text
from .version import USER_AGENT

#: Statuses worth a second attempt.
RETRY_STATUSES = frozenset({502, 503, 504})

#: Attempts include the first try (so 2 means one retry).
DEFAULT_ATTEMPTS = 3
DEFAULT_BACKOFF = 0.5


def _strip_proxy_credentials(proxy: str) -> str:
    """Remove ``user:password@`` from a proxy URL.

    Proxy URLs are legitimate places to carry credentials, and this value is
    surfaced in error messages and ``--verbose`` traces. Dropping the userinfo
    keeps the diagnostic (which proxy was used) without printing a secret.
    """
    try:
        parsed = urlparse(proxy)
    except ValueError:  # pragma: no cover - malformed proxy URL
        return proxy
    if not parsed.username and not parsed.password:
        return proxy
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunparse(
        (parsed.scheme, host, parsed.path, parsed.params, parsed.query, parsed.fragment)
    )


@dataclass(frozen=True)
class Response:
    """A completed HTTP exchange."""

    status: int
    body: str
    headers: Mapping[str, str]
    url: str
    elapsed_ms: float

    def json(self) -> Any:
        """Parse the body as JSON, raising :class:`NetworkError` on failure."""
        try:
            return json.loads(self.body) if self.body else None
        except ValueError as exc:
            raise NetworkError(
                f"response from {self.url} was not valid JSON"
            ) from exc

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def header(self, name: str, default: str = "") -> str:
        target = name.lower()
        for key, value in self.headers.items():
            if key.lower() == target:
                return value
        return default


class HttpTransport:
    """Minimal cookie-authenticated JSON client."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 15.0,
        attempts: int = DEFAULT_ATTEMPTS,
        backoff: float = DEFAULT_BACKOFF,
        verify_tls: bool = True,
        use_proxy: bool = True,
        logger: Any = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.attempts = max(1, attempts)
        self.backoff = backoff
        self.verify_tls = verify_tls
        self.use_proxy = use_proxy
        self._log = logger
        self._cookie: str | None = None
        self._ssl_context: ssl.SSLContext | None = None
        if not verify_tls:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            self._ssl_context = context

        # urllib picks up a system proxy from the environment *and*, on Windows,
        # from the registry. That is usually right (corporate proxies), but it
        # means a local proxy that cannot reach the API produces an opaque
        # SSLEOFError with no mention of the proxy. Building an explicit opener
        # lets `--no-proxy` bypass it and lets errors name the culprit.
        handlers: list[Any] = []
        if not use_proxy:
            handlers.append(urllib.request.ProxyHandler({}))
        if self._ssl_context is not None:
            handlers.append(urllib.request.HTTPSHandler(context=self._ssl_context))
        self._opener = (
            urllib.request.build_opener(*handlers) if handlers else urllib.request.build_opener()
        )

    # ── proxy awareness ───────────────────────────────────────────────────
    def proxy_for(self, url: str) -> str | None:
        """The proxy that would be used for ``url``, if any.

        Returns ``None`` when proxying is disabled for this transport or when
        the host is in ``NO_PROXY``. Used to make connection errors name the
        proxy instead of reporting a bare TLS failure.

        Any ``user:password@`` in the proxy URL is stripped: this string ends
        up in error messages and hints, which are printed and redirected to
        files, so proxy credentials must not travel with it.
        """
        if not self.use_proxy:
            return None
        try:
            proxies = urllib.request.getproxies()
        except Exception:  # pragma: no cover - platform dependent
            return None
        if not proxies:
            return None
        # ``proxy_bypass`` honours NO_PROXY / the registry bypass list.
        try:
            if urllib.request.proxy_bypass(urlparse(url).hostname or ""):
                return None
        except Exception:  # pragma: no cover - platform dependent
            pass
        scheme = urlparse(url).scheme.lower()
        raw = proxies.get(scheme) or proxies.get("all") or None
        return _strip_proxy_credentials(raw) if raw else None

    # ── cookie handling ───────────────────────────────────────────────────
    def set_cookie(self, value: str | None) -> None:
        """Set the ``token`` cookie value used for subsequent requests."""
        self._cookie = value or None

    def clear_cookie(self) -> None:
        self._cookie = None

    @property
    def has_cookie(self) -> bool:
        return bool(self._cookie)

    # ── requests ──────────────────────────────────────────────────────────
    def get_json(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        """GET ``path`` and return the raw :class:`Response`.

        Only GET is implemented: every endpoint this tool uses is read-only,
        and omitting the mutating verbs makes accidental writes impossible.
        """
        url = self._build_url(path, params)
        return self._request("GET", url, timeout=timeout, headers=headers)

    # ── internals ─────────────────────────────────────────────────────────
    def _build_url(self, path: str, params: Mapping[str, Any] | None) -> str:
        if path.startswith(("http://", "https://")):
            url = path
        else:
            url = f"{self.base_url}/{path.lstrip('/')}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                separator = "&" if "?" in url else "?"
                url = f"{url}{separator}{urlencode(clean, doseq=True)}"
        return url

    def _headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN",
            "User-Agent": USER_AGENT,
            # NOTE: no Authorization header, by design.
        }
        if self._cookie:
            headers["Cookie"] = f"token={self._cookie}"
        if extra:
            headers.update({k: v for k, v in extra.items()})
        return headers

    def _request(
        self,
        method: str,
        url: str,
        *,
        timeout: float | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        effective_timeout = timeout if timeout is not None else self.timeout
        last_error: Exception | None = None

        for attempt in range(1, self.attempts + 1):
            started = time.perf_counter()
            request = urllib.request.Request(
                url, headers=self._headers(headers), method=method
            )
            try:
                with self._opener.open(request, timeout=effective_timeout) as response:
                    raw = response.read()
                    elapsed = (time.perf_counter() - started) * 1000.0
                    result = Response(
                        status=response.status,
                        body=raw.decode("utf-8", "replace"),
                        headers=dict(response.headers.items()),
                        url=url,
                        elapsed_ms=elapsed,
                    )
                self._trace(method, url, result.status, elapsed)
                return result

            except urllib.error.HTTPError as exc:
                elapsed = (time.perf_counter() - started) * 1000.0
                body = ""
                try:
                    body = exc.read().decode("utf-8", "replace")
                except Exception:
                    pass
                headers = dict(exc.headers.items()) if exc.headers else {}
                # HTTPError owns the response socket; close it explicitly so a
                # 4xx/5xx does not leak a connection.
                try:
                    exc.close()
                except Exception:
                    pass
                result = Response(
                    status=exc.code,
                    body=body,
                    headers=headers,
                    url=url,
                    elapsed_ms=elapsed,
                )
                self._trace(method, url, exc.code, elapsed)

                if exc.code in RETRY_STATUSES and attempt < self.attempts:
                    last_error = ServerError(f"HTTP {exc.code} from {url}")
                    self._sleep(attempt)
                    continue
                # 4xx and any other status are returned for the caller to
                # classify (401/403 have endpoint-specific meaning).
                return result

            except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
                elapsed = (time.perf_counter() - started) * 1000.0
                reason = getattr(exc, "reason", exc)
                kind = type(reason).__name__ if not isinstance(reason, str) else "URLError"
                message = f"{kind} contacting {url}"
                # An SSL failure through a local proxy looks like a server fault
                # and says nothing about the proxy, which sends people off to
                # debug the wrong thing. Name it when one is in play.
                proxy = self.proxy_for(url)
                hint = None
                if proxy:
                    message += f" (via proxy {proxy})"
                    hint = (
                        f"this request went through the proxy {proxy}. If that "
                        "proxy cannot reach opencsitool.com, retry with "
                        "--no-proxy (or fix HTTPS_PROXY / the Windows proxy "
                        "settings)."
                    )
                last_error = NetworkError(message, hint=hint)
                self._trace(method, url, "ERR", elapsed, detail=str(last_error))
                if attempt < self.attempts:
                    self._sleep(attempt)
                    continue
                raise last_error from exc

        if last_error is not None:
            raise last_error
        raise NetworkError(f"request to {url} failed for an unknown reason")

    def _sleep(self, attempt: int) -> None:
        """Exponential backoff, kept short and jitter-free for predictability."""
        delay = self.backoff * (2 ** (attempt - 1))
        time.sleep(min(delay, 4.0))

    def _trace(
        self,
        method: str,
        url: str,
        status: Any,
        elapsed_ms: float,
        *,
        detail: str = "",
    ) -> None:
        """Emit a verbose trace line.

        Only the path is logged -- never the query string, because a future
        endpoint could carry a token there, and never any header value.
        """
        if self._log is None:
            return
        path = url.split("://", 1)[-1]
        path = path.split("/", 1)[-1] if "/" in path else path
        path = path.split("?", 1)[0]
        suffix = f" ({scrub_text(detail)})" if detail else ""
        self._log(f"{method} /{path} -> {status} in {elapsed_ms:.0f}ms{suffix}")

    def close(self) -> None:
        """Present for symmetry; urllib keeps no persistent connection pool."""
        self._cookie = None

    def __enter__(self) -> "HttpTransport":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            f"HttpTransport(base_url={self.base_url!r}, "
            f"has_cookie={self.has_cookie}, cookie=<redacted>)"
        )
