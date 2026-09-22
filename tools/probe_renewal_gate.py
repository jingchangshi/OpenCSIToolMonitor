"""Prove the renewal gate stays SHUT when there is plenty of credential left.

Why this exists
---------------
`probe_autonomous_renewal.py` proves renewal happens. This proves it *does not*
happen when it should not -- and that is the half that protects the site.

`SessionManager.needs_renewal(margin=...)` is a single comparison, and both
directions of it matter:

* too eager (fires early, or fires always) -> an OAuth round-trip against
  GitCode on every single tick, for every user, forever. The user sees nothing
  wrong, which is exactly why a "renewal works!" demo cannot catch it.
* too lazy (never fires) -> the session dies hourly, which
  `probe_autonomous_renewal.py` catches.

A test that only drives the open case passes just as happily if the gate is
stuck open, so this probe exists to drive the closed case against the live
service and a real credential.

How it decides
--------------
A renewal *replaces* the cookie, resetting its lifetime to the full TTL. So with
production's 300-second margin and ~3500 seconds remaining, several scheduled
ticks must leave the lifetime strictly decaying. Growth would mean an unasked-for
OAuth round trip.

Read-only in the business sense: this probe performs no OAuth at all when it
passes, and touches no business endpoint with anything but GET.

Run: python tools/probe_renewal_gate.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

from opencsi.cli.context import make_context  # noqa: E402
from opencsi.monitor import MonitorService  # noqa: E402
from opencsi.monitor.service import MonitorConfig  # noqa: E402

#: Production's margin, taken from `MonitorConfig`'s default rather than
#: retyped, so this probe cannot drift from the policy it is checking.
PRODUCTION_MARGIN = MonitorConfig().renew_margin

#: A renewal resets the lifetime to the full TTL, so any growth beyond this
#: much is a renewal rather than clock noise. The value is well above the few
#: seconds a handful of ticks can consume.
GROWTH_TOLERANCE = 60.0

TICKS = 4


def main() -> int:
    ctx, _args = make_context(["--no-proxy"])

    provider = ctx.make_provider()
    client = ctx.make_client(provider=provider)
    session = client.session

    before = session.status()
    remaining = before.expires_in

    print(f"credential source     : {before.source}")
    print(f"renewer               : {session.renewer.describe() if session.renewer else '<none>'}")
    print(f"lifetime              : {remaining:.0f}s")
    print(f"production margin     : {PRODUCTION_MARGIN:.0f}s")
    print()

    if remaining is None:
        print("SKIP: no lifetime is known, so the comparison would be meaningless")
        return 2
    if remaining <= PRODUCTION_MARGIN:
        print(
            "SKIP: only "
            f"{remaining:.0f}s remains, which is inside the {PRODUCTION_MARGIN:.0f}s "
            "margin -- the gate *should* be open, so this probe cannot prove it "
            "stays shut. Re-run with a fresh credential."
        )
        return 2

    # A zero refresh interval means every tick reaches the credential check
    # instead of short-circuiting on "not due yet" -- otherwise a tick could
    # pass without the gate ever being consulted, and the probe would prove
    # nothing.
    config = MonitorConfig(
        refresh_interval=0.0,
        credential_check_interval=0.0,
        renew_margin=PRODUCTION_MARGIN,
    )
    service = MonitorService(client, session=session, config=config)

    print(f"gate open right now?  : {session.needs_renewal(margin=PRODUCTION_MARGIN)}")
    print(f"driving {TICKS} scheduled ticks (the autonomous path)...")
    print()

    for index in range(TICKS):
        snapshot = service.tick()
        current = session.status().expires_in
        shown = f"{current:.0f}s" if current is not None else "unknown"
        print(f"  tick {index + 1}: state={snapshot.state.value} lifetime={shown}")

    after = session.status().expires_in
    result = session.last_renewal
    service.stop()

    print()
    print("== outcome ==")
    if result is not None:
        print(f"last renewal          : {result.status.value}")
    else:
        print("last renewal          : <none attempted>")
    print(f"lifetime              : {remaining:.0f}s -> {after:.0f}s")

    failures: list[str] = []

    if result is not None and result.renewed:
        failures.append(
            "a renewal ran while "
            f"{remaining:.0f}s of lifetime remained, well outside the "
            f"{PRODUCTION_MARGIN:.0f}s margin -- the gate is stuck open, and every "
            "tick would cost an OAuth round trip"
        )
    if after is None:
        failures.append("the lifetime became unknown, so nothing could be compared")
    else:
        drift = after - remaining
        print(f"drift                 : {drift:+.0f}s")
        if drift > GROWTH_TOLERANCE:
            failures.append(
                f"the lifetime grew by {drift:.0f}s, which means the cookie was "
                "replaced -- a renewal the policy did not ask for"
            )

    print()
    if failures:
        print("RENEWAL GATE FAILED:")
        for item in failures:
            print(f"  - {item}")
        return 1

    print("VERIFIED: with time to spare the gate stayed shut, and the lifetime")
    print("only decayed -- no unasked-for OAuth round trip.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
