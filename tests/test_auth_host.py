"""The authentication host, and the one thing it must not do to a session.

``AuthBrowserHost`` had no test file. The gap mattered: the host exists to carry
a GitCode session across restarts so a renewal does not need a fresh QR scan, and
``stop()`` was destroying that session on every call.

What was measured on this machine
---------------------------------
Chromium holds its cookie store in memory and flushes to the profile's SQLite
database on a delay. ``taskkill /F`` does not run that flush, so cookies written
over CDP since the last one are discarded. Using a unique nonce per trial so a
surviving value could only be that trial's write:

    graceful Browser.close   -> 2/2 survived
    AuthBrowserHost.stop()   -> 0/2 survived, each time reverting to the
                                previous trial's stale value

After the fix, ``stop()`` closes gracefully and both paths survived 2/2.

These tests do not start a browser -- that needs a real Chromium, a profile and a
port, and the suite has to run offline. They pin the *ordering* that makes the
flush happen, which is the part that regressed and the part a unit test can
actually hold.
"""

from __future__ import annotations

import unittest
from unittest import mock

from opencsi.auth import auth_host
from opencsi.auth.auth_host import AuthBrowserHost


class GracefulStopTest(unittest.TestCase):
    def test_stop_closes_gracefully_before_killing(self) -> None:
        """The regression: a hard kill discards the session the host preserves.

        ``Browser.close`` is what lets Chromium flush. Killing first loses every
        cookie written since the last flush, and the loss is invisible -- the
        next renewal simply finds no session and asks the user to sign in again.
        """
        host = AuthBrowserHost(port=9224)
        with mock.patch.object(
            auth_host, "_graceful_close", return_value=True
        ) as graceful, mock.patch.object(auth_host, "_kill") as kill:
            stopped = host.stop()

        self.assertTrue(stopped)
        graceful.assert_called_once_with(9224)
        kill.assert_not_called()

    def test_the_kill_is_still_the_fallback(self) -> None:
        """A browser that ignores Browser.close must not leave the port held.

        Without this the graceful path would be a hang rather than a fix: an
        unreachable host is worse than one whose newest cookies did not land.
        """
        host = AuthBrowserHost(port=9224)
        with mock.patch.object(
            auth_host, "_graceful_close", return_value=False
        ), mock.patch.object(
            host, "_pids_for_profile", return_value=[4321]
        ), mock.patch.object(auth_host, "_kill") as kill, mock.patch.object(
            auth_host, "_endpoint_answers", return_value=False
        ):
            stopped = host.stop()

        self.assertTrue(stopped)
        kill.assert_called_once_with(4321)

    def test_losing_the_flush_is_reported_rather_than_silent(self) -> None:
        """A silent loss is what made the original bug hard to see.

        The session disappears, the next renewal reports "sign in required", and
        nothing connects that to a stop() that happened hours earlier. Saying so
        at the moment of the kill is what makes it diagnosable.
        """
        host = AuthBrowserHost(port=9224)
        with mock.patch.object(auth_host, "_graceful_close", return_value=False), \
                mock.patch.object(host, "_pids_for_profile", return_value=[]), \
                mock.patch.object(auth_host, "_endpoint_answers", return_value=False), \
                self.assertLogs("opencsi.auth.host", level="WARNING") as captured:
            host.stop()

        self.assertTrue(
            any("graceful close" in line for line in captured.output),
            f"no warning about the forced kill: {captured.output}",
        )


class GracefulCloseTest(unittest.TestCase):
    def test_a_port_that_is_not_answering_is_already_closed(self) -> None:
        """Nothing to close is success, not failure.

        ``stop()`` is called on paths where the host may never have started, and
        treating that as a failed close would send it down the kill branch to
        terminate processes that do not exist.
        """
        with mock.patch.object(
            auth_host, "urlopen", side_effect=OSError("refused")
        ):
            self.assertTrue(auth_host._graceful_close(9224))

    def test_a_dropped_socket_is_not_a_failed_close(self) -> None:
        """Browser.close tears down the socket it arrived on.

        The connection dying is the expected consequence of the request, so
        treating it as an error would report every successful close as a failure
        and fall through to killing an already-exiting browser.
        """
        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b'{"webSocketDebuggerUrl": "ws://127.0.0.1:9224/devtools/browser/x"}'

        with mock.patch.object(auth_host, "urlopen", return_value=_Response()), \
                mock.patch.object(auth_host, "CdpConnection") as conn, \
                mock.patch.object(
                    auth_host, "_endpoint_answers", return_value=False
                ):
            conn.return_value.call.side_effect = OSError("socket closed")
            self.assertTrue(auth_host._graceful_close(9224))
            conn.return_value.call.assert_called_once()
            self.assertEqual(
                conn.return_value.call.call_args.args[0], "Browser.close"
            )

    def test_a_browser_with_no_browser_socket_is_not_closable(self) -> None:
        """Without a browser-level socket there is no way to ask it to exit."""
        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b"{}"

        with mock.patch.object(auth_host, "urlopen", return_value=_Response()):
            self.assertFalse(auth_host._graceful_close(9224))


class DescribeHonestyTest(unittest.TestCase):
    def test_describe_reports_the_observed_mode_not_the_request(self) -> None:
        """A description that contradicts the detected mode is worse than none.

        It is the string a user reads when deciding whether anything appeared on
        their screen, and some Chromium builds ignore ``--headless=new``.
        """
        host = AuthBrowserHost(port=9224, headless=True)
        host._headless_actual = False
        self.assertIn("visible", host.describe())
        self.assertNotIn("no user-visible window", host.describe())

        host._headless_actual = True
        self.assertIn("no user-visible window", host.describe())

    def test_before_a_launch_it_predicts_nothing(self) -> None:
        host = AuthBrowserHost(port=9224, headless=True)
        self.assertIsNone(host._headless_actual)
        self.assertIn("will start hidden", host.describe())


if __name__ == "__main__":
    unittest.main()
