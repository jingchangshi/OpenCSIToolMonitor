"""Prove the *autonomous* renewal path works, not just the manual one.

Why this exists
---------------
`opencsi login --renew` calls `session.renew(force=True)`. That proves the
mechanism works, but it does **not** prove the thing users actually depend on:
that the monitor renews *by itself* when the credential approaches expiry,
without anyone asking.

Those are different code paths. The forced one is a straight line from a CLI
flag. The autonomous one goes through `MonitorService._maybe_renew`, which is
gated by `session.needs_renewal(margin=...)` and reached from the scheduled tick.
A bug in the gate -- a margin compared the wrong way, a policy that never fires
because the clock is never checked -- would leave `--renew` passing while the
tray quietly let the session die every hour.

What this does
--------------
Builds the real object graph (`make_context` -> real `OpenCsiToolClient` ->
real `CdpCookieProvider` + `BrowserOAuthRenewer`), wraps it in a real
`MonitorService`, and drives the *scheduled* path with the renewal margin set
wide enough that the gate must fire. Nothing is forced: if the policy does not
decide to renew, this reports a failure.

It then confirms the result against the server, because a cookie that changed is
not the same as a session the server accepts.

Read-only in the business sense: the only write is the OAuth round trip, which is
authentication, not a business mutation. No business endpoint is touched with
anything but GET.

Safety posture: **LIVE / NETWORK / AUTH_SIDE_EFFECT**. It performs a real
renewal, so it mints a real openCsiTool session and changes the credential the
browser holds. Not read-only, and must not be wired into CI (§44).

Run: python tools/probe_autonomous_renewal.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

from opencsi.auth.session import RenewalStatus  # noqa: E402
from opencsi.cli.context import make_context  # noqa: E402
from opencsi.monitor import MonitorService  # noqa: E402
from opencsi.monitor.service import MonitorConfig, MonitorState  # noqa: E402


def main() -> int:
    argv = ["--no-proxy"]
    ctx, _args = make_context(argv)

    provider = ctx.make_provider()
    client = ctx.make_client(provider=provider)
    session = client.session

    if session.renewer is None:
        print("FAIL: no renewer was attached, so the autonomous path cannot run")
        return 1

    before = session.status()
    print(f"credential source     : {before.source}")
    print(f"lifetime before       : {before.expires_in:.0f}s")
    print(f"renewer               : {session.renewer.describe()}")
    print()

    # The margin is set wider than the remaining lifetime, so `needs_renewal`
    # must be true. This does not force the renewal -- it only makes the gate
    # reachable. The decision to renew is still the service's.
    remaining = before.expires_in or 0.0
    margin = remaining + 60.0
    config = MonitorConfig(
        refresh_interval=300.0,
        credential_check_interval=0.0,  # let every tick check the credential
        renew_margin=margin,
        backoff_base=1.0,
    )
    service = MonitorService(client, session=session, config=config)

    print(f"renew margin set to   : {margin:.0f}s (lifetime is {remaining:.0f}s)")
    print(f"needs_renewal()       : {session.needs_renewal(margin=margin)}")
    print()

    if not session.needs_renewal(margin=margin):
        print("FAIL: the gate did not open, so this probe would prove nothing")
        return 1

    # Drive the *scheduled* path, exactly as the worker's timer would.
    print("driving the scheduled tick (this is the autonomous path)...")
    service.tick()

    after = session.status()
    result = session.last_renewal

    print()
    print("== outcome ==")
    print(f"state                 : {service.snapshot.state.value}")
    if result is not None:
        print(f"renewal status        : {result.status.value}")
        print(f"token changed         : {result.token_changed}")
        print(f"renewed               : {result.renewed}")
    else:
        print("renewal status        : <no renewal was attempted>")
    print(f"lifetime after        : {after.expires_in:.0f}s" if after.expires_in else "lifetime after        : unknown")

    failures: list[str] = []

    if result is None:
        failures.append(
            "the scheduled tick did not attempt a renewal even though the gate "
            "was open -- the autonomous path is broken"
        )
    elif not result.renewed:
        failures.append(
            f"the autonomous renewal did not succeed ({result.status.value})"
        )
    else:
        if not result.token_changed:
            failures.append("the token did not change, so nothing was renewed")
        if before.expires_in and after.expires_in:
            if after.expires_in <= before.expires_in:
                failures.append(
                    f"the lifetime did not extend ({before.expires_in:.0f}s -> "
                    f"{after.expires_in:.0f}s)"
                )
        else:
            failures.append("could not compare lifetimes")

    # A renewed cookie is only meaningful if the server accepts it. This is the
    # check that turns "the jar changed" into "the session works".
    if not failures:
        try:
            identity = client.login_or_restore_session(refresh=True)
            print(f"server accepted it    : yes ({identity.display_name})")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"the server rejected the renewed session: {type(exc).__name__}")
            print(f"server accepted it    : NO ({type(exc).__name__})")

    # And the business data must still flow, which is the actual user-visible
    # outcome of a successful renewal.
    if not failures:
        try:
            snapshot = client.get_my_tools(refresh=True)
            print(f"business data         : {snapshot.total_tokens:,} tokens")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"business data failed after renewal: {type(exc).__name__}")

    print()
    if failures:
        print("AUTONOMOUS RENEWAL FAILED:")
        for item in failures:
            print(f"  - {item}")
        return 1

    print("VERIFIED: the monitor renewed the session by itself, on its own")
    print("schedule, without being forced -- and the server accepted the result.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
