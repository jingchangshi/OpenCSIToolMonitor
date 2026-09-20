"""Minimal RFC 6455 WebSocket client built on the standard library.

Only what Chrome DevTools Protocol needs: a masked client handshake, text
frames, ping/pong, and fragmented-message reassembly. No third-party
dependency, because this tool must run on machines with no package index.

Scope limits (deliberate):

* Client-to-server frames are always masked (required by RFC 6455 §5.3).
* Only text frames are produced; CDP is JSON-over-text.
* Per-message deflate is not negotiated; CDP does not require it.
* Binary frames are accepted and decoded as UTF-8 (CDP uses text, but a
  misbehaving endpoint should not crash the client).
"""

from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
from typing import Any, Iterator
from urllib.parse import urlparse

from .errors import CdpUnavailableError, NetworkError

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

_OP_CONT = 0x0
_OP_TEXT = 0x1
_OP_BINARY = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA

_MAX_FRAME = 64 * 1024 * 1024  # 64 MiB guard against a hostile length header


class WebSocketError(NetworkError):
    """A WebSocket-level failure (handshake rejected, socket dropped)."""

    code = "WEBSOCKET_ERROR"


class WebSocket:
    """A blocking WebSocket client connection."""

    def __init__(
        self,
        url: str,
        *,
        timeout: float = 10.0,
        origin: str | None = None,
    ) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in ("ws", "wss"):
            raise CdpUnavailableError(f"unsupported WebSocket scheme: {parsed.scheme!r}")

        self.url = url
        self._timeout = timeout
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        try:
            raw = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise CdpUnavailableError(
                f"cannot connect to {host}:{port}: {type(exc).__name__}"
            ) from exc

        if parsed.scheme == "wss":
            try:
                ctx = ssl.create_default_context()
                raw = ctx.wrap_socket(raw, server_hostname=host)
            except (ssl.SSLError, OSError) as exc:
                raw.close()
                raise CdpUnavailableError(f"TLS handshake failed: {type(exc).__name__}") from exc

        self._sock = raw
        self._sock.settimeout(timeout)
        self._buf = b""
        self._closed = False
        self._frag_op: int | None = None
        self._frag_data = bytearray()

        # A failed handshake must not leak the TCP connection. Individual
        # failure paths inside _handshake close it too, but that is easy to get
        # wrong when a new branch is added, so the guarantee is enforced here.
        try:
            self._handshake(host, port, path, origin)
        except BaseException:
            self._close_socket()
            self._closed = True
            raise

    # ── handshake ─────────────────────────────────────────────────────────
    def _handshake(self, host: str, port: int, path: str, origin: str | None) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        host_header = f"{host}:{port}"
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host_header}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        if origin:
            lines.append(f"Origin: {origin}")
        request = "\r\n".join(lines) + "\r\n\r\n"

        try:
            self._sock.sendall(request.encode("ascii"))
        except OSError as exc:
            self._close_socket()
            raise CdpUnavailableError(f"handshake send failed: {type(exc).__name__}") from exc

        try:
            while b"\r\n\r\n" not in self._buf:
                chunk = self._sock.recv(4096)
                if not chunk:
                    raise WebSocketError("connection closed during WebSocket handshake")
                self._buf += chunk
        except socket.timeout as exc:
            self._close_socket()
            raise CdpUnavailableError(
                "WebSocket handshake timed out (the DevTools endpoint did not upgrade)"
            ) from exc
        except OSError as exc:
            self._close_socket()
            raise CdpUnavailableError(f"handshake read failed: {type(exc).__name__}") from exc

        head, _, rest = self._buf.partition(b"\r\n\r\n")
        self._buf = rest
        text = head.decode("latin-1")
        status_line = text.split("\r\n", 1)[0]
        parts = status_line.split(" ", 2)
        status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0

        if status != 101:
            self._close_socket()
            if status == 403:
                raise CdpUnavailableError(
                    "DevTools rejected the WebSocket upgrade with 403 (the "
                    "'Allow remote debugging' prompt may be pending, or this "
                    "endpoint is not a DevTools server)"
                )
            raise CdpUnavailableError(
                f"WebSocket upgrade refused with HTTP {status or '???'}"
            )

        expected = base64.b64encode(
            __import__("hashlib").sha1((key + _WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        if expected.lower() not in text.lower():
            self._close_socket()
            raise WebSocketError("WebSocket handshake returned an invalid Sec-WebSocket-Accept")

    # ── frame IO ──────────────────────────────────────────────────────────
    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            try:
                chunk = self._sock.recv(max(4096, n - len(self._buf)))
            except socket.timeout as exc:
                raise WebSocketError("timed out waiting for data from the DevTools endpoint") from exc
            except OSError as exc:
                raise WebSocketError(f"socket read failed: {type(exc).__name__}") from exc
            if not chunk:
                self._closed = True
                raise WebSocketError("connection closed by the DevTools endpoint")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._closed:
            raise WebSocketError("cannot send on a closed WebSocket")
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        try:
            self._sock.sendall(bytes(header) + masked)
        except OSError as exc:
            raise WebSocketError(f"socket write failed: {type(exc).__name__}") from exc

    def send_text(self, text: str) -> None:
        """Send a text frame."""
        self._send_frame(_OP_TEXT, text.encode("utf-8"))

    def recv_text(self) -> str | None:
        """Receive the next complete text message.

        Control frames are handled transparently (pings are answered).
        Returns ``None`` if the peer closed cleanly.
        """
        while True:
            try:
                b0, b1 = self._recv_exact(2)
            except WebSocketError as exc:
                if self._closed and "closed by" in str(exc):
                    return None
                raise

            fin = bool(b0 & 0x80)
            opcode = b0 & 0x0F
            masked = bool(b1 & 0x80)
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]

            if length > _MAX_FRAME:
                raise WebSocketError(f"frame too large: {length} bytes")

            mask_key = self._recv_exact(4) if masked else None
            data = self._recv_exact(length) if length else b""
            if mask_key:
                data = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))

            if opcode == _OP_CLOSE:
                self._closed = True
                try:
                    self._send_frame(_OP_CLOSE, b"")
                except Exception:
                    pass
                self._close_socket()
                return None
            if opcode == _OP_PING:
                self._send_frame(_OP_PONG, data)
                continue
            if opcode == _OP_PONG:
                continue

            if opcode == _OP_CONT:
                if self._frag_op is None:
                    continue  # stray continuation
                self._frag_data += data
                if fin:
                    payload = bytes(self._frag_data)
                    self._frag_data.clear()
                    self._frag_op = None
                    return payload.decode("utf-8", "replace")
                continue

            if opcode in (_OP_TEXT, _OP_BINARY):
                if fin:
                    return data.decode("utf-8", "replace")
                self._frag_op = opcode
                self._frag_data = bytearray(data)
                continue

            # Unknown opcode: ignore rather than fail.

    def close(self) -> None:
        """Send a close frame (best effort) and release the socket."""
        if not self._closed:
            try:
                self._send_frame(_OP_CLOSE, b"")
            except Exception:
                pass
            self._closed = True
        self._close_socket()

    def _close_socket(self) -> None:
        try:
            self._sock.close()
        except Exception:
            pass

    def __enter__(self) -> "WebSocket":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        # Never include query strings: a CDP URL can carry a token in some setups.
        parsed = urlparse(self.url)
        return f"WebSocket({parsed.scheme}://{parsed.hostname}:{parsed.port}{parsed.path}, closed={self._closed})"


class CdpConnection:
    """JSON-RPC style request/response multiplexer over a CDP WebSocket.

    Supports *flattened* sessions: when a message carries a ``sessionId``, the
    reply is matched on ``(sessionId, id)``.
    """

    def __init__(self, url: str, *, timeout: float = 15.0) -> None:
        self._ws = WebSocket(url, timeout=timeout)
        self._next_id = 0
        self._timeout = timeout
        self._events: list[dict[str, Any]] = []

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Invoke ``method`` and return its ``result``.

        Raises :class:`WebSocketError` on a CDP-level ``error`` reply.
        """
        self._next_id += 1
        mid = self._next_id
        message: dict[str, Any] = {"id": mid, "method": method}
        if params is not None:
            message["params"] = params
        if session_id:
            message["sessionId"] = session_id

        self._ws.send_text(json.dumps(message))

        deadline_s = timeout if timeout is not None else self._timeout
        old_timeout = self._ws._sock.gettimeout()
        try:
            self._ws._sock.settimeout(deadline_s)
            while True:
                raw = self._ws.recv_text()
                if raw is None:
                    raise WebSocketError("DevTools endpoint closed the connection")
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("id") != mid:
                    # An event, or a reply to a different in-flight call.
                    if "id" not in obj and "method" in obj:
                        self._events.append(obj)
                    continue
                if session_id and obj.get("sessionId") not in (None, session_id):
                    continue
                if "error" in obj:
                    err = obj["error"] or {}
                    raise WebSocketError(
                        f"CDP {method} failed: {err.get('message') or err}"
                    )
                return obj.get("result") or {}
        finally:
            try:
                self._ws._sock.settimeout(old_timeout)
            except Exception:
                pass

    def drain_events(self) -> list[dict[str, Any]]:
        """Return and clear buffered CDP events seen while awaiting replies."""
        out, self._events = self._events, []
        return out

    def close(self) -> None:
        self._ws.close()

    def __enter__(self) -> "CdpConnection":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def iter_json_messages(ws: WebSocket) -> Iterator[dict[str, Any]]:
    """Yield decoded JSON objects from ``ws`` until it closes.

    Convenience helper for diagnostics; not used by the request/response path.
    """
    while True:
        raw = ws.recv_text()
        if raw is None:
            return
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj
