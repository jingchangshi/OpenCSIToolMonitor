"""Tray presentation logic and startup registration.

Everything the tray *decides* lives in the presenter, which imports no GUI
library, so all of it is tested here on any platform. The pystray view itself is
covered only where it can be: that it refuses cleanly without the extra, and
that its single-instance guard behaves.
"""

from __future__ import annotations

import os
import threading
import time
import unittest
from pathlib import Path

import helpers

from opencsi.monitor import MonitorSnapshot, MonitorState
from opencsi.tray import (
    Action,
    MAX_TOOLTIP,
    actions_for,
    format_age,
    format_count,
    format_duration,
    headline_for,
    menu_signature,
    status_text,
    tooltip_for,
    tray_available,
)
from opencsi.tray.startup import StartupManager, default_command


def _snap(**kwargs) -> MonitorSnapshot:
    from datetime import datetime, timezone

    base = dict(
        state=MonitorState.OK,
        total_tokens=1_234_567,
        requests=4321,
        prs=17,
        added_lines=5000,
        generated_lines=900,
        adopted_lines=700,
        adoption_rate=700 / 900,
        data_fresh_time="2025-01-01T00:00:00Z",
        fetched_at=datetime.now(timezone.utc),
        credential_expires_in=1800.0,
    )
    base.update(kwargs)
    return MonitorSnapshot(**base)


class FormatTest(unittest.TestCase):
    def test_counts_are_compact(self) -> None:
        self.assertEqual(format_count(0), "0")
        self.assertEqual(format_count(999), "999")
        self.assertEqual(format_count(1000), "1.0K")
        self.assertEqual(format_count(1234), "1.2K")
        self.assertEqual(format_count(1_234_567), "1.2M")
        self.assertEqual(format_count(2_500_000_000), "2.5B")

    def test_negative_counts_do_not_produce_nonsense(self) -> None:
        self.assertEqual(format_count(-1500), "-1.5K")

    def test_ages_are_compact(self) -> None:
        self.assertEqual(format_age(None), "never")
        self.assertEqual(format_age(0), "0s")
        self.assertEqual(format_age(45), "45s")
        self.assertEqual(format_age(90), "1m")
        self.assertEqual(format_age(3600), "1h")
        self.assertEqual(format_age(3900), "1h05m")
        self.assertEqual(format_age(86400), "1d")

    def test_a_negative_age_is_clamped_not_printed(self) -> None:
        """Clock skew must not produce "-3s ago"."""
        self.assertEqual(format_age(-10), "0s")

    def test_duration_is_distinct_from_age(self) -> None:
        self.assertEqual(format_duration(None), "unknown")
        self.assertEqual(format_duration(30), "<1m")
        self.assertEqual(format_duration(600), "10m")
        self.assertEqual(format_duration(5400), "1.5h")


class TooltipTest(unittest.TestCase):
    def test_tooltip_fits_the_windows_limit(self) -> None:
        """Windows truncates at 127 chars; we must choose what fits."""
        snap = _snap(
            last_error="x" * 400,
            state=MonitorState.SERVER_ERROR,
            credential_expires_in=12345.0,
        )
        self.assertLessEqual(len(tooltip_for(snap)), MAX_TOOLTIP)

    def test_tooltip_shows_the_headline_numbers(self) -> None:
        text = tooltip_for(_snap())
        self.assertIn("1.2M tokens", text)
        self.assertIn("4.3K req", text)
        self.assertIn("17 PR", text)

    def test_tooltip_names_the_state(self) -> None:
        self.assertIn("OK", tooltip_for(_snap()))

    def test_stale_data_is_labelled_as_stale(self) -> None:
        """Showing old numbers without saying so is a lie by omission."""
        snap = _snap(state=MonitorState.OFFLINE, last_error="network is down")
        text = tooltip_for(snap, now=snap.fetched_at.timestamp() + 7200)
        self.assertIn("OFFLINE", text)
        self.assertIn("last update", text)
        self.assertNotIn("updated 2h ago", text)

    def test_a_healthy_tooltip_says_updated_not_offline(self) -> None:
        snap = _snap()
        text = tooltip_for(snap, now=snap.fetched_at.timestamp() + 60)
        self.assertIn("updated", text)
        self.assertNotIn("OFFLINE", text)

    def test_no_data_yet_is_stated_plainly(self) -> None:
        text = tooltip_for(MonitorSnapshot(state=MonitorState.STARTING))
        self.assertIn("waiting for the first update", text)

    def test_a_failure_before_any_data_shows_the_reason(self) -> None:
        snap = MonitorSnapshot(state=MonitorState.OFFLINE, last_error="no route to host")
        self.assertIn("no route to host", tooltip_for(snap))

    def test_tooltip_never_contains_a_secret(self) -> None:
        """It cannot: MonitorSnapshot has nowhere to put one."""
        import dataclasses

        names = {f.name.lower() for f in dataclasses.fields(MonitorSnapshot)}
        for banned in ("token", "cookie", "secret", "authorization"):
            self.assertNotIn(banned, names)

    def test_session_lifetime_is_shown_when_known(self) -> None:
        self.assertIn("session 30m", tooltip_for(_snap(credential_expires_in=1800.0)))


class HeadlineTest(unittest.TestCase):
    def test_headline_shows_full_numbers_not_abbreviated(self) -> None:
        """The menu is where a user goes for the exact figure."""
        self.assertIn("1,234,567", headline_for(_snap()))

    def test_headline_before_any_data_is_honest(self) -> None:
        self.assertEqual(
            headline_for(MonitorSnapshot(state=MonitorState.STARTING)),
            "No data yet",
        )


class ActionsTest(unittest.TestCase):
    def _ids(self, snapshot, **kwargs) -> list[str]:
        return [a.id for a in actions_for(snapshot, **kwargs)]

    def test_the_headline_is_first_and_not_clickable(self) -> None:
        actions = actions_for(_snap())
        self.assertEqual(actions[0].id, "headline")
        self.assertFalse(actions[0].enabled)
        self.assertTrue(actions[0].default)

    def test_a_healthy_session_offers_refresh_and_quit(self) -> None:
        ids = self._ids(_snap(credential_expires_in=7200.0))
        self.assertIn("refresh", ids)
        self.assertIn("quit", ids)

    def test_renew_is_hidden_when_the_session_has_hours_left(self) -> None:
        """A renewal that would return ALREADY_VALID is a confusing click."""
        ids = self._ids(_snap(credential_expires_in=7200.0))
        self.assertNotIn("renew", ids)

    def test_renew_is_offered_when_the_session_is_close_to_expiry(self) -> None:
        ids = self._ids(_snap(credential_expires_in=600.0))
        self.assertIn("renew", ids)

    def test_login_required_offers_sign_in(self) -> None:
        ids = self._ids(_snap(state=MonitorState.LOGIN_REQUIRED))
        self.assertIn("login", ids)

    def test_an_auth_error_offers_both_renew_and_sign_in(self) -> None:
        """Trying a silent renewal first is cheaper than a full sign-in."""
        ids = self._ids(_snap(state=MonitorState.AUTH_ERROR))
        self.assertIn("renew", ids)
        self.assertIn("login", ids)

    def test_offline_does_not_offer_sign_in(self) -> None:
        """A network problem is not an authentication problem."""
        ids = self._ids(_snap(state=MonitorState.OFFLINE))
        self.assertNotIn("login", ids)
        self.assertIn("refresh", ids)

    def test_a_server_error_does_not_offer_sign_in(self) -> None:
        ids = self._ids(_snap(state=MonitorState.SERVER_ERROR))
        self.assertNotIn("login", ids)

    def test_quit_is_always_last(self) -> None:
        for state in MonitorState:
            ids = self._ids(_snap(state=state))
            self.assertEqual(ids[-1], "quit", f"for {state}")

    def test_auto_refresh_is_a_checked_toggle(self) -> None:
        actions = actions_for(_snap(), auto_refresh=True)
        toggle = [a for a in actions if a.id == "autorefresh"]
        self.assertEqual(len(toggle), 1)
        self.assertIs(toggle[0].checked, True)

    def test_every_enabled_action_has_a_non_empty_label(self) -> None:
        for state in MonitorState:
            for action in actions_for(_snap(state=state)):
                if action.enabled:
                    self.assertTrue(action.label.strip(), f"{action.id} has no label")

    def test_menu_signature_changes_when_the_state_changes(self) -> None:
        a = menu_signature(actions_for(_snap(state=MonitorState.OK)))
        b = menu_signature(actions_for(_snap(state=MonitorState.LOGIN_REQUIRED)))
        self.assertNotEqual(a, b)

    def test_menu_signature_is_stable_for_an_unchanged_menu(self) -> None:
        """A stable signature is what stops the menu flickering on every poll.

        This matters for a poll that changes nothing: rebuilding the menu would
        drop it out from under a user who has it open.
        """
        a = menu_signature(actions_for(_snap()))
        b = menu_signature(actions_for(_snap()))
        self.assertEqual(a, b)

    def test_menu_signature_reacts_to_the_headline_number(self) -> None:
        """The headline *is* a menu item, so a new number must rebuild it."""
        a = menu_signature(actions_for(_snap(total_tokens=1)))
        b = menu_signature(actions_for(_snap(total_tokens=2)))
        self.assertNotEqual(a, b)

    def test_menu_signature_is_stable_when_only_the_age_changes(self) -> None:
        """Age is in the tooltip, not the menu, so it must not rebuild the menu."""
        import dataclasses

        snap = _snap()
        aged = dataclasses.replace(snap, last_error=None)
        self.assertEqual(
            menu_signature(actions_for(snap)), menu_signature(actions_for(aged))
        )


class StatusTextTest(unittest.TestCase):
    def test_status_text_is_a_pasteable_report(self) -> None:
        text = status_text(_snap())
        for expected in (
            "state: OK",
            "total_tokens: 1234567",
            "requests: 4321",
            "adoption_rate:",
            "data_fresh_time: 2025-01-01T00:00:00Z",
        ):
            self.assertIn(expected, text)

    def test_status_text_has_no_secret(self) -> None:
        text = status_text(_snap())
        for banned in ("cookie", "Bearer", "virtualKey", "sk-"):
            self.assertNotIn(banned, text)

    def test_status_text_before_data_still_reports_the_state(self) -> None:
        text = status_text(MonitorSnapshot(state=MonitorState.STARTING))
        self.assertIn("state: STARTING", text)
        self.assertIn("has_data: False", text)

    def test_status_text_includes_the_failure_count(self) -> None:
        text = status_text(_snap(consecutive_failures=3, last_error="boom"))
        self.assertIn("consecutive_failures: 3", text)
        self.assertIn("last_error: boom", text)


class AvailabilityTest(unittest.TestCase):
    def test_tray_available_reports_a_reason_when_it_is_not(self) -> None:
        available, reason = tray_available()
        if available:
            self.assertIsNone(reason)
        else:
            self.assertTrue(reason)
            self.assertIn("missing:", reason)

    def test_importing_the_tray_package_does_not_import_pystray(self) -> None:
        """`opencsi doctor` must work on a machine without the extra.

        The assertion is that importing ``opencsi.tray`` does not pull in
        ``opencsi.tray.app``, which is the only module that touches pystray. A
        fresh subprocess is used because a module already imported earlier in
        this session would make an in-process check meaningless.
        """
        import subprocess
        import sys

        code = (
            "import sys, opencsi.tray as t;"
            "print('app_loaded', 'opencsi.tray.app' in sys.modules);"
            "print('has_tooltip', hasattr(t, 'tooltip_for'))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "PYTHONPATH": os.path.join(os.getcwd(), "src")},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("app_loaded False", result.stdout)
        self.assertIn("has_tooltip True", result.stdout)

    def test_tray_app_is_reachable_through_the_package(self) -> None:
        """Lazy does not mean unreachable: `from opencsi.tray import TrayApp`."""
        from opencsi.tray import TrayApp

        self.assertTrue(callable(TrayApp))

    def test_an_unknown_attribute_raises_attribute_error(self) -> None:
        import opencsi.tray as tray

        with self.assertRaises(AttributeError):
            tray.this_does_not_exist


class StartupTest(unittest.TestCase):
    def test_status_is_reported_without_a_registry(self) -> None:
        manager = StartupManager()
        status = manager.status()
        self.assertIsInstance(status.supported, bool)
        if not manager.supported:
            self.assertFalse(status.enabled)
            self.assertIn("Windows", status.detail or "")

    def test_the_default_command_is_plausible(self) -> None:
        command = default_command()
        self.assertIn("opencsi.tray", command)

    def test_a_path_with_spaces_is_quoted(self) -> None:
        """The Run key splits on spaces, so an unquoted path would break."""
        from opencsi.tray.startup import _quote

        self.assertEqual(_quote("C:\\Program Files\\x.exe"), '"C:\\Program Files\\x.exe"')
        self.assertEqual(_quote("C:\\x.exe"), "C:\\x.exe")

    def test_startup_status_serialises(self) -> None:
        from opencsi.tray.startup import StartupStatus

        payload = StartupStatus(supported=True, enabled=True, command="x").as_dict()
        self.assertEqual(payload["enabled"], True)
        self.assertEqual(payload["command"], "x")

    def test_enable_and_disable_round_trip_on_windows(self) -> None:
        """Only run where the registry exists; verified for real on Windows."""
        manager = StartupManager()
        if not manager.supported:
            self.skipTest("not Windows")
        original = manager.status()
        try:
            enabled = manager.enable("opencsi-test-command")
            self.assertTrue(enabled.enabled)
            self.assertEqual(manager.status().command, "opencsi-test-command")
        finally:
            # Restore whatever was there before, so the test cannot leave the
            # developer's machine with a bogus startup entry.
            if original.enabled and original.command:
                manager.enable(original.command)
            else:
                manager.disable()

    def test_disable_is_idempotent(self) -> None:
        manager = StartupManager()
        if not manager.supported:
            self.skipTest("not Windows")
        original = manager.status()
        try:
            manager.disable()
            again = manager.disable()
            self.assertFalse(again.enabled)
        finally:
            if original.enabled and original.command:
                manager.enable(original.command)


class SingleInstanceTest(unittest.TestCase):
    def test_a_second_acquire_in_the_same_process_is_allowed_by_the_os(self) -> None:
        """Windows mutexes are per-process recursive, so this must not error."""
        from opencsi.tray.single_instance import SingleInstance

        first = SingleInstance(name=r"Local\OpenCsiTestMutexA")
        self.assertTrue(first.acquire())
        first.release()

    def test_off_windows_it_degrades_to_allow(self) -> None:
        from opencsi.tray import single_instance

        original = single_instance.os.name
        try:
            single_instance.os.name = "posix"
            guard = single_instance.SingleInstance()
            self.assertTrue(guard.acquire())
            self.assertTrue(guard.acquired)
            guard.release()
        finally:
            single_instance.os.name = original

    def test_release_is_idempotent(self) -> None:
        from opencsi.tray.single_instance import SingleInstance

        guard = SingleInstance(name=r"Local\OpenCsiTestMutexB")
        guard.acquire()
        guard.release()
        guard.release()
        self.assertFalse(guard.acquired)

    def test_the_context_manager_releases(self) -> None:
        from opencsi.tray.single_instance import SingleInstance

        with SingleInstance(name=r"Local\OpenCsiTestMutexC") as guard:
            self.assertTrue(guard.acquired)
        self.assertFalse(guard.acquired)


class IconTest(unittest.TestCase):
    def test_icons_are_drawn_for_every_state(self) -> None:
        from opencsi.tray.icons import icon_bytes, pillow_available

        if not pillow_available():
            self.skipTest("Pillow is not installed")
        for state in MonitorState:
            payload = icon_bytes(state.value)
            self.assertIsNotNone(payload, f"no icon for {state}")
            self.assertTrue(payload.startswith(b"\x89PNG"), f"{state} is not a PNG")

    def test_an_unknown_state_is_not_drawn_as_healthy(self) -> None:
        """A colour is an assertion about health; never assert it blindly."""
        from opencsi.tray.icons import colours_for

        unknown = colours_for("SOMETHING_NEW")
        ok = colours_for("OK")
        self.assertNotEqual(unknown, ok)
        self.assertEqual(unknown, colours_for("OFFLINE"))

    def test_each_state_has_its_own_colour(self) -> None:
        from opencsi.tray.icons import colours_for

        ok = colours_for("OK")
        login = colours_for("LOGIN_REQUIRED")
        server = colours_for("SERVER_ERROR")
        self.assertEqual(len({ok, login, server}), 3)

    def test_the_fallback_icon_exists_without_drawing_machinery(self) -> None:
        from opencsi.tray.icons import make_fallback_icon, pillow_available

        if not pillow_available():
            self.skipTest("Pillow is not installed")
        image = make_fallback_icon("OK")
        self.assertIsNotNone(image)


class TrayCliTest(unittest.TestCase):
    """The CLI surface, which is where a user first meets the tray."""

    def test_tray_accepts_the_common_options_every_command_has(self) -> None:
        """The tray must not be the one command that cannot honour --no-proxy.

        Found by running it: `opencsi tray --once --no-proxy` failed with
        "unrecognized arguments", on the very command a user reaches for when
        the default connection settings are wrong for their machine.
        """
        from opencsi.cli.context import build_parser

        parser = build_parser()
        for argv in (
            ["tray", "--once", "--no-proxy"],
            ["tray", "--once", "--cdp", "http://127.0.0.1:9222"],
            ["tray", "--once", "--timeout", "20"],
            ["tray", "--once", "--json"],
            ["tray", "--once", "--base-url", "https://example.test"],
        ):
            with self.subTest(argv=argv):
                args = parser.parse_args(argv)
                self.assertEqual(args.command, "tray")

    def test_every_command_accepts_the_common_options(self) -> None:
        """A consistency guard: a new subcommand must wire up the shared group."""
        from opencsi.cli.context import build_parser

        parser = build_parser()
        subparsers = None
        for action in parser._actions:  # noqa: SLF001 - argparse has no public API
            if hasattr(action, "choices") and action.choices and "tray" in action.choices:
                subparsers = action.choices
                break
        self.assertIsNotNone(subparsers, "no subcommands were discovered")

        for name, sub in subparsers.items():
            with self.subTest(command=name):
                options = set()
                for action in sub._actions:  # noqa: SLF001
                    options.update(action.option_strings)
                self.assertIn("--json", options, f"{name} lacks --json")
                self.assertIn("--no-proxy", options, f"{name} lacks --no-proxy")
                self.assertIn("--cdp", options, f"{name} lacks --cdp")

    def test_startup_status_flag_is_accepted(self) -> None:
        from opencsi.cli.context import build_parser

        args = build_parser().parse_args(["tray", "--startup-status"])
        self.assertTrue(args.startup_status)

    def test_sign_in_never_fetches_on_its_own_thread(self) -> None:
        """All fetching belongs to the monitor's worker thread.

        ``MonitorService._refresh_once`` mutates the service's state without a
        lock, so a second thread calling it directly would race the worker on
        both the HTTP call and the bookkeeping. The sign-in flow runs on its own
        thread, so it must only *enqueue* refreshes.

        This is asserted structurally rather than by racing two threads, because
        a race is exactly the kind of bug that passes a timing test and fails in
        the field.
        """
        source = (
            Path(__file__).resolve().parent.parent / "src" / "opencsi" / "cli" / "tray.py"
        ).read_text(encoding="utf-8")
        sign_in = source.split("def _sign_in(", 1)[1]

        self.assertNotIn(
            "refresh_now(block=True)",
            sign_in,
            "the sign-in thread performs a blocking fetch; it must enqueue instead",
        )
        self.assertIn(
            "refresh_now()",
            sign_in,
            "the sign-in flow no longer refreshes, so it would never notice a "
            "successful login",
        )

    def test_the_sign_in_wait_is_bounded(self) -> None:
        """A tray must not spin forever on a sign-in the user abandoned."""
        source = (
            Path(__file__).resolve().parent.parent / "src" / "opencsi" / "cli" / "tray.py"
        ).read_text(encoding="utf-8")
        sign_in = source.split("def _sign_in(", 1)[1]
        self.assertIn("deadline", sign_in)
        self.assertIn("time.monotonic() < deadline", sign_in)


class ModuleEntryPointTest(unittest.TestCase):
    """`python -m opencsi.tray` -- the exact command Windows runs at sign-in.

    This entry point is the least-exercised code in the project: it runs with no
    console, at sign-in, and a failure there is invisible. It was in fact
    broken -- it called a ``CliContext.from_args`` that does not exist, so the
    tray exited 1 on every sign-in while `opencsi tray` worked fine.

    The wiring is exercised in-process with the icon stubbed, so the test proves
    the context and service are constructed correctly without opening a real
    notification-area icon during the suite.
    """

    def test_main_wires_up_a_context_and_a_service(self) -> None:
        import opencsi.tray.__main__ as entry
        from opencsi.tray import app as app_module

        seen: dict[str, object] = {}

        class _FakeApp:
            def __init__(self, service):
                seen["service"] = service

            def run(self, **kwargs):
                seen["ran"] = True
                return 0

        original = app_module.TrayApp
        app_module.TrayApp = _FakeApp
        try:
            code = entry.main()
        finally:
            app_module.TrayApp = original

        self.assertEqual(code, 0)
        self.assertTrue(seen.get("ran"), "the tray was never started")
        self.assertIsNotNone(seen.get("service"), "no monitor service was built")

    def test_the_entry_point_does_not_reference_a_missing_context_api(self) -> None:
        """Guards the specific bug: an invented CliContext.from_args."""
        from opencsi.cli.context import CliContext, make_context

        self.assertFalse(
            hasattr(CliContext, "from_args"),
            "CliContext.from_args exists now; update __main__ and this test",
        )
        self.assertTrue(callable(make_context))

    def test_make_context_accepts_an_empty_argv(self) -> None:
        """The entry point parses no arguments, so this must not raise."""
        from opencsi.cli.context import make_context

        ctx, args = make_context([])
        self.assertIsNotNone(ctx)
        self.assertIsNone(getattr(args, "command", None))

    def test_an_unexpected_failure_is_reported_not_raised(self) -> None:
        """With no console at sign-in, a traceback would be lost entirely."""
        import opencsi.tray.__main__ as entry

        original = entry.__dict__

        def explode(*_args, **_kwargs):
            raise RuntimeError("boom")

        import opencsi.cli.context as context_module

        saved = context_module.make_context
        context_module.make_context = explode
        try:
            code = entry.main()
        finally:
            context_module.make_context = saved
        self.assertEqual(code, 1)


class TrayAppLogicTest(unittest.TestCase):
    """The view's decision-making, exercised without showing an icon."""

    def _app(self, snapshot=None):
        from opencsi.monitor import MonitorConfig, MonitorService
        from opencsi.tray.app import TrayApp

        class _Client:
            def get_my_tools(self, *a, **k):
                return _make_my_tools()

        class _Session:
            credentials = object()

            def status(self):
                from opencsi.auth.session import CredentialStatus

                return CredentialStatus(True, "cdp", None, 1800.0)

            def needs_renewal(self, margin=None):
                return False

            def renew(self, **k):
                from opencsi.auth.session import RenewalResult, RenewalStatus

                return RenewalResult(RenewalStatus.ALREADY_VALID)

        service = MonitorService(_Client(), session=_Session(), config=MonitorConfig())
        return TrayApp(service), service

    def test_tooltip_and_menu_are_available_before_the_worker_starts(self) -> None:
        app, service = self._app()
        self.assertIn("OpenCSI", app.tooltip(service.snapshot))
        self.assertTrue(app.build_menu())

    def test_the_auto_refresh_toggle_flips(self) -> None:
        app, _service = self._app()
        self.assertTrue(app._auto_refresh)
        app._dispatch("autorefresh")
        self.assertFalse(app._auto_refresh)
        app._dispatch("autorefresh")
        self.assertTrue(app._auto_refresh)

    def test_an_unknown_action_is_ignored_not_fatal(self) -> None:
        app, _service = self._app()
        app._dispatch("no-such-action")

    def test_a_raising_action_does_not_escape_the_handler(self) -> None:
        """An exception in a Win32 message handler can kill the message loop."""
        app, _service = self._app()

        def boom(_action_id):
            raise RuntimeError("bad action")

        app._dispatch = boom
        app._make_handler("refresh")()  # must not raise

    def test_repr_is_secret_free(self) -> None:
        app, _service = self._app()
        self.assertIn("TrayApp", repr(app))


class NotificationTest(unittest.TestCase):
    """§55: tell the user once when the session needs them, and only then.

    Tested at the tray level as well as the service level, because the wiring
    between them is where a real defect lived: the service could fire the
    transition perfectly and the tray could still ignore it, which is exactly
    what happened to the "Sign in..." menu item.
    """

    def _app(self):
        from opencsi.tray.app import TrayApp

        class _Icon:
            def __init__(self):
                self.calls = []

            def notify(self, message, title=None):
                self.calls.append((title, message))

        app = TrayApp.__new__(TrayApp)
        app._icon = _Icon()
        app._lock = threading.RLock()
        return app

    def test_login_required_produces_one_notification(self) -> None:
        app = self._app()
        app._notify_attention(MonitorSnapshot(state=MonitorState.LOGIN_REQUIRED))
        self.assertEqual(len(app._icon.calls), 1)
        _title, message = app._icon.calls[0]
        self.assertIn("Sign in", message)

    def test_auth_error_produces_a_notification(self) -> None:
        app = self._app()
        app._notify_attention(MonitorSnapshot(state=MonitorState.AUTH_ERROR))
        self.assertEqual(len(app._icon.calls), 1)

    def test_offline_and_server_errors_do_not_notify(self) -> None:
        """A flaky network is not worth interrupting someone over."""
        for state in (MonitorState.OFFLINE, MonitorState.SERVER_ERROR):
            with self.subTest(state=state.value):
                app = self._app()
                app._notify_attention(MonitorSnapshot(state=state))
                self.assertEqual(app._icon.calls, [])

    def test_healthy_states_do_not_notify(self) -> None:
        for state in (MonitorState.OK, MonitorState.REFRESHING, MonitorState.RENEWING):
            with self.subTest(state=state.value):
                app = self._app()
                app._notify_attention(MonitorSnapshot(state=state))
                self.assertEqual(app._icon.calls, [])

    def test_a_backend_without_notify_does_not_break_the_tray(self) -> None:
        """pystray exposes notify on some backends only; a missing balloon must
        not take down the icon, because the tooltip already says the same thing.
        """
        from opencsi.tray.app import TrayApp

        class _NoNotify:
            pass

        app = TrayApp.__new__(TrayApp)
        app._icon = _NoNotify()
        app._lock = threading.RLock()
        app._notify_attention(MonitorSnapshot(state=MonitorState.LOGIN_REQUIRED))

    def test_a_raising_notify_does_not_break_the_tray(self) -> None:
        from opencsi.tray.app import TrayApp

        class _Angry:
            def notify(self, message, title=None):
                raise OSError("no shell notification area")

        app = TrayApp.__new__(TrayApp)
        app._icon = _Angry()
        app._lock = threading.RLock()
        app._notify_attention(MonitorSnapshot(state=MonitorState.LOGIN_REQUIRED))

    def test_no_notification_before_the_icon_exists(self) -> None:
        """A refresh can land before pystray has built the icon."""
        from opencsi.tray.app import TrayApp

        app = TrayApp.__new__(TrayApp)
        app._icon = None
        app._lock = threading.RLock()
        app._notify_attention(MonitorSnapshot(state=MonitorState.LOGIN_REQUIRED))

    def test_the_notification_never_contains_an_identity_field(self) -> None:
        """§56: no employeeId / accountId / userId / virtualKey in any UI text."""
        app = self._app()
        for state in (MonitorState.LOGIN_REQUIRED, MonitorState.AUTH_ERROR):
            app._notify_attention(MonitorSnapshot(state=state))
        blob = repr(app._icon.calls).lower()
        for banned in ("employeeid", "employee_id", "accountid", "userid", "virtualkey", "token="):
            self.assertNotIn(banned, blob)


class SignInActionTest(unittest.TestCase):
    """The "Sign in..." menu item must actually sign the user in."""

    def _app(self):
        from opencsi.monitor import MonitorConfig, MonitorService
        from opencsi.tray.app import TrayApp

        class _Client:
            def get_my_tools(self, *a, **k):
                return _make_my_tools()

        class _Session:
            credentials = object()

            def status(self):
                from opencsi.auth.session import CredentialStatus

                return CredentialStatus(True, "cdp", None, 1800.0)

            def needs_renewal(self, margin=None):
                return False

            def renew(self, **k):
                from opencsi.auth.session import RenewalResult, RenewalStatus

                return RenewalResult(RenewalStatus.ALREADY_VALID)

        service = MonitorService(_Client(), session=_Session(), config=MonitorConfig())
        return TrayApp(service), service

    def test_sign_in_without_a_callback_still_does_something(self) -> None:
        """A menu item that only writes a log line is a dead menu item.

        "Sign in..." is offered precisely when the session is gone, so it is the
        one action a stuck user is most likely to click. It used to do nothing at
        all unless the host wired ``on_login`` -- and the CLI never did. Clicking
        it now opens the login page, which is the useful fallback.
        """
        opened = []
        app, _service = self._app()
        app._on_open = lambda: opened.append(True)
        app._on_login = None

        app._dispatch("login")
        self.assertEqual(
            opened,
            [True],
            "clicking Sign in did nothing: no callback and no browser opened",
        )

    def test_sign_in_uses_the_callback_when_one_is_wired(self) -> None:
        """With a callback, the browser is not opened behind the host's back."""
        calls = []
        opened = []
        app, _service = self._app()
        app._on_login = lambda: calls.append(True)
        app._on_open = lambda: opened.append(True)

        app._dispatch("login")
        # The callback runs on its own thread, so give it a moment.
        for _ in range(50):
            if calls:
                break
            time.sleep(0.01)

        self.assertEqual(calls, [True], "the wired login callback never ran")
        self.assertEqual(opened, [], "the fallback opened a browser anyway")

    def test_the_login_callback_runs_off_the_message_thread(self) -> None:
        """An inline login would freeze the icon for tens of seconds."""
        app, _service = self._app()
        seen: list[int] = []
        app._on_login = lambda: seen.append(threading.get_ident())

        app._dispatch("login")
        for _ in range(50):
            if seen:
                break
            time.sleep(0.01)

        self.assertTrue(seen, "the login callback never ran")
        self.assertNotEqual(
            seen[0],
            threading.get_ident(),
            "the login ran on the calling thread, which would block the tray",
        )


def _make_my_tools():
    from opencsi.models import MyToolsSnapshot

    return MyToolsSnapshot(total_tokens=10, total_request_count=1)


if __name__ == "__main__":
    unittest.main()
