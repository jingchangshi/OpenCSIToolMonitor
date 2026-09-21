"""GitCode WeChat mini-program QR login, as a pure-HTTP client.

Protocol provenance
-------------------
Every endpoint, method, parameter and state value below was recovered from
GitCode's own login bundles and confirmed on the wire with read-only probes.
The evidence, including the exact JS that issues each call, is in
``docs/gitcode-qr-protocol.md``. Nothing here is guessed, and in particular no
OAuth *Device Authorization Grant* is invented: GitCode publishes no such
endpoint, and none appears in the client bundle.

The flow
--------
.. code-block::

    POST /uc/api/v1/qrcode/wechat_mini_program          -> {scene_id, qrcode}
              │
              ▼
    GET  /uc/api/v1/qrcode/wechat_mini_program?scene_id=…
         -> {status: WAITING | SCAN | LOGIN | TIMEOUT | CANCEL}
              │
              │  status == LOGIN
              ▼
    POST /uc/api/v1/user/oauth/login/qrcode/wechat_mini_program?scene_id=…
         -> {access_token, refresh_token, user_status_enum, …}

Three details that are easy to get wrong and are pinned by tests:

* the login call's path is ``/uc/api/v1/user/...`` even though the source reads
  ``/api/v1/user/...`` -- GitCode's axios interceptor prepends ``/uc``. The
  unprefixed path answers 401; the prefixed one answers 405 to GET, which is
  how the prefix was confirmed;
* the login call takes ``scene_id`` in the **query string**, not the body;
* ``X-Source`` is a telemetry label, not a signature. It is sent because the
  real client sends it, not because the server requires it.

Security posture
----------------
* ``scene_id`` is treated as **sensitive** and is never logged. It is the
  bearer value for the pending login: anyone holding it can complete the login
  for that scan.
* ``access_token`` / ``refresh_token`` are registered for redaction the moment
  they arrive and are never returned to a caller for storage.
* No openCsiTool *business* endpoint is touched. Authentication is not a
  business write (project brief §62), and every call here is a login call.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from http.cookiejar import CookieJar
from typing import Any, Callable, Mapping
from urllib.parse import urlencode

from ..errors import EXIT_QR_PROTOCOL, NetworkError, OpenCsiError
from ..redaction import register_secret, scrub_text

log = logging.getLogger("opencsi.auth.gitcode_qr")

#: GitCode's API host. Confirmed from ``VITE_API_HOST`` in the login bundle;
#: ``api.gitcode.com`` appears nowhere in it.
API_BASE = "https://web-api.gitcode.com"

#: The platform segment. ``wechat_mini_program`` is what the real client sends.
PLATFORM = "wechat_mini_program"

QR_PATH = "/uc/api/v1/qrcode/{platform}"
LOGIN_PATH = "/uc/api/v1/user/oauth/login/qrcode/{platform}"

#: The browser Origin/Referer the real client sends. Sent because Huawei
#: CloudWAF was observed answering 418 without them while probing -- they are
#: plausibly load-bearing for a non-browser client, and cost nothing.
ORIGIN = "https://gitcode.com"
REFERER = "https://gitcode.com/login"

#: Sent because the real client sends them. ``X-Source`` is a *telemetry*
#: label; ``X-Device-ID: unknown`` is what the bundle hard-codes when it has no
#: device id, which is exactly our situation.
DEFAULT_SOURCE = "toolbar_login"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)

#: The cadence the real client uses (``{interval: 1500}``), and a hard ceiling
#: so a stuck poll cannot run forever.
DEFAULT_POLL_INTERVAL = 1.5
DEFAULT_MAX_WAIT = 180.0
DEFAULT_TIMEOUT = 20.0

#: How many times an expired QR is re-issued before giving up. The objective
#: allows exactly one automatic refresh; more would be a loop.
MAX_QR_REFRESHES = 1


class QrProtocolError(OpenCsiError):
    """The QR endpoint answered in a shape this client does not recognise."""

    code = "QR_PROTOCOL_ERROR"
    exit_code = EXIT_QR_PROTOCOL


class QrStatus(str, Enum):
    """The five states GitCode's poll can return. Compared by identity."""

    WAITING = "WAITING"
    SCAN = "SCAN"
    LOGIN = "LOGIN"
    TIMEOUT = "TIMEOUT"
    CANCEL = "CANCEL"

    @classmethod
    def parse(cls, raw: object) -> "QrStatus | None":
        """Map a wire value to a state, or ``None`` if unrecognised.

        An unknown value is *not* treated as an error: GitCode could add a
        state, and the safe response is to keep waiting rather than to abort a
        login the user is in the middle of.
        """
        if not isinstance(raw, str):
            return None
        try:
            return cls(raw.strip().upper())
        except ValueError:
            return None


class QrLoginStatus(str, Enum):
    """Outcome of a whole QR login attempt."""

    SUCCEEDED = "SUCCEEDED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    TIMEOUT = "TIMEOUT"
    NETWORK_ERROR = "NETWORK_ERROR"
    PROTOCOL_ERROR = "PROTOCOL_ERROR"


@dataclass(frozen=True)
class QrChallenge:
    """A pending QR login.

    ``scene_id`` is **sensitive** and is kept out of ``repr``; ``image`` is the
    displayable payload GitCode returned (a data URI or an https URL).
    """

    scene_id: str = field(repr=False)
    image: str = ""
    created_at: float = 0.0

    def __repr__(self) -> str:
        return (
            f"QrChallenge(image={self.image[:32]!r}..., "
            f"scene_id=<redacted>, created_at={self.created_at:.0f})"
        )


@dataclass(frozen=True)
class QrLoginResult:
    """Result of a QR login (secret-free).

    The GitCode tokens are deliberately **not** fields: they are registered for
    redaction and handed to the caller through :meth:`credentials`, which the
    CLI uses immediately and never stores.
    """

    status: QrLoginStatus
    detail: str | None = None
    username: str | None = None
    is_new_user: bool | None = None
    user_status: str | None = None
    polls: int = 0
    refreshes: int = 0
    _credentials: Mapping[str, str] = field(default_factory=dict, repr=False)

    @property
    def ok(self) -> bool:
        return self.status is QrLoginStatus.SUCCEEDED

    @property
    def requires_interaction(self) -> bool:
        return self.status in (
            QrLoginStatus.CANCELLED,
            QrLoginStatus.EXPIRED,
            QrLoginStatus.TIMEOUT,
        )

    def credentials(self) -> Mapping[str, str]:
        """The GitCode tokens. Call once, use immediately, do not store."""
        return dict(self._credentials)

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "status": self.status.value,
            "ok": self.ok,
            "polls": self.polls,
            "refreshes": self.refreshes,
        }
        if self.username:
            out["username"] = self.username
        if self.is_new_user is not None:
            out["is_new_user"] = self.is_new_user
        if self.user_status:
            out["user_status"] = self.user_status
        if self.detail:
            out["detail"] = self.detail
        return out


class GitCodeQrAuthenticator:
    """Drive the GitCode WeChat QR login over plain HTTP.

    Parameters
    ----------
    api_base:
        Overridable for tests; defaults to GitCode's real API host.
    opener:
        A ``urllib`` opener factory. Tests inject one that talks to a fake
        server; production uses a plain opener with a cookie jar, because the
        login call sets session cookies the OAuth completion needs.
    """

    name = "gitcode-qr"

    def __init__(
        self,
        *,
        api_base: str = API_BASE,
        source: str = DEFAULT_SOURCE,
        timeout: float = DEFAULT_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        max_wait: float = DEFAULT_MAX_WAIT,
        opener: Callable[[], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        use_proxy: bool = False,
    ) -> None:
        self._api_base = api_base.rstrip("/")
        self._source = source
        self._timeout = timeout
        self._poll_interval = poll_interval
        self._max_wait = max_wait
        self._sleep = sleep
        self._clock = clock
        self._use_proxy = use_proxy
        self._jar = CookieJar()
        self._opener_factory = opener
        self._opener: Any = None
        self._last_challenge: QrChallenge | None = None

    # ── HTTP plumbing ─────────────────────────────────────────────────────
    def _build_opener(self) -> Any:
        if self._opener_factory is not None:
            return self._opener_factory()
        handlers: list[Any] = [urllib.request.HTTPCookieProcessor(self._jar)]
        if not self._use_proxy:
            # The tool honours the system proxy for the openCsiTool API, but a
            # broken local proxy must not make the QR flow unreachable.
            handlers.append(urllib.request.ProxyHandler({}))
        return urllib.request.build_opener(*handlers)

    @property
    def opener(self) -> Any:
        if self._opener is None:
            self._opener = self._build_opener()
        return self._opener

    def _headers(self, *, json_body: bool = False) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Origin": ORIGIN,
            "Referer": REFERER,
            "X-Source": self._source,
            "X-Platform": "web",
            "X-App-Channel": "gitcode-fe",
            "X-App-Version": "0",
            "X-Device-ID": "unknown",
        }
        if json_body:
            headers["Content-Type"] = "application/json;charset=utf-8"
        return headers

    def _call(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: bytes | None = None,
    ) -> tuple[int, Any]:
        """Perform one request and return ``(status, decoded_body)``.

        Never raises for an HTTP error status: the caller decides what a 4xx
        means, and GitCode uses 400/405 meaningfully.
        """
        url = f"{self._api_base}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers=self._headers(json_body=body is not None),
        )
        try:
            with self.opener.open(request, timeout=self._timeout) as response:
                return response.status, self._decode(response.read())
        except urllib.error.HTTPError as exc:
            payload = b""
            try:
                payload = exc.read()
            except Exception:  # noqa: BLE001 - body is optional
                pass
            status = exc.code
            exc.close()
            return status, self._decode(payload)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise NetworkError(
                f"could not reach GitCode: {type(exc).__name__}"
            ) from exc

    @staticmethod
    def _decode(payload: bytes) -> Any:
        if not payload:
            return None
        try:
            return json.loads(payload.decode("utf-8", "replace"))
        except ValueError:
            return None

    @staticmethod
    def _unwrap(payload: Any) -> Any:
        """Unwrap GitCode's envelope.

        Responses arrive as ``{"data": {...}}`` and the real client reads
        ``response.data.data``. Some endpoints answer with the object directly,
        so both shapes are accepted rather than assuming one.
        """
        if isinstance(payload, Mapping) and "data" in payload:
            inner = payload.get("data")
            if isinstance(inner, Mapping):
                return inner
        return payload

    # ── the three protocol steps ──────────────────────────────────────────
    def start_login(self) -> QrChallenge:
        """Create a QR challenge.

        Raises :class:`OpenCsiError` on a protocol failure. This is the one call
        that creates server-side state; it is a *login* call, not a business
        write, and it is the documented mechanism for authenticating.
        """
        status, payload = self._call(
            "POST", QR_PATH.format(platform=PLATFORM)
        )
        if status != 200:
            raise QrProtocolError(
                f"GitCode refused to create a QR code (HTTP {status})",
                hint=(
                    "the QR endpoint may have changed; see "
                    "docs/gitcode-qr-protocol.md"
                ),
            )
        body = self._unwrap(payload)
        scene_id = ""
        image = ""
        if isinstance(body, Mapping):
            scene_id = str(body.get("scene_id") or "")
            image = str(body.get("qrcode") or "")
        if not scene_id:
            raise QrProtocolError(
                "GitCode returned no scene_id for the QR code",
                hint="the QR response shape may have changed",
            )
        # The scene id is the bearer value for this pending login.
        register_secret(scene_id)
        challenge = QrChallenge(
            scene_id=scene_id, image=image, created_at=self._clock()
        )
        self._last_challenge = challenge
        log.debug("created a QR challenge (image payload %d bytes)", len(image))
        return challenge

    def poll(self, challenge: QrChallenge) -> QrStatus | None:
        """Ask for the challenge's state. ``None`` means "unrecognised value"."""
        status, payload = self._call(
            "GET",
            QR_PATH.format(platform=PLATFORM),
            params={"scene_id": challenge.scene_id},
        )
        if status != 200:
            # A transient poll failure should not abort a login the user is in
            # the middle of; the caller keeps polling until the deadline.
            log.debug("QR poll answered HTTP %s; continuing", status)
            return QrStatus.WAITING
        body = self._unwrap(payload)
        raw = body.get("status") if isinstance(body, Mapping) else None
        return QrStatus.parse(raw)

    def complete(self, challenge: QrChallenge) -> Mapping[str, Any]:
        """Exchange a confirmed challenge for GitCode credentials."""
        status, payload = self._call(
            "POST",
            LOGIN_PATH.format(platform=PLATFORM),
            params={"scene_id": challenge.scene_id},
        )
        body = self._unwrap(payload)
        if status != 200 or not isinstance(body, Mapping):
            raise QrProtocolError(
                f"GitCode did not complete the QR login (HTTP {status})",
                hint="the QR may have expired; run the command again",
            )
        for key in ("access_token", "refresh_token", "xauth_token"):
            value = body.get(key)
            if isinstance(value, str) and value:
                register_secret(value)
        return body

    # ── the whole flow ────────────────────────────────────────────────────
    def login(
        self,
        *,
        timeout: float | None = None,
        on_challenge: Callable[[QrChallenge], None] | None = None,
        on_state: Callable[[QrStatus], None] | None = None,
    ) -> QrLoginResult:
        """Run the full flow: create, poll, complete.

        ``on_challenge`` is called with each new QR (the CLI renders it);
        ``on_state`` is called on every *change* of state so the CLI can print
        progress without repeating itself. Neither callback ever receives a
        secret beyond the challenge object, whose ``scene_id`` is ``repr=False``.
        """
        budget = self._max_wait if timeout is None else timeout
        deadline = self._clock() + max(1.0, budget)
        refreshes = 0
        polls = 0
        last_state: QrStatus | None = None

        while True:
            try:
                challenge = self.start_login()
            except OpenCsiError as exc:
                return QrLoginResult(
                    QrLoginStatus.PROTOCOL_ERROR, detail=scrub_text(str(exc))[:200]
                )
            if on_challenge is not None:
                on_challenge(challenge)

            outcome: QrLoginResult | None = None
            #: Set when the inner loop ended because the QR expired, so the
            #: outer loop re-issues a code instead of trying to *complete* a
            #: challenge that GitCode has already invalidated.
            expired = False
            while True:
                if self._clock() >= deadline:
                    return QrLoginResult(
                        QrLoginStatus.TIMEOUT,
                        detail="the QR login did not finish in time",
                        polls=polls,
                        refreshes=refreshes,
                    )
                self._sleep(self._poll_interval)
                try:
                    state = self.poll(challenge)
                except NetworkError as exc:
                    return QrLoginResult(
                        QrLoginStatus.NETWORK_ERROR,
                        detail=scrub_text(str(exc))[:200],
                        polls=polls,
                        refreshes=refreshes,
                    )
                polls += 1

                if state is not None and state is not last_state:
                    last_state = state
                    if on_state is not None:
                        on_state(state)

                if state is QrStatus.LOGIN:
                    break
                if state is QrStatus.CANCEL:
                    return QrLoginResult(
                        QrLoginStatus.CANCELLED,
                        detail="the scan was cancelled on the phone",
                        polls=polls,
                        refreshes=refreshes,
                    )
                if state is QrStatus.TIMEOUT:
                    # Exactly one automatic re-issue. Refreshing forever would
                    # leave a command running indefinitely with no way to tell
                    # that nothing is happening.
                    if refreshes >= MAX_QR_REFRESHES:
                        return QrLoginResult(
                            QrLoginStatus.EXPIRED,
                            detail="the QR code expired and was not re-issued again",
                            polls=polls,
                            refreshes=refreshes,
                        )
                    refreshes += 1
                    expired = True
                    break
                # WAITING / SCAN / unknown: keep polling.

            if expired:
                # Re-issue the code; do NOT try to complete the dead challenge.
                continue

            try:
                body = self.complete(challenge)
            except OpenCsiError as exc:
                if refreshes >= MAX_QR_REFRESHES:
                    return QrLoginResult(
                        QrLoginStatus.PROTOCOL_ERROR,
                        detail=scrub_text(str(exc))[:200],
                        polls=polls,
                        refreshes=refreshes,
                    )
                refreshes += 1
                continue

            credentials = {
                key: str(body[key])
                for key in ("access_token", "refresh_token", "xauth_token")
                if isinstance(body.get(key), str) and body.get(key)
            }
            return QrLoginResult(
                QrLoginStatus.SUCCEEDED,
                username=str(body.get("username") or "") or None,
                is_new_user=(
                    body.get("is_new") if isinstance(body.get("is_new"), bool) else None
                ),
                user_status=str(body.get("user_status_enum") or "") or None,
                polls=polls,
                refreshes=refreshes,
                _credentials=credentials,
            )

    @property
    def last_challenge(self) -> QrChallenge | None:
        return self._last_challenge

    def session_cookies(self) -> dict[str, str]:
        """Cookies GitCode set during this flow, by name.

        The login response returns tokens in the *body*, but it may also set
        session cookies. Whether it does decides how the openCsiTool OAuth step
        can be completed:

        * cookies present -> they can be planted into the dedicated browser and
          OAuth completes with no user involvement;
        * no cookies -> the tokens are only usable against GitCode's own API,
          and completing OAuth still needs a browser session.

        Reporting this honestly is the difference between "QR login works" and
        "QR login gets you a GitCode account, but openCsiTool still needs the
        browser". Never returns values, only names, so it is safe to print.
        """
        return {cookie.name: "<redacted>" for cookie in self._jar}

    def describe(self) -> str:
        return "GitCode WeChat mini-program QR login (pure HTTP, no browser)"

    def __repr__(self) -> str:
        return (
            f"GitCodeQrAuthenticator(api_base={self._api_base!r}, "
            f"scene_id=<redacted>)"
        )
