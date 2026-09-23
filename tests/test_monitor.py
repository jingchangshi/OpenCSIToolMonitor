"""MonitorService: the polling policy, error classification and data retention.

The service is the whole reason the tray can be dumb. Everything interesting --
when to poll, when to renew, what a failure means, what survives a failure --
is here, and it is all testable with a fake clock and no GUI.
"""

from __future__ import annotations

import threading
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

import helpers

from opencsi.auth.session import (
    CredentialStatus,
    RenewalResult,
    RenewalStatus,
    SessionManager,
)
from opencsi.errors import (
    CdpUnavailableError,
    NetworkError,
    OpenCsiError,
    SessionExpiredError,
)
from opencsi.models import MyToolsSnapshot
from opencsi.monitor import (
    MonitorConfig,
    MonitorService,
    MonitorSnapshot,
    MonitorState,
    state_for_error,
)


class _Clock:
    """A monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _Session:
    """A SessionManager stand-in with scripted renewal behaviour."""

    def __init__(
        self,
        *,
        expires_in: float | None = 3600.0,
        renew_result: RenewalResult | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self.expires_in = expires_in
        self.renew_result = renew_result or RenewalResult(
            RenewalStatus.RENEWED, renewed=True, token_changed=True
        )
        self.raises = raises
        self.renew_calls = 0
        self.credentials = object()

    def status(self) -> CredentialStatus:
        return CredentialStatus(
            available=self.expires_in is not None,
            source="cdp",
            expires_at=None,
            expires_in=self.expires_in,
        )

    def needs_renewal(self, margin: float | None = None) -> bool:
        if self.expires_in is None:
            return False
        return self.expires_in <= (margin if margin is not None else 300.0)

    def renew(self, *, force: bool = False) -> RenewalResult:
        self.renew_calls += 1
        if self.raises is not None:
            raise self.raises
        return self.renew_result


def _snapshot(
    *,
    tokens: int = 1234,
    requests: int = 56,
    prs: int = 7,
    generated: int = 100,
    adopted: int = 80,
) -> MyToolsSnapshot:
    """A MyToolsSnapshot with recognisable numbers."""
    from opencsi.models import SyncStatus, ToolGrant

    grant = ToolGrant(
        request_type="code_completion",
        token_usage=tokens,
        request_count=requests,
        pr_count=prs,
        generated_code_lines=generated,
        adopted_code_lines=adopted,
    )
    snap = MyToolsSnapshot(
        grants=(grant,),
        total_tokens=tokens,
        total_request_count=requests,
        sync_status=SyncStatus(data_fresh_time="2025-01-01T00:00:00Z"),
        fetched_at=datetime.now(timezone.utc),
    )
    return snap


class _Client:
    """A client stand-in that returns a snapshot or raises a scripted error."""

    def __init__(
        self,
        *,
        snapshot=None,
        error: BaseException | None = None,
        last_renewal=None,
    ) -> None:
        self._snapshot = snapshot if snapshot is not None else _snapshot()
        self._error = error
        self.calls = 0
        # Mirrors OpenCsiClient.last_renewal, which the monitor consults to
        # classify a 401 that a renewal attempt could explain.
        self.last_renewal = last_renewal

    def get_my_tools(self, *args, **kwargs):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._snapshot


def _service(client, session=None, clock=None, **cfg):
    session = session if session is not None else _Session()
    client.session = session
    return MonitorService(
        client,
        session=session,
        config=MonitorConfig(**cfg),
        clock=clock or _Clock(),
    )


class SnapshotTest(unittest.TestCase):
    """MonitorSnapshot is the only thing the UI sees, so it must be safe."""

    def test_numbers_come_from_the_api_snapshot(self) -> None:
        snap = MonitorSnapshot.from_snapshot(_snapshot())
        self.assertEqual(snap.total_tokens, 1234)
        self.assertEqual(snap.requests, 56)
        self.assertEqual(snap.prs, 7)
        self.assertEqual(snap.generated_lines, 100)
        self.assertEqual(snap.adopted_lines, 80)
        self.assertAlmostEqual(snap.adoption_rate, 0.8)

    def test_data_fresh_time_is_the_servers_own_timestamp(self) -> None:
        """The server's refresh time must not be conflated with our fetch time."""
        snap = MonitorSnapshot.from_snapshot(_snapshot())
        self.assertEqual(snap.data_fresh_time, "2025-01-01T00:00:00Z")
        self.assertIsNotNone(snap.fetched_at)

    def test_a_snapshot_has_no_field_that_could_hold_a_secret(self) -> None:
        """Secret-freedom is a property of the type, not of each call site."""
        import dataclasses

        forbidden = (
            "token",
            "cookie",
            "virtualkey",
            "virtual_key",
            "secret",
            "authorization",
            "userid",
            "user_id",
            "accountid",
            "account_id",
            "employeeid",
            "employee_id",
            "session",
        )
        names = [f.name for f in dataclasses.fields(MonitorSnapshot)]
        for name in names:
            self.assertNotIn(name.lower(), forbidden, f"{name} looks like a secret")

    def test_as_dict_is_secret_free_and_serialisable(self) -> None:
        import json

        snap = MonitorSnapshot.from_snapshot(_snapshot())
        payload = json.dumps(snap.as_dict())
        for banned in ("cookie", "virtualKey", "Bearer", "sk-"):
            self.assertNotIn(banned, payload)
        # "total_tokens" is a legitimate metric name, not a credential, so the
        # assertion is that no *value* looks like one -- not that the word
        # "token" never appears.
        self.assertIn('"total_tokens": 1234', payload)

    def test_with_state_keeps_the_last_good_data(self) -> None:
        """A failure must not blank the display."""
        snap = MonitorSnapshot.from_snapshot(_snapshot())
        offline = snap.with_state(MonitorState.OFFLINE, error="network is down")
        self.assertEqual(offline.state, MonitorState.OFFLINE)
        self.assertEqual(offline.total_tokens, 1234)
        self.assertEqual(offline.last_error, "network is down")
        self.assertTrue(offline.has_data)

    def test_age_is_measured_from_our_fetch_time(self) -> None:
        snap = MonitorSnapshot.from_snapshot(_snapshot())
        age = snap.age_seconds(now=snap.fetched_at.timestamp() + 120)
        self.assertAlmostEqual(age, 120, places=3)

    def test_age_is_none_before_any_successful_fetch(self) -> None:
        self.assertIsNone(MonitorSnapshot().age_seconds())

    def test_labels_cover_every_state(self) -> None:
        from opencsi.monitor import STATE_LABELS

        for state in MonitorState:
            self.assertIn(state, STATE_LABELS)


class ErrorClassificationTest(unittest.TestCase):
    """Every failure must keep its own identity (objective §31)."""

    def test_login_required_is_not_a_generic_error(self) -> None:
        exc = OpenCsiError("nope")
        exc.code = "OPENCSITOOL_NOT_LOGGED_IN"
        self.assertIs(state_for_error(exc), MonitorState.LOGIN_REQUIRED)

    def test_network_error_maps_to_offline(self) -> None:
        self.assertIs(state_for_error(NetworkError("down")), MonitorState.OFFLINE)

    def test_a_missing_browser_is_not_reported_as_login_required(self) -> None:
        """The two need different actions, so they need different states.

        Mapping ``CDP_UNAVAILABLE`` to ``LOGIN_REQUIRED`` told the user to sign
        in when the actual problem was that no readable browser was running.
        Following that advice could not work -- the login action opened a
        browser without a debugging port -- so the user was sent round a loop
        with no exit. The state must name the real obstacle.
        """
        from opencsi.errors import CdpUnavailableError

        state = state_for_error(CdpUnavailableError("no endpoint"))
        self.assertIs(state, MonitorState.BROWSER_UNAVAILABLE)
        self.assertIsNot(state, MonitorState.LOGIN_REQUIRED)

    def test_no_browser_target_is_also_a_browser_problem(self) -> None:
        """Same remedy: bring the browser up. Not "sign in"."""
        from opencsi.errors import NoBrowserTargetError

        state = state_for_error(NoBrowserTargetError("no page"))
        self.assertIs(state, MonitorState.BROWSER_UNAVAILABLE)

    def test_a_genuinely_missing_cookie_is_still_login_required(self) -> None:
        """The distinction must not swallow the real sign-in case."""
        from opencsi.errors import CookieNotFoundError

        state = state_for_error(CookieNotFoundError("no cookie"))
        self.assertIs(state, MonitorState.LOGIN_REQUIRED)

    def test_the_browser_state_needs_the_user(self) -> None:
        """It is not a wait-and-see condition: nothing recovers on its own."""
        from opencsi.monitor.service import _ATTENTION_STATES

        self.assertIn(MonitorState.BROWSER_UNAVAILABLE, _ATTENTION_STATES)

    def test_server_error_maps_to_server_error(self) -> None:
        exc = OpenCsiError("boom")
        exc.code = "SERVER_ERROR"
        self.assertIs(state_for_error(exc), MonitorState.SERVER_ERROR)

    def test_an_unknown_code_does_not_become_ok(self) -> None:
        """The dangerous default is 'fine'. It must never be the fallback."""
        exc = OpenCsiError("mystery")
        exc.code = "SOMETHING_WE_HAVE_NEVER_SEEN"
        state = state_for_error(exc)
        self.assertIsNot(state, MonitorState.OK)
        self.assertIs(state, MonitorState.SERVER_ERROR)

    def test_no_error_is_ok(self) -> None:
        self.assertIs(state_for_error(None), MonitorState.OK)

    def test_states_are_distinct_identities(self) -> None:
        """States are compared by identity, never by parsing a string."""
        seen = {
            state_for_error(_code_error(code))
            for code in ("OPENCSITOOL_NOT_LOGGED_IN", "NETWORK_ERROR", "SERVER_ERROR")
        }
        self.assertEqual(len(seen), 3)

    def test_every_real_error_code_is_mapped(self) -> None:
        """A new error class must not silently fall through to the default.

        The default is deliberately not ``OK``, but it is also not *right*: a
        code with no entry means the user sees "Server error" for a problem
        that had a better answer. This test is what makes the table stay
        complete as error classes are added.

        The scan walks the whole package rather than just ``errors.py``, because
        ``WebSocketError`` lives in ``ws.py`` and is a real code a user can hit.
        """
        import inspect
        import pkgutil
        import importlib

        import opencsi
        from opencsi.errors import OpenCsiError
        from opencsi.monitor.service import _CODE_STATE

        classes = []
        for info in pkgutil.walk_packages(opencsi.__path__, "opencsi."):
            try:
                module = importlib.import_module(info.name)
            except Exception:  # noqa: BLE001 - an unimportable module is not ours
                continue
            for _name, obj in inspect.getmembers(module, inspect.isclass):
                if issubclass(obj, OpenCsiError) and obj is not OpenCsiError:
                    classes.append(obj)

        real = {
            cls.code for cls in classes if isinstance(getattr(cls, "code", None), str)
        }
        self.assertGreaterEqual(len(real), 13, "the error scan found too few codes")
        # WebSocketError is the canary: it is defined outside errors.py.
        self.assertIn("WEBSOCKET_ERROR", real)

        unmapped = sorted(real - set(_CODE_STATE))
        self.assertEqual(unmapped, [], f"unmapped error codes: {unmapped}")

    def test_no_mapping_points_at_ok(self) -> None:
        """No failure may ever be reported as healthy."""
        from opencsi.monitor.service import _CODE_STATE

        for code, state in _CODE_STATE.items():
            self.assertIsNot(state, MonitorState.OK, f"{code} maps to OK")
            self.assertIsNot(state, MonitorState.STARTING, f"{code} maps to STARTING")

    def test_no_mapping_references_a_code_that_does_not_exist(self) -> None:
        """A stale entry is a lie about the codebase, so it must fail too."""
        import inspect
        import pkgutil
        import importlib

        import opencsi
        from opencsi.errors import OpenCsiError
        from opencsi.monitor.service import _CODE_STATE

        real: set[str] = set()
        for info in pkgutil.walk_packages(opencsi.__path__, "opencsi."):
            try:
                module = importlib.import_module(info.name)
            except Exception:  # noqa: BLE001
                continue
            for _name, obj in inspect.getmembers(module, inspect.isclass):
                if issubclass(obj, OpenCsiError) and obj is not OpenCsiError:
                    code = getattr(obj, "code", None)
                    if isinstance(code, str):
                        real.add(code)

        phantom = sorted(set(_CODE_STATE) - real)
        self.assertEqual(phantom, [], f"mapped codes that do not exist: {phantom}")


def _code_error(code: str) -> OpenCsiError:
    exc = OpenCsiError(code)
    exc.code = code
    return exc


class RefreshPolicyTest(unittest.TestCase):
    def test_a_successful_refresh_publishes_ok_with_data(self) -> None:
        clock = _Clock()
        service = _service(_Client(), clock=clock)
        snap = service.refresh_now(block=True)
        self.assertIs(snap.state, MonitorState.OK)
        self.assertEqual(snap.total_tokens, 1234)
        self.assertEqual(snap.consecutive_failures, 0)

    def test_a_failure_keeps_the_last_good_numbers(self) -> None:
        """The tray must keep showing the last known values when offline."""
        clock = _Clock()
        client = _Client()
        service = _service(client, clock=clock)
        good = service.refresh_now(block=True)
        self.assertEqual(good.total_tokens, 1234)

        client._error = NetworkError("cable unplugged")
        bad = service.refresh_now(block=True)
        self.assertIs(bad.state, MonitorState.OFFLINE)
        self.assertEqual(bad.total_tokens, 1234, "the last good data was lost")
        self.assertTrue(bad.has_data)

    def test_repeated_failures_back_off_exponentially(self) -> None:
        clock = _Clock()
        client = _Client(error=NetworkError("down"))
        service = _service(client, clock=clock, backoff_base=10.0, backoff_max=1000.0)

        service.refresh_now(block=True)
        self.assertEqual(service.snapshot.consecutive_failures, 1)
        first_due = service._next_refresh_at - clock()

        service.refresh_now(block=True)
        second_due = service._next_refresh_at - clock()
        self.assertEqual(service.snapshot.consecutive_failures, 2)
        self.assertGreater(second_due, first_due, "backoff did not grow")

    def test_backoff_is_capped(self) -> None:
        clock = _Clock()
        client = _Client(error=NetworkError("down"))
        service = _service(client, clock=clock, backoff_base=10.0, backoff_max=25.0)
        for _ in range(6):
            service.refresh_now(block=True)
        self.assertLessEqual(service._next_refresh_at - clock(), 25.0)


class BrowserRecoveryTest(unittest.TestCase):
    """Automatic recovery from "the browser is not running" (objective §68, §30).

    **Both** layers are now opt-in (objective §9.2). They used to differ -- the
    hidden host ran by default because it put nothing on the desktop -- but that
    reasoning belonged to a design where the browser *was* the credential store.
    Now the secure store is consulted first, so:

    * "nothing is signed in yet" and "the browser is not running" are different
      states, and the remedy for the first is a QR scan, not a browser;
    * a tray that starts a Chromium engine on every machine that has simply not
      signed in yet is a tray that surprises its user.

    What remains is the ordering: when a user *has* opted in, the invisible
    option must still be tried before the visible one.

    Every test here patches ``_try_auth_host``. Without that the hidden-host layer
    would call ``AuthBrowserHost.ensure_running`` for real, which starts a browser
    and makes the suite depend on what happens to be installed -- and, worse,
    would make these tests pass or fail according to whether a real session
    existed on the machine.
    """

    def setUp(self) -> None:
        patcher = mock.patch.object(
            MonitorService, "_try_auth_host", return_value=False
        )
        self._auth_host = patcher.start()
        self.addCleanup(patcher.stop)

    def _failing(self, clock, **cfg):
        from opencsi.errors import CdpUnavailableError

        client = _Client(error=CdpUnavailableError("no endpoint"))
        return _service(client, clock=clock, **cfg)

    def test_nothing_is_launched_without_opt_in(self) -> None:
        clock = _Clock()
        service = self._failing(clock)

        with mock.patch(
            "opencsi.auth.browser_launch.launch_debug_browser"
        ) as launch:
            snap = service.refresh_now(block=True)

        launch.assert_not_called()
        self.assertIs(snap.state, MonitorState.BROWSER_UNAVAILABLE)

    def test_the_hidden_host_is_not_tried_without_opt_in(self) -> None:
        """§9.2: a browser engine is no longer the normal path.

        This is the change this round makes, and it is asserted directly rather
        than left to the config default: the host must not start merely because
        no credential was found. A machine that has never signed in should be
        told to scan a QR code, not silently have Chromium opened on it.
        """
        clock = _Clock()
        service = self._failing(clock)
        self._auth_host.return_value = True

        with mock.patch(
            "opencsi.auth.browser_launch.launch_debug_browser"
        ) as launch:
            service.refresh_now(block=True)

        self._auth_host.assert_not_called()
        launch.assert_not_called()

    def test_the_hidden_host_runs_when_the_user_opts_in(self) -> None:
        """Opting in still works, because migration needs it (§19).

        A user with a working session in a browser profile and an empty store can
        turn this on to seed the store from that profile. The capability is kept;
        only the default changed.
        """
        clock = _Clock()
        service = self._failing(clock, auto_recover_auth_host=True)
        self._auth_host.return_value = True

        with mock.patch(
            "opencsi.auth.browser_launch.launch_debug_browser"
        ) as launch:
            service.refresh_now(block=True)

        self._auth_host.assert_called_once()
        launch.assert_not_called()

    def test_the_hidden_host_is_preferred_over_a_visible_window(self) -> None:
        """Order matters: the invisible option must be the one tried first.

        If the visible launch ran first it would open a window on every machine
        whose Chrome was not yet running, and the hidden host would only be
        reached when that failed -- inverting §30 for the users who never opted
        in to windows.

        The auth host is opted in explicitly: the ordering only exists to be tested
        when both layers are permitted, and with the new default the host is never
        reached.
        """
        clock = _Clock()
        service = self._failing(
            clock, auto_recover_browser=True, auto_recover_auth_host=True
        )
        self._auth_host.return_value = True

        with mock.patch(
            "opencsi.auth.browser_launch.launch_debug_browser"
        ) as launch:
            service.refresh_now(block=True)

        self._auth_host.assert_called_once()
        launch.assert_not_called()

    def test_a_visible_auth_host_does_not_count_as_recovery(self) -> None:
        """A host that fell back to a window is not the recovery §30 promises.

        Chrome 153 rejects ``--headless=new``, and ``ensure_running`` then opens a
        window and reports it truthfully. Treating that as success would open a
        window for a user who declined windows *and* make the tray claim a hidden
        runtime while one was on screen.
        """
        clock = _Clock()
        service = self._failing(clock)
        self._auth_host.return_value = False

        with mock.patch(
            "opencsi.auth.browser_launch.launch_debug_browser"
        ) as launch:
            snap = service.refresh_now(block=True)

        launch.assert_not_called()
        self.assertIs(snap.state, MonitorState.BROWSER_UNAVAILABLE)

    def test_the_hidden_host_is_rate_limited_too(self) -> None:
        """A host that keeps failing must not be respawned every backoff tick.

        Opt-in is enabled here because the rate limit only means anything when the
        host is actually permitted to run; with the new default it is never
        reached at all, and the assertion would pass for the wrong reason.
        """
        clock = _Clock()
        service = self._failing(
            clock, auto_recover_auth_host=True, browser_recover_cooldown=600.0
        )
        self._auth_host.return_value = False

        for _ in range(5):
            service.refresh_now(block=True)
        self.assertEqual(
            self._auth_host.call_count, 1, "the cooldown did not hold"
        )

        clock.advance(601.0)
        service.refresh_now(block=True)
        self.assertEqual(
            self._auth_host.call_count, 2, "the cooldown never expired"
        )

    def test_an_opted_in_service_starts_a_browser(self) -> None:
        from opencsi.auth.browser_launch import BrowserLaunch, BrowserLaunchStatus

        clock = _Clock()
        service = self._failing(clock, auto_recover_browser=True)

        with mock.patch(
            "opencsi.auth.browser_launch.launch_debug_browser",
            return_value=BrowserLaunch(BrowserLaunchStatus.LAUNCHED, browser="chrome"),
        ) as launch:
            service.refresh_now(block=True)

        launch.assert_called_once()

    def test_recovery_is_not_retried_on_every_tick(self) -> None:
        """A failing launch must not spawn a window on every backoff tick."""
        from opencsi.auth.browser_launch import BrowserLaunch, BrowserLaunchStatus

        clock = _Clock()
        service = self._failing(
            clock, auto_recover_browser=True, browser_recover_cooldown=600.0
        )

        with mock.patch(
            "opencsi.auth.browser_launch.launch_debug_browser",
            return_value=BrowserLaunch(BrowserLaunchStatus.NO_BROWSER_FOUND),
        ) as launch:
            for _ in range(5):
                service.refresh_now(block=True)
            self.assertEqual(launch.call_count, 1, "the cooldown did not hold")

            clock.advance(601.0)
            service.refresh_now(block=True)
            self.assertEqual(launch.call_count, 2, "the cooldown never expired")

    def test_a_failing_launch_does_not_break_the_service(self) -> None:
        """Recovery is best-effort; the state must stay honest."""
        clock = _Clock()
        service = self._failing(clock, auto_recover_browser=True)

        with mock.patch(
            "opencsi.auth.browser_launch.launch_debug_browser",
            side_effect=OSError("boom"),
        ):
            snap = service.refresh_now(block=True)

        self.assertIs(snap.state, MonitorState.BROWSER_UNAVAILABLE)

    def test_a_successful_recovery_retries_in_the_same_cycle(self) -> None:
        """Otherwise the run that fixed the problem still reports failure.

        The browser needs a moment to answer on its port, so the fetch that
        triggered the recovery cannot be the one that benefits from it. Without
        the retry, `opencsi tray --once` would exit 10 on the very run that
        restored the session, and a scheduled task would look like it failed.
        """
        from opencsi.auth.browser_launch import BrowserLaunch, BrowserLaunchStatus

        clock = _Clock()
        client = _Client()
        service = _service(client, clock=clock, auto_recover_browser=True)
        # Fail once (no browser), then succeed -- as a real recovery does.
        client._error = CdpUnavailableError("no endpoint")

        calls = {"n": 0}

        def _fail_then_succeed(refresh=False):
            calls["n"] += 1
            if calls["n"] == 1:
                raise CdpUnavailableError("no endpoint")
            return _snapshot()

        client.get_my_tools = _fail_then_succeed

        with mock.patch(
            "opencsi.auth.browser_launch.launch_debug_browser",
            return_value=BrowserLaunch(BrowserLaunchStatus.LAUNCHED, browser="chrome"),
        ) as launch:
            snap = service.refresh_now(block=True)

        launch.assert_called_once()
        self.assertIs(snap.state, MonitorState.OK, "the retry did not happen")

    def test_the_retry_happens_at_most_once(self) -> None:
        """A browser that starts but still yields no cookie must not loop."""
        from opencsi.auth.browser_launch import BrowserLaunch, BrowserLaunchStatus

        clock = _Clock()
        client = _Client(error=CdpUnavailableError("still nothing"))
        service = _service(client, clock=clock, auto_recover_browser=True)

        with mock.patch(
            "opencsi.auth.browser_launch.launch_debug_browser",
            return_value=BrowserLaunch(BrowserLaunchStatus.LAUNCHED, browser="chrome"),
        ) as launch:
            snap = service.refresh_now(block=True)

        self.assertEqual(launch.call_count, 1, "the recovery looped")
        self.assertIs(snap.state, MonitorState.BROWSER_UNAVAILABLE)

    def test_a_failed_launch_is_not_retried_in_the_same_cycle(self) -> None:
        """Retrying a failed launch would repeat the failure and double the work."""
        from opencsi.auth.browser_launch import BrowserLaunch, BrowserLaunchStatus

        clock = _Clock()
        client = _Client(error=CdpUnavailableError("no endpoint"))
        service = _service(client, clock=clock, auto_recover_browser=True)

        with mock.patch(
            "opencsi.auth.browser_launch.launch_debug_browser",
            return_value=BrowserLaunch(BrowserLaunchStatus.NO_BROWSER_FOUND),
        ) as launch:
            service.refresh_now(block=True)

        self.assertEqual(launch.call_count, 1)

    def test_recovery_is_only_for_the_browser_state(self) -> None:
        """A network outage must not make the tool start a browser."""
        clock = _Clock()
        client = _Client(error=NetworkError("down"))
        service = _service(client, clock=clock, auto_recover_browser=True)

        with mock.patch(
            "opencsi.auth.browser_launch.launch_debug_browser"
        ) as launch:
            snap = service.refresh_now(block=True)

        launch.assert_not_called()
        self.assertIs(snap.state, MonitorState.OFFLINE)

    def test_an_unexpected_exception_does_not_escape_the_worker(self) -> None:
        """A tray must never die because a library raised something odd."""
        service = _service(_Client(error=RuntimeError("surprise!")))
        snap = service.refresh_now(block=True)
        self.assertIsNot(snap.state, MonitorState.OK)
        self.assertIsNotNone(snap.last_error)

    def test_the_scheduled_fetch_honours_the_interval(self) -> None:
        clock = _Clock()
        client = _Client()
        service = _service(client, clock=clock, refresh_interval=300.0)
        service.refresh_now(block=True)
        self.assertEqual(client.calls, 1)

        # A tick before the interval elapses must not fetch again.
        clock.advance(10.0)
        service._tick_once()
        self.assertEqual(client.calls, 1)

        clock.advance(400.0)
        service._tick_once()
        self.assertEqual(client.calls, 2)

    def test_an_unknown_state_is_never_published_as_ok(self) -> None:
        service = _service(_Client(error=_code_error("TOTALLY_NEW_CODE")))
        snap = service.refresh_now(block=True)
        self.assertIsNot(snap.state, MonitorState.OK)


class RenewalPolicyTest(unittest.TestCase):
    def test_a_healthy_credential_is_not_renewed(self) -> None:
        session = _Session(expires_in=3600.0)
        clock = _Clock()
        service = _service(_Client(), session=session, clock=clock)
        service._tick_once()
        self.assertEqual(session.renew_calls, 0)

    def test_a_nearly_expired_credential_is_renewed_proactively(self) -> None:
        session = _Session(expires_in=60.0)
        clock = _Clock()
        service = _service(_Client(), session=session, clock=clock)
        service._maybe_renew()
        self.assertEqual(session.renew_calls, 1)

    def test_renewal_is_not_attempted_on_every_tick(self) -> None:
        """The credential check is throttled; renewal must not be a hot loop."""
        session = _Session(expires_in=60.0)
        clock = _Clock()
        service = _service(_Client(), session=session, clock=clock)
        service._tick_once()
        self.assertEqual(session.renew_calls, 1)
        # A second tick immediately after must not renew again.
        service._tick_once()
        self.assertEqual(session.renew_calls, 1)

    def test_a_failed_renewal_becomes_login_required(self) -> None:
        session = _Session(
            expires_in=10.0,
            renew_result=RenewalResult(
                RenewalStatus.LOGIN_REQUIRED, requires_interaction=True
            ),
        )
        service = _service(_Client(), session=session)
        service._maybe_renew()
        self.assertIs(service.snapshot.state, MonitorState.LOGIN_REQUIRED)

    def test_an_unanswered_consent_page_becomes_consent_required(self) -> None:
        """Not LOGIN_REQUIRED: the user is still signed in.

        The remedy is one approval click, so the tray must offer that rather than
        sending an authenticated user to sign in again.
        """
        session = _Session(
            expires_in=10.0,
            renew_result=RenewalResult(
                RenewalStatus.CONSENT_REQUIRED, requires_interaction=True
            ),
        )
        service = _service(_Client(), session=session)
        service._maybe_renew()
        self.assertIs(service.snapshot.state, MonitorState.CONSENT_REQUIRED)

    def test_the_consent_state_raises_an_attention_notification(self) -> None:
        """The fastest-to-fix state must not be the one the tray stays silent about.

        It will never resolve on its own, so a user who is not told will simply
        see a tray that has quietly stopped collecting.
        """
        session = _Session(
            expires_in=10.0,
            renew_result=RenewalResult(
                RenewalStatus.CONSENT_REQUIRED, requires_interaction=True
            ),
        )
        service = _service(_Client(), session=session)
        seen: list = []
        service.subscribe_attention(seen.append)
        service._maybe_renew()

        self.assertIs(service.snapshot.state, MonitorState.CONSENT_REQUIRED)
        self.assertEqual(
            len(seen), 1, "the consent state did not raise an attention callback"
        )
        self.assertIs(seen[0].state, MonitorState.CONSENT_REQUIRED)

    def test_a_manual_renew_reports_consent_distinctly(self) -> None:
        session = _Session(
            expires_in=10.0,
            renew_result=RenewalResult(
                RenewalStatus.CONSENT_REQUIRED, requires_interaction=True
            ),
        )
        service = _service(_Client(), session=session)
        service.renew_now(block=True)
        self.assertIs(service.snapshot.state, MonitorState.CONSENT_REQUIRED)


class ReactiveFailureClassificationTest(unittest.TestCase):
    """A 401 that a renewal attempt explains must not report a generic state.

    The scheduled path is not the only one that can meet GitCode's approval page.
    A 401 makes the client reload the credential and then re-run OAuth, and that
    round-trip lands on the same form. The error the client finally raises is a
    plain ``SESSION_EXPIRED``, which maps to ``AUTH_ERROR`` -- "session expired,
    renew it" -- and the offered remedy cannot work, because the renewal parks on
    the same form every time.
    """

    def _expired(self):
        from opencsi.errors import SessionExpiredError

        return SessionExpiredError("openCsiTool rejected the session cookie (HTTP 401)")

    def test_a_401_whose_renewal_hit_consent_reports_consent(self) -> None:
        client = _Client(
            error=self._expired(),
            last_renewal=RenewalResult(
                RenewalStatus.CONSENT_REQUIRED, requires_interaction=True
            ),
        )
        service = _service(client)
        service._refresh_once(force=True)
        self.assertIs(service.snapshot.state, MonitorState.CONSENT_REQUIRED)

    def test_a_401_whose_renewal_needed_a_login_reports_login(self) -> None:
        client = _Client(
            error=self._expired(),
            last_renewal=RenewalResult(
                RenewalStatus.LOGIN_REQUIRED, requires_interaction=True
            ),
        )
        service = _service(client)
        service._refresh_once(force=True)
        self.assertIs(service.snapshot.state, MonitorState.LOGIN_REQUIRED)

    def test_a_401_with_no_renewal_outcome_stays_auth_error(self) -> None:
        """The upgrade must be evidence-based, not a blanket relabel."""
        service = _service(_Client(error=self._expired()))
        service._refresh_once(force=True)
        self.assertIs(service.snapshot.state, MonitorState.AUTH_ERROR)

    def test_a_network_failure_is_not_upgraded_by_a_stale_renewal(self) -> None:
        """A renewal result must not recolour an unrelated failure.

        Otherwise a network blip arriving after a consent attempt would be
        reported as "approval required", sending the user to click a button that
        has nothing to do with the problem.
        """
        from opencsi.errors import NetworkError

        client = _Client(
            error=NetworkError("connection reset by peer"),
            last_renewal=RenewalResult(
                RenewalStatus.CONSENT_REQUIRED, requires_interaction=True
            ),
        )
        service = _service(client)
        state = service._classify_failure(NetworkError("connection reset by peer"))
        self.assertIsNot(state, MonitorState.CONSENT_REQUIRED)

    def test_cdp_unavailable_becomes_an_auth_error_not_a_login_prompt(self) -> None:
        """No browser is a different problem from 'your SSO session is gone'."""
        session = _Session(
            expires_in=10.0,
            renew_result=RenewalResult(RenewalStatus.CDP_UNAVAILABLE),
        )
        service = _service(_Client(), session=session)
        service._maybe_renew()
        self.assertIs(service.snapshot.state, MonitorState.AUTH_ERROR)

    def test_an_unknown_expiry_does_not_trigger_renewal(self) -> None:
        session = _Session(expires_in=None)
        service = _service(_Client(), session=session)
        service._maybe_renew()
        self.assertEqual(session.renew_calls, 0)

    def test_manual_renew_refreshes_the_data_afterwards(self) -> None:
        """Renewing from the menu should show fresh numbers, not stale ones."""
        session = _Session(expires_in=3600.0)
        client = _Client()
        service = _service(client, session=session)
        service.renew_now(block=True)
        self.assertEqual(session.renew_calls, 1)
        self.assertEqual(client.calls, 1)
        self.assertIs(service.snapshot.state, MonitorState.OK)

    def test_an_autonomous_renewal_does_not_leave_the_icon_stuck(self) -> None:
        """The bug a live probe found: the tray sat on "Renewing session".

        ``_tick_once`` returns early after a renewal, so publishing RENEWING and
        then returning on success stranded the display there until the next
        scheduled fetch -- up to five minutes of showing work that finished in
        two seconds. The autonomous path is the one users actually live with, so
        this is the state they would have seen most often.
        """
        session = _Session(expires_in=60.0)
        client = _Client()
        service = _service(client, session=session)

        service.tick()

        self.assertIsNot(
            service.snapshot.state,
            MonitorState.RENEWING,
            "the tray was left displaying a renewal that had already finished",
        )
        self.assertIs(service.snapshot.state, MonitorState.OK)
        self.assertEqual(client.calls, 1, "the data was not refreshed after renewal")

    def test_an_autonomous_renewal_that_did_nothing_does_not_strand_the_state(self) -> None:
        """ALREADY_VALID/TIMEOUT leave the credential unchanged, so restore OK."""
        for status in (RenewalStatus.ALREADY_VALID, RenewalStatus.TIMEOUT):
            with self.subTest(status=status.value):
                session = _Session(
                    expires_in=60.0,
                    renew_result=RenewalResult(status),
                )
                service = _service(_Client(), session=session)
                service._maybe_renew()
                self.assertIsNot(
                    service.snapshot.state,
                    MonitorState.RENEWING,
                    f"a {status.value} renewal stranded the display on RENEWING",
                )

    def test_the_public_tick_runs_the_scheduled_path(self) -> None:
        """``tick()`` is the documented way to drive the schedule from outside.

        The class docstring has always claimed the service "can be driven
        entirely synchronously in tests via refresh_now and tick", but ``tick``
        did not exist -- only the private ``_tick_once``. That mattered beyond
        tidiness: it meant the autonomous renewal path could not be exercised
        except by waiting for a real timer.
        """
        session = _Session(expires_in=60.0)
        service = _service(_Client(), session=session)
        snapshot = service.tick()
        self.assertIsInstance(snapshot, MonitorSnapshot)
        self.assertEqual(session.renew_calls, 1, "tick did not run the scheduled path")


class SubscriptionTest(unittest.TestCase):
    def test_subscribers_are_notified_with_each_snapshot(self) -> None:
        service = _service(_Client())
        seen: list[MonitorSnapshot] = []
        service.subscribe(seen.append)
        service.refresh_now(block=True)
        self.assertTrue(seen)
        self.assertIs(seen[-1].state, MonitorState.OK)

    def test_unsubscribe_stops_notifications(self) -> None:
        service = _service(_Client())
        seen: list[MonitorSnapshot] = []
        off = service.subscribe(seen.append)
        off()
        service.refresh_now(block=True)
        self.assertEqual(seen, [])

    def test_a_raising_subscriber_does_not_break_the_service(self) -> None:
        """One broken UI callback must not stop the monitor."""
        service = _service(_Client())

        def explode(_snapshot):
            raise RuntimeError("bad UI")

        service.subscribe(explode)
        snap = service.refresh_now(block=True)
        self.assertIs(snap.state, MonitorState.OK)

    def test_unsubscribing_twice_is_harmless(self) -> None:
        service = _service(_Client())
        off = service.subscribe(lambda _s: None)
        off()
        off()


class AttentionNotificationTest(unittest.TestCase):
    """The tray must be told *once* when the session needs the user.

    This is the §55 requirement, and the edge-triggering is the whole point: the
    monitor polls every five minutes, so a level-triggered notification would
    pop a balloon twelve times an hour saying the same thing. Users mute apps
    that do that, and then miss the one notification that mattered.
    """

    def _login_required_service(self):
        from opencsi.errors import OpenCsiError

        class _NotLoggedIn(OpenCsiError):
            code = "OPENCSITOOL_NOT_LOGGED_IN"

        return _service(_Client(error=_NotLoggedIn("no session")))

    def test_entering_login_required_fires_once(self) -> None:
        service = self._login_required_service()
        seen: list[MonitorSnapshot] = []
        service.subscribe_attention(seen.append)

        service.refresh_now(block=True)

        self.assertEqual(len(seen), 1, "the attention callback did not fire exactly once")
        self.assertIs(seen[0].state, MonitorState.LOGIN_REQUIRED)

    def test_repeated_polls_do_not_re_notify(self) -> None:
        """The regression this exists to prevent: nagging every five minutes."""
        service = self._login_required_service()
        seen: list[MonitorSnapshot] = []
        service.subscribe_attention(seen.append)

        for _ in range(5):
            service.refresh_now(block=True)

        self.assertEqual(
            len(seen),
            1,
            f"the user was notified {len(seen)} times about one problem",
        )

    def test_recovery_then_relapse_notifies_again(self) -> None:
        """A new lapse is new information, so the user is told again."""
        client = _Client()
        service = _service(client)
        seen: list[MonitorSnapshot] = []
        service.subscribe_attention(seen.append)

        from opencsi.errors import OpenCsiError

        class _NotLoggedIn(OpenCsiError):
            code = "OPENCSITOOL_NOT_LOGGED_IN"

        service.refresh_now(block=True)  # OK
        self.assertEqual(seen, [])

        client._error = _NotLoggedIn("gone")
        service.refresh_now(block=True)  # lapses
        self.assertEqual(len(seen), 1)

        client._error = None
        service.refresh_now(block=True)  # recovers
        self.assertEqual(len(seen), 1)

        client._error = _NotLoggedIn("gone again")
        service.refresh_now(block=True)  # lapses again
        self.assertEqual(len(seen), 2, "the second lapse was not reported")

    def test_a_network_failure_does_not_notify(self) -> None:
        """A flaky network is not worth interrupting someone over."""
        from opencsi.errors import NetworkError

        service = _service(_Client(error=NetworkError("down")))
        seen: list[MonitorSnapshot] = []
        service.subscribe_attention(seen.append)

        service.refresh_now(block=True)

        self.assertEqual(seen, [], "an offline blip interrupted the user")

    def test_a_server_error_does_not_notify(self) -> None:
        from opencsi.errors import ServerError

        service = _service(_Client(error=ServerError("500")))
        seen: list[MonitorSnapshot] = []
        service.subscribe_attention(seen.append)

        service.refresh_now(block=True)

        self.assertEqual(seen, [])

    def test_unsubscribing_stops_attention_notifications(self) -> None:
        service = self._login_required_service()
        seen: list[MonitorSnapshot] = []
        off = service.subscribe_attention(seen.append)
        off()
        service.refresh_now(block=True)
        self.assertEqual(seen, [])

    def test_a_raising_attention_callback_does_not_break_the_service(self) -> None:
        """A missing balloon must not take down the monitor."""
        service = self._login_required_service()

        def explode(_snapshot):
            raise RuntimeError("no notifications on this backend")

        service.subscribe_attention(explode)
        snap = service.refresh_now(block=True)
        self.assertIs(snap.state, MonitorState.LOGIN_REQUIRED)

    def test_attention_and_plain_subscribers_are_independent(self) -> None:
        """Both kinds fire; neither replaces the other."""
        service = self._login_required_service()
        plain: list[MonitorSnapshot] = []
        attention: list[MonitorSnapshot] = []
        service.subscribe(plain.append)
        service.subscribe_attention(attention.append)

        service.refresh_now(block=True)

        self.assertTrue(plain, "the plain subscriber stopped being called")
        self.assertEqual(len(attention), 1)


class WorkerThreadTest(unittest.TestCase):
    """The blocking work happens off the caller's thread."""

    def test_the_worker_publishes_a_first_snapshot_without_being_asked(self) -> None:
        service = _service(_Client())
        arrived = threading.Event()
        service.subscribe(lambda snap: arrived.set() if snap.has_data else None)
        service.start()
        try:
            self.assertTrue(arrived.wait(5.0), "the worker never fetched")
        finally:
            service.stop()

    def test_refresh_now_does_not_block_the_caller(self) -> None:
        """A menu click must never freeze the tray."""
        service = _service(_Client())
        service.start()
        try:
            started = time.monotonic()
            service.refresh_now()  # enqueues, returns immediately
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 1.0)
        finally:
            service.stop()

    def test_stop_is_idempotent_and_joins_the_thread(self) -> None:
        service = _service(_Client())
        service.start()
        service.stop()
        service.stop()
        self.assertFalse(service._thread.is_alive())

    def test_start_is_idempotent(self) -> None:
        service = _service(_Client())
        service.start()
        first = service._thread
        service.start()
        try:
            self.assertIs(service._thread, first)
        finally:
            service.stop()

    def test_a_stopped_service_can_be_restarted(self) -> None:
        service = _service(_Client())
        service.start()
        service.stop()
        service.start()
        try:
            self.assertTrue(service._thread.is_alive())
        finally:
            service.stop()


class ReprTest(unittest.TestCase):
    def test_repr_is_useful_and_secret_free(self) -> None:
        service = _service(_Client())
        service.refresh_now(block=True)
        text = repr(service)
        self.assertIn("MonitorService", text)
        self.assertIn("OK", text)
        self.assertNotIn("token=", text)


if __name__ == "__main__":
    unittest.main()
