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

import tempfile
import threading
import time
import unittest
from pathlib import Path
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


class HeadlessProbeTest(unittest.TestCase):
    """The capability probe must answer from evidence, and leave nothing behind.

    Two defects lived here, and both were invisible to the suite because it only
    ever exercised the probe's *decision* and never its *mechanics*.

    The first: the probe asked ``chrome --headless=new --version``. On Chrome 153
    that neither rejects the flag nor prints a version but hangs, so a 15s timeout
    fired and a build with working headless support was reported as having none.
    The probe now asks the browser to take a screenshot and checks for a real PNG
    signature, which is evidence rather than an exit code.

    The second: it leaked. ``subprocess.run(timeout=...)`` kills the process it
    started, and that process is only a launcher -- Chromium hands the work to a
    child and exits in 0.1s, so the timeout killed a dead PID and left eleven
    orphans per call, each holding a profile in %TEMP%.
    """

    def test_the_artifact_is_the_evidence_not_the_exit_code(self) -> None:
        """A launcher that exits 0 proves nothing; measured, it exits in 0.1s.

        Every candidate probe tried here exited 0, including ``--dump-dom``,
        which produced no output at all. So a probe keyed on the exit code would
        report success for a browser that did nothing.
        """
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty.png"
            empty.write_bytes(b"")
            self.assertFalse(auth_host._is_png(empty))

            truncated = Path(tmp) / "trunc.png"
            truncated.write_bytes(b"\x89PNG\r\n\x1a")  # one byte short
            self.assertFalse(auth_host._is_png(truncated))

            # Something that exists, is non-empty, and is still not a PNG.
            notpng = Path(tmp) / "not.png"
            notpng.write_bytes(b"<html>not an image</html>")
            self.assertFalse(auth_host._is_png(notpng))

            real = Path(tmp) / "real.png"
            real.write_bytes(auth_host._PNG_MAGIC + b"rest of a png")
            self.assertTrue(auth_host._is_png(real))

    def test_a_missing_artifact_is_not_an_error(self) -> None:
        """The probe's own failure mode must be False, never an exception."""
        self.assertFalse(auth_host._is_png(Path("no-such-file-anywhere.png")))

    def test_the_probe_waits_for_the_browser_not_the_launcher(self) -> None:
        """Waiting on the launcher destroyed the artifact it was waiting for.

        Chromium's launcher exits in ~0.1s while the browser needs ~0.6s to write
        the screenshot. The first version of this probe called
        ``process.wait()``, which returned immediately, and then killed the tree
        -- so it reported "unsupported" for a build where headless provably
        works, and it did so by killing the very browser it had asked for
        evidence.
        """
        written = {}

        class FakeProcess:
            pid = 4242

            def poll(self) -> int:
                # The launcher is gone almost immediately, as it really is.
                return 0

        def fake_popen(argv, **kwargs):  # noqa: ANN001, ARG001
            # The screenshot only appears once the browser has had time to run.
            def appear() -> None:
                time.sleep(0.35)
                target = Path(argv[2].split("=", 1)[1])
                target.write_bytes(auth_host._PNG_MAGIC + b"png")
                written["yes"] = True

            threading.Thread(target=appear, daemon=True).start()
            return FakeProcess()

        with (
            mock.patch.object(auth_host.subprocess, "Popen", fake_popen),
            mock.patch.object(auth_host, "_sweep_probe"),
            mock.patch.object(auth_host, "_probe_still_alive", return_value=True),
            mock.patch.object(auth_host, "_remove_quietly"),
        ):
            self.assertTrue(
                auth_host._headless_supported(Path("chrome.exe")),
                "the probe gave up on the launcher instead of waiting for the browser",
            )
        self.assertTrue(written, "the artifact was never written by the fake")

    def test_the_sweep_kills_by_the_profile_path(self) -> None:
        """The parent link is gone by cleanup time, so the path is the handle.

        ``taskkill /T`` cannot work here: the launcher has exited and its
        children have been reparented, so there is no tree to walk from the PID
        we know. Every child still carries ``--user-data-dir=<token>`` on its
        command line, which is what the sweep matches.
        """
        with (
            mock.patch.object(auth_host.os, "name", "nt"),
            mock.patch.object(auth_host.subprocess, "run") as run,
        ):
            auth_host._sweep_probe("opencsi-headless-probe-9999")

        blob = " ".join(
            " ".join(map(str, c.args[0])) for c in run.call_args_list if c.args
        )
        self.assertIn("opencsi-headless-probe-9999", blob)
        self.assertIn("Stop-Process", blob)

    def test_the_sweep_does_not_use_wmic(self) -> None:
        """``wmic`` is removed on Windows 11, and its absence was silent.

        The first version of this cleanup called ``wmic`` inside an
        ``except OSError`` block. On this machine that raised
        ``FileNotFoundError``, the handler swallowed it, and the sweep reported
        success while killing nothing -- which is exactly how a cleanup path
        becomes a no-op nobody notices.
        """
        with (
            mock.patch.object(auth_host.os, "name", "nt"),
            mock.patch.object(auth_host.subprocess, "run") as run,
        ):
            auth_host._sweep_probe("opencsi-headless-probe-9999")

        blob = " ".join(
            " ".join(map(str, c.args[0])) for c in run.call_args_list if c.args
        )
        self.assertNotIn("wmic", blob)
        self.assertIn("powershell.exe", blob.lower())

    def test_cleanup_runs_even_when_the_probe_times_out(self) -> None:
        """The timeout is a normal path, not an edge case.

        If cleanup lived after the ``try`` instead of in a ``finally``, the one
        path that actually leaves browsers behind -- the hang -- would be the one
        path that skips it.
        """
        class HangingProcess:
            pid = 4321

            def poll(self):  # noqa: ANN201
                return None

        with (
            mock.patch.object(auth_host.subprocess, "Popen", return_value=HangingProcess()),
            mock.patch.object(auth_host, "_sweep_probe") as sweep,
            mock.patch.object(auth_host, "_probe_still_alive", return_value=True),
            mock.patch.object(auth_host, "_remove_quietly"),
            mock.patch.object(auth_host.time, "monotonic", side_effect=_clock_then_far_future()),
        ):
            result = auth_host._headless_supported(Path("chrome.exe"))

        self.assertFalse(result)
        # Once before the launch and once in the finally.
        self.assertGreaterEqual(sweep.call_count, 2)

    def test_the_mode_probe_reads_the_field_that_carries_the_marker(self) -> None:
        """The headless marker is in ``userAgent``, not ``product``.

        Measured on Chrome 153: ``product`` is ``Chrome/153.0.8010.53`` while
        ``userAgent`` is ``...HeadlessChrome/153.0.0.0...``. Checking only
        ``product`` reported every headless browser as VISIBLE -- so the host ran
        hidden while telling the user a window was on their screen, and a caller
        that had asked for no window rejected its own working host.
        """
        host = auth_host.AuthBrowserHost(port=9224)

        class FakeConn:
            def __init__(self, *a, **k) -> None:  # noqa: ANN002, ANN003
                pass

            def __enter__(self):  # noqa: ANN201
                return self

            def __exit__(self, *a):  # noqa: ANN002
                return False

            def call(self, method, params, timeout=None):  # noqa: ANN001, ARG002
                return {
                    "product": "Chrome/153.0.8010.53",
                    "userAgent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                        " (KHTML, like Gecko) HeadlessChrome/153.0.0.0 Safari/537.36"
                    ),
                }

        class FakeEndpoint:
            def browser_ws_url(self) -> str:
                return "ws://127.0.0.1:9224/devtools/browser/fake"

        with (
            mock.patch.object(
                auth_host, "discover_cdp_endpoint", return_value=FakeEndpoint()
            ),
            mock.patch.object(auth_host, "CdpConnection", FakeConn),
        ):
            self.assertIs(host._probe_mode(), auth_host.AuthHostMode.HEADLESS)

    def test_a_visible_browser_is_still_reported_visible(self) -> None:
        """The fix must not turn every browser into a headless one."""
        host = auth_host.AuthBrowserHost(port=9224)

        class FakeConn:
            def __init__(self, *a, **k) -> None:  # noqa: ANN002, ANN003
                pass

            def __enter__(self):  # noqa: ANN201
                return self

            def __exit__(self, *a):  # noqa: ANN002
                return False

            def call(self, method, params, timeout=None):  # noqa: ANN001, ARG002
                return {
                    "product": "Chrome/153.0.8010.53",
                    "userAgent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                        " (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
                    ),
                }

        class FakeEndpoint:
            def browser_ws_url(self) -> str:
                return "ws://127.0.0.1:9224/devtools/browser/fake"

        with (
            mock.patch.object(
                auth_host, "discover_cdp_endpoint", return_value=FakeEndpoint()
            ),
            mock.patch.object(auth_host, "CdpConnection", FakeConn),
        ):
            self.assertIs(host._probe_mode(), auth_host.AuthHostMode.VISIBLE)

    def test_an_unreadable_endpoint_is_reported_visible(self) -> None:
        """Unknown must resolve to the conservative answer, not the flattering one."""
        host = auth_host.AuthBrowserHost(port=9224)
        with mock.patch.object(
            auth_host, "discover_cdp_endpoint", side_effect=RuntimeError("no endpoint")
        ):
            self.assertIs(host._probe_mode(), auth_host.AuthHostMode.VISIBLE)


def _clock_then_far_future():
    """A monotonic() that lets the first deadline check pass and then expires."""
    calls = {"n": 0}

    def now() -> float:
        calls["n"] += 1
        return 0.0 if calls["n"] <= 2 else 1e9

    return now


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


class VisibleFallbackTest(unittest.TestCase):
    """A caller that promised no window must be able to keep that promise.

    ``ensure_running`` used to open the window *first* and report it afterwards.
    A caller that inspected the result and declined was therefore too late: the
    window was already on screen, and the only thing it could still control was
    what it said about it. These tests pin the refusal to the point *before* the
    launch, which is the only point where it changes anything.

    No browser is started: ``_headless_supported`` is patched to the pessimistic
    answer so the fallback branch is reached, and ``launch_debug_browser`` is
    patched so the launch is observable rather than real.
    """

    def _host(self, **kw):
        return AuthBrowserHost(port=9224, headless=True, **kw)

    def test_a_caller_that_forbids_a_window_gets_no_launch(self) -> None:
        host = self._host(visible_fallback=False)

        with (
            mock.patch.object(host, "is_running", return_value=False),
            mock.patch.object(auth_host, "find_browser", return_value=("chrome", auth_host.Path("chrome.exe"))),
            mock.patch.object(auth_host, "_headless_supported", return_value=False),
            mock.patch.object(auth_host, "launch_debug_browser") as launch,
        ):
            result = host.ensure_running()

        launch.assert_not_called()
        self.assertFalse(result.ok)
        self.assertFalse(result.visible)
        self.assertIs(result.status, auth_host.AuthHostStatus.FAILED)
        # The reason must name the actual cause, or a user cannot act on it.
        self.assertIn("does not open windows", result.detail or "")

    def test_the_default_still_falls_back_to_a_window(self) -> None:
        """The interactive path wants a browser, so it must keep the fallback.

        Refusing it by default would break ``login --qr`` on any build that
        rejects ``--headless=new``, which is the opposite of the fix's intent.
        """
        from opencsi.auth.browser_launch import BrowserLaunch, BrowserLaunchStatus

        host = self._host()

        with (
            mock.patch.object(host, "is_running", return_value=False),
            mock.patch.object(auth_host, "find_browser", return_value=("chrome", auth_host.Path("chrome.exe"))),
            mock.patch.object(auth_host, "_headless_supported", return_value=False),
            mock.patch.object(
                auth_host,
                "launch_debug_browser",
                return_value=BrowserLaunch(BrowserLaunchStatus.LAUNCHED, browser="chrome"),
            ) as launch,
        ):
            result = host.ensure_running()

        launch.assert_called_once()
        self.assertTrue(result.ok)
        self.assertTrue(result.visible)
        self.assertFalse(result.headless, "a visible engine must not be reported as headless")

    def test_a_visible_result_is_never_reported_as_headless(self) -> None:
        """The one field a caller uses to decide whether to trust "no window"."""
        from opencsi.auth.browser_launch import BrowserLaunch, BrowserLaunchStatus

        host = self._host()

        with (
            mock.patch.object(host, "is_running", return_value=False),
            mock.patch.object(auth_host, "find_browser", return_value=("chrome", auth_host.Path("chrome.exe"))),
            mock.patch.object(auth_host, "_headless_supported", return_value=False),
            mock.patch.object(
                auth_host,
                "launch_debug_browser",
                return_value=BrowserLaunch(BrowserLaunchStatus.LAUNCHED, browser="chrome"),
            ),
        ):
            result = host.ensure_running()

        self.assertIs(result.mode, auth_host.AuthHostMode.VISIBLE)
        self.assertFalse(result.headless)
        self.assertNotIn("no user-visible window", host.describe())


if __name__ == "__main__":
    unittest.main()
