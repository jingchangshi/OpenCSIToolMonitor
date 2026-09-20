"""Human-readable rendering: number abbreviations, tables, JSON.

Two rules drive this module.

1. **Match the web page.** openCsiTool renders large counts with the Chinese
   myriad units (``30.6亿``, ``2.2万``) and one decimal place. A CLI that printed
   ``3061130999`` where the site says ``30.6亿`` would force users to convert in
   their heads, so :func:`format_count` reproduces the site's rule exactly.

2. **Be honest about width.** Chinese glyphs occupy two terminal columns. A
   table padded by ``len()`` misaligns the moment a column contains 使用中, so
   every width calculation goes through :func:`display_width`.

Nothing here touches credentials; the only secret-aware behaviour is that
``-`` is used for absent values instead of ``None``.
"""

from __future__ import annotations

import json
import unicodedata
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

# The site switches from 万 to 亿 at 1e8 and always shows one decimal place.
WAN = 10_000
YI = 100_000_000
PLACEHOLDER = "-"

#: Wide (2-column) East Asian classes. ``A`` (ambiguous) is treated as narrow
#: because that is what the terminals this tool targets actually do.
_WIDE = frozenset({"W", "F"})


# ── width ─────────────────────────────────────────────────────────────────
def display_width(text: str) -> int:
    """Terminal columns occupied by ``text``.

    Combining marks count zero, East Asian wide/fullwidth characters count two,
    everything else counts one.
    """
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in _WIDE else 1
    return width


def pad(text: str, width: int, *, align: str = "left") -> str:
    """Pad ``text`` to ``width`` terminal columns.

    ``align`` is ``left``, ``right`` or ``center``. Text wider than ``width`` is
    returned unchanged rather than truncated, so a value is never silently
    destroyed by a narrow column.
    """
    filler = max(0, width - display_width(text))
    if align == "right":
        return " " * filler + text
    if align == "center":
        left = filler // 2
        return " " * left + text + " " * (filler - left)
    return text + " " * filler


# ── numbers ───────────────────────────────────────────────────────────────
def format_count(value: int | float | None) -> str:
    """Abbreviate a count the way the openCsiTool page does.

    ``3061130999 -> 30.6亿``, ``21632 -> 2.2万``, ``999 -> 999``.
    Values below 10,000 are printed exactly; there is nothing to abbreviate and
    rounding them would lose information.
    """
    if value is None:
        return PLACEHOLDER
    try:
        number = float(value)
    except (TypeError, ValueError):
        return PLACEHOLDER

    sign = "-" if number < 0 else ""
    number = abs(number)

    if number >= YI:
        return f"{sign}{number / YI:.1f}亿"
    if number >= WAN:
        return f"{sign}{number / WAN:.1f}万"
    if number.is_integer():
        return f"{sign}{int(number)}"
    return f"{sign}{number:g}"


def format_int(value: int | None) -> str:
    """Thousands-separated exact integer, for columns where precision matters."""
    if value is None:
        return PLACEHOLDER
    return f"{value:,}"


def format_percent(fraction: float | None, *, digits: int = 1) -> str:
    """Format a *ratio* as a percentage: ``0.03809 -> 3.8%``."""
    if fraction is None:
        return PLACEHOLDER
    return f"{fraction * 100:.{digits}f}%"


def format_money(value: float | None, *, currency: str = "CNY") -> str:
    """Format an amount with two decimals, e.g. ``¥200.00``."""
    if value is None:
        return PLACEHOLDER
    symbol = {"CNY": "¥", "RMB": "¥", "USD": "$"}.get(currency.upper(), "")
    return f"{symbol}{value:,.2f}"


def format_price(value: float | None) -> str:
    """Per-million-token price; keeps up to 4 decimals without trailing zeros."""
    if value is None:
        return PLACEHOLDER
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def format_ratio(a: int, b: int) -> str:
    """``a/b`` as a percentage, tolerating a zero denominator."""
    if not b:
        return PLACEHOLDER
    return format_percent(a / b)


def format_datetime(value: datetime | str | None, *, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Format a datetime (or ISO-8601 string) in local-independent form."""
    if value is None or value == "":
        return PLACEHOLDER
    if isinstance(value, datetime):
        return value.strftime(fmt)
    return str(value)


def format_relative_seconds(seconds: float | None) -> str:
    """``412.3 -> 6m52s``; used for cookie lifetime readouts."""
    if seconds is None:
        return PLACEHOLDER
    if seconds < 0:
        return "expired"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def yes_no(flag: bool | None) -> str:
    if flag is None:
        return PLACEHOLDER
    return "yes" if flag else "no"


def active_text(flag: bool) -> str:
    """使用中 / 已失效, matching the page's status column."""
    return "使用中" if flag else "已失效"


def or_dash(value: Any) -> str:
    """Render ``None``/``""`` as ``-`` and everything else via ``str``."""
    if value is None or value == "":
        return PLACEHOLDER
    return str(value)


# ── tables ────────────────────────────────────────────────────────────────
class Table:
    """A width-aware text table.

    Renders a header, an optional separator rule and rows, aligning each column
    by terminal width rather than character count.
    """

    def __init__(
        self,
        columns: Sequence[str],
        *,
        aligns: Sequence[str] | None = None,
        gap: str = "  ",
    ) -> None:
        if aligns is not None and len(aligns) != len(columns):
            raise ValueError("aligns must have one entry per column")
        self.columns = list(columns)
        self.aligns = list(aligns) if aligns else ["left"] * len(columns)
        self.gap = gap
        self._rows: list[list[str]] = []

    def add(self, *cells: Any) -> "Table":
        if len(cells) != len(self.columns):
            raise ValueError(
                f"expected {len(self.columns)} cells, got {len(cells)}"
            )
        self._rows.append(["" if c is None else str(c) for c in cells])
        return self

    def extend(self, rows: Iterable[Sequence[Any]]) -> "Table":
        for row in rows:
            self.add(*row)
        return self

    def __len__(self) -> int:
        return len(self._rows)

    def _widths(self, *, include_header: bool = True) -> list[int]:
        widths = [display_width(c) for c in self.columns]
        for row in self._rows:
            for index, cell in enumerate(row):
                widths[index] = max(widths[index], display_width(cell))
        return widths

    def render(self, *, rule: bool = True, header: bool = True) -> str:
        """Render to text. ``rule`` draws a dashed line under the header."""
        if not self._rows and not header:
            return ""
        widths = self._widths()
        lines: list[str] = []

        if header:
            lines.append(
                self.gap.join(
                    pad(c, w, align=a)
                    for c, w, a in zip(self.columns, widths, self.aligns)
                ).rstrip()
            )
            if rule:
                lines.append(
                    self.gap.join("-" * w for w in widths).rstrip()
                )

        for row in self._rows:
            lines.append(
                self.gap.join(
                    pad(c, w, align=a) for c, w, a in zip(row, widths, self.aligns)
                ).rstrip()
            )
        return "\n".join(lines)


def render_kv(pairs: Iterable[tuple[str, Any]], *, indent: str = "") -> str:
    """Two-column key/value block; keys are padded to a common width."""
    items = [(k, or_dash(v)) for k, v in pairs]
    if not items:
        return ""
    width = max(display_width(k) for k, _ in items)
    return "\n".join(f"{indent}{pad(k, width)} : {v}" for k, v in items)


def section(title: str) -> str:
    """A visual section heading used by the CLI sub-commands."""
    return f"== {title} =="


# ── JSON ──────────────────────────────────────────────────────────────────
def json_default(obj: Any) -> Any:
    """``json.dumps`` fallback for dataclasses, datetimes and tuples."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "as_dict") and callable(obj.as_dict):
        return obj.as_dict()
    if hasattr(obj, "__dataclass_fields__"):
        from dataclasses import asdict

        return asdict(obj)
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    return str(obj)


def to_json(payload: Any, *, pretty: bool = True) -> str:
    """Serialise to JSON.

    ``ensure_ascii=False`` keeps Chinese text readable; the CLI writes UTF-8.
    Dataclass fields marked ``repr=False`` (notably the raw virtual key) are
    dropped by :func:`_strip_private` before serialisation, and the result is
    passed through :func:`~opencsi.redaction.redact_mapping` as a second line of
    defence -- some commands (``logs``) emit records taken verbatim from the
    server, which this package does not model and therefore cannot vouch for.
    """
    from .redaction import redact_mapping  # local import: avoids a cycle

    scrubbed = redact_mapping(_strip_private(payload))
    return json.dumps(
        scrubbed,
        ensure_ascii=False,
        indent=2 if pretty else None,
        sort_keys=False,
        default=json_default,
    )


def _strip_private(value: Any, depth: int = 0) -> Any:
    """Recursively drop private fields so secrets cannot reach ``--json``.

    ``ToolGrant._virtual_key`` is the reason this exists: the CLI must be able
    to emit a full snapshot as JSON without ever including the raw key.
    """
    if depth > 12:
        return value
    if isinstance(value, Mapping):
        return {
            k: _strip_private(v, depth + 1)
            for k, v in value.items()
            if not (isinstance(k, str) and k.startswith("_"))
        }
    if isinstance(value, (list, tuple)):
        return [_strip_private(v, depth + 1) for v in value]
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import fields

        return {
            f.name: _strip_private(getattr(value, f.name), depth + 1)
            for f in fields(value)
            if not f.name.startswith("_")
        }
    return value
