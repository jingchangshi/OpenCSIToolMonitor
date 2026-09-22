"""The browser launcher: the step that makes "sign in" advice actionable.

Every other failure message in this project ends with "start a browser with
``--remote-debugging-port``". For a long time nothing actually did that, so the
instruction was a manual chore the user had to assemble themselves -- and the
obvious alternative, letting :mod:`webbrowser` open the login page, starts a
browser with **no** debugging port. The cookie it writes is invisible to this
tool, so the user signs in successfully and the tool still reports that they are
not signed in.

These tests never launch a real browser. They assert the decision logic -- which
browser is chosen, whether a launch happens at all, what the caller is told --
because that is where the bugs are. The one thing that genuinely needs a real
process, that a launched browser eventually answers on its DevTools port, is
verified on a real machine and reported in the implementation report.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401  (imported for its sys.path side effect)

from opencsi.auth.browser_launch import (
    DEDICATED_PROFILE_DIRNAME,
    BrowserLaunch,
    BrowserLaunchStatus,
    dedicated_profile_dir,
    find_browser,
    launch_debug_browser,
)


class StatusTest(unittest.TestCase):
    def test_only_launched_and_already_running_count_as_ok(self) -> None:
        """A caller that branches on ``ok`` must not treat a failure as success.

        The failure modes are kept separate rather than collapsed into a bool
        precisely so the message can name the real obstacle: "no browser is
        installed" and "the profile is already open" need different fixes.
        """
        self.assertTrue(BrowserLaunch(BrowserLaunchStatus.LAUNCHED).ok)
        self.assertTrue(BrowserLaunch(BrowserLaunchStatus.ALREADY_RUNNING).ok)
        for status in (
            BrowserLaunchStatus.NO_BROWSER_FOUND,
            BrowserLaunchStatus.FAILED,
            BrowserLaunchStatus.UNSUPPORTED,
        ):
            with self.subTest(status=status.value):
                self.assertFalse(BrowserLaunch(status).ok)

    def test_the_status_is_reported_by_value(self) -> None:
        payload = BrowserLaunch(BrowserLaunchStatus.LAUNCHED, port=9222).as_dict()
        self.assertEqual(payload["status"], "LAUNCHED")
        self.assertEqual(payload["port"], 9222)


class ProfileTest(unittest.TestCase):
    def test_the_profile_is_the_one_the_documentation_names(self) -> None:
        """The launcher and the README must not drift apart.

        A browser started here has to be the same browser the rest of the tool
        already looks for. Two different profile directories would mean the
        launcher "succeeds" and the credential provider still sees nothing.
        """
        path = dedicated_profile_dir()
        self.assertEqual(path.name, DEDICATED_PROFILE_DIRNAME)
        self.assertIn("opencsi", str(path).lower())

    def test_the_profile_is_under_local_app_data_on_windows(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows-specific location")
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            self.skipTest("LOCALAPPDATA is not set")
        self.assertEqual(dedicated_profile_dir().parent, Path(local))

    def test_the_profile_is_not_the_users_default_profile(self) -> None:
        """Chrome 147+ refuses remote debugging on the default profile.

        Reusing the user's everyday profile is therefore not merely impolite --
        it does not work. This asserts we never point at it.
        """
        path = str(dedicated_profile_dir()).lower()
        self.assertNotIn("user data", path.replace("\\", "/").replace("_", " "))


class FindBrowserTest(unittest.TestCase):
    def test_a_missing_browser_is_a_value_not_an_exception(self) -> None:
        """"No browser installed" is a normal outcome with its own message."""
        with mock.patch(
            "opencsi.auth.browser_launch._windows_candidates", return_value=[]
        ), mock.patch("opencsi.auth.browser_launch.shutil.which", return_value=None), mock.patch(
            "opencsi.auth.browser_launch.os.name", "nt"
        ):
            self.assertIsNone(find_browser())

    def test_the_first_installed_candidate_wins(self) -> None:
        fake = [("chrome", Path("/nope/chrome")), ("edge", Path("/yes/msedge"))]

        def is_file(self):  # noqa: ANN001, ARG001
            return str(self).endswith("msedge")

        with mock.patch(
            "opencsi.auth.browser_launch._windows_candidates", return_value=fake
        ), mock.patch("opencsi.auth.browser_launch.os.name", "nt"), mock.patch.object(
            Path, "is_file", is_file
        ):
            found = find_browser()
        self.assertIsNotNone(found)
        self.assertEqual(found[0], "edge")


class LaunchTest(unittest.TestCase):
    """``launch_debug_browser`` decides whether to spawn anything at all."""

    def test_an_existing_endpoint_is_reused_rather_than_duplicated(self) -> None:
        """Launching a second instance would silently do nothing useful.

        Chromium forwards a launch for an already-open profile to the existing
        process, and that process has no debugging port. The result is a browser
        window that appears, a user who signs in, and a tool that still cannot
        see the cookie. So an answering port must short-circuit the launch.
        """
        with mock.patch(
            "opencsi.auth.browser_launch._endpoint_answers", return_value=True
        ), mock.patch("opencsi.auth.browser_launch.subprocess.Popen") as popen:
            result = launch_debug_browser("https://example.invalid")

        self.assertIs(result.status, BrowserLaunchStatus.ALREADY_RUNNING)
        popen.assert_not_called()

    def test_no_browser_found_is_reported_without_spawning(self) -> None:
        with mock.patch(
            "opencsi.auth.browser_launch._endpoint_answers", return_value=False
        ), mock.patch(
            "opencsi.auth.browser_launch.find_browser", return_value=None
        ), mock.patch("opencsi.auth.browser_launch.subprocess.Popen") as popen:
            result = launch_debug_browser("https://example.invalid")

        self.assertIs(result.status, BrowserLaunchStatus.NO_BROWSER_FOUND)
        self.assertFalse(result.ok)
        self.assertIn("--remote-debugging-port", result.detail or "")
        popen.assert_not_called()

    def test_the_launch_passes_the_flags_the_tool_needs(self) -> None:
        """The whole point: a port to talk to, and a profile we may use."""
        with mock.patch(
            "opencsi.auth.browser_launch._endpoint_answers", return_value=False
        ), mock.patch(
            "opencsi.auth.browser_launch.find_browser",
            return_value=("chrome", Path("/fake/chrome")),
        ), mock.patch(
            "opencsi.auth.browser_launch.subprocess.Popen"
        ) as popen, mock.patch.object(Path, "mkdir", lambda *a, **k: None):
            result = launch_debug_browser(
                "https://opencsitool.com/myTools", port=9333, timeout=0.0
            )

        popen.assert_called_once()
        argv = popen.call_args[0][0]
        self.assertIn("--remote-debugging-port=9333", argv)
        self.assertTrue(
            any(str(a).startswith("--user-data-dir=") for a in argv),
            f"no user-data-dir in {argv}",
        )
        self.assertIn("https://opencsitool.com/myTools", argv)
        self.assertEqual(result.browser, "chrome")
        self.assertEqual(result.port, 9333)

    def test_a_browser_that_never_opens_a_port_explains_the_likely_cause(
        self,
    ) -> None:
        """The commonest cause is a profile already open elsewhere.

        Chromium then treats the launch as "open a tab over there" and never
        creates an endpoint. A bare timeout would leave the user with no idea
        what to close, so the message names it.
        """
        with mock.patch(
            "opencsi.auth.browser_launch._endpoint_answers", return_value=False
        ), mock.patch(
            "opencsi.auth.browser_launch.find_browser",
            return_value=("chrome", Path("/fake/chrome")),
        ), mock.patch(
            "opencsi.auth.browser_launch.subprocess.Popen"
        ), mock.patch.object(Path, "mkdir", lambda *a, **k: None):
            result = launch_debug_browser("https://x.invalid", timeout=0.0)

        self.assertIs(result.status, BrowserLaunchStatus.FAILED)
        self.assertFalse(result.ok)
        self.assertIn("already open", result.detail or "")

    def test_wait_false_does_not_block_on_the_port(self) -> None:
        """Callers that would rather poll themselves can."""
        with mock.patch(
            "opencsi.auth.browser_launch._endpoint_answers", return_value=False
        ), mock.patch(
            "opencsi.auth.browser_launch.find_browser",
            return_value=("chrome", Path("/fake/chrome")),
        ), mock.patch(
            "opencsi.auth.browser_launch.subprocess.Popen"
        ), mock.patch.object(Path, "mkdir", lambda *a, **k: None):
            result = launch_debug_browser("https://x.invalid", wait=False)

        self.assertIs(result.status, BrowserLaunchStatus.LAUNCHED)

    def test_a_spawn_failure_is_reported_not_raised(self) -> None:
        with mock.patch(
            "opencsi.auth.browser_launch._endpoint_answers", return_value=False
        ), mock.patch(
            "opencsi.auth.browser_launch.find_browser",
            return_value=("chrome", Path("/fake/chrome")),
        ), mock.patch(
            "opencsi.auth.browser_launch.subprocess.Popen",
            side_effect=OSError("nope"),
        ), mock.patch.object(Path, "mkdir", lambda *a, **k: None):
            result = launch_debug_browser("https://x.invalid")

        self.assertIs(result.status, BrowserLaunchStatus.FAILED)
        self.assertIn("OSError", result.detail or "")

    def test_an_unwritable_profile_directory_is_reported(self) -> None:
        with mock.patch(
            "opencsi.auth.browser_launch._endpoint_answers", return_value=False
        ), mock.patch(
            "opencsi.auth.browser_launch.find_browser",
            return_value=("chrome", Path("/fake/chrome")),
        ), mock.patch.object(
            Path, "mkdir", side_effect=OSError("denied")
        ), mock.patch("opencsi.auth.browser_launch.subprocess.Popen") as popen:
            result = launch_debug_browser("https://x.invalid")

        self.assertIs(result.status, BrowserLaunchStatus.FAILED)
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
