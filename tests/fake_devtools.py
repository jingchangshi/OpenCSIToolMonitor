"""A minimal in-process fake DevTools server.

Implements just enough of the Chrome DevTools Protocol to exercise
:class:`~opencsi.auth.cdp.CdpCookieProvider` without a browser:

* ``GET /json/version``  -- browser metadata (or a 404, to model Chrome 147+)
* ``GET /json/list``     -- page targets
* a WebSocket endpoint that answers ``Storage.getCookies``,
  ``Network.getCookies`` and ``Target.attachToTarget``.

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
from typing import Any, Callable

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

#: A synthetic cookie value; never a real credential.
FAKE_COOKIE = "TESTCOOKIE" + "f0e1d2c3b4a5" * 20

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
    import time

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
    ) -> None:
        self.cookies = list(cookies) if cookies is not None else [opencsitool_cookie()]
        self.json_api = json_api
        self.allow_ws = allow_ws
        self.refuse_browser_ws = refuse_browser_ws
        self.on_call = on_call
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
        return {}, session_id

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
