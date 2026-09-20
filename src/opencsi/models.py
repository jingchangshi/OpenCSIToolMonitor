"""Domain models for openCsiTool data.

Design rules (from the investigation report, §17):

* The server may add fields at any time -- unknown keys are ignored, never fatal.
* The server may omit non-critical fields -- parsing degrades to ``None``/``0``.
* Only the *core* identifiers (``id``, ``requestType``) are treated as required,
  and even those are coerced rather than raising.

Every model is a frozen dataclass so instances are safe to cache and share.
Raw credentials are never stored on these objects: ``ToolGrant`` keeps the
``virtualKey`` in a private field marked ``repr=False`` and exposes only the
site's own masked form.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping

# ── coercion helpers ──────────────────────────────────────────────────────
_INT_RE = re.compile(r"-?\d+")


def as_str(value: Any, default: str = "") -> str:
    """Coerce to ``str``; ``None`` becomes ``default``."""
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def as_opt_str(value: Any) -> str | None:
    """Coerce to ``str | None``; empty strings become ``None``."""
    if value is None:
        return None
    s = value if isinstance(value, str) else str(value)
    return s if s != "" else None


def as_int(value: Any, default: int = 0) -> int:
    """Best-effort integer coercion.

    Accepts ints, floats, numeric strings and strings with separators such as
    ``"1,234"``. Anything unparseable yields ``default`` instead of raising.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        try:
            return int(value)
        except (OverflowError, ValueError):
            return default
    m = _INT_RE.search(str(value).replace(",", ""))
    if not m:
        return default
    try:
        return int(m.group(0))
    except ValueError:
        return default


def as_opt_float(value: Any) -> float | None:
    """Best-effort float coercion; ``None`` on failure."""
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def parse_iso8601(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp, honouring its own UTC offset.

    The server emits ``2026-09-19T22:07:07+08:00``. We keep the offset rather
    than assuming a fixed timezone (report §58). Returns ``None`` when the
    value is absent or unparseable.
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    # Python 3.10's fromisoformat cannot parse a trailing "Z".
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


# ── models ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SyncStatus:
    """Server-side data freshness markers (``data.syncStatus``)."""

    data_fresh_time: str | None = None
    etl_time: str | None = None
    replication_time: str | None = None

    @classmethod
    def from_json(cls, raw: Mapping[str, Any] | None) -> "SyncStatus":
        raw = raw or {}
        return cls(
            data_fresh_time=as_opt_str(raw.get("dataFreshTime")),
            etl_time=as_opt_str(raw.get("etlTime")),
            replication_time=as_opt_str(raw.get("replicationTime")),
        )

    @property
    def data_fresh_dt(self) -> datetime | None:
        return parse_iso8601(self.data_fresh_time)


@dataclass(frozen=True)
class TokenBudget:
    """Token budget for an employee.

    Sourced from ``data.tokenBudget`` and, when available, overridden by the
    llmgateway ``key-budget`` endpoint -- this mirrors the page behaviour where
    the latter wins (report §8.4).
    """

    exists: bool = False
    max_budget: float | None = None
    budget_duration: str | None = None
    spend: float | None = None

    @classmethod
    def from_json(cls, raw: Mapping[str, Any] | None) -> "TokenBudget | None":
        if not isinstance(raw, Mapping):
            return None
        return cls(
            exists=bool(raw.get("exists")),
            max_budget=as_opt_float(raw.get("maxBudget")),
            budget_duration=as_opt_str(raw.get("budgetDuration")),
            spend=as_opt_float(raw.get("spend")),
        )


@dataclass(frozen=True)
class ToolGrant:
    """One entry of ``data.requestList`` -- a granted AI tool account.

    Field names mirror the API. ``_virtual_key`` is private, excluded from
    ``repr``/``eq`` and never emitted; use :attr:`virtual_key_masked` instead.
    """

    id: int = 0
    application_number: str = ""
    request_type: str = ""
    status: int = 0
    account_name: str = ""
    issue_date: str | None = None
    create_time: str | None = None
    last_used_date: str | None = None
    token_usage: int = 0
    request_count: int = 0
    pr_count: int = 0
    added_lines_count: int = 0
    generated_code_lines: int = 0
    adopted_code_lines: int = 0
    remark: str | None = None
    wait_days: int = 0
    queue_position: int = 0
    estimated_wait_time: str | None = None
    issue_count: int = 0
    ai_tool_name: str | None = None
    employee_id: str | None = None
    # repr=False and compare=False keep the raw key out of logs and equality.
    _virtual_key: str | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> "ToolGrant":
        raw = raw or {}
        return cls(
            id=as_int(raw.get("id")),
            application_number=as_str(raw.get("applicationNumber")),
            request_type=as_str(raw.get("requestType")),
            status=as_int(raw.get("status")),
            account_name=as_str(raw.get("accountName")),
            issue_date=as_opt_str(raw.get("issueDate")),
            create_time=as_opt_str(raw.get("createTime")),
            last_used_date=as_opt_str(raw.get("lastUsedDate")),
            token_usage=as_int(raw.get("tokenUsage")),
            request_count=as_int(raw.get("requestCount")),
            pr_count=as_int(raw.get("prCount")),
            added_lines_count=as_int(raw.get("addedLinesCount")),
            generated_code_lines=as_int(raw.get("generatedCodeLines")),
            adopted_code_lines=as_int(raw.get("adoptedCodeLines")),
            remark=as_opt_str(raw.get("remark")),
            wait_days=as_int(raw.get("waitDays")),
            queue_position=as_int(raw.get("queuePosition")),
            estimated_wait_time=as_opt_str(raw.get("estimatedWaitTime")),
            issue_count=as_int(raw.get("issueCount")),
            ai_tool_name=as_opt_str(raw.get("aiToolName")),
            employee_id=as_opt_str(raw.get("employeeId")),
            _virtual_key=as_opt_str(raw.get("virtualKey")),
        )

    # -- page-derived presentation rules (report §5.2) --------------------
    @property
    def status_text(self) -> str:
        """``status == 1`` renders as 使用中; anything else as 已失效."""
        return "使用中" if self.status == 1 else "已失效"

    @property
    def is_active(self) -> bool:
        return self.status == 1

    @property
    def virtual_key_masked(self) -> str:
        """The site's own mask: first 10 characters then ``****``.

        Returns ``"-"`` when the account has no key. The full value is never
        returned by this property.
        """
        if not self._virtual_key:
            return "-"
        return f"{self._virtual_key[:10]}****"

    @property
    def has_virtual_key(self) -> bool:
        return bool(self._virtual_key)


@dataclass(frozen=True)
class TokenTrendPoint:
    """One ``data.tokenTrend`` sample: tokens for a model on a date."""

    date: str = ""
    request_type: str = ""
    tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> "TokenTrendPoint":
        raw = raw or {}
        return cls(
            date=as_str(raw.get("date")),
            request_type=as_str(raw.get("requestType")),
            tokens=as_int(raw.get("tokens")),
            prompt_tokens=as_int(raw.get("promptTokens")),
            completion_tokens=as_int(raw.get("completionTokens")),
        )


@dataclass(frozen=True)
class ModelPrice:
    """One row of ``ai/config/cost`` -- a billable model or tool.

    ``bill_type`` distinguishes ``TOKEN`` (priced per token via
    ``blended_price``) from ``FLAT`` (a recurring ``monthly_fee``). The two must
    never be conflated (report §19).
    """

    request_type: str = ""
    display_name: str = ""
    bill_type: str = ""
    enabled: int = 0
    price_mode: str | None = None
    blended_price: float | None = None
    input_price: float | None = None
    output_price: float | None = None
    monthly_fee: float | None = None
    remark: str | None = None

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> "ModelPrice":
        raw = raw or {}
        return cls(
            request_type=as_str(raw.get("requestType")),
            display_name=as_str(raw.get("displayName")),
            bill_type=as_str(raw.get("billType")),
            enabled=as_int(raw.get("enabled")),
            price_mode=as_opt_str(raw.get("priceMode")),
            blended_price=as_opt_float(raw.get("blendedPrice")),
            input_price=as_opt_float(raw.get("inputPrice")),
            output_price=as_opt_float(raw.get("outputPrice")),
            monthly_fee=as_opt_float(raw.get("monthlyFee")),
            remark=as_opt_str(raw.get("remark")),
        )

    @property
    def is_enabled(self) -> bool:
        return self.enabled == 1

    @property
    def is_token_billed(self) -> bool:
        return self.bill_type.upper() == "TOKEN"

    @property
    def is_flat_billed(self) -> bool:
        return self.bill_type.upper() == "FLAT"


@dataclass(frozen=True)
class Identity:
    """Signed-in user, from ``user/getUserInfo`` (+ role lookup)."""

    user_id: str = ""
    employee_id: str = ""
    user_name: str = ""
    user_email: str | None = None
    account_id: str = ""
    account_login: str = ""
    organization_id: str = ""
    organization_name: str | None = None
    role_view: str | None = None
    role_view_name: str | None = None
    roles: tuple[str, ...] = ()

    @property
    def display_name(self) -> str:
        return self.user_name or self.account_login or self.employee_id or "(unknown)"


@dataclass(frozen=True)
class MyToolsSnapshot:
    """Everything the "My Tools" page renders, from one request.

    ``fetched_at`` (when *we* called the API) is deliberately separate from
    ``sync_status.data_fresh_time`` (when the *server* last refreshed its data).
    The two must not be conflated (report §57).
    """

    identity: Identity | None = None
    bound_employee_id: str = ""
    user_id: str = ""
    grants: tuple[ToolGrant, ...] = ()
    token_trend: tuple[TokenTrendPoint, ...] = ()
    total_tokens: int = 0
    total_request_count: int = 0
    sync_status: SyncStatus = field(default_factory=SyncStatus)
    token_budget: TokenBudget | None = None
    fetched_at: datetime | None = None
    start_date: str | None = None
    end_date: str | None = None

    # ── client-side aggregation, replicating the page exactly (§18) ──────
    @property
    def pr_count(self) -> int:
        return sum(g.pr_count for g in self.grants)

    @property
    def added_lines_count(self) -> int:
        return sum(g.added_lines_count for g in self.grants)

    @property
    def generated_code_lines(self) -> int:
        return sum(g.generated_code_lines for g in self.grants)

    @property
    def adopted_code_lines(self) -> int:
        return sum(g.adopted_code_lines for g in self.grants)

    @property
    def adoption_rate(self) -> float:
        """``adopted / generated``; ``0.0`` when nothing was generated."""
        generated = self.generated_code_lines
        if generated <= 0:
            return 0.0
        return self.adopted_code_lines / generated

    @property
    def tokens_by_request_type(self) -> dict[str, int]:
        """Token totals grouped by ``requestType`` (drives the card's sub-line)."""
        out: dict[str, int] = {}
        for g in self.grants:
            out[g.request_type] = out.get(g.request_type, 0) + g.token_usage
        return out

    @property
    def active_grants(self) -> tuple[ToolGrant, ...]:
        return tuple(g for g in self.grants if g.is_active)

    @property
    def expired_grants(self) -> tuple[ToolGrant, ...]:
        return tuple(g for g in self.grants if not g.is_active)

    @property
    def trend_dates(self) -> tuple[str, ...]:
        seen: list[str] = []
        for p in self.token_trend:
            if p.date not in seen:
                seen.append(p.date)
        return tuple(seen)

    @property
    def trend_models(self) -> tuple[str, ...]:
        seen: list[str] = []
        for p in self.token_trend:
            if p.request_type not in seen:
                seen.append(p.request_type)
        return tuple(seen)

    @property
    def trend_tokens(self) -> int:
        return sum(p.tokens for p in self.token_trend)


def coerce_grants(items: Iterable[Any] | None) -> tuple[ToolGrant, ...]:
    """Convert a raw list to models, skipping entries that are not mappings."""
    if not items:
        return ()
    out: list[ToolGrant] = []
    for item in items:
        if isinstance(item, Mapping):
            out.append(ToolGrant.from_json(item))
    return tuple(out)


def coerce_trend(items: Iterable[Any] | None) -> tuple[TokenTrendPoint, ...]:
    """Convert a raw list to trend points, skipping malformed entries."""
    if not items:
        return ()
    out: list[TokenTrendPoint] = []
    for item in items:
        if isinstance(item, Mapping):
            out.append(TokenTrendPoint.from_json(item))
    return tuple(out)


def coerce_prices(items: Iterable[Any] | None) -> tuple[ModelPrice, ...]:
    """Convert a raw list to model prices, skipping malformed entries."""
    if not items:
        return ()
    out: list[ModelPrice] = []
    for item in items:
        if isinstance(item, Mapping):
            out.append(ModelPrice.from_json(item))
    return tuple(out)
