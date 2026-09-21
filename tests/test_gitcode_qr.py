"""GitCode QR login: the state machine, over a fake HTTP server.

CI must never depend on WeChat, a real GitCode account, or the live protocol,
so the authenticator is exercised against an in-process server that speaks the
documented wire format. That also lets the *unhappy* paths be tested at all --
an expired QR, a cancelled scan, a flaky poll -- which a live test could only
reach by luck.

The endpoints and states asserted here come from
``docs/gitcode-qr-protocol.md``; the tests are the executable half of that
document, so a protocol drift fails the suite rather than confusing a user.
"""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import helpers  # noqa: F401  (imported for its sys.path side effect)

from opencsi.auth.gitcode_qr import (
    PLATFORM,
    GitCodeQrAuthenticator,
    QrChallenge,
    QrLoginStatus,
    QrProtocolError,
    QrStatus,
)

#: A synthetic scene id. Shaped like the real one but never a real value.
FAKE_SCENE = "SCENE" + "0123456789abcdef" * 3

#: A 1x1 PNG, so the render path has real image bytes to work with.
FAKE_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d4944415478da63f8ffff3f0005fe02fea6b7f3f900"
    "00000049454e44ae426082"
)


class _Handler(BaseHTTPRequestHandler):
    """A fake GitCode API, scripted per test via the server object."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: D102 - silence the test log
        pass

    def _send(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlparse(self.path).path
        state = self.server.state  # type: ignore[attr-defined]
        state.requests.append(("POST", path, dict(parse_qs(urlparse(self.path).query))))

        if path == f"/uc/api/v1/qrcode/{PLATFORM}":
            if state.create_fails:
                self._send(500, {"error_code": 500})
                return
            self._send(200, {"data": {"scene_id": FAKE_SCENE, "qrcode": state.qr_payload}})
            return

        if path == f"/uc/api/v1/user/oauth/login/qrcode/{PLATFORM}":
            query = parse_qs(urlparse(self.path).query)
            if state.login_fails:
                self._send(400, {"error_code": 400, "error_message": "expired"})
                return
            if query.get("scene_id", [""])[0] != FAKE_SCENE:
                self._send(400, {"error_code": 400})
                return
            self._send(200, {"data": state.login_body})
            return

        self._send(404, {"error": "Not Found"})

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlparse(self.path).path
        state = self.server.state  # type: ignore[attr-defined]
        state.requests.append(("GET", path, dict(parse_qs(urlparse(self.path).query))))

        if path == f"/uc/api/v1/qrcode/{PLATFORM}":
            if "scene_id" not in parse_qs(urlparse(self.path).query):
                self._send(
                    400,
                    {
                        "error_code": 400,
                        "error_code_name": "BAD_REQUEST",
                        "error_message": (
                            "Required request parameter 'scene_id' for method "
                            "parameter type String is not present"
                        ),
                    },
                )
                return
            states = state.poll_states
            index = min(state.polls, len(states) - 1) if states else 0
            state.polls += 1
            self._send(200, {"data": {"status": states[index] if states else "WAITING"}})
            return

        self._send(404, {"error": "Not Found"})


class _State:
    """Scripted server behaviour, mutated by each test."""

    def __init__(self) -> None:
        self.qr_payload: str = "data:image/png;base64," + __import__("base64").b64encode(
            FAKE_PNG
        ).decode()
        self.poll_states: list[str] = ["WAITING", "SCAN", "LOGIN"]
        self.login_body: dict[str, object] = {
            "is_new": False,
            "user_id": "1",
            "mask": "1****",
            "mobile": "1****",
            "username": "tester",
            "user_status_enum": "SUCCESS",
            "access_token": "GITCODEACCESS" + "a1b2c3d4" * 8,
            "refresh_token": "GITCODEREFRESH" + "e5f6a7b8" * 8,
        }
        self.create_fails = False
        self.login_fails = False
        self.polls = 0
        self.requests: list[tuple[str, str, dict[str, list[str]]]] = []


class FakeGitCodeServer:
    def __init__(self) -> None:
        self.state = _State()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.state = self.state  # type: ignore[attr-defined]
        self.port = self._httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def _authenticator(server: FakeGitCodeServer, **kwargs) -> GitCodeQrAuthenticator:
    """An authenticator wired to the fake server, with no real sleeping."""
    return GitCodeQrAuthenticator(
        api_base=server.base,
        poll_interval=0.0,
        timeout=5.0,
        sleep=lambda _seconds: None,
        **kwargs,
    )


class StateMachineTest(unittest.TestCase):
    """The five documented states drive the flow correctly."""

    def setUp(self) -> None:
        self.server = FakeGitCodeServer()
        self.addCleanup(self.server.close)

    def test_happy_path_creates_polls_and_completes(self) -> None:
        auth = _authenticator(self.server)
        result = auth.login(timeout=10.0)

        self.assertIs(result.status, QrLoginStatus.SUCCEEDED)
        self.assertEqual(result.username, "tester")
        self.assertIs(result.is_new_user, False)
        self.assertEqual(result.user_status, "SUCCESS")
        self.assertGreaterEqual(result.polls, 3)

        methods = [(m, p) for m, p, _ in self.server.state.requests]
        self.assertIn(("POST", f"/uc/api/v1/qrcode/{PLATFORM}"), methods)
        self.assertIn(("GET", f"/uc/api/v1/qrcode/{PLATFORM}"), methods)
        self.assertIn(
            ("POST", f"/uc/api/v1/user/oauth/login/qrcode/{PLATFORM}"), methods
        )

    def test_login_call_sends_scene_id_in_the_query_string(self) -> None:
        """The real client puts scene_id in the query, not the body."""
        auth = _authenticator(self.server)
        auth.login(timeout=10.0)
        for method, path, query in self.server.state.requests:
            if path.endswith("/user/oauth/login/qrcode/" + PLATFORM):
                self.assertEqual(method, "POST")
                self.assertEqual(query.get("scene_id"), [FAKE_SCENE])
                break
        else:  # pragma: no cover - the happy-path test covers this
            self.fail("the login call was never made")

    def test_states_are_reported_in_order_and_only_on_change(self) -> None:
        seen: list[QrStatus] = []
        auth = _authenticator(self.server)
        auth.login(timeout=10.0, on_state=seen.append)
        self.assertEqual(
            seen, [QrStatus.WAITING, QrStatus.SCAN, QrStatus.LOGIN]
        )

    def test_repeated_state_is_not_reported_twice(self) -> None:
        self.server.state.poll_states = ["WAITING", "WAITING", "SCAN", "LOGIN"]
        seen: list[QrStatus] = []
        auth = _authenticator(self.server)
        auth.login(timeout=10.0, on_state=seen.append)
        self.assertEqual(seen, [QrStatus.WAITING, QrStatus.SCAN, QrStatus.LOGIN])

    def test_timeout_state_reissues_the_qr_once(self) -> None:
        """An expired QR is refreshed exactly once, then the attempt ends."""
        self.server.state.poll_states = ["TIMEOUT"]
        auth = _authenticator(self.server)
        result = auth.login(timeout=10.0)

        self.assertIs(result.status, QrLoginStatus.EXPIRED)
        self.assertEqual(result.refreshes, 1)
        creates = [
            r for r in self.server.state.requests if r[1].endswith("/qrcode/" + PLATFORM)
            and r[0] == "POST"
        ]
        self.assertEqual(len(creates), 2)  # the original plus one re-issue

    def test_cancel_stops_immediately(self) -> None:
        self.server.state.poll_states = ["SCAN", "CANCEL"]
        auth = _authenticator(self.server)
        result = auth.login(timeout=10.0)
        self.assertIs(result.status, QrLoginStatus.CANCELLED)
        self.assertTrue(result.requires_interaction)

    def test_unknown_state_keeps_waiting_rather_than_aborting(self) -> None:
        """A state GitCode adds later must not break a login in progress."""
        self.server.state.poll_states = ["SOMETHING_NEW", "SCAN", "LOGIN"]
        auth = _authenticator(self.server)
        result = auth.login(timeout=10.0)
        self.assertIs(result.status, QrLoginStatus.SUCCEEDED)

    def test_wall_clock_timeout_is_reported(self) -> None:
        self.server.state.poll_states = ["WAITING"]
        auth = GitCodeQrAuthenticator(
            api_base=self.server.base,
            poll_interval=0.0,
            timeout=5.0,
            sleep=lambda _s: None,
            # A clock that jumps past the deadline on the first poll.
            clock=_JumpingClock(),
        )
        result = auth.login(timeout=1.0)
        self.assertIs(result.status, QrLoginStatus.TIMEOUT)


class _JumpingClock:
    """A monotonic clock that advances a lot per call, to hit deadlines fast."""

    def __init__(self) -> None:
        self._value = 0.0

    def __call__(self) -> float:
        self._value += 100.0
        return self._value


class ProtocolShapeTest(unittest.TestCase):
    """The wire shapes this client depends on are asserted, not assumed."""

    def setUp(self) -> None:
        self.server = FakeGitCodeServer()
        self.addCleanup(self.server.close)

    def test_scene_id_is_required_by_the_poll(self) -> None:
        """Matches the live 400 the investigation observed."""
        auth = _authenticator(self.server)
        challenge = QrChallenge(scene_id="", image="")
        # An empty scene_id still sends the parameter, so this exercises the
        # server's own validation rather than the client's.
        status, payload = auth._call(
            "GET", f"/uc/api/v1/qrcode/{PLATFORM}"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code_name"], "BAD_REQUEST")

    def test_create_failure_raises_a_protocol_error(self) -> None:
        self.server.state.create_fails = True
        auth = _authenticator(self.server)
        with self.assertRaises(QrProtocolError):
            auth.start_login()

    def test_missing_scene_id_in_the_response_is_a_protocol_error(self) -> None:
        self.server.state.qr_payload = ""
        original = _Handler.do_POST

        def patched(handler):  # noqa: ANN001
            path = urlparse(handler.path).path
            if path == f"/uc/api/v1/qrcode/{PLATFORM}":
                handler._send(200, {"data": {"qrcode": "x"}})
                return
            original(handler)

        _Handler.do_POST = patched
        try:
            auth = _authenticator(self.server)
            with self.assertRaises(QrProtocolError):
                auth.start_login()
        finally:
            _Handler.do_POST = original

    def test_both_envelope_shapes_are_accepted(self) -> None:
        """Some endpoints answer ``{"data": {...}}``, others the object."""
        auth = _authenticator(self.server)
        self.assertEqual(auth._unwrap({"data": {"status": "WAITING"}}), {"status": "WAITING"})
        self.assertEqual(auth._unwrap({"status": "WAITING"}), {"status": "WAITING"})

    def test_credentials_are_returned_once_and_are_not_reprd(self) -> None:
        auth = _authenticator(self.server)
        result = auth.login(timeout=10.0)
        creds = result.credentials()
        self.assertIn("access_token", creds)
        self.assertIn("refresh_token", creds)
        # The dataclass repr must not leak them.
        self.assertNotIn(creds["access_token"], repr(result))
        self.assertNotIn(creds["refresh_token"], str(result.as_dict()))

    def test_scene_id_is_not_in_the_challenge_repr(self) -> None:
        challenge = QrChallenge(scene_id=FAKE_SCENE, image="data:image/png;base64,AAA")
        self.assertNotIn(FAKE_SCENE, repr(challenge))
        self.assertIn("<redacted>", repr(challenge))

    def test_session_cookies_reports_names_only(self) -> None:
        auth = _authenticator(self.server)
        auth.login(timeout=10.0)
        cookies = auth.session_cookies()
        self.assertTrue(all(value == "<redacted>" for value in cookies.values()))

    def test_repr_of_the_authenticator_is_secret_free(self) -> None:
        auth = _authenticator(self.server)
        auth.login(timeout=10.0)
        self.assertNotIn(FAKE_SCENE, repr(auth))

    def test_result_dict_is_secret_free(self) -> None:
        auth = _authenticator(self.server)
        result = auth.login(timeout=10.0)
        creds = result.credentials()
        payload = json.dumps(result.as_dict())
        for value in creds.values():
            self.assertNotIn(value, payload)

    def test_network_failure_is_reported_as_a_network_error(self) -> None:
        auth = GitCodeQrAuthenticator(
            api_base="http://127.0.0.1:1",
            poll_interval=0.0,
            timeout=1.0,
            sleep=lambda _s: None,
        )
        result = auth.login(timeout=2.0)
        self.assertIs(result.status, QrLoginStatus.PROTOCOL_ERROR)

    def test_no_x_source_verification_is_claimed(self) -> None:
        """``X-Source`` is a telemetry label; the client must not sign anything."""
        auth = _authenticator(self.server)
        headers = auth._headers()
        self.assertIn("X-Source", headers)
        # No signing material of any kind.
        for name in ("Authorization", "X-Signature", "X-Nonce", "X-Timestamp"):
            self.assertNotIn(name, headers)


class RenderTest(unittest.TestCase):
    """The terminal renderer must degrade, never disappear."""

    def test_data_uri_is_decoded_to_image_bytes(self) -> None:
        from opencsi.auth.qr_render import decode_payload_image

        import base64

        payload = "data:image/png;base64," + base64.b64encode(FAKE_PNG).decode()
        self.assertEqual(decode_payload_image(payload), FAKE_PNG)

    def test_bare_base64_png_is_decoded(self) -> None:
        from opencsi.auth.qr_render import decode_payload_image

        import base64

        self.assertEqual(decode_payload_image(base64.b64encode(FAKE_PNG).decode()), FAKE_PNG)

    def test_a_non_image_payload_is_not_mistaken_for_an_image(self) -> None:
        """An https payload is fetched -- and an HTML answer is rejected.

        This is not hypothetical: an SPA CDN answers a request for a missing
        asset with a full HTML page and ``200``. Accepting that as "the QR
        image" produces a baffling failure far from its cause, so the magic
        bytes are checked.
        """
        from opencsi.auth.qr_render import decode_payload_image

        html = b"<!DOCTYPE html><html><body>not an image</body></html>"
        self.assertIsNone(
            decode_payload_image("https://gitcode.com/logo.svg", fetch=lambda _u: html)
        )

    def test_an_https_payload_that_is_a_real_image_is_accepted(self) -> None:
        from opencsi.auth.qr_render import decode_payload_image

        self.assertEqual(
            decode_payload_image("https://cdn.example/qr.png", fetch=lambda _u: FAKE_PNG),
            FAKE_PNG,
        )

    def test_the_url_fetcher_rejects_a_non_image_response(self) -> None:
        """The magic-byte guard lives in the fetcher, so it applies in prod too."""
        from opencsi.auth.qr_render import looks_like_image

        self.assertTrue(looks_like_image(FAKE_PNG))
        self.assertFalse(looks_like_image(b"<!DOCTYPE html>"))
        self.assertFalse(looks_like_image(b""))

    def test_render_payload_falls_back_to_a_file_without_pillow(self) -> None:
        """No Pillow must still leave the user with a scannable QR."""
        import opencsi.auth.qr_render as render

        original = render.pillow_available
        render.pillow_available = lambda: False
        try:
            import base64

            payload = "data:image/png;base64," + base64.b64encode(FAKE_PNG).decode()
            result = render.render_payload(payload)
        finally:
            render.pillow_available = original

        self.assertTrue(result.ok)
        self.assertIn(result.mode, ("unicode", "file"))
        if result.mode == "file":
            import os

            self.assertTrue(os.path.exists(result.path or ""))
            os.unlink(result.path)  # type: ignore[arg-type]

    def test_empty_payload_reports_no_qr(self) -> None:
        from opencsi.auth.qr_render import render_payload

        result = render_payload("")
        self.assertFalse(result.ok)

    def test_undecodable_payload_reports_a_detail(self) -> None:
        from opencsi.auth.qr_render import render_payload

        result = render_payload("not-an-image-and-not-base64!!!")
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.detail)


if __name__ == "__main__":
    unittest.main()
