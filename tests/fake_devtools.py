"""A minimal in-process fake DevTools server.

Implements just enough of the Chrome DevTools Protocol to exercise
:class:`~opencsi.auth.cdp.CdpCookieProvider` and
:class:`~opencsi.auth.oauth_browser.BrowserOAuthRenewer` without a browser:

* ``GET /json/version``  -- browser metadata (or a 404, to model Chrome 147+)
* ``GET /json/list``     -- page targets
* a WebSocket endpoint that answers ``Storage.getCookies``,
  ``Network.getCookies`` and ``Target.attachToTarget``.
* ``Target.createTarget`` / ``Target.closeTarget`` / ``Page.navigate`` /
  ``Runtime.evaluate`` -- the silent-renewal surface, driven by an
  :class:`OAuthScenario` so a test can script "SSO still valid, here is a new
  cookie", "SSO gone, we landed on the login page" or "the callback never
  completes".

It also models the two failure modes that actually occur in the field:

* **``/json`` disabled** (Chrome 147+ on the default profile): ``/json/*``
  returns 404 and the browser-level WebSocket upgrade is *refused*, which is
  what produces the actionable hint.
* **no target / no cookie**: the endpoint works but yields nothing useful.

Everything is bound to ``127.0.0.1`` on an ephemeral port and torn down after
each test, so the suite stays hermetic.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

#: A synthetic cookie value; never a real credential.
FAKE_COOKIE = "TESTCOOKIE" + "f0e1d2c3b4a5" * 20

#: A second synthetic cookie, distinct from :data:`FAKE_COOKIE`, used as the
#: "newly issued after renewal" value. Also never a real credential.
RENEWED_COOKIE = "RENEWEDCOOKIE" + "9a8b7c6d5e4f" * 20

#: Two other cookies so the selector has to actually choose.
OTHER_COOKIES = [
    {"name": "SESSION", "value": "unrelated", "domain": "example.com", "expires": -1},
    {"name": "token", "value": "short", "domain": "other.example", "expires": -1},
]


def opencsitool_cookie(
    value: str = FAKE_COOKIE,
    *,
    expires_in: float = 3600.0,
    domain: str = "opencsitool.com",
) -> dict[str, Any]:
    """Build a cookie record in CDP's shape."""
    return {
        "name": "token",
        "value": value,
        "domain": domain,
        "path": "/",
        "expires": time.time() + expires_in,
        "size": len(value),
        "httpOnly": True,
        "secure": True,
        "session": False,
    }


@dataclass
class OAuthScenario:
    """Scripted behaviour for the silent-renewal CDP surface.

    ``outcome`` drives what a ``Page.navigate`` to the OAuth URL does:

    ``"renew"``
        The GitCode SSO session is alive. The tab ends up back on the app host
        and the cookie jar is replaced with ``new_cookie``.
    ``"login"``
        The GitCode SSO session is gone. The tab settles on the GitCode login
        page and no new cookie appears.
    ``"noop"``
        The round-trip completes but the cookie is unchanged (the server
        re-issued an identical value with the same expiry) -- renewal must NOT
        be reported as success.
    ``"timeout"``
        The tab never leaves the OAuth URL; the caller's deadline expires.
    ``"consent"``
        The tab parks on ``gitcode.com/oauth/authorize`` with an unanswered
        approval control. The SSO session is alive, so this is *not* a login
        problem -- and it must not be reported as a timeout either, which is what
        the renewer used to do: it polled until its budget expired and blamed the
        browser. Measured live, approving the page issued a fresh 60-minute
        token, so the flow was one click from working.
    ``"no_cookie"``
        Back on the app host, but the cookie jar is empty.
    """

    outcome: str = "renew"
    new_cookie: str = RENEWED_COOKIE
    new_expires_in: float = 3600.0
    navigate_raises: str | None = None
    create_target_fails: bool = False
    close_target_fails: bool = False
    #: Extra delay before the cookie is swapped, to exercise the settle wait.
    settle_delay: float = 0.0
    #: Methods observed, for assertions about what the renewer actually did.
    calls: list[str] = field(default_factory=list)
    created_targets: list[str] = field(default_factory=list)
    closed_targets: list[str] = field(default_factory=list)
    navigated_urls: list[str] = field(default_factory=list)


class FakeDevToolsServer:
    """A threaded TCP server speaking the DevTools HTTP + WebSocket protocol."""

    def __init__(
        self,
        *,
        cookies: list[dict[str, Any]] | None = None,
        json_api: bool = True,
        allow_ws: bool = True,
        pages: list[dict[str, Any]] | None = None,
        list_pages: list[dict[str, Any]] | None = None,
        refuse_browser_ws: bool = False,
        on_call: Callable[[str, dict[str, Any]], dict[str, Any] | None] | None = None,
        oauth: OAuthScenario | None = None,
    ) -> None:
        self.cookies = list(cookies) if cookies is not None else [opencsitool_cookie()]
        self.json_api = json_api
        self.allow_ws = allow_ws
        self.refuse_browser_ws = refuse_browser_ws
        self.on_call = on_call
        self.oauth = oauth
        self.pages = pages if pages is not None else [
            {
                "id": "PAGE1",
                "type": "page",
                "title": "My Tools",
                "url": "https://opencsitool.com/myTools",
                "webSocketDebuggerUrl": "",  # filled in after bind
            }
        ]
        # ``Target.getTargets`` and ``/json/list`` do not always agree: a target
        # can be attachable while exposing no page-level WebSocket URL. Modelling
        # that divergence is what lets a test force the browser strategy.
        self.list_pages = list_pages if list_pages is not None else self.pages
        self.requests: list[str] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        #: Target id -> current URL, for the renewal surface.
        self.target_urls: dict[str, str] = {}
        self._target_seq = 0
        self._lock = threading.Lock()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        self.host = "127.0.0.1"
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        self._fix_page_urls()

    # -- lifecycle --------------------------------------------------------
    def _fix_page_urls(self) -> None:
        for page in self.pages:
            page["webSocketDebuggerUrl"] = (
                f"ws://{self.host}:{self.port}/devtools/page/{page['id']}"
            )
        for page in self.list_pages:
            page.setdefault(
                "webSocketDebuggerUrl",
                f"ws://{self.host}:{self.port}/devtools/page/{page['id']}",
            )

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def browser_ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}/devtools/browser/FAKE-BROWSER-ID"

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=3)

    def __enter__(self) -> "FakeDevToolsServer":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- accept loop ------------------------------------------------------
    def _serve(self) -> None:
        self._sock.settimeout(0.4)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(5.0)
            request = self._read_http_head(conn)
            if not request:
                return
            method, path, headers = request
            self.requests.append(f"{method} {path}")

            # An upgrade request carries ``Upgrade: websocket``. Compare the
            # value, not substring-membership of the header name.
            if headers.get("upgrade", "").lower() == "websocket":
                self._do_websocket(conn, path, headers)
                return
            self._do_http(conn, path)
        except (OSError, ValueError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _read_http_head(conn: socket.socket) -> tuple[str, str, dict[str, str]] | None:
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = conn.recv(4096)
            if not chunk:
                return None
            buffer += chunk
        head = buffer.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        lines = head.split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) < 2:
            return None
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                key, _, value = line.partition(":")
                headers[key.strip().lower()] = value.strip()
        return parts[0], parts[1], headers

    # -- HTTP -------------------------------------------------------------
    def _do_http(self, conn: socket.socket, path: str) -> None:
        if not self.json_api:
            # Chrome 147+ on the default profile: /json/* is not served at all.
            self._send_http(conn, 404, b"", "text/plain")
            return
        if path.startswith("/json/version"):
            body = json.dumps(
                {
                    "Browser": "Chrome/153.0.8010.50",
                    "Protocol-Version": "1.3",
                    "webSocketDebuggerUrl": self.browser_ws_url,
                }
            ).encode()
            self._send_http(conn, 200, body, "application/json")
            return
        if path.startswith("/json/list") or path == "/json":
            body = json.dumps(self.list_pages).encode()
            self._send_http(conn, 200, body, "application/json")
            return
        self._send_http(conn, 404, b"", "text/plain")

    @staticmethod
    def _send_http(conn: socket.socket, status: int, body: bytes, content_type: str) -> None:
        reason = {200: "OK", 404: "Not Found"}.get(status, "Error")
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        conn.sendall(head + body)

    # -- WebSocket --------------------------------------------------------
    def _do_websocket(self, conn: socket.socket, path: str, headers: dict[str, str]) -> None:
        if not self.allow_ws:
            self._send_http(conn, 403, b"", "text/plain")
            return
        if self.refuse_browser_ws and "/devtools/browser/" in path:
            # The exact field failure: the TCP connection is accepted and the
            # HTTP head is read, then nothing is ever sent back.
            return
        key = headers.get("sec-websocket-key", "")
        if not key:
            self._send_http(conn, 400, b"", "text/plain")
            return
        accept = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        conn.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode("latin-1")
        )
        session_id: str | None = None
        while True:
            message = self._recv_frame(conn)
            if message is None:
                return
            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                continue
            method = str(payload.get("method", ""))
            params = payload.get("params") or {}
            self.calls.append((method, params))
            reply, session_id = self._dispatch(method, params, session_id, path)
            if reply is not None:
                frame = {"id": payload.get("id"), "result": reply}
                if session_id:
                    frame["sessionId"] = session_id
                self._send_frame(conn, json.dumps(frame))

    def _dispatch(
        self,
        method: str,
        params: dict[str, Any],
        session_id: str | None,
        path: str,
    ) -> tuple[dict[str, Any] | None, str | None]:
        if self.on_call is not None:
            override = self.on_call(method, params)
            if override is not None:
                return override, session_id

        if method == "Storage.getCookies":
            return {"cookies": self.cookies}, session_id
        if method == "Network.getCookies":
            return {"cookies": self.cookies}, session_id
        if method == "Target.getTargets":
            return {"targetInfos": self.pages}, session_id
        if method == "Target.attachToTarget":
            return {"sessionId": "SESSION-1"}, "SESSION-1"
        if method == "Browser.getVersion":
            return {"product": "Chrome/153.0.8010.50"}, session_id

        # ── silent-renewal surface ────────────────────────────────────────
        if method == "Target.createTarget":
            if self.oauth is not None and self.oauth.create_target_fails:
                return {"targetId": ""}, session_id
            with self._lock:
                self._target_seq += 1
                target_id = f"RENEW-{self._target_seq}"
                self.target_urls[target_id] = str(params.get("url") or "about:blank")
            if self.oauth is not None:
                self.oauth.calls.append(method)
                self.oauth.created_targets.append(target_id)
            return {"targetId": target_id}, session_id

        if method == "Target.closeTarget":
            target_id = str(params.get("targetId") or "")
            if self.oauth is not None:
                self.oauth.calls.append(method)
                self.oauth.closed_targets.append(target_id)
                if self.oauth.close_target_fails:
                    raise ValueError("closeTarget refused")
            self.target_urls.pop(target_id, None)
            return {"success": True}, session_id

        if method == "Page.navigate":
            if self.oauth is not None:
                self.oauth.calls.append(method)
                self.oauth.navigated_urls.append(str(params.get("url") or ""))
                if self.oauth.navigate_raises:
                    raise ValueError(self.oauth.navigate_raises)
            return {"frameId": "FRAME-1"}, session_id

        if method == "Runtime.evaluate":
            # Two different questions are asked through this one method, and the
            # fake has to answer them differently. `location.href` drives the
            # state machine; the consent probe asks the document whether an
            # approval control exists. Telling them apart by the expression text
            # is deliberate: it keeps the fake honest about which call the
            # renewer actually made, so a renewer that stopped asking the consent
            # question would get a location string back and fail the test rather
            # than silently pass.
            expression = str(params.get("expression") or "")
            if "querySelectorAll" in expression:
                pending = self.oauth is not None and self.oauth.outcome == "consent"
                return {"result": {"type": "boolean", "value": pending}}, session_id
            return {"result": {"type": "string", "value": self._renewal_location()}}, session_id

        if method == "Page.enable":
            return {}, session_id

        return {}, session_id

    def _renewal_location(self) -> str:
        """Where the renewal tab currently is, per the scenario.

        This is what the renewer polls, so it is where the state machine of a
        renewal is actually encoded:

        * ``renew``   -- first poll: still on GitCode; later polls: back on the
          app host, and the cookie jar has been swapped by then.
        * ``login``   -- settles on the GitCode login page.
        * ``noop``    -- back on the app host, cookie unchanged.
        * ``timeout`` -- never leaves the OAuth URL.
        * ``consent`` -- parks on the authorize URL with an approval control
          waiting; the page-level probe (not this method) reports the control.
        * ``timeout_then_renew`` -- never leaves the OAuth URL *within the
          budget*, but does install a fresh cookie. This models a cold start
          that outran its deadline yet genuinely renewed, which is a real
          outcome: it was observed on a live browser.
        * ``no_cookie`` -- back on the app host with an empty jar.
        """
        scenario = self.oauth
        if scenario is None:
            return "https://opencsitool.com/myTools"
        outcome = scenario.outcome

        if outcome == "timeout":
            return "https://gitcode.com/oauth/authorize?client_id=fake"
        if outcome == "consent":
            # Same URL as `timeout`, on purpose: the *path* cannot tell them
            # apart, which is the whole reason the renewer has to ask the page
            # rather than pattern-match the URL.
            return "https://gitcode.com/oauth/authorize?client_id=fake"

        if outcome == "timeout_then_renew":
            # Still on GitCode as far as the URL is concerned -- so the renewer
            # sees a timeout -- but the cookie has already been installed.
            with self._lock:
                self.cookies = [
                    opencsitool_cookie(
                        scenario.new_cookie, expires_in=scenario.new_expires_in
                    )
                ]
            if scenario.settle_delay:
                time.sleep(scenario.settle_delay)
            return "https://gitcode.com/oauth/authorize?client_id=fake"

        if outcome == "login":
            return "https://gitcode.com/login"

        # Every other outcome ends up back on the app host. The cookie swap
        # happens on the *second* poll so the renewer's settle wait is exercised
        # rather than accidentally skipped.
        with self._lock:
            polls = getattr(self, "_renewal_polls", 0) + 1
            self._renewal_polls = polls
            if polls >= 2:
                if outcome == "renew":
                    self.cookies = [
                        opencsitool_cookie(
                            scenario.new_cookie, expires_in=scenario.new_expires_in
                        )
                    ]
                elif outcome == "no_cookie":
                    self.cookies = []
        if scenario.settle_delay:
            time.sleep(scenario.settle_delay)
        return "https://opencsitool.com/myTools"

    @staticmethod
    def _recv_frame(conn: socket.socket) -> str | None:
        """Read one masked text frame from the client."""
        header = _recv_exact(conn, 2)
        if header is None:
            return None
        b0, b1 = header
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            extra = _recv_exact(conn, 2)
            if extra is None:
                return None
            length = struct.unpack(">H", extra)[0]
        elif length == 127:
            extra = _recv_exact(conn, 8)
            if extra is None:
                return None
            length = struct.unpack(">Q", extra)[0]

        mask = _recv_exact(conn, 4) if masked else b"\x00\x00\x00\x00"
        if mask is None:
            return None
        payload = _recv_exact(conn, length)
        if payload is None:
            return None
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

        if opcode == 0x8:  # close
            return None
        if opcode == 0x9:  # ping -> pong
            return ""
        return payload.decode("utf-8", "replace")

    @staticmethod
    def _send_frame(conn: socket.socket, text: str) -> None:
        payload = text.encode("utf-8")
        header = bytearray([0x80 | 0x1])
        length = len(payload)
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header += struct.pack(">H", length)
        else:
            header.append(127)
            header += struct.pack(">Q", length)
        conn.sendall(bytes(header) + payload)


def _recv_exact(conn: socket.socket, n: int) -> bytes | None:
    buffer = b""
    while len(buffer) < n:
        try:
            chunk = conn.recv(n - len(buffer))
        except (OSError, TimeoutError):
            return None
        if not chunk:
            return None
        buffer += chunk
    return buffer
