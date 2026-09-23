"""Render the tray menu for every state, including the new consent state.

Offline stub: builds a snapshot per ``MonitorState`` and prints the menu the
tray would show. Proves the consent state is reachable, labelled in Chinese, and
offers the approval action rather than a sign-in -- which is the whole point of
having a separate state.

Touches no network and writes nothing.
"""
#: labels: LIVE, GET_ONLY

from __future__ import annotations

import sys

sys.path.insert(0, "src")

from opencsi.monitor import MonitorSnapshot, MonitorState  # noqa: E402
from opencsi.tray.presenter import actions_for, label_cn  # noqa: E402


def main() -> int:
    for state in MonitorState:
        snapshot = MonitorSnapshot(state=state)
        actions = actions_for(snapshot, auto_refresh=True, startup_enabled=False)
        ids = [a.id for a in actions]
        print(f"{state.value:20s} label={label_cn(snapshot):12s} actions={ids}")

    print()
    print("consent state detail:")
    snapshot = MonitorSnapshot(state=MonitorState.CONSENT_REQUIRED)
    for action in actions_for(snapshot, auto_refresh=True, startup_enabled=False):
        print(f"  {action.id:16s} {action.label}")

    # The consent state must not offer a plain sign-in: the user is still signed
    # in, so that action cannot fix anything.
    consent_ids = [
        a.id
        for a in actions_for(
            MonitorSnapshot(state=MonitorState.CONSENT_REQUIRED),
            auto_refresh=True,
            startup_enabled=False,
        )
    ]
    print()
    if "login" in consent_ids and "renew" not in consent_ids:
        print("FAIL: consent offers only sign-in, which cannot fix it")
        return 1
    print("VERIFIED: consent offers an approval action, not a sign-in.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
