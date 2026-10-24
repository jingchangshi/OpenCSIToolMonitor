"""Turn a :class:`MonitorSnapshot` into the data view model for the popup panel.

This module is the panel's *entire* presentation logic, and it deliberately
imports nothing from Tkinter. Everything here is plain data, so every rule the
panel depends on is assertable in a test without a display -- including Linux
CI, where no window can exist.

The rules that matter, each of which is easy to get subtly wrong:

* **A missing value is never rendered as zero.** "0 tokens" is a factual claim
  that needs the server's own freshness signal behind it; "not ready yet" and
  "fetch failed" are different facts with different captions.
* **Stale data is labelled stale.** A panel showing this morning's numbers at
  midnight without saying so is a lie by omission.
* **Costs are always "估算费用".** The number is ``tokens / 1e6 * blendedPrice``
  from :mod:`opencsi.aggregation`, never a billed amount, and an unknown cost is
  ``--`` rather than ¥0.
* **No per-day request count exists.** :class:`~opencsi.monitor.DailyUsage`
  carries tokens and cost only; the account-wide request total is a *cumulative*
  figure and is shown in the cumulative card, never as "today's requests".
* **Chinese magnitude formatting** keeps the two headline numbers readable at
  panel size; the full digit string stays available for a tooltip or copy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from ..monitor import MonitorSnapshot, MonitorState

#: The accounting states a today-card can be in. Named rather than implied so
#: the panel cannot accidentally render "no data" as "0.00".
class TodayState(str, Enum):
    AVAILABLE_NONZERO = "AVAILABLE_NONZERO"
    """The server answered, the day is complete enough, and tokens are non-zero."""

    AVAILABLE_ZERO = "AVAILABLE_ZERO"
    """The server answered with an empty trend *and* its own freshness signal
    covers today -- the only case where "0" is an honest rendering."""

    NOT_READY = "NOT_READY"
    """Today's numbers are not in yet: the server's freshness signal predates
    the requested day, or the day rolled over after the last fetch."""

    UNAVAILABLE = "UNAVAILABLE"
    """There is no today data to show: no fetch has succeeded, or the daily
    leg failed."""

    STALE = "STALE"
    """The numbers on screen are the last successful fetch's, retained through
    a later failure. Shown with that fetch's time, never as current."""

    COST_UNKNOWN = "COST_UNKNOWN"
    """Tokens are valid, but the price table cannot account for every model,
    so the total cost is not rendered as a number."""


@dataclass(frozen=True)
class StatusView:
    """The header line: state, its colour and the recovery hint."""

    state: MonitorState
    label: str
    color: str
    reason: str = ""
    hint: str = ""


@dataclass(frozen=True)
class TodayCard:
    """The visual centre of the panel."""

    state: TodayState
    date: str | None
    tokens_display: str
    tokens_full: str | None
    cost_display: str
    caption: str
    note: str = ""


@dataclass(frozen=True)
class ModelRow:
    name: str
    tokens_display: str
    tokens_full: str
    cost_display: str
    is_total: bool = False
    flat: bool = False


@dataclass(frozen=True)
class CumulativeCard:
    tokens_display: str
    tokens_full: str
    requests_display: str


@dataclass(frozen=True)
class PanelView:
    """Everything the panel renders, as inert data."""

    status: StatusView
    today: TodayCard
    models: tuple[ModelRow, ...] = ()
    cumulative: CumulativeCard | None = None
    #: Whether the manual renewal action should be offered at all. Offered only
    #: when renewal is useful (the credential is expiring or failing); offering
    #: it on a healthy session invites a pointless OAuth round trip.
    renew_available: bool = False
    renew_reason: str = ""
    auto_refresh: bool = True
    #: Three-valued: True/False reflect the real registry state; None means the
    #: control is not applicable and must not be shown.
    startup: bool | None = None


#: Neutral Win11-style palette. Blue is reserved for the usage area, green for
#: a healthy state, warm colours for attention states; backgrounds stay
#: white/grey so a dark desktop cannot wash the panel out.
COLOR_OK = "#16a34a"
COLOR_INFO = "#2563eb"
COLOR_BUSY = "#64748b"
COLOR_WARN = "#b45309"
COLOR_ERROR = "#b91c1c"

_STATUS_COLORS: dict[MonitorState, str] = {
    MonitorState.STARTING: COLOR_BUSY,
    MonitorState.OK: COLOR_OK,
    MonitorState.REFRESHING: COLOR_INFO,
    MonitorState.RENEWING: COLOR_INFO,
    MonitorState.LOGIN_REQUIRED: COLOR_ERROR,
    MonitorState.CONSENT_REQUIRED: COLOR_WARN,
    MonitorState.BROWSER_UNAVAILABLE: COLOR_WARN,
    MonitorState.OFFLINE: COLOR_BUSY,
    MonitorState.SERVER_ERROR: COLOR_WARN,
    MonitorState.AUTH_ERROR: COLOR_ERROR,
}

#: Chinese labels, mirroring :data:`opencsi.tray.presenter.STATE_LABELS_CN`
#: (the tray menu and the panel must not disagree about what a state is called).
_STATUS_LABELS: dict[MonitorState, str] = {
    MonitorState.STARTING: "启动中",
    MonitorState.OK: "正常",
    MonitorState.REFRESHING: "正在刷新",
    MonitorState.RENEWING: "正在续约",
    MonitorState.LOGIN_REQUIRED: "需要登录",
    MonitorState.CONSENT_REQUIRED: "需要授权",
    MonitorState.BROWSER_UNAVAILABLE: "浏览器未运行",
    MonitorState.OFFLINE: "网络不可用",
    MonitorState.SERVER_ERROR: "服务异常",
    MonitorState.AUTH_ERROR: "会话失效",
}

#: A short reason and a user-executable next step per state. The panel must
#: never reduce a failure to a red dot: the reason and the fix are the point.
_STATUS_DETAIL: dict[MonitorState, tuple[str, str]] = {
    MonitorState.STARTING: ("正在连接 openCsiTool", "稍候即可"),
    MonitorState.OK: ("数据同步正常", ""),
    MonitorState.REFRESHING: ("正在获取最新数据", "无需操作"),
    MonitorState.RENEWING: ("正在静默续期会话", "无需操作"),
    MonitorState.LOGIN_REQUIRED: (
        "GitCode 登录已失效",
        "右键托盘图标选择「扫码登录」",
    ),
    MonitorState.CONSENT_REQUIRED: (
        "GitCode 正在等待应用授权",
        "打开 opencsitool.com 点击一次批准",
    ),
    MonitorState.BROWSER_UNAVAILABLE: (
        "浏览器调试端口不可用",
        "右键托盘图标选择「扫码登录（无需浏览器）」",
    ),
    MonitorState.OFFLINE: ("无法连接服务器", "检查网络后点击「立即刷新」"),
    MonitorState.SERVER_ERROR: ("服务器返回异常", "稍后自动重试，或点击「立即刷新」"),
    MonitorState.AUTH_ERROR: ("会话被拒绝", "点击「立即续期」，失败则扫码登录"),
}


def format_cn_tokens(value: int | None) -> str:
    """A count in 中文 magnitude units: ``52852000`` -> ``5,285.2 万``.

    ``None`` -- which means "no number to show" -- is ``--``, never ``0``.
    Below one 万 the raw digits are clearer than a fraction, so they are shown
    grouped instead.
    """
    if value is None:
        return "--"
    if abs(value) >= 1e8:
        return f"{value / 1e8:,.2f} 亿"
    if abs(value) >= 1e4:
        return f"{value / 1e4:,.1f} 万"
    return f"{value:,}"


def format_cost_display(value: float | None, currency: str = "CNY") -> str:
    """Panel cost rendering: ``¥14.80``, or ``--`` when not computable."""
    if value is None:
        return "--"
    prefix = "¥" if currency.upper() == "CNY" else f"{currency} "
    return f"{prefix}{value:,.2f}"


def format_clock(moment: datetime | None) -> str:
    """``HH:MM`` in local time, or an explicit placeholder."""
    if moment is None:
        return "--:--"
    return moment.astimezone().strftime("%H:%M")


def _parse_server_date(text: str | None) -> str | None:
    """The local calendar date of the server's freshness stamp, if parseable.

    ``syncStatus.dataFreshTime`` arrives as an ISO string with an offset. A
    stamp that cannot be parsed yields ``None`` and the caller treats the
    freshness as unknown rather than as fresh.
    """
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).astimezone().date().isoformat()
    except ValueError:
        return None


def _status_view(snapshot: MonitorSnapshot) -> StatusView:
    label = _STATUS_LABELS.get(snapshot.state, snapshot.label)
    color = _STATUS_COLORS.get(snapshot.state, COLOR_BUSY)
    reason, hint = _STATUS_DETAIL.get(snapshot.state, (snapshot.state.value, ""))
    if snapshot.state in (
        MonitorState.OFFLINE,
        MonitorState.SERVER_ERROR,
        MonitorState.AUTH_ERROR,
        MonitorState.BROWSER_UNAVAILABLE,
    ) and snapshot.last_error:
        reason = snapshot.last_error
    return StatusView(state=snapshot.state, label=label, color=color, reason=reason, hint=hint)


def _today_card(
    snapshot: MonitorSnapshot, *, today: str | None, now: datetime | None
) -> TodayCard:
    """The today card, with the zero/not-ready/stale distinction pinned.

    The decision table, in the order it is evaluated:

    * no daily data at all -- ``UNAVAILABLE``;
    * the daily data is for a *different* calendar day (a rollover the monitor
      has not refreshed through yet) -- ``NOT_READY``; yesterday's numbers must
      not be re-labelled as today's;
    * the snapshot itself is unhealthy but retains data -- ``STALE``: the
      numbers are the last success's, captioned with that success's time;
    * an empty trend whose server freshness stamp does not cover today --
      ``NOT_READY``. An empty trend with a freshness stamp that *does* cover
      today is the one case where zero is an honest answer
      (``AVAILABLE_ZERO``);
    * non-zero tokens with an incomplete cost estimate -- ``COST_UNKNOWN``;
    * otherwise ``AVAILABLE_NONZERO``.
    """
    daily = snapshot.daily_usage
    if daily is None:
        if snapshot.has_data:
            note = "今日明细暂不可用，累计数据仍为最近一次成功查询的结果"
        else:
            note = "正在等待首次数据"
        return TodayCard(
            state=TodayState.UNAVAILABLE,
            date=today,
            tokens_display="--",
            tokens_full=None,
            cost_display="--",
            caption="数据暂不可用　数据来源：openCsiTool",
            note=note,
        )

    if today is not None and daily.date != today:
        return TodayCard(
            state=TodayState.NOT_READY,
            date=today,
            tokens_display="--",
            tokens_full=None,
            cost_display="--",
            caption=f"数据暂不可用　数据来源：openCsiTool",
            note="已跨日，正在等待下一次刷新；昨日数据不并入今日",
        )

    healthy = snapshot.state in (
        MonitorState.OK,
        MonitorState.REFRESHING,
        MonitorState.RENEWING,
    )
    fetched_caption = f"上次成功更新：{format_clock(snapshot.fetched_at)}"
    if not healthy:
        # Unhealthy snapshot: whatever the daily card holds is the last
        # success's data -- including "nothing was in it". Falling through to
        # the zero/not-ready logic here would dress a failed fetch up as a
        # statement about today's usage.
        return TodayCard(
            state=TodayState.STALE,
            date=daily.date,
            tokens_display=format_cn_tokens(daily.total_tokens),
            tokens_full=f"{daily.total_tokens:,}",
            cost_display=format_cost_display(daily.total_cost, daily.currency),
            caption=f"{fetched_caption}　数据来源：openCsiTool",
            note="网络或会话异常，以下为最近一次成功查询的数据",
        )

    tokens_display = format_cn_tokens(daily.total_tokens)
    tokens_full = f"{daily.total_tokens:,}" if daily.total_tokens is not None else None
    cost_display = format_cost_display(daily.total_cost, daily.currency)

    if daily.total_tokens == 0 and not daily.models:
        fresh_date = _parse_server_date(snapshot.data_fresh_time)
        if today is not None and fresh_date is not None and fresh_date >= today:
            state = TodayState.AVAILABLE_ZERO
            note = ""
        else:
            state = TodayState.NOT_READY
            note = "服务端今日数据尚未同步，不代表今天没有用量"
        if state is TodayState.AVAILABLE_ZERO:
            tokens_display = "0"
            tokens_full = "0"
            cost_display = "¥0.00" if daily.currency.upper() == "CNY" else "0.00"
        else:
            tokens_display = "--"
            tokens_full = None
            cost_display = "--"
        caption = (
            f"最近更新：{format_clock(snapshot.fetched_at)}　数据来源：openCsiTool"
        )
        return TodayCard(
            state=state,
            date=daily.date,
            tokens_display=tokens_display,
            tokens_full=tokens_full,
            cost_display=cost_display,
            caption=caption,
            note=note,
        )

    if daily.total_cost is None:
        caption = (
            f"最近更新：{format_clock(snapshot.fetched_at)}　数据来源：openCsiTool"
        )
        return TodayCard(
            state=TodayState.COST_UNKNOWN,
            date=daily.date,
            tokens_display=tokens_display,
            tokens_full=tokens_full,
            cost_display="--",
            caption=caption,
            note="部分模型无按 Token 价格，费用无法完整估算",
        )

    state = (
        TodayState.AVAILABLE_NONZERO
        if (daily.total_tokens or 0) > 0
        else TodayState.AVAILABLE_ZERO
    )
    caption = f"最近更新：{format_clock(snapshot.fetched_at)}　数据来源：openCsiTool"
    return TodayCard(
        state=state,
        date=daily.date,
        tokens_display=tokens_display,
        tokens_full=tokens_full,
        cost_display=cost_display,
        caption=caption,
    )


def _model_rows(daily) -> tuple[ModelRow, ...]:
    """The model breakdown, sorted by tokens descending, totals last.

    Rows are presented exactly as :mod:`opencsi.aggregation` priced them: a
    flat-billed model keeps its ``--`` cost (its monthly fee is *not* prorated
    into the day), and an unknown price keeps ``--`` rather than ¥0.
    """
    rows: list[ModelRow] = []
    # Sorted here as well as upstream: :meth:`build_daily_usage` already orders
    # by tokens, but the view model is the last line of defence -- a fixture or
    # a future caller handing rows unsorted must not unsort the panel.
    for model in sorted(
        daily.models,
        key=lambda m: (-m.tokens, m.display_name, m.request_type),
    ):
        rows.append(
            ModelRow(
                name=model.display_name,
                tokens_display=format_cn_tokens(model.tokens),
                tokens_full=f"{model.tokens:,}",
                cost_display=format_cost_display(model.cost, daily.currency),
                flat=model.cost is None and model.tokens > 0,
            )
        )
    return tuple(rows)


def _renewal_view(snapshot: MonitorSnapshot) -> tuple[bool, str]:
    """Whether "立即续期" is an honest offer right now.

    Renewal is useful when the session is failing or close to expiry. A
    healthy, freshly renewed session hides the action: clicking it would spend
    an OAuth round trip for nothing.
    """
    from .presenter import _renewal_is_useful

    if snapshot.state is MonitorState.RENEWING:
        return False, "正在续期"
    if snapshot.state in (
        MonitorState.AUTH_ERROR,
        MonitorState.LOGIN_REQUIRED,
        MonitorState.CONSENT_REQUIRED,
    ):
        return True, "会话异常，尝试静默续期"
    if _renewal_is_useful(snapshot):
        remaining = snapshot.credential_expires_in
        if remaining is not None:
            minutes = max(1, int(remaining // 60))
            return True, f"会话将在约 {minutes} 分钟后到期"
        return True, "会话临近到期"
    return False, ""


def build_panel_view(
    snapshot: MonitorSnapshot,
    *,
    today: str | None = None,
    now: datetime | None = None,
    auto_refresh: bool = True,
    startup: bool | None = None,
) -> PanelView:
    """Build the full panel view model from a snapshot.

    ``today`` is the local calendar date the *panel* believes it is; the caller
    computes it so a test can pin rollover behaviour. ``startup`` is passed
    through unchanged: ``None`` must reach the UI as "hide the control", not as
    "off".
    """
    daily = snapshot.daily_usage
    can_renew, renew_reason = _renewal_view(snapshot)
    cumulative = None
    if snapshot.has_data:
        cumulative = CumulativeCard(
            tokens_display=format_cn_tokens(snapshot.total_tokens),
            tokens_full=f"{snapshot.total_tokens:,}",
            requests_display=f"{snapshot.requests:,}",
        )
    models: tuple[ModelRow, ...] = ()
    if daily is not None:
        models = _model_rows(daily)
    return PanelView(
        status=_status_view(snapshot),
        today=_today_card(snapshot, today=today, now=now),
        models=models,
        cumulative=cumulative,
        renew_available=can_renew,
        renew_reason=renew_reason,
        auto_refresh=auto_refresh,
        startup=startup,
    )
