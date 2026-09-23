"""Live probe: the tray's real menu actions against the real service.

`probe_tray_live.py` uses an offline stub to prove the icon reaches the
notification area. This does the opposite: it keeps the GUI out of the way and
drives the *actions* behind the menu against a live `MonitorService` with a real
client, so "Refresh now" and "Renew session" are proven to work end to end
rather than merely to exist.

Read-only in the business sense: the only write is the OAuth round trip, which is
authentication. No business endpoint is touched with anything but GET. Nothing
sensitive is printed -- only state, counts and lifetimes.

Run: python tools/probe_tray_actions.py
"""
#: labels: LIVE, AUTH_SIDE_EFFECT

from __future__ import annotations

import sys

sys.path.insert(0, "src")

from opencsi.cli.context import make_context  # noqa: E402
from opencsi.monitor import MonitorService  # noqa: E402
from opencsi.monitor.service import MonitorConfig, MonitorState  # noqa: E402
from opencsi.tray.presenter import actions_for, label_cn, tooltip_for  # noqa: E402


def main() -> int:
    ctx, _args = make_context(["--no-proxy"])

    provider = ctx.make_provider()
    client = ctx.make_client(provider=provider)
    session = client.session

    config = MonitorConfig(refresh_interval=300.0, credential_check_interval=0.0)
    service = MonitorService(client, session=session, config=config)

    failures: list[str] = []

    # ── the first fetch, as the worker would do it ────────────────────────
    print("== initial refresh ==")
    snapshot = service.refresh_now(block=True)
    print(f"state        : {snapshot.state.value}")
    print(f"has_data     : {snapshot.has_data}")
    if snapshot.has_data:
        print(f"tokens       : {snapshot.total_tokens:,}")
        print(f"requests     : {snapshot.requests:,}")
        print(f"prs          : {snapshot.prs:,}")
        print(f"adoption     : {snapshot.adoption_rate:.1%}")
        print(f"server data  : {snapshot.data_fresh_time}")

    if snapshot.state is not MonitorState.OK:
        failures.append(f"the initial refresh did not reach OK ({snapshot.state.value})")
    if not snapshot.has_data:
        failures.append("no business data was returned")

    # ── what the user would actually see ──────────────────────────────────
    print("\n== rendered UI text ==")
    print(f"label (cn)   : {label_cn(snapshot)}")
    tooltip = tooltip_for(snapshot)
    for line in tooltip.splitlines():
        print(f"tooltip      : {line}")
    print(f"tooltip len  : {len(tooltip)} (Windows limit is 127)")

    if len(tooltip) > 127:
        failures.append(f"the tooltip is {len(tooltip)} chars, over the 127 limit")

    # §56: no identity fields may reach the UI.
    for banned in ("employeeId", "accountId", "userId", "virtualKey"):
        if banned.lower() in tooltip.lower():
            failures.append(f"{banned} leaked into the tooltip")

    # ── the menu, and the actions behind it ───────────────────────────────
    print("\n== menu ==")
    actions = actions_for(snapshot)
    for action in actions:
        marker = " (default)" if action.default else ""
        print(f"  {action.id:14s} {action.label}{marker}")

    ids = [action.id for action in actions]
    for required in ("refresh", "renew", "open", "quit"):
        if required not in ids:
            failures.append(f"the menu is missing '{required}'")

    # ── "Refresh now" ─────────────────────────────────────────────────────
    print("\n== Refresh now ==")
    refreshed = service.refresh_now(block=True)
    print(f"state        : {refreshed.state.value}")
    if refreshed.state is not MonitorState.OK:
        failures.append(f"Refresh now did not reach OK ({refreshed.state.value})")

    # ── "Renew session", exactly as the menu calls it ─────────────────────
    print("\n== Renew session (forced, as the menu does) ==")
    before = session.status().expires_in
    renewed = service.renew_now(block=True)
    after = session.status().expires_in
    result = session.last_renewal

    print(f"state        : {renewed.state.value}")
    print(f"lifetime     : {before:.0f}s -> {after:.0f}s")
    if result is not None:
        print(f"renewal      : {result.status.value}")
        print(f"token changed: {result.token_changed}")

    if result is None or not result.renewed:
        failures.append("Renew session did not renew")
    elif after is not None and before is not None and after <= before:
        failures.append(f"the lifetime did not extend ({before:.0f}s -> {after:.0f}s)")

    # A renewal is only meaningful if the session still works afterwards.
    print("\n== after renewal ==")
    final = service.refresh_now(block=True)
    print(f"state        : {final.state.value}")
    if final.has_data:
        print(f"tokens       : {final.total_tokens:,}")
    if final.state is not MonitorState.OK:
        failures.append("the session did not work after renewal")

    service.stop()

    print()
    if failures:
        print("TRAY ACTIONS FAILED:")
        for item in failures:
            print(f"  - {item}")
        return 1

    print("VERIFIED: refresh, menu and renewal all work against the live service.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
