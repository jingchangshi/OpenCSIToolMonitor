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

import helpers

from opencsi.auth.session import (
    CredentialStatus,
    RenewalResult,
    RenewalStatus,
    SessionManager,
)
from opencsi.errors import NetworkError, OpenCsiError, SessionExpiredError
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

    def __init__(self, *, snapshot=None, error: BaseException | None = None) -> None:
        self._snapshot = snapshot if snapshot is not None else _snapshot()
        self._error = error
        self.calls = 0

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
