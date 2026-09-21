"""Watch the tray's own renewal policy across a real cookie expiry.

`probe_autonomous_renewal.py` proves the policy fires and works. This proves
something stronger and slower: that leaving the monitor running across an actual
expiry keeps the session alive **without anyone touching it**.

The distinction matters because the two failure modes look different. A policy
that never fires fails immediately and loudly. A policy that fires but is not
*fast enough* -- or that stops firing after the first success, or that renews and
then does not notice it succeeded -- fails quietly, an hour later, in a way only a
long observation catches.

What it does
------------
Builds the real monitor with production timings and calls ``tick()`` on a
schedule, sampling the state. It runs until either the deadline passes or the
session has been renewed ``--renewals`` times.

It deliberately uses the *production* margin (300 s) rather than a widened one, so
the thing being observed is the shipped configuration, not a test-only shortcut.

Run: python tools/probe_renewal_soak.py --minutes 70
Writes nothing except a line per sample. Never prints a credential.
"""

from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, "src")

from opencsi.cli.context import make_context  # noqa: E402
from opencsi.monitor import MonitorService  # noqa: E402
from opencsi.monitor.service import MonitorConfig, MonitorState  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Soak-test the renewal policy.")
    parser.add_argument(
        "--minutes", type=float, default=70.0, help="how long to observe (default: 70)"
    )
    parser.add_argument(
        "--renewals",
        type=int,
        default=2,
        help="stop early after this many renewals (default: 2)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=15.0,
        help="seconds between observations (default: 15)",
    )
    args = parser.parse_args()

    ctx, _ = make_context(["--no-proxy"])
    provider = ctx.make_provider()
    client = ctx.make_client(provider=provider)
    session = client.session

    if session.renewer is None:
        print("FAIL: no renewer attached; the soak would prove nothing")
        return 1

    start = session.status()
    print(f"start lifetime : {start.expires_in:.0f}s" if start.expires_in else "start: unknown")
    print(f"renew margin   : {MonitorConfig().renew_margin:.0f}s (production value)")
    print(f"observing for  : {args.minutes:.0f} min, every {args.interval:.0f}s")
    print(f"stop after     : {args.renewals} renewal(s)")
    print()

    # Production timings, except the credential check: the real 60 s cadence is
    # fine, and using it means the observed behaviour is the shipped behaviour.
    config = MonitorConfig()
    service = MonitorService(client, session=session, config=config)

    deadline = time.monotonic() + args.minutes * 60.0

    # Count renewals by wrapping the renewer, not by inspecting the result
    # object. `id()` on a fresh dataclass can be reused after collection, so
    # identity-based counting can silently miss or double-count a renewal --
    # exactly the kind of flakiness that makes a soak test untrustworthy.
    renewals = 0
    original_renew = session.renew

    def counting_renew(*call_args, **call_kwargs):
        nonlocal renewals
        result = original_renew(*call_args, **call_kwargs)
        if getattr(result, "renewed", False):
            renewals += 1
            print(
                f"[{time.strftime('%H:%M:%S')}] RENEWED  "
                f"token_changed={result.token_changed}"
            )
        return result

    session.renew = counting_renew  # type: ignore[method-assign]

    problems: list[str] = []
    samples = 0

    while time.monotonic() < deadline:
        service.tick()
        snapshot = service.snapshot
        status = session.status()
        samples += 1

        if renewals >= args.renewals:
            break

        lifetime = f"{status.expires_in:6.0f}s" if status.expires_in else " unknown"
        if samples % 4 == 1:  # keep the log readable over a long run
            print(f"[{time.strftime('%H:%M:%S')}] {snapshot.state.value:<16} lifetime {lifetime}")

        # A session that needs the user is the failure this soak exists to catch.
        if snapshot.state is MonitorState.LOGIN_REQUIRED:
            problems.append(
                "the session required interactive login, so silent renewal did not "
                "survive an expiry"
            )
            break

        time.sleep(args.interval)

    print()
    print(f"renewals observed: {renewals}")

    if problems:
        print("SOAK FAILED:")
        for item in problems:
            print(f"  - {item}")
        return 1

    if renewals < args.renewals:
        print(
            f"INCONCLUSIVE: only {renewals} of {args.renewals} renewals observed "
            f"within {args.minutes:.0f} minutes. The session is still healthy, so "
            "this is not a failure -- run it longer to see more."
        )
        return 2

    # The final state must be a working session, not merely a renewed cookie.
    try:
        identity = client.login_or_restore_session(refresh=True)
        print(f"final check    : server accepted the session ({identity.display_name})")
    except Exception as exc:  # noqa: BLE001
        print(f"final check    : FAILED ({type(exc).__name__})")
        return 1

    print()
    print(f"VERIFIED: {renewals} silent renewals across real expiries, with no")
    print("user interaction and no interactive login required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
