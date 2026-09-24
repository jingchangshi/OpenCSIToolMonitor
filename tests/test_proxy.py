"""Proxy handling.

This exists because of a real failure found during the online smoke test.

``urllib`` resolves proxies from the environment **and**, on Windows, from the
registry (``HKCU\\...\\Internet Settings``). ``curl`` does not read the Windows
registry, so on the machine where this was developed:

* ``curl https://opencsitool.com/...``        -> ``401`` (correct)
* ``python -c "urllib.request.urlopen(...)"`` -> ``SSLEOFError``

The local proxy at ``127.0.0.1:7890`` could not carry the connection, and the
resulting error mentioned nothing about a proxy -- it looked exactly like a
server-side TLS fault. These tests pin the behaviour that makes that
diagnosable instead of mysterious.
"""

from __future__ import annotations

import unittest
from unittest import mock

from helpers import StubCredentialProvider, make_client

from opencsi.errors import NetworkError
from opencsi.transport import HttpTransport

URL = "https://opencsitool.com/opencsitool/rest/v1/user/getUserInfo"


class ProxyDetectionTest(unittest.TestCase):
    """``proxy_for`` reports what urllib would actually use."""

    def test_no_proxy_flag_disables_detection(self) -> None:
        transport = HttpTransport("https://opencsitool.com", use_proxy=False)
        with mock.patch(
            "urllib.request.getproxies", return_value={"https": "http://127.0.0.1:7890"}
        ):
            self.assertIsNone(transport.proxy_for(URL))

    def test_configured_proxy_is_reported(self) -> None:
        transport = HttpTransport("https://opencsitool.com", use_proxy=True)
        with mock.patch(
            "urllib.request.getproxies", return_value={"https": "http://127.0.0.1:7890"}
        ), mock.patch("urllib.request.proxy_bypass", return_value=False):
            self.assertEqual(transport.proxy_for(URL), "http://127.0.0.1:7890")

    def test_no_proxy_configured_returns_none(self) -> None:
        transport = HttpTransport("https://opencsitool.com", use_proxy=True)
        with mock.patch("urllib.request.getproxies", return_value={}):
            self.assertIsNone(transport.proxy_for(URL))

    def test_bypass_list_is_honoured(self) -> None:
        """A host in NO_PROXY must not be reported as proxied."""
        transport = HttpTransport("https://opencsitool.com", use_proxy=True)
        with mock.patch(
            "urllib.request.getproxies", return_value={"https": "http://127.0.0.1:7890"}
        ), mock.patch("urllib.request.proxy_bypass", return_value=True):
            self.assertIsNone(transport.proxy_for(URL))

    def test_the_scheme_specific_proxy_is_preferred(self) -> None:
        transport = HttpTransport("https://opencsitool.com", use_proxy=True)
        with mock.patch(
            "urllib.request.getproxies",
            return_value={"http": "http://a:1", "https": "http://b:2"},
        ), mock.patch("urllib.request.proxy_bypass", return_value=False):
            self.assertEqual(transport.proxy_for(URL), "http://b:2")

    def test_all_scheme_proxy_is_a_fallback(self) -> None:
        transport = HttpTransport("https://opencsitool.com", use_proxy=True)
        with mock.patch(
            "urllib.request.getproxies", return_value={"all": "http://c:3"}
        ), mock.patch("urllib.request.proxy_bypass", return_value=False):
            self.assertEqual(transport.proxy_for(URL), "http://c:3")

    def test_detection_never_raises_on_a_broken_platform(self) -> None:
        """Proxy lookup is platform-specific; it must not break a request."""
        transport = HttpTransport("https://opencsitool.com", use_proxy=True)
        with mock.patch(
            "urllib.request.getproxies", side_effect=OSError("no registry")
        ):
            self.assertIsNone(transport.proxy_for(URL))


class ProxyErrorReportingTest(unittest.TestCase):
    """A proxied connection failure must name the proxy and suggest the fix."""

    def test_network_error_names_the_proxy(self) -> None:
        transport = HttpTransport(
            "https://opencsitool.com", attempts=1, use_proxy=True, backoff=0.0
        )
        with mock.patch(
            "urllib.request.getproxies", return_value={"https": "http://127.0.0.1:7890"}
        ), mock.patch("urllib.request.proxy_bypass", return_value=False), mock.patch.object(
            transport._opener, "open", side_effect=OSError("boom")
        ):
            with self.assertRaises(NetworkError) as ctx:
                transport.get_json("/opencsitool/rest/v1/user/getUserInfo")

        message = str(ctx.exception)
        self.assertIn("127.0.0.1:7890", message)
        self.assertIsNotNone(ctx.exception.hint)
        self.assertIn("--no-proxy", ctx.exception.hint)

    def test_error_without_a_proxy_has_no_proxy_hint(self) -> None:
        transport = HttpTransport(
            "https://opencsitool.com", attempts=1, use_proxy=False, backoff=0.0
        )
        with mock.patch.object(
            transport._opener, "open", side_effect=OSError("boom")
        ):
            with self.assertRaises(NetworkError) as ctx:
                transport.get_json("/opencsitool/rest/v1/user/getUserInfo")
        self.assertNotIn("--no-proxy", ctx.exception.hint or "")
        self.assertNotIn("proxy", str(ctx.exception).lower())

    def test_a_bypassed_host_does_not_get_a_proxy_hint(self) -> None:
        """NO_PROXY means the proxy is not in play, so do not blame it."""
        transport = HttpTransport(
            "https://opencsitool.com", attempts=1, use_proxy=True, backoff=0.0
        )
        with mock.patch(
            "urllib.request.getproxies", return_value={"https": "http://127.0.0.1:7890"}
        ), mock.patch("urllib.request.proxy_bypass", return_value=True), mock.patch.object(
            transport._opener, "open", side_effect=OSError("boom")
        ):
            with self.assertRaises(NetworkError) as ctx:
                transport.get_json("/opencsitool/rest/v1/user/getUserInfo")
        self.assertNotIn("--no-proxy", ctx.exception.hint or "")


class OpenerWiringTest(unittest.TestCase):
    """``use_proxy=False`` must actually disable proxying.

    The mechanism is subtler than it looks: ``urllib.request.build_opener``
    *removes* its default ``ProxyHandler`` when you pass one in, so
    ``build_opener(ProxyHandler({}))`` yields an opener with **no** proxy
    handler at all -- which is exactly the desired "never proxy" behaviour.
    The default opener, by contrast, carries a ProxyHandler populated from the
    environment and (on Windows) the registry.
    """

    def _proxy_handlers(self, transport: HttpTransport) -> list[object]:
        import urllib.request

        return [
            h for h in transport._opener.handlers if isinstance(h, urllib.request.ProxyHandler)
        ]

    def test_no_proxy_removes_proxy_handling_entirely(self) -> None:
        transport = HttpTransport("https://opencsitool.com", use_proxy=False)
        self.assertEqual(self._proxy_handlers(transport), [])

    def test_default_transport_is_direct(self) -> None:
        transport = HttpTransport("https://opencsitool.com")
        self.assertFalse(transport.use_proxy)
        self.assertEqual(self._proxy_handlers(transport), [])

    def test_client_passes_the_flag_through(self) -> None:
        from opencsi.client import OpenCsiToolClient

        provider = StubCredentialProvider()
        client = OpenCsiToolClient(provider, use_proxy=False)
        self.assertFalse(client.http.use_proxy)
        client.close()

    def test_client_defaults_to_direct_connections(self) -> None:
        from opencsi.client import OpenCsiToolClient

        provider = StubCredentialProvider()
        client = OpenCsiToolClient(provider)
        self.assertFalse(client.http.use_proxy)
        client.close()

    def test_repr_does_not_leak_the_cookie(self) -> None:
        transport = HttpTransport("https://opencsitool.com")
        transport.set_cookie("SUPER_SECRET_COOKIE_123")
        self.assertNotIn("SUPER_SECRET_COOKIE_123", repr(transport))
        self.assertIn("redacted", repr(transport))


class ProxyCredentialRedactionTest(unittest.TestCase):
    """A proxy URL may carry user:password; it must not reach the output."""

    def test_credentials_are_stripped_from_proxy_for(self) -> None:
        transport = HttpTransport("https://opencsitool.com", use_proxy=True)
        with mock.patch(
            "urllib.request.getproxies",
            return_value={"https": "http://user:SUPER_SECRET_KEY_456@proxy:8080"},
        ), mock.patch("urllib.request.proxy_bypass", return_value=False):
            reported = transport.proxy_for(URL)
        self.assertEqual(reported, "http://proxy:8080")
        self.assertNotIn("SUPER_SECRET_KEY_456", reported)
        self.assertNotIn("user", reported)

    def test_credentials_are_stripped_from_the_error_message(self) -> None:
        transport = HttpTransport(
            "https://opencsitool.com", attempts=1, use_proxy=True, backoff=0.0
        )
        with mock.patch(
            "urllib.request.getproxies",
            return_value={"https": "http://user:SUPER_SECRET_KEY_456@proxy:8080"},
        ), mock.patch("urllib.request.proxy_bypass", return_value=False), mock.patch.object(
            transport._opener, "open", side_effect=OSError("boom")
        ):
            with self.assertRaises(NetworkError) as ctx:
                transport.get_json("/opencsitool/rest/v1/user/getUserInfo")

        combined = f"{ctx.exception}{ctx.exception.hint}"
        self.assertNotIn("SUPER_SECRET_KEY_456", combined)
        # The useful part -- which proxy -- is still reported.
        self.assertIn("proxy:8080", combined)

    def test_a_proxy_without_credentials_is_unchanged(self) -> None:
        transport = HttpTransport("https://opencsitool.com", use_proxy=True)
        with mock.patch(
            "urllib.request.getproxies", return_value={"https": "http://127.0.0.1:7890"}
        ), mock.patch("urllib.request.proxy_bypass", return_value=False):
            self.assertEqual(transport.proxy_for(URL), "http://127.0.0.1:7890")

    def test_a_username_only_proxy_is_stripped(self) -> None:
        transport = HttpTransport("https://opencsitool.com", use_proxy=True)
        with mock.patch(
            "urllib.request.getproxies", return_value={"https": "http://alice@proxy:8080"}
        ), mock.patch("urllib.request.proxy_bypass", return_value=False):
            self.assertEqual(transport.proxy_for(URL), "http://proxy:8080")


class CliProxyFlagTest(unittest.TestCase):
    def test_no_proxy_is_accepted_by_every_read_command(self) -> None:
        import contextlib
        import io

        import opencsi.cli.context as ctx_module
        from opencsi.cli.app import main

        client, _, _ = make_client()
        original = ctx_module.CliContext.make_client
        ctx_module.CliContext.make_client = lambda self, provider=None: client
        try:
            for command in ("status", "tools", "usage", "trend", "prices", "logs"):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = main([command, "--no-proxy"])
                self.assertEqual(code, 0, f"{command}: {err.getvalue()}")
        finally:
            ctx_module.CliContext.make_client = original


class ProxyOptInTest(unittest.TestCase):
    def _context(self, *argv):
        import io
        from opencsi.cli.context import CliContext, build_parser
        args = build_parser().parse_args(list(argv))
        return CliContext(args=args, stdout=io.StringIO(), stderr=io.StringIO())

    def test_default_is_direct_even_when_proxy_environment_exists(self) -> None:
        with mock.patch.dict("os.environ", {"HTTP_PROXY": "http://127.0.0.1:7890", "HTTPS_PROXY": "http://127.0.0.1:7890"}, clear=False):
            self.assertFalse(self._context("usage").use_proxy)

    def test_proxy_flag_opts_in(self) -> None:
        self.assertTrue(self._context("usage", "--proxy").use_proxy)

    def test_no_proxy_remains_direct(self) -> None:
        self.assertFalse(self._context("usage", "--no-proxy").use_proxy)

    def test_conflicting_proxy_flags_are_rejected(self) -> None:
        from opencsi.cli.context import build_parser
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["usage", "--proxy", "--no-proxy"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
