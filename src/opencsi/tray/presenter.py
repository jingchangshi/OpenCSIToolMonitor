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

#: Chinese display labels for the tray, kept *separate* from
#: :data:`opencsi.monitor.STATE_LABELS` rather than replacing it. The enum values
#: and their English labels are a stable contract that appears in JSON and in
#: logs; the tray is read by a Chinese-speaking user who thinks in these words.
#: Conflating the two would mean either a Chinese ``state`` field in machine
#: output or an English tooltip for the person using it.
STATE_LABELS_CN: dict[MonitorState, str] = {
    MonitorState.STARTING: "启动中",
    MonitorState.OK: "正常",
    MonitorState.REFRESHING: "刷新中",
    MonitorState.RENEWING: "续期中",
    MonitorState.LOGIN_REQUIRED: "需要登录",
    MonitorState.BROWSER_UNAVAILABLE: "浏览器未运行",
    MonitorState.OFFLINE: "离线",
    MonitorState.SERVER_ERROR: "服务异常",
    MonitorState.AUTH_ERROR: "会话失效",
}


def label_cn(snapshot: MonitorSnapshot) -> str:
    """The state, in the language the tray's user reads."""
    return STATE_LABELS_CN.get(snapshot.state, snapshot.label)


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


def format_count_cn(value: int) -> str:
    """Compact count in the units a Chinese reader reads at a glance.

    ``3634063175`` -> ``36.3亿``. The site is Chinese, the numbers are Chinese
    users' own usage, and the brief asks for Chinese output -- but the real
    reason is legibility: ``3.6B`` requires the reader to convert a
    Western-scale unit back into the 亿 they think in, which is exactly the
    mental arithmetic a tooltip exists to save.

    万 (10^4) and 亿 (10^8) are the units that matter. Below 万 the raw number
    is short enough to just show.
    """
    if value < 0:
        return "-" + format_count_cn(-value)
    if value < 10_000:
        return str(value)
    if value < 100_000_000:
        return f"{value / 10_000:.1f}万"
    return f"{value / 100_000_000:.1f}亿"


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
    head = f"OpenCSI | {label_cn(snapshot)}"

    if not snapshot.has_data:
        line = snapshot.last_error or "等待首次更新"
        text = f"{head}\n{line}"
        return text[:MAX_TOOLTIP]

    parts = [
        head,
        f"{format_count_cn(snapshot.total_tokens)} tokens  "
        f"{format_count_cn(snapshot.requests)} 次请求  {snapshot.prs} PR",
    ]

    age = snapshot.age_seconds(now=now)
    if snapshot.is_healthy:
        freshness = f"更新于 {format_age(age)} 前"
    else:
        freshness = f"离线 - 最后更新 {format_age(age)} 前"
    if snapshot.credential_expires_in is not None:
        freshness += f" | 会话 {format_duration(snapshot.credential_expires_in)}"
    parts.append(freshness)

    if not snapshot.is_healthy and snapshot.last_error:
        parts.append(snapshot.last_error)

    return "\n".join(parts)[:MAX_TOOLTIP]


def headline_for(snapshot: MonitorSnapshot) -> str:
    """The default (bold, first) menu item: the number people open the menu for.

    Exact figures, not the abbreviated ones the tooltip uses. The tooltip has
    127 characters for everything and must compress; the menu has room, and a
    user who opens a menu to read a number wants the number, not ``123.5万``.
    Abbreviating here would throw away precision exactly where someone came
    looking for it.
    """
    if not snapshot.has_data:
        return snapshot.last_error or "暂无数据"
    return (
        f"{snapshot.total_tokens:,} tokens / {snapshot.requests:,} 次请求"
    )


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

    if snapshot.state is MonitorState.BROWSER_UNAVAILABLE:
        # The honest first action. "Sign in" is not offered because it cannot
        # work: there is no browser to sign in *to*, so the login page would be
        # opened without a debugging port and the cookie written where this tool
        # cannot read it. Starting the browser is the step that unblocks
        # everything else, and the login flow is offered behind it.
        items.append(Action("launch_browser", "启动浏览器并登录"))
        items.append(Action("login", "打开登录页面"))
    elif snapshot.state is MonitorState.LOGIN_REQUIRED:
        items.append(Action("login", "登录 / Sign in...", default=True))
    elif snapshot.state is MonitorState.AUTH_ERROR:
        items.append(Action("renew", "立即续期"))
        items.append(Action("login", "登录 / Sign in..."))
    elif snapshot.state is MonitorState.STARTING:
        items.append(Action("refresh", "立即刷新"))
    else:
        # Healthy, refreshing, offline or server-side: a manual refresh is
        # always meaningful, and a renewal is offered when the credential is
        # close to expiry.
        items.append(Action("refresh", "立即刷新"))
        if _renewal_is_useful(snapshot):
            items.append(Action("renew", "立即续期"))

    items.append(Action("open", "打开 openCsiTool 网站"))
    items.append(Action("sep1", "-", enabled=False))
    items.append(
        Action("autorefresh", "自动刷新", checked=auto_refresh)
    )
    items.append(Action("copy", "复制状态到剪贴板"))
    items.append(Action("sep2", "-", enabled=False))
    items.append(Action("quit", "退出"))
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
