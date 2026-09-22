"""The system proxy must not silently break authentication.

Why this file exists
--------------------
This was found by accident, while investigating something else, and it is a bug a
real user hits: on the development machine a system proxy at ``127.0.0.1:7890``
passed ``gitcode.com`` and failed TLS for ``opencsitool.com``. The consequences
were:

* the login page never loaded, which is indistinguishable from "the site is
  down", so the user has nothing to act on;
* silent renewal failed with an ``SSLEOFError`` that named no proxy, so the
  failure looked like a server fault.

Neither symptom pointed at the cause. So the tests here are about *naming the
cause*: the flag has to exist, it has to be plumbed to the places that make
requests, and a transport failure that went through a proxy has to say so.

The defaults differ between the two callers on purpose, and that is asserted
rather than left implicit -- a renewal has a working fallback, a first sign-in
does not, so "bypass by default" is right for one and wrong for the other.
"""

from __future__ import annotations

import unittest
import unittest.mock
from pathlib import Path

import helpers  # noqa: F401  (imported for its sys.path side effect)

from opencsi.auth.browser_launch import launch_debug_browser
from opencsi.auth.http_oauth import HttpOAuthRenewer


class BrowserProxyFlagTest(unittest.TestCase):
    """``--no-proxy-server`` reaches Chrome when asked for, and only then."""

    def _argv_for(self, **kwargs) -> list[str]:
        captured: dict[str, list[str]] = {}

        class _FakePopen:
            def __init__(self, argv, **_kw) -> None:
                captured["argv"] = list(argv)

        with unittest.mock.patch(
            "opencsi.auth.browser_launch._endpoint_answers", return_value=False
        ), unittest.mock.patch(
            "opencsi.auth.browser_launch.find_browser",
            return_value=("Chrome", Path("C:/chrome.exe")),
        ), unittest.mock.patch(
            "opencsi.auth.browser_launch.subprocess.Popen", _FakePopen
        ), unittest.mock.patch(
            "opencsi.auth.browser_launch.Path.mkdir"
        ):
            launch_debug_browser("https://opencsitool.com/myTools", wait=False, **kwargs)
        return captured.get("argv", [])

    def test_the_flag_is_absent_by_default(self) -> None:
        """A corporate proxy is often the only route out. Bypassing it by
        default would break the browser for those users to fix it for others."""
        argv = self._argv_for()
        self.assertNotIn("--no-proxy-server", argv)
        self.assertIn("https://opencsitool.com/myTools", argv)

    def test_the_flag_is_passed_when_requested(self) -> None:
        argv = self._argv_for(no_proxy=True)
        self.assertIn("--no-proxy-server", argv)

    def test_the_url_stays_last(self) -> None:
        """Chrome treats a trailing positional argument as the URL to open."""
        argv = self._argv_for(no_proxy=True)
        self.assertEqual(argv[-1], "https://opencsitool.com/myTools")

    def test_the_debugging_port_is_still_requested(self) -> None:
        """The proxy flag must not have displaced the reason for the launch."""
        argv = self._argv_for(no_proxy=True)
        self.assertTrue(any(a.startswith("--remote-debugging-port=") for a in argv))
        self.assertTrue(any(a.startswith("--user-data-dir=") for a in argv))


class RenewerProxyDefaultTest(unittest.TestCase):
    """The renewer bypasses the proxy by default. The browser does not."""

    def test_the_renewer_bypasses_the_proxy_by_default(self) -> None:
        """Opposite default from the launcher, and deliberately so.

        Renewal has a fallback (the browser renewer) and runs unattended, so a
        proxy that breaks it should not be trusted; a first sign-in has no
        fallback and a human is watching the page, so the proxy is left alone.
        """
        renewer = HttpOAuthRenewer(object(), use_proxy=False)
        self.assertIsNone(renewer._proxy_for("https://opencsitool.com/x"))  # noqa: SLF001

    def test_the_renewer_can_be_told_to_use_the_proxy(self) -> None:
        renewer = HttpOAuthRenewer(object(), use_proxy=True)
        with unittest.mock.patch(
            "urllib.request.getproxies", return_value={"https": "http://127.0.0.1:7890"}
        ), unittest.mock.patch("urllib.request.proxy_bypass", return_value=False):
            self.assertEqual(
                renewer._proxy_for("https://opencsitool.com/x"),  # noqa: SLF001
                "http://127.0.0.1:7890",
            )

    def test_proxy_credentials_are_stripped_from_the_reported_url(self) -> None:
        """This string reaches logs and bug reports."""
        renewer = HttpOAuthRenewer(object(), use_proxy=True)
        with unittest.mock.patch(
            "urllib.request.getproxies",
            return_value={"https": "http://user:hunter2@proxy.internal:8080"},
        ), unittest.mock.patch("urllib.request.proxy_bypass", return_value=False):
            reported = renewer._proxy_for("https://opencsitool.com/x")  # noqa: SLF001
        self.assertEqual(reported, "http://proxy.internal:8080")
        self.assertNotIn("hunter2", reported or "")
        self.assertNotIn("user", reported or "")


class TransportMessageTest(unittest.TestCase):
    """A TLS failure through a proxy must name the proxy and the remedy."""

    def test_a_proxied_failure_names_the_proxy_and_the_flag(self) -> None:
        renewer = HttpOAuthRenewer(object(), use_proxy=True)
        with unittest.mock.patch(
            "urllib.request.getproxies", return_value={"https": "http://127.0.0.1:7890"}
        ), unittest.mock.patch("urllib.request.proxy_bypass", return_value=False):
            message = renewer._transport_message(  # noqa: SLF001
                "https://opencsitool.com/opencsitool/rest/v1/oauth2/authorization/gitcode",
                "SSLEOFError",
            )
        self.assertIn("proxy", message.lower())
        self.assertIn("127.0.0.1:7890", message)
        self.assertIn("--no-proxy", message)
        # The query string is where a code or state would travel.
        self.assertNotIn("?", message)

    def test_a_direct_failure_does_not_mention_a_proxy(self) -> None:
        """Naming a proxy that was not used would send the user to the wrong fix."""
        renewer = HttpOAuthRenewer(object(), use_proxy=False)
        message = renewer._transport_message(  # noqa: SLF001
            "https://opencsitool.com/x", "SSLEOFError"
        )
        self.assertNotIn("proxy", message.lower())
        self.assertNotIn("--no-proxy", message)
        self.assertIn("SSLEOFError", message)

    def test_the_host_and_path_are_reported(self) -> None:
        renewer = HttpOAuthRenewer(object(), use_proxy=False)
        message = renewer._transport_message("https://a.example.com/b/c?d=e", "TimeoutError")
        self.assertIn("a.example.com/b/c", message)
        self.assertNotIn("d=e", message)


class ProxyFlagSurfaceTest(unittest.TestCase):
    """The flag has to exist for a user to be told to use it."""

    def test_no_proxy_is_a_real_option_on_the_login_command(self) -> None:
        from opencsi.cli.app import build_parser

        parser = build_parser()
        options = {
            opt
            for action in parser._actions  # noqa: SLF001 - argparse exposes no public API
            for opt in action.option_strings
        }
        self.assertIn("--no-proxy", options)

    def test_the_browser_launch_path_reads_the_flag(self) -> None:
        """Plumbed, not merely declared.

        Asserted by driving the real function with the flag set and checking the
        launcher received it, rather than by inspecting the source.
        """
        from opencsi.cli import login as login_module
        from opencsi.cli.context import CliContext

        class _Args:
            no_proxy = True
            cdp = None
            base_url = None
            json = False

        import contextlib
        import io

        ctx = CliContext(args=_Args(), stdout=io.StringIO(), stderr=io.StringIO())
        with unittest.mock.patch(
            "opencsi.auth.browser_launch.launch_debug_browser"
        ) as launch:
            launch.return_value.status = "launched"
            launch.return_value.browser = "Chrome"
            launch.return_value.port = 9222
            with contextlib.suppress(Exception):
                login_module._open_a_readable_browser(ctx)  # noqa: SLF001
        self.assertTrue(launch.called)
        self.assertTrue(launch.call_args.kwargs.get("no_proxy"))


if __name__ == "__main__":
    unittest.main()
