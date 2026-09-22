"""Tray presentation logic and startup registration.

Everything the tray *decides* lives in the presenter, which imports no GUI
library, so all of it is tested here on any platform. The pystray view itself is
covered only where it can be: that it refuses cleanly without the extra, and
that its single-instance guard behaves.
"""

from __future__ import annotations

import os
import sys
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
    format_count_cn,
    format_duration,
    headline_for,
    label_cn,
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


class ChineseUnitTest(unittest.TestCase):
    """The tray is read by someone who thinks in 万 and 亿, not K and B."""

    def test_counts_use_chinese_units(self) -> None:
        self.assertEqual(format_count_cn(0), "0")
        self.assertEqual(format_count_cn(9999), "9999")
        self.assertEqual(format_count_cn(10_000), "1.0万")
        self.assertEqual(format_count_cn(43_210), "4.3万")
        self.assertEqual(format_count_cn(1_234_567), "123.5万")
        self.assertEqual(format_count_cn(100_000_000), "1.0亿")
        self.assertEqual(format_count_cn(3_634_063_175), "36.3亿")

    def test_small_counts_stay_exact(self) -> None:
        """Padding 4321 into "0.4万" would lose information for no gain."""
        self.assertEqual(format_count_cn(4321), "4321")

    def test_negative_counts_do_not_produce_nonsense(self) -> None:
        self.assertEqual(format_count_cn(-15_000), "-1.5万")

    def test_the_real_figure_from_the_live_account_reads_naturally(self) -> None:
        """The number this was built for: 3,634,063,175 tokens."""
        self.assertEqual(format_count_cn(3_634_063_175), "36.3亿")

    def test_every_state_has_a_chinese_label(self) -> None:
        """A missing label would silently fall back to English mid-tooltip."""
        for state in MonitorState:
            with self.subTest(state=state.value):
                label = label_cn(MonitorSnapshot(state=state))
                self.assertTrue(label)
                self.assertTrue(
                    any("\u4e00" <= ch <= "\u9fff" for ch in label),
                    f"{state.value} has no Chinese label: {label!r}",
                )


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
        # Chinese units: 1,234,567 tokens is 123.5万, and 4,321 requests is below
        # 万 so it stays exact rather than being padded to "0.4万".
        self.assertIn("123.5万 tokens", text)
        self.assertIn("4321 次请求", text)
        self.assertIn("17 PR", text)

    def test_tooltip_names_the_state(self) -> None:
        self.assertIn("正常", tooltip_for(_snap()))

    def test_stale_data_is_labelled_as_stale(self) -> None:
        """Showing old numbers without saying so is a lie by omission."""
        snap = _snap(state=MonitorState.OFFLINE, last_error="network is down")
        text = tooltip_for(snap, now=snap.fetched_at.timestamp() + 7200)
        self.assertIn("离线", text)
        self.assertIn("最后更新", text)
        self.assertNotIn("更新于 2h 前", text)

    def test_a_healthy_tooltip_says_updated_not_offline(self) -> None:
        snap = _snap()
        text = tooltip_for(snap, now=snap.fetched_at.timestamp() + 60)
        self.assertIn("更新于", text)
        self.assertNotIn("离线", text)

    def test_no_data_yet_is_stated_plainly(self) -> None:
        text = tooltip_for(MonitorSnapshot(state=MonitorState.STARTING))
        self.assertIn("等待首次更新", text)

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
        self.assertIn("会话 30m", tooltip_for(_snap(credential_expires_in=1800.0)))


class HeadlineTest(unittest.TestCase):
    def test_headline_shows_full_numbers_not_abbreviated(self) -> None:
        """The menu is where a user goes for the exact figure."""
        self.assertIn("1,234,567", headline_for(_snap()))

    def test_headline_before_any_data_is_honest(self) -> None:
        self.assertEqual(
            headline_for(MonitorSnapshot(state=MonitorState.STARTING)),
            "暂无数据",
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

    def test_the_start_menu_item_is_omitted_where_it_cannot_work(self) -> None:
        """§28 requires "Start with Windows"; §34 makes it Windows-only.

        The three-valued parameter exists so a non-Windows host (or an unreadable
        Run key) omits the item rather than showing a checkbox that silently
        does nothing. A control that cannot act is worse than no control.
        """
        self.assertNotIn("startup", self._ids(_snap(), startup_enabled=None))

    def test_the_start_menu_item_appears_with_its_real_state(self) -> None:
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                actions = actions_for(_snap(), startup_enabled=enabled)
                item = next(a for a in actions if a.id == "startup")
                self.assertEqual(item.checked, enabled)
                self.assertTrue(item.enabled, "the item must be clickable")

    def test_the_start_item_reflects_state_rather_than_offering_a_fixed_action(
        self,
    ) -> None:
        """One item that toggles, not two items whose availability changes.

        A menu whose entries appear and disappear is harder to use than one whose
        tick moves, and the tick is what Windows' own Startup tab shows.
        """
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                ids = self._ids(_snap(), startup_enabled=enabled)
                self.assertEqual(ids.count("startup"), 1)
                self.assertNotIn("startup_on", ids)
                self.assertNotIn("startup_off", ids)

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

    def test_a_missing_browser_offers_starting_one_first(self) -> None:
        """The fix for a closed loop the user could not escape.

        With no DevTools endpoint, the tray used to say "login required" and
        offer "Sign in...". Clicking it opened the *default* browser with no
        debugging port, so the cookie landed somewhere this tool cannot read,
        and the next poll said "login required" again. Every click reproduced
        the state it was meant to fix.

        The first action must therefore be one that actually unblocks the
        situation. There are now two that do, and **QR comes first** because it
        is the only one that does not depend on the browser that is missing: the
        credential arrives over plain HTTP from the QR flow and the OAuth leg is
        plain HTTP too. Offering only browser-based routes in the state where the
        browser is the broken part is the same closed loop in a new shape.

        ``launch_browser`` stays, and stays available -- a user whose browser is
        simply not running yet is better served by it than by fetching a phone.
        """
        actions = actions_for(_snap(state=MonitorState.BROWSER_UNAVAILABLE))
        ids = [a.id for a in actions]
        self.assertIn("launch_browser", ids)
        self.assertIn("login_qr", ids)
        actionable = [i for i in ids if i not in ("headline",) and not i.startswith("sep")]
        self.assertEqual(actionable[0], "login_qr")
        self.assertIn("launch_browser", actionable)

    def test_the_qr_route_is_offered_when_the_browser_is_the_broken_part(
        self,
    ) -> None:
        """The property that makes the browserless flow worth having here.

        In ``BROWSER_UNAVAILABLE`` every other actionable route needs a browser.
        If QR were not offered, the state would have no route that works without
        first fixing the thing that is broken.
        """
        actions = actions_for(_snap(state=MonitorState.BROWSER_UNAVAILABLE))
        by_id = {a.id: a for a in actions}
        self.assertIn("login_qr", by_id)
        self.assertTrue(by_id["login_qr"].default)

    def test_a_missing_browser_does_not_offer_sign_in_as_the_primary_action(
        self,
    ) -> None:
        """Signing in is still reachable, but never the default click.

        It is not removed: a user who has a correctly-started browser open in
        another window can still use it. It simply must not be what a stuck user
        hits first, because that is the click that does nothing.
        """
        actions = actions_for(_snap(state=MonitorState.BROWSER_UNAVAILABLE))
        by_id = {a.id: a for a in actions}
        self.assertIn("login", by_id)
        self.assertFalse(by_id["login"].default)
        self.assertFalse(by_id["launch_browser"].default)
        # The default must be the route that needs no browser.
        self.assertTrue(by_id["login_qr"].default)

    def test_the_missing_browser_state_has_its_own_label(self) -> None:
        """It must not read as "sign in" -- that is the whole point."""
        unavailable = label_cn(_snap(state=MonitorState.BROWSER_UNAVAILABLE))
        login = label_cn(_snap(state=MonitorState.LOGIN_REQUIRED))
        self.assertNotEqual(unavailable, login)
        self.assertTrue(
            any("\u4e00" <= ch <= "\u9fff" for ch in unavailable),
            f"no Chinese label: {unavailable!r}",
        )

    def test_the_missing_browser_state_is_not_drawn_as_login_required(self) -> None:
        """Amber for both would be fine, but the *tooltip* must differ.

        The two states share the "you must act" colour deliberately -- the icon
        answers "do I need to do something?", not "what exactly?". What must not
        be shared is the text, because the text is the instruction.
        """
        from opencsi.tray.icons import colours_for

        self.assertEqual(
            colours_for(MonitorState.BROWSER_UNAVAILABLE.value),
            colours_for(MonitorState.LOGIN_REQUIRED.value),
        )
        self.assertNotEqual(
            tooltip_for(_snap(state=MonitorState.BROWSER_UNAVAILABLE)),
            tooltip_for(_snap(state=MonitorState.LOGIN_REQUIRED)),
        )

    def test_quit_is_always_last(self) -> None:
        for state in MonitorState:
            ids = self._ids(_snap(state=state))
            self.assertEqual(ids[-1], "quit", f"for {state}")

    def test_the_consent_state_offers_approval_not_a_plain_sign_in(self) -> None:
        """The whole reason CONSENT_REQUIRED exists as its own state.

        The user is still authenticated -- GitCode rendered "授权 OpenCsitool S
        <user>" -- so "sign in again" is an action that cannot fix anything. The
        menu must offer the approval step and the renewal retry instead.
        """
        actions = actions_for(_snap(state=MonitorState.CONSENT_REQUIRED))
        labels = {a.id: a.label for a in actions}
        self.assertIn("renew", labels, "consent must offer a renewal retry")
        self.assertIn("打开页面", labels.get("login", ""), "the login action must say what to do")

    def test_the_consent_state_is_distinct_from_login_required(self) -> None:
        consent = self._ids(_snap(state=MonitorState.CONSENT_REQUIRED))
        login = self._ids(_snap(state=MonitorState.LOGIN_REQUIRED))
        self.assertNotEqual(
            consent, login, "consent and login must not present the same menu"
        )

    def test_the_consent_state_has_a_chinese_label(self) -> None:
        self.assertEqual(label_cn(_snap(state=MonitorState.CONSENT_REQUIRED)), "需要授权确认")

    def test_every_state_has_a_label_and_a_colour(self) -> None:
        """A state with no label would render as a blank menu headline.

        The colour check asserts *membership*, not distinctness. Several states
        legitimately share the neutral grey -- STARTING and OFFLINE both mean
        "nothing is wrong, but nothing is happening" -- so comparing against the
        unknown-state fallback would fail on a correct map. What matters is that
        each state has an explicit entry, because an absent one silently inherits
        the grey used for "state I do not recognise", which would make a new
        state look like a bug rather than a state.
        """
        from opencsi.tray.icons import _COLOURS

        for state in MonitorState:
            with self.subTest(state=state):
                self.assertTrue(label_cn(_snap(state=state)).strip())
                self.assertIn(
                    state.value,
                    _COLOURS,
                    f"{state} has no explicit colour and would render as unknown",
                )

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


class StartupCommandContextTest(unittest.TestCase):
    """§35: the three running contexts, each with the command it must register.

    ``startup_command_for_tray`` had no test coverage at all, which is how the
    P4 defect shipped: from the frozen CLI it registered ``opencsi.exe`` with no
    sub-command, so sign-in ran a program that printed usage and exited. Nothing
    in the suite noticed, because nothing in the suite called this function.

    The three contexts are simulated by patching ``sys.frozen`` and
    ``sys.executable``, which is exactly what PyInstaller changes, so no real
    frozen build is needed to cover the branches. The frozen build is still
    exercised separately -- a branch test cannot prove the spec emits the name
    these tests assume.
    """

    def _context(self, *, frozen: bool, executable: str, sibling: str | None = None):
        """Patch the two attributes PyInstaller sets, and the sibling lookup.

        ``sibling`` is injected rather than created on disk: the test asserts the
        *decision*, and writing a fake ``opencsi-tray.exe`` into the source tree
        to observe a branch would leave a stray file behind on any failure.
        """
        from opencsi.tray import startup

        class _Ctx:
            def __enter__(self_inner):
                self_inner._frozen = getattr(sys, "frozen", None)
                self_inner._exe = sys.executable
                self_inner._sibling = startup._frozen_tray_sibling
                sys.frozen = frozen
                sys.executable = executable
                startup._frozen_tray_sibling = lambda: (
                    Path(sibling) if sibling else None
                )
                return startup

            def __exit__(self_inner, *exc):
                if self_inner._frozen is None:
                    sys.__dict__.pop("frozen", None)
                else:
                    sys.frozen = self_inner._frozen
                sys.executable = self_inner._exe
                startup._frozen_tray_sibling = self_inner._sibling
                return False

        return _Ctx()

    def test_a_source_checkout_registers_the_module_form(self) -> None:
        """`pythonw -m opencsi.tray`, not a bare interpreter.

        ``pythonw`` rather than ``python`` because a console window flashing at
        every sign-in is the thing the windowed binary exists to avoid.
        """
        with self._context(frozen=False, executable=r"C:\Py\python.exe") as startup:
            command, source = startup.startup_command_for_tray()

        self.assertEqual(source, startup.SOURCE_SOURCE_INSTALL)
        self.assertIn("-m opencsi.tray", command)
        self.assertIn("python", command.lower())

    def test_the_frozen_tray_registers_itself_with_no_arguments(self) -> None:
        """The tray binary takes no sub-command.

        Registering ``"opencsi-tray.exe" tray`` would start a tray that tries to
        interpret ``tray`` as an argument, which is a different bug from the one
        being fixed but just as silent.
        """
        with self._context(
            frozen=True, executable=r"C:\App\opencsi-tray.exe"
        ) as startup:
            command, source = startup.startup_command_for_tray()

        self.assertEqual(source, startup.SOURCE_FROZEN_TRAY)
        self.assertIn("opencsi-tray.exe", command)
        self.assertNotIn("tray", command.replace("opencsi-tray.exe", "").strip())

    def test_the_frozen_cli_registers_the_sibling_tray(self) -> None:
        """The §32 defect, asserted directly: never a naked ``opencsi.exe``."""
        with self._context(
            frozen=True,
            executable=r"C:\App\opencsi.exe",
            sibling=r"C:\App\opencsi-tray.exe",
        ) as startup:
            command, source = startup.startup_command_for_tray()

        self.assertEqual(source, startup.SOURCE_FROZEN_CLI_TRAY)
        self.assertIn("opencsi-tray.exe", command)
        self.assertNotIn(
            "opencsi.exe",
            command,
            "the CLI registered itself; sign-in would print usage and exit",
        )

    def test_the_frozen_cli_without_a_sibling_uses_the_subcommand(self) -> None:
        """The fallback must pass ``tray``, or it reproduces the same defect."""
        with self._context(
            frozen=True, executable=r"C:\App\opencsi.exe", sibling=None
        ) as startup:
            command, source = startup.startup_command_for_tray()

        self.assertEqual(source, startup.SOURCE_FROZEN_CLI)
        self.assertIn("opencsi.exe", command)
        self.assertTrue(
            command.rstrip().endswith("tray"),
            f"the sub-command is mandatory here, got {command!r}",
        )

    def test_the_source_label_never_claims_a_context_that_is_not_running(
        self,
    ) -> None:
        """The label must describe this build, not the command it produced.

        ``frozen-tray`` means "this process *is* the windowed tray binary". It was
        being reported by the frozen CLI whenever a sibling existed, so
        ``opencsi.exe tray --startup-status`` printed ``derived from: frozen-tray``
        -- a false statement about the running process, in the one output a user
        consults to find out what will actually start at sign-in.
        """
        from opencsi.tray import startup

        cases = [
            (False, r"C:\Py\python.exe", None, startup.SOURCE_SOURCE_INSTALL),
            (True, r"C:\App\opencsi-tray.exe", None, startup.SOURCE_FROZEN_TRAY),
            (
                True,
                r"C:\App\opencsi.exe",
                r"C:\App\opencsi-tray.exe",
                startup.SOURCE_FROZEN_CLI_TRAY,
            ),
            (True, r"C:\App\opencsi.exe", None, startup.SOURCE_FROZEN_CLI),
        ]
        for frozen, executable, sibling, expected in cases:
            with self.subTest(executable=executable, sibling=sibling):
                with self._context(
                    frozen=frozen, executable=executable, sibling=sibling
                ) as mod:
                    _command, source = mod.startup_command_for_tray()
                    is_tray = mod._is_frozen_tray()
                self.assertEqual(source, expected)
                # The invariant: only a process that really is the tray binary
                # may report `frozen-tray`.
                self.assertEqual(
                    source == mod.SOURCE_FROZEN_TRAY,
                    is_tray,
                    f"{source!r} disagrees with _is_frozen_tray()={is_tray}",
                )

    def test_matches_this_build_compares_against_the_tray_command(self) -> None:
        """A registered entry must be judged against the tray command.

        If this compared against the *CLI* command, a correct entry would be
        reported as stale -- the warning would fire on exactly the installs that
        are right.
        """
        from opencsi.tray import startup

        with self._context(
            frozen=True,
            executable=r"C:\App\opencsi.exe",
            sibling=r"C:\App\opencsi-tray.exe",
        ):
            command = startup.startup_command_for_tray()[0]
            status = startup.StartupStatus(
                supported=True, enabled=True, command=command
            )
            self.assertTrue(status.matches_this_build)

            wrong = startup.StartupStatus(
                supported=True, enabled=True, command=r"C:\App\opencsi.exe"
            )
            self.assertFalse(
                wrong.matches_this_build,
                "a naked CLI entry must not look correct",
            )


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

    def test_the_already_running_exit_is_a_named_constant(self) -> None:
        """The entry point must recognise this outcome to show a dialog.

        A bare ``2`` in ``run()`` could not be distinguished from any other
        return, so the one failure that leaves a windowed user with no icon and
        no message went unreported. The constant is what makes it addressable.
        """
        from opencsi.tray.app import ALREADY_RUNNING_EXIT

        self.assertIsInstance(ALREADY_RUNNING_EXIT, int)
        source = (Path(__file__).resolve().parent.parent / "src" / "opencsi"
                  / "tray" / "app.py").read_text(encoding="utf-8")
        self.assertIn(
            "return ALREADY_RUNNING_EXIT",
            source,
            "run() is back to returning a bare number for the already-running case",
        )


class AlreadyRunningReportTest(unittest.TestCase):
    """A second launch must say something, not fail silently.

    Reproduced on the real frozen build: with an orphaned tray holding the
    mutex, double-clicking ``opencsi-tray.exe`` exited 2 while the message went
    to a log file that does not exist. The user saw no icon and no text.
    """

    def test_the_message_names_the_recovery_step(self) -> None:
        from opencsi.tray.__main__ import _already_running_message

        text = _already_running_message()
        self.assertIn("already running", text.lower())
        # The user has to be told where to look and how to get out of the state,
        # because "no icon appeared" is the symptom they are staring at.
        self.assertIn("notification area", text)
        self.assertIn("Exit", text)

    def test_a_usable_stderr_is_preferred_over_a_dialog(self) -> None:
        """A console user must get the message on the stream they are reading.

        And a *test* must not get a modal dialog: gating the box on the platform
        alone meant every Windows test that ran the entry point blocked forever
        on a real ``MessageBoxW``, which is how this requirement was found.
        """
        from unittest import mock

        from opencsi.tray import __main__ as entry

        printed: list[str] = []
        fake_stderr = mock.MagicMock()
        fake_stderr.write = printed.append  # type: ignore[method-assign]
        box = mock.MagicMock()
        with mock.patch.object(entry.sys, "stderr", fake_stderr), mock.patch(
            "ctypes.windll", create=True, new=box
        ):
            entry._show_message("something went wrong")

        self.assertTrue(
            any("something went wrong" in str(item) for item in printed),
            "the message was swallowed even though stderr was available",
        )
        box.user32.MessageBoxW.assert_not_called()

    def test_the_dialog_is_used_when_there_is_no_stderr(self) -> None:
        """That is the windowed build, and the only case the box is for."""
        from unittest import mock

        from opencsi.tray import __main__ as entry

        box = mock.MagicMock()
        with mock.patch.object(entry.sys, "stderr", None), mock.patch.object(
            entry.sys, "platform", "win32"
        ), mock.patch("ctypes.windll", create=True, new=box):
            entry._show_message("nothing to read this")

        box.user32.MessageBoxW.assert_called_once()

    def test_showing_a_message_never_raises(self) -> None:
        """Diagnostics are best-effort; an exception here would mask the cause."""
        from unittest import mock

        from opencsi.tray import __main__ as entry

        # No stderr, and the box itself fails: this must still return quietly.
        with mock.patch.object(entry.sys, "stderr", None), mock.patch.object(
            entry.sys, "platform", "win32"
        ), mock.patch(
            "ctypes.windll",
            create=True,
            new=mock.MagicMock(side_effect=OSError("no desktop")),
        ):
            entry._show_message("unshowable")


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

    def test_the_colour_answers_do_i_need_to_act(self) -> None:
        """The palette is coarser than the state machine, and deliberately so.

        A user glancing at the notification area is asking one question: does this
        need me? So green means fine, blue means working, amber means the user
        must act, red means the server is broken, grey means we cannot tell.
        Encoding all eight states as eight colours would make them *harder* to
        tell apart, not easier.
        """
        from opencsi.tray.icons import colours_for

        green = colours_for("OK")
        blue = colours_for("REFRESHING")
        amber = colours_for("LOGIN_REQUIRED")
        red = colours_for("SERVER_ERROR")
        grey = colours_for("OFFLINE")

        # Working states share a colour: both mean "wait".
        self.assertEqual(blue, colours_for("RENEWING"))
        # Action states share a colour: both mean "you must do something".
        self.assertEqual(amber, colours_for("AUTH_ERROR"))
        # And the five meanings are mutually distinct.
        self.assertEqual(len({green, blue, amber, red, grey}), 5)

    def test_the_healthy_colour_is_only_used_for_healthy_states(self) -> None:
        """Asserting "fine" about anything else is the failure mode to prevent."""
        from opencsi.tray.icons import colours_for

        healthy = colours_for("OK")
        for state in MonitorState:
            if state is MonitorState.OK:
                continue
            with self.subTest(state=state.value):
                self.assertNotEqual(
                    colours_for(state.value),
                    healthy,
                    f"{state.value} is drawn as healthy",
                )

    def test_not_ok_states_have_a_shape_signal_too(self) -> None:
        """Colour alone excludes colour-blind users, so the shape changes.

        A notch is cut out of the top-right for anything that is not OK, which
        survives greyscale and a monochrome theme.
        """
        from opencsi.tray.icons import make_icon, pillow_available

        if not pillow_available():
            self.skipTest("Pillow is not installed")

        ok = make_icon("OK").convert("RGBA")
        bad = make_icon("SERVER_ERROR").convert("RGBA")

        # Count pixels that differ in alpha, rather than probing one guessed
        # coordinate: the rounded-rectangle inset already makes the extreme
        # corner transparent, so a naive (63, 1) probe passes for both states.
        differing = sum(
            1
            for y in range(64)
            for x in range(64)
            if ok.getpixel((x, y))[3] != bad.getpixel((x, y))[3]
        )
        self.assertGreater(
            differing,
            50,
            "a non-OK state differs from OK only in colour, so the signal is "
            "lost in greyscale and for colour-blind users",
        )
        # And the difference is that the non-OK icon lost pixels (the notch),
        # not that it gained them.
        self.assertTrue(
            all(
                bad.getpixel((x, y))[3] <= ok.getpixel((x, y))[3]
                for y in range(64)
                for x in range(64)
            ),
            "the notch should cut pixels away, not add them",
        )

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

    def test_browser_auto_recovery_is_off_unless_asked_for(self) -> None:
        """It puts a window on someone's desktop; that is not a default."""
        from opencsi.cli.context import build_parser

        parser = build_parser()
        self.assertFalse(parser.parse_args(["tray"]).auto_recover_browser)
        self.assertTrue(
            parser.parse_args(["tray", "--auto-recover-browser"]).auto_recover_browser
        )

    def test_the_hidden_host_is_on_by_default_and_can_be_refused(self) -> None:
        """Opposite defaults, because the two recoveries cost the user differently.

        The hidden host opens nothing, so it is on by default and the flag turns
        it *off* (``--no-auth-host``). The visible browser opens a window, so it
        is off by default and the flag turns it *on*. Getting either polarity
        backwards would either open windows unasked or leave §30's post-reboot
        case reporting a failure it was supposed to resolve.
        """
        from opencsi.cli.context import build_parser

        parser = build_parser()
        self.assertTrue(parser.parse_args(["tray"]).auto_recover_auth_host)
        self.assertFalse(
            parser.parse_args(["tray", "--no-auth-host"]).auto_recover_auth_host
        )
        # The two flags are independent, not aliases of one another.
        both = parser.parse_args(["tray", "--no-auth-host", "--auto-recover-browser"])
        self.assertFalse(both.auto_recover_auth_host)
        self.assertTrue(both.auto_recover_browser)

    def test_once_exit_codes_name_the_cause_not_a_blanket_permission_error(
        self,
    ) -> None:
        """A one-shot's exit code is a public contract; 20 was a catch-all.

        ``_once`` used to fall through to ``return 20`` -- "permission denied" --
        for every state that was not explicitly listed. A missing browser is not
        a permissions problem, and a script branching on 20 would look for the
        wrong fix entirely.
        """
        from opencsi.cli.tray import _once_exit_code
        from opencsi.errors import (
            EXIT_CDP_UNAVAILABLE,
            EXIT_NETWORK_ERROR,
            EXIT_SERVER_ERROR,
            EXIT_SESSION_EXPIRED,
        )

        self.assertEqual(_once_exit_code(MonitorState.OK), 0)
        self.assertEqual(
            _once_exit_code(MonitorState.BROWSER_UNAVAILABLE), EXIT_CDP_UNAVAILABLE
        )
        self.assertEqual(
            _once_exit_code(MonitorState.LOGIN_REQUIRED), EXIT_SESSION_EXPIRED
        )
        self.assertEqual(_once_exit_code(MonitorState.AUTH_ERROR), EXIT_SESSION_EXPIRED)
        self.assertEqual(_once_exit_code(MonitorState.OFFLINE), EXIT_NETWORK_ERROR)
        self.assertEqual(
            _once_exit_code(MonitorState.SERVER_ERROR), EXIT_SERVER_ERROR
        )

    def test_no_state_shares_an_exit_code_by_accident(self) -> None:
        """Distinct causes must stay distinguishable, and no state may be 20.

        ``20`` is reserved for a genuine permission denial; no monitor state
        means that, so reaching it from a state would be a mapping mistake.
        """
        from opencsi.cli.tray import _once_exit_code

        codes = {state: _once_exit_code(state) for state in MonitorState}
        self.assertNotIn(20, set(codes.values()), f"a state fell through: {codes}")
        for state in (
            MonitorState.BROWSER_UNAVAILABLE,
            MonitorState.LOGIN_REQUIRED,
            MonitorState.CONSENT_REQUIRED,
            MonitorState.OFFLINE,
            MonitorState.SERVER_ERROR,
        ):
            self.assertNotEqual(codes[state], 0, f"{state} reported success")

    def test_the_consent_state_reports_a_session_problem_not_a_server_one(self) -> None:
        """A page waiting for a click is not the server's fault.

        Falling through to the ``EXIT_SERVER_ERROR`` tail would tell a script that
        openCsiTool is broken when in fact the user has one button to press.
        """
        from opencsi.cli.tray import _once_exit_code
        from opencsi.errors import EXIT_SERVER_ERROR

        code = _once_exit_code(MonitorState.CONSENT_REQUIRED)
        self.assertNotEqual(code, EXIT_SERVER_ERROR)
        self.assertEqual(code, _once_exit_code(MonitorState.LOGIN_REQUIRED))

    def test_only_the_states_that_should_fall_through_do(self) -> None:
        """Pin the catch-all's membership, so a new state must be decided.

        ``_once_exit_code`` ends in an unconditional ``return EXIT_SERVER_ERROR``.
        That is the same silent fallback that made a new ``QrLoginStatus`` report
        exit 1 and that let a new ``RenewalStatus`` do the same -- both were
        fixed this round by asserting their maps exhaustive. This one cannot be
        asserted that way, because the tail is not a map, so instead the set of
        states *allowed* to reach it is pinned exactly.

        Adding a monitor state therefore fails here until someone decides, in
        public, whether the server is really the best explanation for it. That is
        the decision ``CONSENT_REQUIRED`` needed and very nearly did not get: it
        was one line away from being reported as a server fault.
        """
        from opencsi.cli.tray import _once_exit_code
        from opencsi.errors import EXIT_SERVER_ERROR

        falls_through = {
            state for state in MonitorState if _once_exit_code(state) == EXIT_SERVER_ERROR
        }
        self.assertEqual(
            falls_through,
            {
                MonitorState.STARTING,
                MonitorState.REFRESHING,
                MonitorState.RENEWING,
                MonitorState.SERVER_ERROR,
            },
            "a monitor state reached the catch-all without an explicit decision",
        )

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

    def test_once_text_output_shows_the_credential_lifetime(self) -> None:
        """The human-readable form must carry what the JSON form already did.

        The tray menu shows the session lifetime as "会话 ...", and `--json` has
        always emitted `credential_expires_in_seconds`. Only the text form left
        it out, so the one number that predicts "will I be asked to sign in
        again soon?" was invisible to a person running `tray --once` while being
        visible to a script.
        """
        from unittest import mock

        from opencsi.cli import tray as tray_cli
        from opencsi.monitor.service import MonitorSnapshot, MonitorState

        snapshot = MonitorSnapshot(
            state=MonitorState.OK,
            total_tokens=1000,
            requests=5,
            prs=1,
            credential_expires_in=1800.0,
        )

        class _Service:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def refresh_now(self, block=False):
                return snapshot

        class _Ctx:
            def __init__(self) -> None:
                self.lines: list[str] = []

            def make_client(self):
                return object()

            def out(self, text: str = "") -> None:
                self.lines.append(text)

            def err(self, text: str) -> None:
                self.lines.append(f"ERR {text}")

            def emit(self, payload, render) -> None:
                render()

        ctx = _Ctx()
        with mock.patch("opencsi.monitor.MonitorService", _Service):
            tray_cli._once(ctx, object())

        joined = "\n".join(ctx.lines)
        self.assertIn("credential:", joined)
        # 1800s is the tray's own "30m" wording, not a raw second count.
        self.assertIn("30m", joined)

    def test_once_text_output_omits_the_line_when_the_lifetime_is_unknown(self) -> None:
        """No credential reading must not be printed as a zero or a blank."""
        from unittest import mock

        from opencsi.cli import tray as tray_cli
        from opencsi.monitor.service import MonitorSnapshot, MonitorState

        snapshot = MonitorSnapshot(state=MonitorState.OK, credential_expires_in=None)

        class _Service:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def refresh_now(self, block=False):
                return snapshot

        class _Ctx:
            def __init__(self) -> None:
                self.lines: list[str] = []

            def make_client(self):
                return object()

            def out(self, text: str = "") -> None:
                self.lines.append(text)

            def err(self, text: str) -> None:
                self.lines.append(f"ERR {text}")

            def emit(self, payload, render) -> None:
                render()

        ctx = _Ctx()
        with mock.patch("opencsi.monitor.MonitorService", _Service):
            tray_cli._once(ctx, object())

        self.assertNotIn("credential:", "\n".join(ctx.lines))


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
            # An explicit empty argv, not the ambient one. `entry.main()` now
            # forwards arguments to the CLI, so inheriting the *test runner's*
            # argv would send `unittest discover -s tests` to argparse and fail
            # the run for a reason that has nothing to do with the tray.
            code = entry.main([])
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

    def test_a_redundant_tray_prefix_is_tolerated(self) -> None:
        """``opencsi-tray.exe tray --check`` must not be a usage error.

        This binary *is* the tray, so the sub-command name is redundant here --
        but a user who has just read ``opencsi tray --check`` will type it, and
        so did the first draft of the CI workflow. The old answer was
        "unrecognized arguments: tray", which names the problem but not the fix.

        Asserted on the argv actually forwarded, so it tests the de-duplication
        rather than merely that nothing raised.
        """
        import opencsi.tray.__main__ as entry
        from opencsi.cli import app as cli_app

        seen: dict[str, object] = {}

        def fake_cli_main(argv):
            seen["argv"] = list(argv)
            return 0

        original = cli_app.main
        cli_app.main = fake_cli_main
        try:
            code = entry.main(["tray", "--check"])
        finally:
            cli_app.main = original

        self.assertEqual(code, 0)
        self.assertEqual(
            seen.get("argv"),
            ["tray", "--check"],
            "the prefix must be removed, not doubled",
        )

    def test_an_ordinary_flag_is_still_prefixed(self) -> None:
        """The fix must not break the form that always worked."""
        import opencsi.tray.__main__ as entry
        from opencsi.cli import app as cli_app

        seen: dict[str, object] = {}

        def fake_cli_main(argv):
            seen["argv"] = list(argv)
            return 0

        original = cli_app.main
        cli_app.main = fake_cli_main
        try:
            code = entry.main(["--check"])
        finally:
            cli_app.main = original

        self.assertEqual(code, 0)
        self.assertEqual(seen.get("argv"), ["tray", "--check"])

    def test_a_bare_tray_prefix_starts_the_resident_tray(self) -> None:
        """``opencsi-tray.exe tray`` means "start the tray", not "no arguments
        for the CLI" -- the latter would be a usage error."""
        import opencsi.tray.__main__ as entry
        from opencsi.tray import app as app_module

        started: dict[str, object] = {}

        class _FakeApp:
            def __init__(self, service):
                started["service"] = service

            def run(self, **kwargs):
                started["ran"] = True
                return 0

        original = app_module.TrayApp
        app_module.TrayApp = _FakeApp
        try:
            code = entry.main(["tray"])
        finally:
            app_module.TrayApp = original

        self.assertEqual(code, 0)
        self.assertTrue(started.get("ran"), "the resident tray was not started")

    def test_an_unexpected_failure_is_reported_not_raised(self) -> None:
        """With no console at sign-in, a traceback would be lost entirely."""
        import opencsi.tray.__main__ as entry

        def explode(*_args, **_kwargs):
            raise RuntimeError("boom")

        import opencsi.cli.context as context_module

        saved = context_module.make_context
        context_module.make_context = explode
        try:
            # Explicitly empty: see the note in the wiring test above.
            code = entry.main([])
        finally:
            context_module.make_context = saved
        self.assertEqual(code, 1)

    def test_the_ambient_argv_is_used_when_none_is_passed(self) -> None:
        """`python -m opencsi.tray --once` must work without a caller.

        The explicit-argv parameter is a test seam; the real module invocation
        passes nothing and must still read the process's own arguments.
        """
        from unittest import mock

        import opencsi.cli.app as cli_app

        import opencsi.tray.__main__ as entry

        reached = []
        with mock.patch.object(
            cli_app, "main", lambda argv=None: reached.append(list(argv)) or 0
        ), mock.patch.object(sys, "argv", ["opencsi.tray", "--once", "--json"]):
            entry.main()

        self.assertEqual(reached, [["tray", "--once", "--json"]])


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

    def test_the_startup_toggle_reads_back_the_real_registry_state(self) -> None:
        """A toggle must report what happened, not what was requested.

        Assuming success is how a failed write stays invisible until the next
        reboot -- the worst possible moment to find out the tray does not start.
        Here the write is scripted to *not* take effect, and the app must show
        the true state rather than the intended one.
        """
        from unittest import mock

        from opencsi.tray.startup import StartupStatus

        app, _service = self._app()
        app._startup = False

        # enable() claims success but the registry still says disabled.
        with mock.patch(
            "opencsi.tray.startup.StartupManager.enable",
            return_value=StartupStatus(supported=True, enabled=False),
        ):
            app._toggle_startup()

        self.assertFalse(
            app._startup, "the app trusted the request instead of the read-back"
        )

    def test_the_startup_toggle_reflects_a_successful_write(self) -> None:
        from unittest import mock

        from opencsi.tray.startup import StartupStatus

        app, _service = self._app()
        app._startup = False

        with mock.patch(
            "opencsi.tray.startup.StartupManager.enable",
            return_value=StartupStatus(supported=True, enabled=True),
        ):
            app._toggle_startup()

        self.assertTrue(app._startup)

    def test_the_startup_toggle_turns_it_off_when_already_on(self) -> None:
        from unittest import mock

        from opencsi.tray.startup import StartupStatus

        app, _service = self._app()
        app._startup = True

        with mock.patch(
            "opencsi.tray.startup.StartupManager.disable",
            return_value=StartupStatus(supported=True, enabled=False),
        ) as disable:
            app._toggle_startup()

        disable.assert_called_once()
        self.assertFalse(app._startup)

    def test_a_failing_startup_toggle_does_not_break_the_tray(self) -> None:
        """The Run key can be unreadable; that must not kill the message loop."""
        from unittest import mock

        app, _service = self._app()
        app._startup = False

        with mock.patch(
            "opencsi.tray.startup.StartupManager.enable",
            side_effect=OSError("registry unavailable"),
        ):
            app._toggle_startup()  # must not raise

        self.assertFalse(app._startup, "the state changed without a successful write")

    def test_a_checked_menu_item_reads_its_state_at_render_time(self) -> None:
        """pystray calls the checked callback on every display.

        A closure over the value at build time would freeze the tick, so the
        toggle would look broken even though the registry had changed.
        """
        app, _service = self._app()
        app._auto_refresh = True
        app._startup = False

        self.assertTrue(app._checked_state("autorefresh"))
        self.assertFalse(app._checked_state("startup"))

        app._auto_refresh = False
        app._startup = True

        self.assertFalse(app._checked_state("autorefresh"))
        self.assertTrue(app._checked_state("startup"))

    def test_an_unreadable_registry_omits_the_item_rather_than_guessing(self) -> None:
        """No startup support -> no menu item, not a checkbox that does nothing."""
        from unittest import mock

        from opencsi.tray.startup import StartupStatus

        with mock.patch(
            "opencsi.tray.startup.StartupManager.status",
            return_value=StartupStatus(supported=False, detail="only on Windows"),
        ):
            app, _service = self._app()

        ids = [a.id for a in app.build_menu()]
        self.assertNotIn("startup", ids)


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
        self.assertIn("登录", message)

    def test_auth_error_produces_a_notification(self) -> None:
        app = self._app()
        app._notify_attention(MonitorSnapshot(state=MonitorState.AUTH_ERROR))
        self.assertEqual(len(app._icon.calls), 1)

    def test_every_attention_state_has_something_to_say(self) -> None:
        """A state the service flags for attention must actually notify.

        ``_ATTENTION_STATES`` decides *when* to fire and ``_notify_attention``
        decides *what* to say. Adding a state to the first without the second
        silently produces no balloon at all -- the latch is set, the user is
        never told, and the state is effectively muted. That is how
        ``BROWSER_UNAVAILABLE`` first shipped.
        """
        from opencsi.monitor.service import _ATTENTION_STATES

        for state in _ATTENTION_STATES:
            with self.subTest(state=state.value):
                app = self._app()
                app._notify_attention(MonitorSnapshot(state=state))
                self.assertEqual(
                    len(app._icon.calls),
                    1,
                    f"{state.value} is an attention state but notifies nothing",
                )
                _title, message = app._icon.calls[0]
                self.assertTrue(message.strip())

    def test_the_browser_notification_names_the_action_not_the_symptom(self) -> None:
        """"Browser not running" is not something a user can act on by itself."""
        app = self._app()
        app._notify_attention(MonitorSnapshot(state=MonitorState.BROWSER_UNAVAILABLE))
        self.assertEqual(len(app._icon.calls), 1)
        _title, message = app._icon.calls[0]
        self.assertIn("启动浏览器", message)

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
