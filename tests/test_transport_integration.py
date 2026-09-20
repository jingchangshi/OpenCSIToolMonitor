"""Integration tests over a real socket.

The rest of the suite drives the client through ``FakeTransport``, which records
calls without performing any I/O. That is the right default -- hermetic and fast
-- but it means the layer that actually speaks HTTP was never exercised end to
end, so a bug in header construction, URL building, retry or error
classification would have gone unnoticed.

These tests start a real ``http.server`` on an ephemeral loopback port and put
the real ``HttpTransport`` underneath the real ``OpenCsiToolClient``. Still no
network access and no external dependency: everything is bound to 127.0.0.1 and
torn down per test.

The two layers have distinct jobs, and these tests pin down the boundary:

* ``HttpTransport`` returns a :class:`Response` for **any** HTTP status, and
  raises only for transport-level trouble (connection failure, unparseable
  body). It retries 502/503/504.
* ``OpenCsiToolClient`` turns a status into a documented exception.

Asserting on what the *server actually received* is the point: a source-level
grep for "Authorization" would be fooled by a comment.
"""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

import helpers

from opencsi.client import OpenCsiToolClient
from opencsi.errors import (
    BusinessApiError,
    NetworkError,
    PermissionDeniedError,
    ServerError,
    SessionExpiredError,
)
from opencsi.transport import HttpTransport

#: Set per test; the handler reads it to decide what to answer.
_SCRIPT: dict = {}

#: The openCsiTool envelope for a healthy response.
_OK_BODY = {"code": 200, "data": {"ok": True}, "message": "success"}


class _Handler(BaseHTTPRequestHandler):
    received: list[dict] = []

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802 - http.server naming
        type(self).received.append(
            {
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
            }
        )
        entry = _SCRIPT.get(self.path.split("?")[0], _SCRIPT.get("*", {}))
        status = entry.get("status", 200)
        body = entry.get("body", _OK_BODY)
        if not isinstance(body, (str, bytes)):
            body = json.dumps(body)
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", entry.get("content_type", "application/json"))
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _Server:
    """Context manager around a loopback HTTP server."""

    def __init__(self) -> None:
        _Handler.received = []
        self.httpd = HTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "_Server":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def _transport(server: _Server, **kwargs) -> HttpTransport:
    """A transport pointed at the loopback server, never through a proxy."""
    return HttpTransport(server.base_url, use_proxy=False, **kwargs)


class TransportOverRealSocketTest(unittest.TestCase):
    """What the HTTP layer itself guarantees."""

    def setUp(self) -> None:
        _SCRIPT.clear()

    def test_a_get_round_trips(self) -> None:
        with _Server() as server:
            transport = _transport(server)
            try:
                response = transport.get_json("/opencsitool/rest/v1/user/getUserInfo")
            finally:
                transport.close()

        self.assertEqual(response.status, 200)
        self.assertTrue(response.ok)
        self.assertEqual(response.json()["data"]["ok"], True)
        self.assertGreaterEqual(response.elapsed_ms, 0.0)

    def test_the_cookie_is_sent_in_the_format_the_site_expects(self) -> None:
        with _Server() as server:
            transport = _transport(server)
            transport.set_cookie("SUPER_SECRET_COOKIE_123")
            try:
                transport.get_json("/opencsitool/rest/v1/user/getUserInfo")
            finally:
                transport.close()

        sent = _Handler.received[0]["headers"]
        self.assertEqual(sent.get("cookie"), "token=SUPER_SECRET_COOKIE_123")

    def test_no_authorization_header_is_ever_sent(self) -> None:
        """The site rejects a Bearer header, so assert on what arrived."""
        with _Server() as server:
            transport = _transport(server)
            transport.set_cookie("SUPER_SECRET_COOKIE_123")
            try:
                transport.get_json("/opencsitool/rest/v1/user/getUserInfo")
            finally:
                transport.close()

        sent = _Handler.received[0]["headers"]
        self.assertNotIn("authorization", sent)
        # urllib adds Host, Accept-Encoding and Connection itself; the headers
        # this package chooses are exactly these three.
        for header in ("accept", "accept-language", "cookie", "user-agent"):
            self.assertIn(header, sent)
        self.assertTrue(sent["user-agent"].startswith("opencsi-cli/"))

    def test_query_parameters_are_encoded(self) -> None:
        with _Server() as server:
            transport = _transport(server)
            try:
                transport.get_json(
                    "/opencsitool/rest/v1/ai/operations/personalQueueStatus",
                    {"startDate": "2026-08-20", "endDate": "2026-09-19"},
                )
            finally:
                transport.close()

        path = _Handler.received[0]["path"]
        self.assertIn("startDate=2026-08-20", path)
        self.assertIn("endDate=2026-09-19", path)

    def test_none_valued_parameters_are_omitted(self) -> None:
        with _Server() as server:
            transport = _transport(server)
            try:
                transport.get_json("/x", {"a": "1", "b": None})
            finally:
                transport.close()
        path = _Handler.received[0]["path"]
        self.assertIn("a=1", path)
        self.assertNotIn("b=", path)

    def test_the_transport_returns_a_response_for_any_status(self) -> None:
        """Classification is the client's job, not the transport's."""
        with _Server() as server:
            _SCRIPT["*"] = {"status": 401, "body": "empty Authorization"}
            transport = _transport(server, attempts=1)
            try:
                response = transport.get_json("/opencsitool/rest/v1/user/getUserInfo")
            finally:
                transport.close()
        self.assertEqual(response.status, 401)
        self.assertFalse(response.ok)
        self.assertEqual(response.body, "empty Authorization")

    def test_5xx_is_retried(self) -> None:
        with _Server() as server:
            _SCRIPT["*"] = {"status": 503, "body": {"message": "unavailable"}}
            transport = _transport(server, attempts=3, backoff=0.0)
            try:
                response = transport.get_json("/opencsitool/rest/v1/user/getUserInfo")
            finally:
                transport.close()
        self.assertEqual(response.status, 503)
        # attempts=3 means the first try plus two retries.
        self.assertEqual(len(_Handler.received), 3)

    def test_a_404_is_not_retried(self) -> None:
        """A client error is a result, not something to hammer the server with."""
        with _Server() as server:
            _SCRIPT["*"] = {"status": 404, "body": {"message": "not found"}}
            transport = _transport(server, attempts=3, backoff=0.0)
            try:
                transport.get_json("/opencsitool/rest/v1/nope")
            finally:
                transport.close()
        self.assertEqual(len(_Handler.received), 1)

    def test_a_non_json_body_raises_a_network_error_on_parse(self) -> None:
        """The SPA shell is HTML; parsing it must not leak a JSONDecodeError."""
        with _Server() as server:
            _SCRIPT["*"] = {
                "status": 200,
                "body": "<!doctype html><html><body>SPA shell</body></html>",
                "content_type": "text/html",
            }
            transport = _transport(server)
            try:
                response = transport.get_json("/v3/api-docs")
                with self.assertRaises(NetworkError) as ctx:
                    response.json()
            finally:
                transport.close()
        self.assertNotIsInstance(ctx.exception, json.JSONDecodeError)

    def test_a_connection_refused_becomes_a_network_error(self) -> None:
        transport = HttpTransport("http://127.0.0.1:1", use_proxy=False, attempts=1)
        try:
            with self.assertRaises(NetworkError):
                transport.get_json("/opencsitool/rest/v1/user/getUserInfo")
        finally:
            transport.close()

    def test_the_transport_has_no_write_method(self) -> None:
        """Read-only is structural, not a convention."""
        for verb in ("post", "put", "patch", "delete", "request"):
            self.assertFalse(
                hasattr(HttpTransport, verb), f"HttpTransport must not expose {verb}()"
            )

    def test_repr_never_shows_the_cookie(self) -> None:
        transport = HttpTransport("http://127.0.0.1:1", use_proxy=False)
        transport.set_cookie("SUPER_SECRET_COOKIE_123")
        try:
            self.assertNotIn("SUPER_SECRET_COOKIE_123", repr(transport))
        finally:
            transport.close()


class ClientOverRealSocketTest(unittest.TestCase):
    """The full stack: real client -> real HTTP -> real socket."""

    def setUp(self) -> None:
        _SCRIPT.clear()

    def _client(self, server: _Server, provider) -> OpenCsiToolClient:
        return OpenCsiToolClient(
            provider,
            base_url=server.base_url,
            transport=_transport(server, attempts=1),
            cache_ttl=0.0,
        )

    def test_a_business_failure_inside_a_200_is_not_a_success(self) -> None:
        """HTTP 200 with code != 200 must raise, not return quietly."""
        with _Server() as server:
            _SCRIPT["*"] = {
                "status": 200,
                "body": {"code": 500, "data": None, "message": "internal error"},
            }
            client = self._client(server, helpers.StubCredentialProvider())
            try:
                with self.assertRaises(BusinessApiError) as ctx:
                    client.get_my_tools()
            finally:
                client.close()
        self.assertIn("500", str(ctx.exception))

    def test_401_becomes_session_expired(self) -> None:
        with _Server() as server:
            _SCRIPT["*"] = {"status": 401, "body": "empty Authorization"}
            client = self._client(server, helpers.StubCredentialProvider())
            try:
                with self.assertRaises(SessionExpiredError) as ctx:
                    client.login_or_restore_session()
            finally:
                client.close()
        self.assertEqual(ctx.exception.http_status, 401)

    def test_403_becomes_permission_denied(self) -> None:
        with _Server() as server:
            _SCRIPT["*"] = {"status": 403, "body": {"message": "forbidden"}}
            client = self._client(server, helpers.StubCredentialProvider())
            try:
                with self.assertRaises(PermissionDeniedError):
                    client.login_or_restore_session()
            finally:
                client.close()

    def test_a_500_becomes_a_server_error(self) -> None:
        with _Server() as server:
            _SCRIPT["*"] = {"status": 500, "body": {"message": "boom"}}
            client = self._client(server, helpers.StubCredentialProvider())
            try:
                with self.assertRaises(ServerError):
                    client.login_or_restore_session()
            finally:
                client.close()

    def test_an_unexpected_authorization_header_is_reported_as_such(self) -> None:
        """A 401 saying "Invalid Authorization" means we sent a bad header.

        The site answers "empty Authorization" when the cookie is missing and
        "Invalid Authorization" when a header was supplied. Those are different
        bugs, so they must not collapse into one message.
        """
        from opencsi.errors import BadAuthHeaderError

        with _Server() as server:
            _SCRIPT["*"] = {"status": 401, "body": "Invalid Authorization"}
            client = self._client(server, helpers.StubCredentialProvider())
            try:
                with self.assertRaises(BadAuthHeaderError):
                    client.login_or_restore_session()
            finally:
                client.close()

    def test_a_real_session_flow_succeeds_end_to_end(self) -> None:
        """getUserInfo -> personalQueueStatus over an actual socket."""
        with _Server() as server:
            _SCRIPT["*"] = {"body": {"code": 200, "data": {"ok": True}, "message": "ok"}}
            provider = helpers.StubCredentialProvider()
            client = self._client(server, provider)
            try:
                identity = client.login_or_restore_session()
            finally:
                client.close()

        self.assertIsNotNone(identity)
        # The cookie really travelled: the provider was asked for it.
        self.assertGreaterEqual(provider.reads, 1)
        self.assertEqual(_Handler.received[0]["headers"].get("cookie"), f"token={helpers.FAKE_TOKEN}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
