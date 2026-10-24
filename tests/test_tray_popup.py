"""The usage panel: view-model semantics, geometry, toggle policy, wiring.

The bulk of this file tests :mod:`opencsi.tray.popup_presenter` and the pure
decision functions in :mod:`opencsi.tray.popup` -- all runnable without a
display, which is what keeps them in CI on every platform. The small set of
tests that drive a real Tk tree is skipped when no display exists; on this
project's Windows runners that never happens, on Linux CI it always does, and
a skipped GUI test beats a red one that only proves "no X server here".

The scenarios are the acceptance list from the panel brief:

* the two headline numbers lead the panel and are honest about their state;
* "0" is claimed only when the server's own freshness signal covers today;
* a failed fetch keeps the last good numbers and says when they are from;
* cost is "估算" and never renders unknown as ¥0;
* the model list carries every model, sorted, totals last, `--` for unknowns;
* the toggle survives a click storm without fighting its own FocusOut;
* auto-refresh reaches the service, not a UI-only flag.
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from typing import Any
from dataclasses import replace
from datetime import datetime, timezone

import helpers  # noqa: F401  (imported for its sys.path side effect)

from opencsi.monitor import (
    DailyModelUsage,
    DailyUsage,
    MonitorSnapshot,
    MonitorState,
)
from opencsi.tray import popup_presenter as pp
from opencsi.tray.popup import compute_popup_position, should_toggle
from opencsi.tray.presenter import actions_for

# ── fixtures ────────────────────────────────────────────────────────────────
TODAY = "2026-10-24"
FETCHED = datetime(2026, 10, 24, 8, 5, tzinfo=timezone.utc)


def a_snapshot(
    *,
    state: MonitorState = MonitorState.OK,
    daily: DailyUsage | None = None,
    total_tokens: int = 6_563_731_577,
    requests: int = 62_808,
    fresh_time: str | None = "2026-10-24T07:58:29+08:00",
    fetched_at: datetime | None = FETCHED,
) -> MonitorSnapshot:
    return MonitorSnapshot(
        state=state,
        total_tokens=total_tokens,
        requests=requests,
        prs=258,
        added_lines=41088,
        generated_lines=3150,
        adopted_lines=120,
        adoption_rate=0.0381,
        data_fresh_time=fresh_time,
        fetched_at=fetched_at,
        daily_usage=daily,
    )


def a_daily(
    *,
    date: str = TODAY,
    models: tuple[DailyModelUsage, ...] | None = None,
    total_cost: float | None = 14.8,
) -> DailyUsage:
    if models is None:
        models = (
            DailyModelUsage("code_completion", "GLM-5.3-Flash", 41_000_000, 9.20),
            DailyModelUsage("code_completion", "DeepSeek-V4-Flash", 11_852_000, 5.60),
        )
    tokens = sum(m.tokens for m in models)
    return DailyUsage(
        date=date,
        total_tokens=tokens,
        total_cost=total_cost if models else (total_cost if total_cost else 0.0),
        currency="CNY",
        models=models,
    )


def a_view(snapshot: MonitorSnapshot, **kwargs):
    return pp.build_panel_view(snapshot, today=TODAY, **kwargs)


# ── formatting ──────────────────────────────────────────────────────────────
class FormatTest(unittest.TestCase):
    def test_magnitudes_use_wan_and_yi(self) -> None:
        self.assertEqual(pp.format_cn_tokens(52_852_000), "5,285.2 万")
        self.assertEqual(pp.format_cn_tokens(6_563_731_577), "65.64 亿")
        self.assertEqual(pp.format_cn_tokens(9_999), "9,999")
        self.assertEqual(pp.format_cn_tokens(0), "0")

    def test_none_is_never_zero(self) -> None:
        self.assertEqual(pp.format_cn_tokens(None), "--")
        self.assertEqual(pp.format_cost_display(None), "--")

    def test_cost_is_estimated_cny(self) -> None:
        self.assertEqual(pp.format_cost_display(14.8), "¥14.80")
        self.assertEqual(pp.format_cost_display(0.0), "¥0.00")
        self.assertEqual(pp.format_cost_display(3.2, "USD"), "USD 3.20")


# ── the today card's honesty rules ──────────────────────────────────────────
class TodayCardTest(unittest.TestCase):
    def test_nonzero_usage_is_the_headline(self) -> None:
        """Scenario 1: today's tokens and cost, prominent and correct."""
        view = a_view(a_snapshot(daily=a_daily()))
        self.assertIs(view.today.state, pp.TodayState.AVAILABLE_NONZERO)
        self.assertEqual(view.today.tokens_display, "5,285.2 万")
        self.assertEqual(view.today.tokens_full, "52,852,000")
        self.assertEqual(view.today.cost_display, "¥14.80")
        self.assertIn("2026-10-24", view.today.date or "")
        self.assertIn("最近更新", view.today.caption)
        self.assertIn("openCsiTool", view.today.caption)

    def test_confirmed_zero_says_zero(self) -> None:
        """Scenario 2: an empty trend *with* today's freshness stamp is zero."""
        daily = a_daily(models=(), total_cost=0.0)
        view = a_view(a_snapshot(daily=daily, fresh_time="2026-10-24T07:00:00+08:00"))
        self.assertIs(view.today.state, pp.TodayState.AVAILABLE_ZERO)
        self.assertEqual(view.today.tokens_display, "0")
        self.assertEqual(view.today.cost_display, "¥0.00")

    def test_empty_trend_without_freshness_is_not_ready(self) -> None:
        """Scenario 3a: the server has not synced today; zero would be a lie."""
        daily = a_daily(models=(), total_cost=0.0)
        view = a_view(a_snapshot(daily=daily, fresh_time="2026-10-23T23:00:00+08:00"))
        self.assertIs(view.today.state, pp.TodayState.NOT_READY)
        self.assertEqual(view.today.tokens_display, "--")
        self.assertIn("尚未同步", view.today.note)

    def test_a_stale_freshness_stamp_with_no_server_time_is_not_ready(self) -> None:
        daily = a_daily(models=(), total_cost=0.0)
        view = a_view(a_snapshot(daily=daily, fresh_time=None))
        self.assertIs(view.today.state, pp.TodayState.NOT_READY)

    def test_no_daily_data_is_unavailable(self) -> None:
        """Scenario 3b: no today data at all is `--`, never 0."""
        view = a_view(a_snapshot(daily=None))
        self.assertIs(view.today.state, pp.TodayState.UNAVAILABLE)
        self.assertEqual(view.today.tokens_display, "--")
        self.assertEqual(view.today.cost_display, "--")
        self.assertIn("暂不可用", view.today.caption)

    def test_partial_costs_are_never_presented_as_total(self) -> None:
        """Scenario 4: an unpriceable model makes the total `--`, with a note."""
        daily = a_daily(
            models=(
                DailyModelUsage("code_completion", "GLM-5.3-Flash", 41_000_000, 9.20),
                DailyModelUsage("flat_plan", "包月模型", 1_000_000, None),
            ),
            total_cost=None,
        )
        view = a_view(a_snapshot(daily=daily))
        self.assertIs(view.today.state, pp.TodayState.COST_UNKNOWN)
        self.assertEqual(view.today.cost_display, "--")
        self.assertIn("无法完整估算", view.today.note)
        # The per-model rows keep the same honesty: unknown is --, not ¥0.
        rows = {row.name: row for row in view.models}
        self.assertEqual(rows["包月模型"].cost_display, "--")

    def test_stale_data_is_labelled_with_its_time(self) -> None:
        """Scenario 7: retained numbers through a failure must say when."""
        snapshot = a_snapshot(state=MonitorState.OFFLINE, daily=a_daily())
        view = a_view(snapshot)
        self.assertIs(view.today.state, pp.TodayState.STALE)
        self.assertIn("上次成功更新", view.today.caption)
        self.assertIn("最近一次成功查询", view.today.note)
        # The numbers themselves survive: a failure never blanks the panel.
        self.assertEqual(view.today.tokens_display, "5,285.2 万")

    def test_day_rollover_never_relabels_yesterday(self) -> None:
        """Scenario 10: yesterday's card is not today's, even with numbers."""
        daily = a_daily(date="2026-10-23")
        view = a_view(a_snapshot(daily=daily))
        self.assertIs(view.today.state, pp.TodayState.NOT_READY)
        self.assertEqual(view.today.tokens_display, "--")
        self.assertIn("跨日", view.today.note)

    def test_cumulative_is_kept_separate_from_today(self) -> None:
        view = a_view(a_snapshot(daily=a_daily()))
        assert view.cumulative is not None
        self.assertEqual(view.cumulative.tokens_display, "65.64 亿")
        self.assertEqual(view.cumulative.tokens_full, "6,563,731,577")
        self.assertEqual(view.cumulative.requests_display, "62,808")

    def test_huge_cumulative_stays_readable(self) -> None:
        """Scenario 6: 100 亿+ formats without collapsing to digits."""
        view = a_view(a_snapshot(total_tokens=12_345_678_901))
        assert view.cumulative is not None
        self.assertEqual(view.cumulative.tokens_display, "123.46 亿")


# ── the model breakdown ─────────────────────────────────────────────────────
class ModelRowsTest(unittest.TestCase):
    def test_rows_are_sorted_descending_with_totals_last(self) -> None:
        daily = a_daily(
            models=(
                DailyModelUsage("a", "Small-Model", 100, 0.01),
                DailyModelUsage("b", "GLM-5.3-Flash", 41_000_000, 9.20),
                DailyModelUsage("c", "DeepSeek-V4-Flash", 11_852_000, 5.60),
            ),
            total_cost=14.81,
        )
        view = a_view(a_snapshot(daily=daily))
        names = [row.name for row in view.models]
        self.assertEqual(names, ["GLM-5.3-Flash", "DeepSeek-V4-Flash", "Small-Model"])

    def test_more_than_ten_models_all_appear(self) -> None:
        """Scenario 5: a long list is data-complete; scrolling is the UI's job."""
        models = tuple(
            DailyModelUsage(f"type-{i}", f"Model-{i:02d}", 1000 * (50 - i), 0.5)
            for i in range(14)
        )
        view = a_view(a_snapshot(daily=a_daily(models=models, total_cost=7.0)))
        self.assertEqual(len(view.models), 14)
        tokens = [row.tokens_display for row in view.models]
        self.assertEqual(tokens[0], "5.0 万")  # the largest first

    def test_row_displays_are_magnitude_formatted(self) -> None:
        daily = a_daily()
        view = a_view(a_snapshot(daily=daily))
        self.assertEqual(view.models[0].tokens_display, "4,100.0 万")
        self.assertEqual(view.models[0].tokens_full, "41,000,000")


# ── status line ─────────────────────────────────────────────────────────────
class StatusViewTest(unittest.TestCase):
    def test_every_attention_state_has_a_reason_and_a_fix(self) -> None:
        """Scenario 8: a failure is never just a red dot."""
        for state in (
            MonitorState.LOGIN_REQUIRED,
            MonitorState.CONSENT_REQUIRED,
            MonitorState.BROWSER_UNAVAILABLE,
            MonitorState.OFFLINE,
            MonitorState.SERVER_ERROR,
            MonitorState.AUTH_ERROR,
        ):
            with self.subTest(state=state.value):
                view = a_view(a_snapshot(state=state, daily=None))
                self.assertEqual(view.status.state, state)
                self.assertTrue(view.status.reason, "no reason shown")
                self.assertTrue(view.status.hint, "no recovery step shown")

    def test_labels_match_the_menu_vocabulary(self) -> None:
        view = a_view(a_snapshot(state=MonitorState.LOGIN_REQUIRED))
        self.assertEqual(view.status.label, "需要登录")
        view = a_view(a_snapshot(state=MonitorState.REFRESHING))
        self.assertEqual(view.status.label, "正在刷新")

    def test_refreshing_and_renewing_are_busy_not_failed(self) -> None:
        """Scenario 9: in-progress work is calm blue, and renewal is hidden."""
        view = a_view(
            replace(
                a_snapshot(state=MonitorState.REFRESHING, daily=a_daily()),
                credential_expires_in=7200.0,
            )
        )
        self.assertEqual(view.status.label, "正在刷新")
        self.assertFalse(view.renew_available)
        view = a_view(
            replace(
                a_snapshot(state=MonitorState.RENEWING, daily=a_daily()),
                credential_expires_in=7200.0,
            )
        )
        self.assertEqual(view.status.label, "正在续约")
        self.assertFalse(view.renew_available)

    def test_renewal_is_offered_only_when_useful(self) -> None:
        # Two hours left: a renewal would answer ALREADY_VALID, so the action
        # hides. The unknown-lifetime case follows the existing menu contract
        # (offered), which is why the healthy fixture here carries a lifetime.
        healthy = replace(
            a_snapshot(daily=a_daily()), credential_expires_in=7200.0
        )
        self.assertFalse(a_view(healthy).renew_available)
        expiring = replace(
            a_snapshot(daily=a_daily()), credential_expires_in=120.0
        )
        view = a_view(expiring)
        self.assertTrue(view.renew_available)
        self.assertIn("分钟", view.renew_reason)

    def test_startup_none_hides_the_control(self) -> None:
        view = a_view(a_snapshot(daily=a_daily()), startup=None)
        self.assertIsNone(view.startup)
        view = a_view(a_snapshot(daily=a_daily()), startup=True)
        self.assertTrue(view.startup)


# ── geometry and the toggle policy (pure) ───────────────────────────────────
class GeometryTest(unittest.TestCase):
    WORK = (0, 0, 1920, 1040)

    def test_bottom_taskbar_places_the_panel_above_the_anchor(self) -> None:
        x, y = compute_popup_position(1800, 1040, self.WORK, 420, 520)
        self.assertLess(y + 520, 1040)  # above the taskbar edge
        self.assertGreaterEqual(x, 0)
        self.assertLessEqual(x + 420, 1920)

    def test_top_taskbar_drops_the_panel_below_the_anchor(self) -> None:
        x, y = compute_popup_position(1800, 48, self.WORK, 420, 520)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(y + 520, 1040)

    def test_left_and_right_taskbars_hug_their_edge(self) -> None:
        x, y = compute_popup_position(0, 500, self.WORK, 420, 520)
        self.assertEqual(x, 0)
        x, _ = compute_popup_position(1920, 500, self.WORK, 420, 520)
        self.assertEqual(x, 1920 - 420)

    def test_clamped_inside_a_small_work_area(self) -> None:
        x, y = compute_popup_position(50, 50, (0, 0, 800, 600), 420, 520)
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(x + 420, 800)
        self.assertLessEqual(y + 520, 600)

    def test_anchor_centering_keeps_the_panel_on_screen(self) -> None:
        x, _ = compute_popup_position(100, 1040, self.WORK, 420, 520)
        self.assertEqual(x, 0)  # centered on x=100 would be negative


class TogglePolicyTest(unittest.TestCase):
    """Scenario 12: a click storm must not make the panel flicker."""

    def test_hidden_panel_always_opens(self) -> None:
        self.assertTrue(should_toggle(visible=False, last_hidden_at=None, now=100.0))
        self.assertTrue(
            should_toggle(visible=False, last_hidden_at=99.9, now=100.0)
        )

    def test_visible_panel_toggles_closed(self) -> None:
        self.assertTrue(
            should_toggle(visible=True, last_hidden_at=None, now=100.0)
        )

    def test_a_click_right_after_focus_out_is_suppressed(self) -> None:
        """The FocusOut hide consumed the click; the toggle must not reopen."""
        self.assertFalse(
            should_toggle(visible=True, last_hidden_at=100.1, now=100.3)
        )

    def test_a_later_click_toggles_normally(self) -> None:
        self.assertTrue(
            should_toggle(visible=True, last_hidden_at=100.1, now=100.7)
        )


# ── the menu keeps a single left-click target ───────────────────────────────
class MenuPanelEntryTest(unittest.TestCase):
    def test_panel_entry_is_the_sole_default_when_supported(self) -> None:
        view = a_view(a_snapshot(state=MonitorState.LOGIN_REQUIRED, daily=None))
        _ = view
        actions = actions_for(
            a_snapshot(state=MonitorState.LOGIN_REQUIRED), panel_supported=True
        )
        self.assertEqual(actions[0].id, "panel")
        defaults = [a for a in actions if a.default]
        self.assertEqual(len(defaults), 1)
        self.assertEqual(defaults[0].id, "panel")

    def test_legacy_menu_is_unchanged_without_the_panel(self) -> None:
        actions = actions_for(
            a_snapshot(state=MonitorState.LOGIN_REQUIRED), panel_supported=False
        )
        self.assertNotIn("panel", [a.id for a in actions])
        # The state-specific default action is back in charge of left-click.
        self.assertTrue(any(a.default and a.id == "login_qr" for a in actions))


# ── real-Tk tests (skipped without a display) ───────────────────────────────
def _tk_available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import tkinter

        root = tkinter.Tk()
        root.destroy()
        return True
    except Exception:  # noqa: BLE001 - no display, no tcl/tk
        return False


class _FakeService:
    """Just enough MonitorService surface for the panel."""

    def __init__(self) -> None:
        self.snapshot = a_snapshot(daily=a_daily())
        self._subs: list = []
        self.auto_refresh_enabled = True
        self.refresh_calls = 0
        self.renew_calls = 0
        self.stop_calls = 0
        self.auto_flags: list[bool] = []

    def subscribe(self, callback):
        self._subs.append(callback)

        def unsub() -> None:
            self._subs.remove(callback)

        return unsub

    def publish(self, snapshot: MonitorSnapshot) -> None:
        self.snapshot = snapshot
        for cb in list(self._subs):
            cb(snapshot)

    def refresh_now(self, *, block: bool = False) -> None:
        self.refresh_calls += 1

    def renew_now(self, *, block: bool = False) -> None:
        self.renew_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1

    def set_auto_refresh(self, enabled: bool) -> None:
        self.auto_refresh_enabled = enabled
        self.auto_flags.append(enabled)


@unittest.skipUnless(_tk_available(), "no display or no tcl/tk")
class PanelInteractionTest(unittest.TestCase):
    """The widget tree, driven the way the threads drive it.

    One Tk root is shared by the whole class and created with a short retry:
    on this tcl version, creating a new interpreter immediately after another
    was destroyed can fail once with "can't find init.tcl" -- a race in Tcl's
    notifier teardown, not a missing installation. A retry that succeeds on the
    second try is exactly what that looks like.
    """

    root: Any = None

    @classmethod
    def setUpClass(cls) -> None:
        import tkinter as tk

        last: Exception | None = None
        for _attempt in range(3):
            try:
                cls.root = tk.Tk()
                break
            except Exception as exc:  # noqa: BLE001 - the retry IS the test
                last = exc
                time.sleep(0.2)
        if cls.root is None:
            raise last if last is not None else RuntimeError("no Tk")

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.root is not None:
            cls.root.destroy()
            cls.root = None

    def setUp(self) -> None:
        from opencsi.tray.popup import PopupController

        self.service = _FakeService()
        self.controller = PopupController(self.service, poll_ms=10)
        self.controller._root = type(self).root
        self.controller._build_panel()
        self.controller._apply_snapshot(self.service.snapshot)

    def tearDown(self) -> None:
        # The panel is a child of the shared class root; destroy its tree but
        # not the root itself.
        self.controller._panel.destroy()
        self.controller._panel = None

    def test_the_panel_renders_the_headline_numbers(self) -> None:
        w = self.controller._widgets
        self.assertEqual(w["today_tokens"]["text"], "5,285.2 万")
        self.assertEqual(w["today_cost"]["text"], "¥14.80")
        self.assertIn("2026-10-24", w["today_caption"]["text"])
        self.assertEqual(w["status"]["text"], "正常")

    def test_a_background_snapshot_updates_without_rebuilding(self) -> None:
        """Scenario 13: the monitor publishes; the widgets patch in place."""
        w = self.controller._widgets
        token_widget = w["today_tokens"]
        daily = a_daily(models=(
            DailyModelUsage("code_completion", "GLM-5.3-Flash", 52_852_345, 22.4),
        ), total_cost=22.4)
        self.service.publish(a_snapshot(daily=daily))
        self.controller.enqueue("snapshot", self.service.snapshot)
        self.controller._drain()
        self.assertEqual(token_widget, w["today_tokens"])  # same widget object
        self.assertEqual(w["today_tokens"]["text"], "5,285.2 万")

    def test_updates_from_many_threads_never_break_the_loop(self) -> None:
        """Scenario 14: the monitor thread never touches a widget directly."""
        errors: list[BaseException] = []

        def hammer() -> None:
            try:
                for i in range(30):
                    self.service.publish(
                        a_snapshot(daily=a_daily(total_cost=1.0 + i))
                    )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=hammer) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.controller._drain()
        self.controller._root.update()
        self.assertEqual(errors, [])

    def test_rapid_toggles_settle_closed_or_open_without_duplicates(self) -> None:
        """Scenario 12, through the queue: 5 toggles in a burst.

        Five, starting hidden: open, close, open, close, open -- one window,
        no duplicates, and the final state matches the parity of the count.
        """
        for _ in range(5):
            self.controller.enqueue("toggle")
        self.controller._drain()
        self.controller._root.update()
        self.assertTrue(self.controller._visible)
        self.controller.enqueue("toggle")
        self.controller._drain()
        self.controller._root.update()
        self.assertFalse(self.controller._visible)

    def test_hiding_leaves_the_monitor_running(self) -> None:
        """Scenario 15: closing the panel is not stopping the service."""
        self.controller.enqueue("hide")
        self.controller._drain()
        self.assertFalse(self.controller._visible)
        self.assertEqual(self.service.stop_calls, 0)

    def test_the_auto_refresh_checkbox_reaches_the_service(self) -> None:
        """Scenario 11: the switch is the service's switch, not a UI flag."""
        self.controller._widgets["auto_refresh_var"].set(False)
        self.controller._on_auto_refresh()
        self.assertEqual(self.service.auto_flags, [False])
        self.controller._widgets["auto_refresh_var"].set(True)
        self.controller._on_auto_refresh()
        self.assertEqual(self.service.auto_flags, [False, True])

    def test_refresh_and_renew_buttons_call_the_service(self) -> None:
        self.controller._refresh()
        self.controller._renew()
        self.assertEqual(self.service.refresh_calls, 1)
        self.assertEqual(self.service.renew_calls, 1)

    def test_renew_button_hidden_on_a_healthy_session(self) -> None:
        self.controller._apply_snapshot(
            replace(a_snapshot(daily=a_daily()), credential_expires_in=7200.0)
        )
        self.controller._root.update()
        renew = self.controller._widgets["btn_renew"]
        self.assertEqual(renew.winfo_manager(), "")  # not packed
        self.controller._apply_snapshot(
            replace(a_snapshot(daily=a_daily()), credential_expires_in=120.0)
        )
        self.controller._root.update()
        self.assertEqual(self.controller._widgets["btn_renew"].winfo_manager(), "pack")

    def test_stale_state_shows_the_stale_caption(self) -> None:
        self.controller._apply_snapshot(
            a_snapshot(state=MonitorState.OFFLINE, daily=a_daily())
        )
        self.controller._root.update()
        w = self.controller._widgets
        self.assertIn("上次成功更新", w["today_sub"]["text"])
        self.assertEqual(w["today_tokens"]["text"], "5,285.2 万")


if __name__ == "__main__":
    unittest.main()
