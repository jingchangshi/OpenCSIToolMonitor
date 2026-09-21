"""Turn a :class:`MonitorSnapshot` into tooltip text and menu items.

This module is the tray's *entire* presentation logic, and it deliberately
imports nothing from pystray. A tooltip string and a list of labelled actions
are plain data, so they can be asserted in a test on any platform -- including
Linux CI, where a real tray cannot run at all.

The split matters for correctness, not just for testability. The rules that
matter here are the ones that are easy to get subtly wrong and impossible to
eyeball in a screenshot:

* a tooltip is capped at 127 characters (a Windows ``NOTIFYICONDATA`` limit),
  so it must be *designed* to fit rather than truncated by the OS;
* a stale number must be labelled as stale, or a tray showing "1.2M tokens"
  from three hours ago is a lie by omission;
* no string built here may contain a secret, which is guaranteed by the fact
  that :class:`MonitorSnapshot` has nowhere to put one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..monitor import MonitorSnapshot, MonitorState

#: Windows truncates the tooltip at 127 characters (128 including the NUL).
#: Staying under it means we choose what the user reads, rather than the OS.
MAX_TOOLTIP = 127


def format_count(value: int) -> str:
    """Compact, human-readable count: ``1234`` -> ``1.2K``.

    A tooltip has ~127 characters for everything, and a raw ``1234567`` wastes
    seven of them on digits nobody reads at a glance.
    """
    if value < 0:
        return "-" + format_count(-value)
    if value < 1000:
        return str(value)
    if value < 1_000_000:
        return f"{value / 1000:.1f}K"
    if value < 1_000_000_000:
        return f"{value / 1_000_000:.1f}M"
    return f"{value / 1_000_000_000:.1f}B"


def format_age(seconds: float | None) -> str:
    """``90`` -> ``1m``; ``None`` -> ``never``."""
    if seconds is None:
        return "never"
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"{hours}h{minutes:02d}m" if minutes else f"{hours}h"
    return f"{int(seconds // 86400)}d"


def format_duration(seconds: float | None) -> str:
    """A credential lifetime, worded so it cannot be mistaken for data age."""
    if seconds is None:
        return "unknown"
    seconds = max(0.0, seconds)
    if seconds < 60:
        return "<1m"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    return f"{seconds / 3600:.1f}h"


def tooltip_for(snapshot: MonitorSnapshot, *, now: float | None = None) -> str:
    """The hover text.

    Shape: ``OpenCSI | <state>`` then the headline numbers, then freshness.
    When there is no data yet, or the last fetch failed, the freshness line says
    so explicitly -- a tray that shows stale numbers without saying they are
    stale is worse than one that shows nothing.
    """
    head = f"OpenCSI | {snapshot.label}"

    if not snapshot.has_data:
        line = snapshot.last_error or "waiting for the first update"
        text = f"{head}\n{line}"
        return text[:MAX_TOOLTIP]

    parts = [
        head,
        f"{format_count(snapshot.total_tokens)} tokens  "
        f"{format_count(snapshot.requests)} req  {snapshot.prs} PR",
    ]

    age = snapshot.age_seconds(now=now)
    if snapshot.is_healthy:
        freshness = f"updated {format_age(age)} ago"
    else:
        freshness = f"OFFLINE - last update {format_age(age)} ago"
    if snapshot.credential_expires_in is not None:
        freshness += f" | session {format_duration(snapshot.credential_expires_in)}"
    parts.append(freshness)

    if not snapshot.is_healthy and snapshot.last_error:
        parts.append(snapshot.last_error)

    return "\n".join(parts)[:MAX_TOOLTIP]


def headline_for(snapshot: MonitorSnapshot) -> str:
    """The default (bold, first) menu item: the number people open the menu for."""
    if not snapshot.has_data:
        return snapshot.last_error or "No data yet"
    return f"{snapshot.total_tokens:,} tokens / {snapshot.requests:,} requests"


@dataclass(frozen=True)
class Action:
    """One menu item: a label, an id and whether it is clickable.

    Kept as data so the menu can be asserted without constructing a pystray
    Menu -- and so the *policy* (what is offered when) is separated from the
    plumbing (how pystray renders it).
    """

    id: str
    label: str
    enabled: bool = True
    default: bool = False
    checked: bool | None = None


def actions_for(snapshot: MonitorSnapshot, *, auto_refresh: bool = True) -> list[Action]:
    """The menu, in order, for the current state.

    The list is state-dependent on purpose. Offering "Renew session now" while
    the session is healthy invites a pointless OAuth round trip; offering
    nothing but "Sign in" when the *network* is down sends the user to fix the
    wrong thing.
    """
    items: list[Action] = [
        Action("headline", headline_for(snapshot), enabled=False, default=True)
    ]

    if snapshot.state is MonitorState.LOGIN_REQUIRED:
        items.append(Action("login", "Sign in...", default=True))
    elif snapshot.state is MonitorState.AUTH_ERROR:
        items.append(Action("renew", "Renew session now"))
        items.append(Action("login", "Sign in..."))
    elif snapshot.state is MonitorState.STARTING:
        items.append(Action("refresh", "Refresh now"))
    else:
        # Healthy, refreshing, offline or server-side: a manual refresh is
        # always meaningful, and a renewal is offered when the credential is
        # close to expiry.
        items.append(Action("refresh", "Refresh now"))
        if _renewal_is_useful(snapshot):
            items.append(Action("renew", "Renew session now"))

    items.append(Action("open", "Open openCsiTool in browser"))
    items.append(Action("sep1", "-", enabled=False))
    items.append(
        Action("autorefresh", "Auto refresh", checked=auto_refresh)
    )
    items.append(Action("copy", "Copy status to clipboard"))
    items.append(Action("sep2", "-", enabled=False))
    items.append(Action("quit", "Quit"))
    return items


def _renewal_is_useful(snapshot: MonitorSnapshot) -> bool:
    """Whether offering "Renew session now" is likely to do something.

    A renewal with hours of validity left would complete as ``ALREADY_VALID``,
    which is a confusing thing to show for a deliberate click. Under an hour,
    the round trip is worth it.
    """
    if snapshot.credential_expires_in is None:
        return True
    return snapshot.credential_expires_in <= 3600.0


def status_text(snapshot: MonitorSnapshot, *, now: float | None = None) -> str:
    """The text the "Copy status" action puts on the clipboard.

    Plain text, several lines, no secrets -- suitable for pasting into a bug
    report, which is the only reason this action exists.
    """
    lines = [
        f"state: {snapshot.state.value}",
        f"label: {snapshot.label}",
        f"has_data: {snapshot.has_data}",
    ]
    if snapshot.has_data:
        lines.extend(
            [
                f"total_tokens: {snapshot.total_tokens}",
                f"requests: {snapshot.requests}",
                f"prs: {snapshot.prs}",
                f"added_lines: {snapshot.added_lines}",
                f"generated_lines: {snapshot.generated_lines}",
                f"adopted_lines: {snapshot.adopted_lines}",
                f"adoption_rate: {snapshot.adoption_rate:.1%}",
                f"data_fresh_time: {snapshot.data_fresh_time}",
                f"data_age: {format_age(snapshot.age_seconds(now=now))}",
            ]
        )
    if snapshot.credential_expires_in is not None:
        lines.append(
            f"credential_expires_in: {format_duration(snapshot.credential_expires_in)}"
        )
    if snapshot.consecutive_failures:
        lines.append(f"consecutive_failures: {snapshot.consecutive_failures}")
    if snapshot.last_error:
        lines.append(f"last_error: {snapshot.last_error}")
    return "\n".join(lines)


def menu_signature(actions: list[Action]) -> tuple[tuple[str, str, bool, bool | None], ...]:
    """A hashable fingerprint of a menu, so the tray can skip a needless rebuild.

    pystray rebuilds the whole menu on every update; doing that on every poll
    would flicker and, worse, would drop the menu out from under a user who has
    it open. Comparing signatures lets an unchanged menu stay put.
    """
    return tuple(
        (action.id, action.label, action.enabled, action.checked) for action in actions
    )
