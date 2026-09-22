"""Background monitoring service: the domain layer behind the tray UI.

Why this is not in the tray
---------------------------
The tray is a *view*. Everything it needs -- polling cadence, session renewal,
error classification, the last good snapshot -- lives here, in a cross-platform
module that a test can drive with a fake clock and no GUI. That split is what
makes the tray testable at all, and it is why ``opencsi tray`` never shells out
to ``opencsi usage --json``: the tray imports this, and this imports the client.

.. code-block::

    Tray UI  ──reads──>  MonitorSnapshot
       │                      ▲
       │ enqueue(Refresh)     │ publish
       ▼                      │
    MonitorService ──> SessionManager ──> OpenCsiToolClient ──> HTTP API
       (worker thread)

Threading model (objective §58)
-------------------------------
One background worker thread does every blocking thing -- the API fetch and any
OAuth renewal. The UI thread only ever reads an immutable snapshot, so a slow
network call can never freeze the tray. Snapshots are swapped atomically under a
lock, and ``subscribe`` lets a UI redraw when one lands.

Safety properties
-----------------
* :class:`MonitorSnapshot` is **structurally** secret-free. It has no field that
  could hold a token, a cookie, a virtual key or an internal identifier, so a
  tooltip cannot leak one by accident.
* A failure never clears the last good data. The tray keeps showing the last
  known numbers with an honest "Offline / last update" label, rather than
  blanking out.
* Errors are classified, not collapsed into "Error" (objective §31).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from queue import Empty, Queue
from typing import Callable

from ..auth.session import RenewalStatus, SessionManager
from ..errors import OpenCsiError
from ..models import MyToolsSnapshot

log = logging.getLogger("opencsi.monitor")

#: The page a browser is pointed at when the service has to start one. Kept
#: here rather than imported from the CLI so the monitor does not depend on the
#: command layer -- the dependency runs the other way.
_LOGIN_URL = "https://opencsitool.com/myTools"


class MonitorState(str, Enum):
    """What the tray should show. One value per *kind* of problem.

    The objective is explicit that every exception must not become a generic
    "Error": a user needs to tell "sign in again" from "the network is down"
    from "the server is unhappy", because the fix differs.
    """

    STARTING = "STARTING"
    OK = "OK"
    REFRESHING = "REFRESHING"
    RENEWING = "RENEWING"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    #: The credential *source* is unreachable: no browser is running with a
    #: DevTools endpoint. Distinct from ``LOGIN_REQUIRED`` because the remedy is
    #: different -- and because "sign in" is not something the user can
    #: successfully do while the browser is down. Conflating the two produced a
    #: closed loop: the tray said "login required", the login action opened a
    #: browser with no debugging port, and the next poll said "login required"
    #: again. Nothing the user could click would ever break the cycle.
    BROWSER_UNAVAILABLE = "BROWSER_UNAVAILABLE"
    #: GitCode is showing an OAuth *approval* page and waiting for it.
    #:
    #: Distinct from ``LOGIN_REQUIRED`` because the remedy is much smaller: the
    #: SSO session is alive, so nothing needs signing in -- one click on the
    #: consent page completes it. Telling an already-authenticated user to "sign
    #: in again" sends them to do work that does not fix anything, which is the
    #: same class of mistake as the ``BROWSER_UNAVAILABLE`` conflation above.
    CONSENT_REQUIRED = "CONSENT_REQUIRED"
    OFFLINE = "OFFLINE"
    SERVER_ERROR = "SERVER_ERROR"
    AUTH_ERROR = "AUTH_ERROR"


#: Provider error codes mapped to a monitor state. Kept as data so the mapping
#: is auditable and testable rather than buried in an if-chain.
#:
#: Every code in :mod:`opencsi.errors` appears here, and a test asserts that:
#: a code with no entry would fall through to the generic branch, and the whole
#: point of this table is that a user can tell "sign in again" from "the
#: network is down". Misconfigurations map to ``SERVER_ERROR`` rather than
#: ``OK`` because the safe default is never "fine".
_CODE_STATE: dict[str, MonitorState] = {
    # Authentication: the user must act.
    "OPENCSITOOL_NOT_LOGGED_IN": MonitorState.LOGIN_REQUIRED,
    # The credential *source* is down. The user must still act, but not by
    # signing in: `opencsi login` cannot succeed with no browser to read from.
    # Mapping these to LOGIN_REQUIRED is what produced the closed loop described
    # on ``MonitorState.BROWSER_UNAVAILABLE``.
    "CDP_UNAVAILABLE": MonitorState.BROWSER_UNAVAILABLE,
    "NO_BROWSER_TARGET": MonitorState.BROWSER_UNAVAILABLE,
    # The QR endpoint answered in an unrecognised shape: the user still needs to
    # sign in, just by another route, so this is not a server-health problem.
    "QR_PROTOCOL_ERROR": MonitorState.LOGIN_REQUIRED,
    # Authentication: the session was rejected mid-flight.
    "SESSION_EXPIRED": MonitorState.AUTH_ERROR,
    "BAD_AUTH_HEADER": MonitorState.AUTH_ERROR,
    "PERMISSION_DENIED": MonitorState.AUTH_ERROR,
    # The browserless renewal flow failed at a step it named. That is an
    # authentication problem -- the session could not be re-established -- not a
    # server-health one, and the remedy is the same as for any other failure to
    # renew: check the GitCode session.
    "BROWSERLESS_OAUTH_ERROR": MonitorState.AUTH_ERROR,
    # Transport.
    "NETWORK_ERROR": MonitorState.OFFLINE,
    "WEBSOCKET_ERROR": MonitorState.OFFLINE,
    # A quiet CDP socket is the *normal* state of a healthy session, so this
    # only reaches the classifier when a caller treated a poll deadline as a
    # failure. It is a transport condition either way.
    "WEBSOCKET_TIMEOUT": MonitorState.OFFLINE,
    # The server is reachable but unhappy, or its contract moved.
    "SERVER_ERROR": MonitorState.SERVER_ERROR,
    "BUSINESS_API_ERROR": MonitorState.SERVER_ERROR,
    "CONTRACT_DRIFT": MonitorState.SERVER_ERROR,
    # Misconfiguration. Not "OK" -- the monitor cannot do its job.
    "INVALID_ARGUMENTS": MonitorState.SERVER_ERROR,
    "INVALID_CONFIGURATION": MonitorState.SERVER_ERROR,
    "MISSING_PARAMETER": MonitorState.SERVER_ERROR,
    # The tray extra is not installed. A local setup problem, not a server one,
    # but the monitor genuinely cannot display anything either way.
    "TRAY_UNAVAILABLE": MonitorState.SERVER_ERROR,
}


def state_for_error(exc: BaseException | None) -> MonitorState:
    """Classify a failure into a monitor state."""
    if exc is None:
        return MonitorState.OK
    code = getattr(exc, "code", "")
    return _CODE_STATE.get(str(code), MonitorState.SERVER_ERROR)


#: Human-facing labels. The tray is Chinese-facing (the site is), but the enum
#: stays English so it is stable in JSON and in logs.
STATE_LABELS: dict[MonitorState, str] = {
    MonitorState.STARTING: "Starting",
    MonitorState.OK: "OK",
    MonitorState.REFRESHING: "Refreshing",
    MonitorState.RENEWING: "Renewing session",
    MonitorState.LOGIN_REQUIRED: "Login required",
    MonitorState.CONSENT_REQUIRED: "Approval required",
    MonitorState.BROWSER_UNAVAILABLE: "Browser not running",
    MonitorState.OFFLINE: "Offline",
    MonitorState.SERVER_ERROR: "Server error",
    MonitorState.AUTH_ERROR: "Session expired",
}

#: States the user must do something about. Deliberately excludes ``OFFLINE``
#: and ``SERVER_ERROR``: a flaky network is not something to interrupt someone
#: over, and it usually fixes itself. A session that needs a sign-in does not.
#:
#: ``BROWSER_UNAVAILABLE`` is included for the same reason as the other two: the
#: tool is not collecting anything, and no amount of waiting will change that.
#: It is the state a user lands in right after a reboot, which is exactly when
#: they have not yet noticed the tray is doing nothing.
#:
#: ``CONSENT_REQUIRED`` is included because it is the *most* actionable state of
#: the set: it resolves with a single click and will never resolve on its own.
#: Leaving it out would mean the one problem the user can fix fastest is the one
#: the tray stays silent about.
_ATTENTION_STATES = frozenset(
    {
        MonitorState.LOGIN_REQUIRED,
        MonitorState.CONSENT_REQUIRED,
        MonitorState.AUTH_ERROR,
        MonitorState.BROWSER_UNAVAILABLE,
    }
)

#: States that describe work in progress rather than an outcome. These must not
#: reset the attention latch: every refresh publishes ``REFRESHING`` first, so
#: treating it as "recovered" would re-arm the notification on every poll and
#: nag the user exactly as badly as no edge detection at all. (That is not
#: hypothetical -- the first version of this did exactly that, and
#: ``test_repeated_polls_do_not_re_notify`` caught it.)
_TRANSIENT_STATES = frozenset(
    {MonitorState.STARTING, MonitorState.REFRESHING, MonitorState.RENEWING}
)


@dataclass(frozen=True)
class MonitorSnapshot:
    """An immutable, secret-free view of the account for a UI to render.

    There is deliberately **no** token, cookie, virtual key, ``userId``,
    ``accountId`` or ``employeeId`` field. The tooltip and menu are built from
    this object, so their secret-freedom is a property of the type rather than
    something a reviewer has to check at each call site.
    """

    state: MonitorState = MonitorState.STARTING
    #: Headline numbers, from the account-wide aggregate.
    total_tokens: int = 0
    requests: int = 0
    prs: int = 0
    added_lines: int = 0
    generated_lines: int = 0
    adopted_lines: int = 0
    adoption_rate: float = 0.0
    #: When the *server* last refreshed its own data.
    data_fresh_time: str | None = None
    #: When *we* last fetched successfully.
    fetched_at: datetime | None = None
    #: Redacted, human-readable description of the last failure.
    last_error: str | None = None
    #: Seconds of credential life left, when known. Advisory only.
    credential_expires_in: float | None = None
    #: How many consecutive failures have occurred (drives backoff).
    consecutive_failures: int = 0

    @property
    def has_data(self) -> bool:
        return self.fetched_at is not None

    @property
    def is_healthy(self) -> bool:
        return self.state in (MonitorState.OK, MonitorState.REFRESHING, MonitorState.RENEWING)

    @property
    def label(self) -> str:
        return STATE_LABELS.get(self.state, self.state.value)

    def age_seconds(self, *, now: float | None = None) -> float | None:
        """How stale the displayed data is, in seconds."""
        if self.fetched_at is None:
            return None
        reference = time.time() if now is None else now
        return reference - self.fetched_at.timestamp()

    def as_dict(self) -> dict[str, object]:
        """Serialisable form. Secret-free by construction."""
        out: dict[str, object] = {
            "state": self.state.value,
            "label": self.label,
            "has_data": self.has_data,
            "total_tokens": self.total_tokens,
            "requests": self.requests,
            "prs": self.prs,
            "added_lines": self.added_lines,
            "generated_lines": self.generated_lines,
            "adopted_lines": self.adopted_lines,
            "adoption_rate": round(self.adoption_rate, 4),
        }
        if self.data_fresh_time:
            out["data_fresh_time"] = self.data_fresh_time
        if self.fetched_at is not None:
            out["fetched_at"] = self.fetched_at.isoformat()
        if self.last_error:
            out["last_error"] = self.last_error
        if self.credential_expires_in is not None:
            out["credential_expires_in_seconds"] = round(self.credential_expires_in, 1)
        if self.consecutive_failures:
            out["consecutive_failures"] = self.consecutive_failures
        return out

    @classmethod
    def from_snapshot(
        cls,
        snapshot: MyToolsSnapshot,
        *,
        state: MonitorState = MonitorState.OK,
        credential_expires_in: float | None = None,
    ) -> "MonitorSnapshot":
        """Build a monitor snapshot from an API response."""
        fresh = snapshot.sync_status.data_fresh_time if snapshot.sync_status else None
        return cls(
            state=state,
            total_tokens=snapshot.total_tokens,
            requests=snapshot.total_request_count,
            prs=snapshot.pr_count,
            added_lines=snapshot.added_lines_count,
            generated_lines=snapshot.generated_code_lines,
            adopted_lines=snapshot.adopted_code_lines,
            adoption_rate=snapshot.adoption_rate,
            data_fresh_time=fresh,
            fetched_at=snapshot.fetched_at or datetime.now(timezone.utc),
            credential_expires_in=credential_expires_in,
        )

    def with_state(self, state: MonitorState, *, error: str | None = None) -> "MonitorSnapshot":
        """A copy in a new state, keeping the last good numbers.

        This is what stops a transient failure from blanking the display: the
        state changes, the data does not.
        """
        return replace(self, state=state, last_error=error)


@dataclass
class MonitorConfig:
    """Polling policy. Defaults follow the objective's §30 cadence."""

    #: How often to fetch business data. Not every minute: this is a usage
    #: dashboard, and hammering the API would be rude and pointless.
    refresh_interval: float = 300.0
    #: How often to check the local credential expiry. Cheap (no network), so
    #: it can run far more often than the fetch.
    credential_check_interval: float = 60.0
    #: Renew when the credential has less than this left.
    renew_margin: float = 300.0
    #: Backoff after consecutive failures: multiplied each time, capped.
    backoff_base: float = 30.0
    backoff_max: float = 900.0
    #: How long a single fetch may take before it is treated as failed.
    fetch_timeout: float = 30.0
    #: Whether the service may start a browser by itself when the credential
    #: source has gone away (objective §68: "user notices nothing").
    #:
    #: Off by default, and deliberately so. Starting a browser puts a window on
    #: someone's desktop, and a monitoring tool that opens windows unasked is
    #: the kind of behaviour users are right to resent. Opting in is a choice
    #: about unattended operation; it is not something to assume on a user's
    #: behalf because it happens to make the happy path smoother.
    auto_recover_browser: bool = False
    #: Whether the service may start the *hidden* authentication host when the
    #: credential source has gone away.
    #:
    #: On by default, unlike :attr:`auto_recover_browser`, and the difference is
    #: the whole point of objective §30: the objection to starting a browser
    #: unasked is that it puts a window on someone's desktop, and a headless host
    #: does not. Without this, a machine where Chrome simply was not running yet
    #: reports ``BROWSER_UNAVAILABLE`` -- which reads as "this tool is broken"
    #: when the truth is "nothing is signed in yet" and the remedy is a QR scan.
    #:
    #: It is still not unconditional. Some Chromium builds reject
    #: ``--headless=new`` (Chrome 153 on the development machine does), and this
    #: host then falls back to a *visible* window. A visible fallback is exactly
    #: the behaviour :attr:`auto_recover_browser` exists to gate, so it is refused
    #: unless the user opted in -- see :meth:`_maybe_recover_browser`.
    auto_recover_auth_host: bool = True
    #: Minimum gap between automatic browser launches. Without this, a machine
    #: where the launch keeps failing would retry on every backoff tick and
    #: spawn a browser window each time.
    browser_recover_cooldown: float = 600.0


class MonitorService:
    """Owns the polling loop, the last good snapshot and the error state.

    The service is deliberately passive: it does nothing until :meth:`start`,
    and it can be driven entirely synchronously in tests via
    :meth:`refresh_now` and :meth:`tick`. That is what keeps the tray's logic
    testable without a GUI event loop or a real clock.
    """

    def __init__(
        self,
        client,
        *,
        session: SessionManager | None = None,
        config: MonitorConfig | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._session = session if session is not None else client.session
        self.config = config or MonitorConfig()
        self._clock = clock
        self._now = now or (lambda: datetime.now(timezone.utc))

        self._lock = threading.RLock()
        self._snapshot = MonitorSnapshot(state=MonitorState.STARTING)
        self._subscribers: list[Callable[[MonitorSnapshot], None]] = []
        self._attention: list[Callable[[MonitorSnapshot], None]] = []

        self._commands: "Queue[str]" = Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_refresh_at = 0.0
        self._last_credential_check = 0.0
        #: Set once the user has been told the session needs them; cleared by a
        #: settled healthy state. See :meth:`_publish`.
        self._attention_latched = False
        #: When an automatic browser recovery was last attempted, for the
        #: cooldown. ``None`` means never.
        self._last_browser_recover: float | None = None

    # ── observation ───────────────────────────────────────────────────────
    @property
    def snapshot(self) -> MonitorSnapshot:
        """The current snapshot. Safe to read from any thread."""
        with self._lock:
            return self._snapshot

    def subscribe(self, callback: Callable[[MonitorSnapshot], None]) -> Callable[[], None]:
        """Register a UI callback; returns an unsubscribe function."""
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._subscribers.remove(callback)
                except ValueError:
                    pass

        return unsubscribe

    def subscribe_attention(
        self, callback: Callable[[MonitorSnapshot], None]
    ) -> Callable[[], None]:
        """Register a callback for states that need the user to *act*.

        Fired on the **transition into** such a state, not on every poll. The
        difference is the whole point: the tray polls every five minutes, so a
        level-triggered notification would pop a balloon twelve times an hour
        telling the user the same thing they already know. Windows notifications
        that repeat are how a helpful app becomes one people mute.

        The transition is re-armed by recovery: if the session comes back and
        then lapses again, that is genuinely new information and the user is told
        again.
        """
        with self._lock:
            self._attention.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._attention.remove(callback)
                except ValueError:
                    pass

        return unsubscribe

    def _publish(self, snapshot: MonitorSnapshot) -> None:
        """Swap in a new snapshot and notify subscribers.

        Subscribers are called *outside* the lock: a UI callback that
        re-entered the service would otherwise deadlock, and a slow one would
        block the worker.
        """
        with self._lock:
            self._snapshot = snapshot
            callbacks = list(self._subscribers)

            # A *latch*, not a comparison with the previous snapshot. Every
            # refresh publishes a transient REFRESHING first, so "the previous
            # state was not an attention state" is true on every single poll --
            # comparing neighbours re-notifies forever. The latch is cleared only
            # by a settled, healthy state.
            if snapshot.state in _ATTENTION_STATES:
                notify = not self._attention_latched
                self._attention_latched = True
            elif snapshot.state in _TRANSIENT_STATES:
                notify = False  # work in progress says nothing about recovery
            else:
                self._attention_latched = False
                notify = False

            attention = list(self._attention) if notify else []

        for callback in callbacks:
            try:
                callback(snapshot)
            except Exception as exc:  # noqa: BLE001 - a bad UI must not kill us
                log.debug("snapshot subscriber raised %s", type(exc).__name__)

        if notify:
            log.info("the session needs attention: %s", snapshot.state.value)
        for callback in attention:
            try:
                callback(snapshot)
            except Exception as exc:  # noqa: BLE001
                log.debug("attention subscriber raised %s", type(exc).__name__)

    # ── commands ──────────────────────────────────────────────────────────
    def refresh_now(self, *, block: bool = False) -> MonitorSnapshot:
        """Fetch immediately.

        With ``block=False`` (the UI path) the work is handed to the worker, so
        a menu click never freezes the tray. ``block=True`` is for tests and for
        the one-shot ``opencsi tray --once`` path.
        """
        if block:
            return self._refresh_once(force=True)
        self._commands.put("refresh")
        return self.snapshot

    def renew_now(self, *, block: bool = False) -> MonitorSnapshot:
        """Attempt a silent renewal immediately."""
        if block:
            return self._renew_once()
        self._commands.put("renew")
        return self.snapshot

    def tick(self) -> MonitorSnapshot:
        """Run one scheduled cycle synchronously.

        This is the same work the worker performs when its timer expires, made
        callable from outside. It exists because the *autonomous* renewal path --
        the one users actually depend on -- was previously reachable only by
        waiting for a real timer, so the only thing anyone could verify was the
        CLI's forced renewal. Those are different code paths, and a bug in the
        policy gate would have left the forced one passing while the tray let the
        session die hourly.

        Exposed publicly (rather than only as ``_tick_once``) so a probe can
        drive the real schedule, and so the documented API matches what the class
        docstring has always claimed.
        """
        self._tick_once()
        return self.snapshot

    def stop(self) -> None:
        """Ask the worker to finish. Idempotent."""
        self._stop.set()
        self._commands.put("stop")
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5.0)

    # ── lifecycle ─────────────────────────────────────────────────────────
    def start(self) -> None:
        """Start the background worker. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="opencsi-monitor", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        """The worker loop.

        A blocking fetch or renewal happens here and nowhere else, so the UI
        thread never waits on the network.
        """
        # First fetch immediately, so a freshly started tray is not blank.
        self._refresh_once(force=True)

        while not self._stop.is_set():
            try:
                command = self._commands.get(timeout=self._wait_seconds())
            except Empty:
                command = "tick"

            if command == "stop":
                break
            if command == "refresh":
                self._refresh_once(force=True)
            elif command == "renew":
                self._renew_once()
            else:
                self._tick_once()

    def _wait_seconds(self) -> float:
        """How long to sleep before the next scheduled action."""
        now = self._clock()
        candidates = [max(0.0, self._next_refresh_at - now)]
        credential_due = (
            self._last_credential_check + self.config.credential_check_interval - now
        )
        candidates.append(max(0.0, credential_due))
        # Never sleep longer than a second in tests' interest, but in
        # production this is bounded by the shortest real interval.
        return max(0.05, min(min(candidates), self.config.credential_check_interval))

    def _tick_once(self) -> None:
        """The scheduled path: check the credential, then maybe fetch."""
        now = self._clock()
        if now - self._last_credential_check >= self.config.credential_check_interval:
            self._last_credential_check = now
            if self._maybe_renew():
                return
        if now >= self._next_refresh_at:
            self._refresh_once()

    # ── work ──────────────────────────────────────────────────────────────
    def _maybe_renew(self) -> bool:
        """Renew proactively if the credential is inside the margin.

        Returns whether a renewal was attempted. Never raises: a renewal that
        fails becomes a state, and the next fetch reports the real outcome.
        """
        if not self._session.needs_renewal(margin=self.config.renew_margin):
            return False
        self._publish(self.snapshot.with_state(MonitorState.RENEWING))
        result = self._session.renew()
        if result.renewed:
            log.info("session renewed silently")
            # Publish the new data rather than returning in the RENEWING state.
            # The tick returns early after a renewal, so without this the icon
            # would read "Renewing session" until the next scheduled fetch --
            # up to five minutes of showing work that finished in two seconds.
            # `force=True` because the refresh clock was not the reason we are
            # here; the credential changed, which is a better reason.
            self._refresh_once(force=True)
            return True
        if result.status is RenewalStatus.LOGIN_REQUIRED:
            self._publish(
                self.snapshot.with_state(
                    MonitorState.LOGIN_REQUIRED,
                    error=result.detail or "GitCode sign-in is required",
                )
            )
        elif result.status is RenewalStatus.CONSENT_REQUIRED:
            self._publish(
                self.snapshot.with_state(
                    MonitorState.CONSENT_REQUIRED,
                    error=result.detail or "GitCode is waiting for approval",
                )
            )
        elif result.status in (RenewalStatus.CDP_UNAVAILABLE, RenewalStatus.OAUTH_FAILED):
            self._publish(
                self.snapshot.with_state(
                    MonitorState.AUTH_ERROR,
                    error=result.detail or "silent renewal failed",
                )
            )
        else:
            # ALREADY_VALID, TIMEOUT, UNSUPPORTED: the credential did not change,
            # so the previous state is still the truthful one. Restoring it stops
            # the display being stranded on RENEWING for a renewal that did
            # nothing.
            self._publish(self.snapshot.with_state(MonitorState.OK))
        return True

    def _renew_once(self) -> MonitorSnapshot:
        """Force one renewal (the tray's 'Renew session' action)."""
        self._publish(self.snapshot.with_state(MonitorState.RENEWING))
        result = self._session.renew(force=True)
        if result.renewed:
            self._publish(self.snapshot.with_state(MonitorState.OK))
            return self._refresh_once(force=True)
        state = (
            MonitorState.LOGIN_REQUIRED
            if result.status is RenewalStatus.LOGIN_REQUIRED
            else MonitorState.CONSENT_REQUIRED
            if result.status is RenewalStatus.CONSENT_REQUIRED
            else MonitorState.AUTH_ERROR
        )
        self._publish(
            self.snapshot.with_state(state, error=result.detail or "renewal failed")
        )
        return self.snapshot

    def _refresh_once(self, *, force: bool = False, _recovered: bool = False) -> MonitorSnapshot:
        """Fetch the business data once and publish the outcome.

        Never raises. A failure updates the *state* and keeps the last good
        numbers, because a tray that vanishes or blanks on a network blip is
        worse than one that says "Offline, last update 23:18".

        ``_recovered`` is internal and guards against a second recovery attempt
        in the same cycle: the retry below must be tried exactly once, or a
        browser that starts but still yields no cookie would loop.
        """
        if not force and self._clock() < self._next_refresh_at:
            return self.snapshot

        self._publish(self.snapshot.with_state(MonitorState.REFRESHING))

        try:
            snapshot = self._client.get_my_tools(refresh=force)
        except OpenCsiError as exc:
            return self._handle_failure(exc, recovered=_recovered)
        except Exception as exc:  # noqa: BLE001 - the tray must not die
            return self._handle_failure(exc, recovered=_recovered)

        credential = self._credential_status()
        published = MonitorSnapshot.from_snapshot(
            snapshot,
            state=MonitorState.OK,
            credential_expires_in=credential,
        )
        self._next_refresh_at = self._clock() + self.config.refresh_interval
        self._publish(published)
        return published

    def _classify_failure(self, exc: BaseException) -> MonitorState:
        """The state for a failure, preferring what renewal actually said.

        The reactive 401 path is the reason this exists. A 401 makes the client
        reload the credential and then re-run OAuth, and *that* round-trip can
        land on GitCode's approval page -- the same one the scheduled path
        detects. The error the client finally raises is a plain
        ``SESSION_EXPIRED``, which :func:`state_for_error` maps to ``AUTH_ERROR``:
        "session expired, renew it".

        That is wrong in the same way defect 10 was wrong. The user is still
        authenticated and the credential is still being refused for a reason
        renewal can name, so reporting the generic state both loses the remedy and
        offers one ("renew") that cannot work -- the renewal will park on the same
        form every time.

        The client records the renewal outcome, so ask it. This is deliberately
        narrow: only the two states whose remedy differs from "retry" are
        upgraded, and only when the failure really was an auth failure. A network
        blip during a renewal must still be reported as a network problem.
        """
        state = state_for_error(exc)
        if state is not MonitorState.AUTH_ERROR:
            return state

        renewal = getattr(self._client, "last_renewal", None)
        status = getattr(renewal, "status", None)
        if status is RenewalStatus.CONSENT_REQUIRED:
            return MonitorState.CONSENT_REQUIRED
        if status is RenewalStatus.LOGIN_REQUIRED:
            return MonitorState.LOGIN_REQUIRED
        return state

    def _handle_failure(
        self, exc: BaseException, *, recovered: bool = False
    ) -> MonitorSnapshot:
        """Classify a failure, apply backoff, and keep the last good data."""
        state = self._classify_failure(exc)
        failures = self.snapshot.consecutive_failures + 1

        # Exponential backoff, capped. A tray must not amplify a network
        # problem by retrying every five minutes forever at full rate.
        delay = min(
            self.config.backoff_base * (2 ** (failures - 1)), self.config.backoff_max
        )
        self._next_refresh_at = self._clock() + delay

        detail = str(exc)
        published = replace(
            self.snapshot,
            state=state,
            last_error=detail or type(exc).__name__,
            consecutive_failures=failures,
            credential_expires_in=self._credential_status(),
        )
        log.info("refresh failed (%s): %s", state.value, type(exc).__name__)

        # A successful launch does not by itself mean the data is reachable: the
        # browser needs a moment to answer on its port, and the profile may hold
        # no cookie. Retrying once, in this same cycle, is what makes an
        # unattended recovery actually unattended -- without it the one-shot
        # path still reports BROWSER_UNAVAILABLE on the run that fixed it, and a
        # scheduled task would look like it had failed.
        if state is MonitorState.BROWSER_UNAVAILABLE and not recovered:
            if self._maybe_recover_browser():
                return self._refresh_once(force=True, _recovered=True)

        self._publish(published)
        return published

    def _maybe_recover_browser(self) -> bool:
        """Try to restore a credential source, hidden first.

        This exists for the unattended case the objective describes: Windows
        starts the tray at sign-in, but Chrome is not running yet, so the tray
        would otherwise sit at "browser not running" until someone clicked.

        Objective §30 puts the hidden host first, and §61 spells out why: the user
        must not see ``BROWSER_UNAVAILABLE`` merely because Chrome was not already
        open. The order is therefore:

        1. ``AuthBrowserHost.ensure_running(headless=True)`` -- the engine needed
           for the OAuth leg, with no window. Costs the user nothing visible.
        2. ``launch_debug_browser`` -- a *visible* window. Only when the user
           opted in with ``auto_recover_browser``, because that is the behaviour
           they would object to, and only when the hidden attempt did not work.

        The visible fallback is gated rather than automatic because
        ``--headless=new`` is not universally supported: on a build that rejects
        it, the auth host falls back to a visible window itself, and honouring
        that silently would open a window for a user who never asked for one.
        ``ensure_running`` reports which mode it got, so that case is detectable
        instead of guessed.

        Returns whether a **usable** credential source was started -- not merely
        whether a launch was attempted. The caller retries the fetch on ``True``,
        and retrying after a failed launch would only repeat the same failure
        while doubling the work. Never raises: a recovery that fails must leave
        the state honest, not take down the worker.
        """
        now = self._clock()
        if (
            self._last_browser_recover is not None
            and now - self._last_browser_recover < self.config.browser_recover_cooldown
        ):
            return False

        # Stamped *before* either attempt, not after a success. A host that fails
        # to start -- no browser installed, or a build that rejects
        # ``--headless=new`` and is refused below -- would otherwise be retried on
        # every backoff tick, spawning a doomed process each time. The visible
        # path always did this; the hidden path has the same failure mode and the
        # same cost, which is why the stamp is shared rather than per-branch.
        self._last_browser_recover = now

        if self.config.auto_recover_auth_host and self._try_auth_host():
            return True

        if not self.config.auto_recover_browser:
            return False

        try:
            from ..auth.browser_launch import launch_debug_browser
        except Exception as exc:  # noqa: BLE001 - an import problem is not fatal
            log.debug("browser recovery unavailable: %s", type(exc).__name__)
            return False

        try:
            result = launch_debug_browser(_LOGIN_URL)
        except Exception as exc:  # noqa: BLE001 - launching must never raise here
            log.info("automatic browser recovery failed: %s", type(exc).__name__)
            return False

        if result.ok:
            log.info("started %s to restore the credential source", result.browser)
            return True

        log.info("automatic browser recovery: %s", result.status.value)
        return False

    def _try_auth_host(self) -> bool:
        """Bring up the hidden authentication host. Returns whether it is usable.

        Kept separate from :meth:`_maybe_recover_browser` because it answers a
        different question: not "did a browser start?" but "is there now an engine
        answering that a renewal could use?".

        A **visible** fallback is ruled out before the launch, not rejected after
        it. Chrome 153 on the development machine rejects ``--headless=new``, and
        the host would otherwise open a window as part of starting and only then
        report it -- so a caller that inspected the result and declined would have
        already put the window on screen. ``visible_fallback=False`` keeps the
        promise instead of merely describing it, and the visible path is left to
        the opt-in flag.

        Reporting success for a visible engine would also make the tray claim a
        hidden runtime while a window sat on the desktop, which is the same class
        of false report as the browser-bound conclusion this project already had
        to correct.
        """
        try:
            from ..auth.auth_host import AuthBrowserHost
        except Exception as exc:  # noqa: BLE001 - an import problem is not fatal
            log.debug("auth host unavailable: %s", type(exc).__name__)
            return False

        try:
            result = AuthBrowserHost(visible_fallback=False).ensure_running()
        except Exception as exc:  # noqa: BLE001 - recovery must never raise
            log.info("auth host recovery failed: %s", type(exc).__name__)
            return False

        if not result.ok:
            log.info("auth host recovery: %s", result.status.value)
            return False

        if result.visible:
            # Should be unreachable now that the fallback is refused up front, so
            # it is kept as a guard rather than a branch: if a future launch path
            # reintroduces a window, this refuses to call it a hidden recovery.
            log.info("auth host reported a visible window; not counting it")
            return False

        log.info("started the hidden auth host on port %s", result.port)
        return True

    def _credential_status(self) -> float | None:
        """Seconds of credential life left, or ``None``. Never raises."""
        try:
            return self._session.status().expires_in
        except Exception:  # noqa: BLE001 - status is advisory
            return None

    def __repr__(self) -> str:
        return (
            f"MonitorService(state={self.snapshot.state.value}, "
            f"has_data={self.snapshot.has_data})"
        )
