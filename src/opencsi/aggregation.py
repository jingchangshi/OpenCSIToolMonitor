"""Aggregation and cost estimation.

Two responsibilities:

1. :func:`summarise` -- produce the exact figures the page shows. The page
   computes PR count, added/generated/adopted lines and the token split by
   ``requestType`` **client-side** from ``requestList``; the API only supplies
   ``tokenSummary.totalTokens`` and ``totalRequestCount``. Reproducing this
   faithfully is what makes the CLI's numbers match the website (report §5,
   §18).

2. :func:`estimate_usage_cost` -- turn token volumes into money using
   ``ai/config/cost``. ``billType`` matters: ``TOKEN`` rows have a blended
   per-million-token price, ``FLAT`` rows have a monthly fee and must **not** be
   multiplied by tokens (report §19).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from .models import ModelPrice, MyToolsSnapshot, TokenTrendPoint, ToolGrant

#: Prices in ``ai/config/cost`` are per million tokens (observed blendedPrice
#: values such as 0.28 for DeepSeek-V4-Flash are consistent with CNY per 1M).
TOKENS_PER_UNIT = 1_000_000


@dataclass(frozen=True)
class Summary:
    """The "Personal data overview" card, plus tool counts."""

    total_tokens: int = 0
    total_request_count: int = 0
    pr_count: int = 0
    added_lines_count: int = 0
    generated_code_lines: int = 0
    adopted_code_lines: int = 0
    adoption_rate: float = 0.0
    tokens_by_request_type: Mapping[str, int] = field(default_factory=dict)
    active_tools: int = 0
    expired_tools: int = 0
    total_tools: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "total_tokens": self.total_tokens,
            "request_count": self.total_request_count,
            "pr_count": self.pr_count,
            "added_lines": self.added_lines_count,
            "generated_lines": self.generated_code_lines,
            "adopted_lines": self.adopted_code_lines,
            "adoption_rate": round(self.adoption_rate, 4),
            "tokens_by_request_type": dict(self.tokens_by_request_type),
            "tools": {
                "active": self.active_tools,
                "expired": self.expired_tools,
                "total": self.total_tools,
            },
        }


def summarise(snapshot: MyToolsSnapshot) -> Summary:
    """Derive every headline figure from a snapshot."""
    grants = snapshot.grants
    return Summary(
        total_tokens=snapshot.total_tokens,
        total_request_count=snapshot.total_request_count,
        pr_count=sum(g.pr_count for g in grants),
        added_lines_count=sum(g.added_lines_count for g in grants),
        generated_code_lines=sum(g.generated_code_lines for g in grants),
        adopted_code_lines=sum(g.adopted_code_lines for g in grants),
        adoption_rate=snapshot.adoption_rate,
        tokens_by_request_type=dict(snapshot.tokens_by_request_type),
        active_tools=sum(1 for g in grants if g.is_active),
        expired_tools=sum(1 for g in grants if not g.is_active),
        total_tools=len(grants),
    )


@dataclass(frozen=True)
class CostLine:
    """Cost contribution of one ``requestType``."""

    request_type: str
    display_name: str
    bill_type: str
    tokens: int
    unit_price: float | None
    estimated_cost: float | None
    monthly_fee: float | None
    note: str | None = None


@dataclass(frozen=True)
class CostEstimate:
    """Result of :func:`estimate_usage_cost`."""

    lines: tuple[CostLine, ...] = ()
    token_cost: float = 0.0
    flat_cost: float = 0.0
    currency: str = "CNY"

    @property
    def total_cost(self) -> float:
        """Token-based spend plus recurring flat fees.

        Flat fees are reported separately in the CLI so a user can tell the two
        apart; this total is a convenience for scripts.
        """
        return self.token_cost + self.flat_cost

    def as_dict(self) -> dict[str, object]:
        return {
            "currency": self.currency,
            "token_cost": round(self.token_cost, 4),
            "flat_cost": round(self.flat_cost, 4),
            "total_cost": round(self.total_cost, 4),
            "lines": [
                {
                    "request_type": ln.request_type,
                    "display_name": ln.display_name,
                    "bill_type": ln.bill_type,
                    "tokens": ln.tokens,
                    "unit_price": ln.unit_price,
                    "estimated_cost": (
                        round(ln.estimated_cost, 4) if ln.estimated_cost is not None else None
                    ),
                    "monthly_fee": ln.monthly_fee,
                    "note": ln.note,
                }
                for ln in self.lines
            ],
        }


def estimate_usage_cost(
    tokens_by_request_type: Mapping[str, int],
    prices: Sequence[ModelPrice],
) -> CostEstimate:
    """Estimate spend per ``requestType``.

    ``TOKEN`` rows are priced as ``tokens / 1e6 * blendedPrice``.
    ``FLAT`` rows contribute their ``monthlyFee`` and explicitly no token cost,
    so a subscription such as Trae is never billed per token.
    Unknown request types produce a line with ``estimated_cost=None`` and an
    explanatory note rather than a silent zero.
    """
    by_type = {p.request_type: p for p in prices}
    lines: list[CostLine] = []
    token_cost = 0.0
    flat_cost = 0.0

    for request_type, tokens in sorted(tokens_by_request_type.items()):
        price = by_type.get(request_type)
        if price is None:
            lines.append(
                CostLine(
                    request_type=request_type,
                    display_name=request_type,
                    bill_type="UNKNOWN",
                    tokens=tokens,
                    unit_price=None,
                    estimated_cost=None,
                    monthly_fee=None,
                    note="no price configured for this request type",
                )
            )
            continue

        if price.is_token_billed:
            unit = price.blended_price
            if unit is None:
                cost: float | None = None
                note = "token-billed but no blended price configured"
            else:
                cost = tokens / TOKENS_PER_UNIT * unit
                token_cost += cost
                note = None
            lines.append(
                CostLine(
                    request_type=request_type,
                    display_name=price.display_name or request_type,
                    bill_type=price.bill_type,
                    tokens=tokens,
                    unit_price=unit,
                    estimated_cost=cost,
                    monthly_fee=None,
                    note=note,
                )
            )
        elif price.is_flat_billed:
            fee = price.monthly_fee
            if fee is not None:
                flat_cost += fee
            lines.append(
                CostLine(
                    request_type=request_type,
                    display_name=price.display_name or request_type,
                    bill_type=price.bill_type,
                    tokens=tokens,
                    unit_price=None,
                    estimated_cost=None,
                    monthly_fee=fee,
                    note="flat monthly fee; not billed per token",
                )
            )
        else:
            lines.append(
                CostLine(
                    request_type=request_type,
                    display_name=price.display_name or request_type,
                    bill_type=price.bill_type or "UNKNOWN",
                    tokens=tokens,
                    unit_price=None,
                    estimated_cost=None,
                    monthly_fee=price.monthly_fee,
                    note=f"unsupported bill type {price.bill_type!r}",
                )
            )

    return CostEstimate(
        lines=tuple(lines),
        token_cost=token_cost,
        flat_cost=flat_cost,
    )


def display_name_for(request_type: str, prices: Iterable[ModelPrice]) -> str:
    """Map a ``requestType`` to its human label, falling back to the raw value."""
    for price in prices:
        if price.request_type == request_type:
            return price.display_name or request_type
    return request_type


def aggregate_trend(
    points: Sequence[TokenTrendPoint],
    *,
    by_model: bool = True,
) -> dict[str, int]:
    """Sum trend tokens, grouped by model or by date."""
    out: dict[str, int] = {}
    for point in points:
        key = point.request_type if by_model else point.date
        out[key] = out.get(key, 0) + point.tokens
    return out


def group_grants_by_type(grants: Iterable[ToolGrant]) -> dict[str, list[ToolGrant]]:
    """Group tool grants by ``requestType``, preserving encounter order."""
    out: dict[str, list[ToolGrant]] = {}
    for grant in grants:
        out.setdefault(grant.request_type, []).append(grant)
    return out
