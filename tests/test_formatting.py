"""Output formatting: CJK-aware width, site number rules, JSON safety.

Two things here are easy to get wrong and both are user-visible:

* **Column alignment.** ``使用中`` is four characters but six terminal columns.
  Padding by ``len()`` misaligns every table that contains CJK text, which is
  most of them.
* **Number scaling.** The site renders ``3061130999`` as ``30.6亿``. Reproducing
  that rule exactly is what makes the CLI's output recognisable to someone who
  has seen the web page.
"""

from __future__ import annotations

import json
import unittest

from helpers import make_client

from opencsi.formatting import (
    PLACEHOLDER,
    Table,
    active_text,
    display_width,
    format_count,
    format_datetime,
    format_int,
    format_money,
    format_percent,
    format_price,
    format_ratio,
    format_relative_seconds,
    format_server_time,
    or_dash,
    pad,
    render_kv,
    section,
    to_json,
    yes_no,
)


class DisplayWidthTest(unittest.TestCase):
    """East Asian wide characters occupy two columns."""

    def test_ascii_is_one_column_each(self) -> None:
        self.assertEqual(display_width("abc"), 3)

    def test_chinese_is_two_columns_each(self) -> None:
        self.assertEqual(display_width("使用中"), 6)

    def test_mixed_text(self) -> None:
        # "API_BUNDLE" is 10 columns, the space is 1, "使用中" is 6.
        self.assertEqual(display_width("API_BUNDLE 使用中"), 17)

    def test_fullwidth_punctuation_counts_as_two(self) -> None:
        self.assertEqual(display_width("（测试）"), 8)

    def test_combining_marks_add_nothing(self) -> None:
        # "e" + combining acute: two code points, one column.
        self.assertEqual(display_width("e\u0301"), 1)

    def test_empty_string(self) -> None:
        self.assertEqual(display_width(""), 0)

    def test_len_is_not_width_for_cjk(self) -> None:
        """The bug this module exists to prevent."""
        text = "已失效"
        self.assertEqual(len(text), 3)
        self.assertEqual(display_width(text), 6)


class PadTest(unittest.TestCase):
    def test_pads_ascii_to_width(self) -> None:
        self.assertEqual(pad("ab", 5), "ab   ")

    def test_pads_cjk_by_display_width(self) -> None:
        # 6 columns of content, target 8 -> two spaces, not five.
        self.assertEqual(pad("使用中", 8), "使用中  ")

    def test_right_alignment(self) -> None:
        self.assertEqual(pad("ab", 5, align="right"), "   ab")

    def test_centre_alignment(self) -> None:
        self.assertEqual(pad("ab", 6, align="center"), "  ab  ")

    def test_text_wider_than_the_target_is_returned_unchanged(self) -> None:
        self.assertEqual(pad("abcdef", 3), "abcdef")
        self.assertEqual(pad("使用中", 4), "使用中")

    def test_padded_width_is_exactly_the_target(self) -> None:
        for text, width in [("ab", 6), ("使用中", 10), ("a中b", 9)]:
            self.assertEqual(display_width(pad(text, width)), width, text)


class CountFormattingTest(unittest.TestCase):
    """The site's 亿/万 rule, verified against the live page."""

    def test_yi_scale(self) -> None:
        self.assertEqual(format_count(3_061_130_999), "30.6亿")
        self.assertEqual(format_count(2_800_206_464), "28.0亿")
        self.assertEqual(format_count(260_924_535), "2.6亿")

    def test_wan_scale(self) -> None:
        self.assertEqual(format_count(21_632), "2.2万")
        self.assertEqual(format_count(31_167), "3.1万")

    def test_below_wan_is_plain(self) -> None:
        self.assertEqual(format_count(9_999), "9999")
        self.assertEqual(format_count(3_150), "3150")
        self.assertEqual(format_count(0), "0")

    def test_exact_boundaries(self) -> None:
        self.assertEqual(format_count(10_000), "1.0万")
        self.assertEqual(format_count(100_000_000), "1.0亿")

    def test_just_below_boundaries(self) -> None:
        self.assertEqual(format_count(9_999), "9999")
        self.assertEqual(format_count(99_999_999), "10000.0万")

    def test_negative_values_keep_the_sign(self) -> None:
        self.assertEqual(format_count(-21_632), "-2.2万")

    def test_none_becomes_the_placeholder(self) -> None:
        self.assertEqual(format_count(None), PLACEHOLDER)

    def test_one_decimal_place_always(self) -> None:
        self.assertEqual(format_count(10_000), "1.0万")
        self.assertEqual(format_count(15_000), "1.5万")


class ScalarFormattingTest(unittest.TestCase):
    def test_format_int_is_thousands_separated(self) -> None:
        """Exact integers get separators; ``format_count`` does the 亿/万 work."""
        self.assertEqual(format_int(21_632), "21,632")
        self.assertEqual(format_int(0), "0")
        self.assertEqual(format_int(None), PLACEHOLDER)

    def test_format_percent(self) -> None:
        self.assertEqual(format_percent(0.038), "3.8%")
        self.assertEqual(format_percent(0.0), "0.0%")
        self.assertEqual(format_percent(None), PLACEHOLDER)

    def test_format_percent_digit_override(self) -> None:
        self.assertEqual(format_percent(0.03809, digits=2), "3.81%")

    def test_format_money_includes_the_currency_symbol(self) -> None:
        self.assertEqual(format_money(200.0), "¥200.00")
        self.assertEqual(format_money(1234.5), "¥1,234.50")
        self.assertEqual(format_money(0), "¥0.00")
        self.assertEqual(format_money(None), PLACEHOLDER)

    def test_format_money_currency_override(self) -> None:
        self.assertEqual(format_money(1.5, currency="USD"), "$1.50")

    def test_format_price_trims_trailing_zeros(self) -> None:
        """A per-token price reads better as ``0.28`` than ``0.2800``."""
        self.assertEqual(format_price(0.28), "0.28")
        self.assertEqual(format_price(0.5), "0.5")
        self.assertEqual(format_price(1.0), "1")
        self.assertEqual(format_price(0.0001), "0.0001")
        self.assertEqual(format_price(0), "0")
        self.assertEqual(format_price(None), PLACEHOLDER)

    def test_format_ratio_handles_a_zero_denominator(self) -> None:
        self.assertEqual(format_ratio(120, 3150), "3.8%")
        self.assertEqual(format_ratio(1, 0), PLACEHOLDER)

    def test_yes_no(self) -> None:
        self.assertEqual(yes_no(True), "yes")
        self.assertEqual(yes_no(False), "no")

    def test_active_text_matches_the_site(self) -> None:
        self.assertEqual(active_text(True), "使用中")
        self.assertEqual(active_text(False), "已失效")

    def test_or_dash(self) -> None:
        self.assertEqual(or_dash("x"), "x")
        self.assertEqual(or_dash(""), PLACEHOLDER)
        self.assertEqual(or_dash(None), PLACEHOLDER)
        self.assertEqual(or_dash(0), "0")

    def test_format_datetime_tolerates_junk(self) -> None:
        self.assertEqual(format_datetime(None), PLACEHOLDER)
        self.assertEqual(format_datetime(""), PLACEHOLDER)
        # A string the server already formatted is passed through untouched.
        self.assertEqual(format_datetime("not a date"), "not a date")
        self.assertEqual(format_datetime("2026-08-17 16:28:00"), "2026-08-17 16:28:00")

    def test_format_datetime_accepts_a_datetime_object(self) -> None:
        from datetime import datetime

        value = datetime(2026, 8, 17, 16, 28, 0)
        self.assertEqual(format_datetime(value), "2026-08-17 16:28:00")

    def test_format_server_time_keeps_the_offset_it_was_given(self) -> None:
        """The server's wall time is reported, not a guessed conversion.

        The service sends ISO-8601 with an offset. Converting to the viewer's
        local time would assume which clock the user cares about, and hardcoding
        UTC+8 would be wrong the moment the server moves (objective §58).
        """
        self.assertEqual(format_server_time("2026-09-19T21:40:27+08:00"), "2026-09-19 21:40 UTC+8")
        self.assertEqual(format_server_time("2026-09-19T13:40:27+00:00"), "2026-09-19 13:40 UTC+0")
        self.assertEqual(format_server_time("2026-09-19T21:40:27Z"), "2026-09-19 21:40 UTC+0")
        # A half-hour offset must not be truncated to whole hours.
        self.assertEqual(format_server_time("2026-09-19T21:40:27+05:30"), "2026-09-19 21:40 UTC+5:30")

    def test_format_server_time_does_not_reinterpret_a_plain_timestamp(self) -> None:
        """A naive timestamp carries no offset, so none is invented."""
        self.assertEqual(format_server_time("2026-08-17 16:28:00"), "2026-08-17 16:28")
        self.assertEqual(format_server_time("not a date"), "not a date")
        self.assertEqual(format_server_time(None), PLACEHOLDER)
        self.assertEqual(format_server_time(""), PLACEHOLDER)

    def test_format_relative_seconds(self) -> None:
        self.assertEqual(format_relative_seconds(0), "0s")
        self.assertEqual(format_relative_seconds(45), "45s")
        self.assertEqual(format_relative_seconds(90), "1m30s")
        # Hours drop the seconds for compactness; minutes keep two digits.
        self.assertEqual(format_relative_seconds(3661), "1h01m")
        self.assertEqual(format_relative_seconds(-5), "expired")
        self.assertEqual(format_relative_seconds(None), PLACEHOLDER)


class TableTest(unittest.TestCase):
    """Tables must line up, including with CJK content.

    ``render`` right-strips each line, so trailing padding is absent and lines
    are *not* of equal display width. Alignment is therefore asserted on the
    display column at which each cell's text begins -- which is the property
    that actually matters visually -- rather than by comparing line lengths.
    """

    @staticmethod
    def _cell_offsets(line: str, cells: list[str]) -> list[int]:
        """Display-column offset of each cell's text within a rendered line."""
        offsets: list[int] = []
        pos = 0
        for cell in cells:
            index = line.index(cell, pos)
            offsets.append(display_width(line[:index]))
            pos = index + len(cell)
        return offsets

    def _data_lines(self, text: str, header: str) -> list[str]:
        """Rendered data rows: no header, no dashed rule."""
        out: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if line.startswith(header):
                continue
            if set(stripped) <= {"-", " "}:  # the rule row
                continue
            out.append(line)
        return out

    def test_simple_table(self) -> None:
        table = Table(["ID", "NAME"])
        table.add("1", "alpha")
        table.add("2", "b")
        text = table.render()
        self.assertIn("ID", text.splitlines()[0])
        self.assertIn("NAME", text.splitlines()[0])
        lines = self._data_lines(text, "ID")
        self.assertEqual(len(lines), 2, text)
        # Column NAME starts at the same display column on both rows.
        offsets = [
            self._cell_offsets(lines[0], ["1", "alpha"])[1],
            self._cell_offsets(lines[1], ["2", "b"])[1],
        ]
        self.assertEqual(len(set(offsets)), 1, text)

    def test_cjk_rows_align(self) -> None:
        """The regression this module exists for.

        ``使用中`` and ``已失效`` are 3 characters but 6 display columns. With an
        ``ok`` row in the mix, a ``len()``-based pad would place the second
        column at different offsets per row.
        """
        table = Table(["STATUS", "ID"])
        table.add("使用中", "5593")
        table.add("已失效", "1094")
        table.add("ok", "1")
        text = table.render()
        lines = self._data_lines(text, "STATUS")
        self.assertEqual(len(lines), 3, text)

        offsets = [
            self._cell_offsets(lines[0], ["使用中", "5593"])[1],
            self._cell_offsets(lines[1], ["已失效", "1094"])[1],
            self._cell_offsets(lines[2], ["ok", "1"])[1],
        ]
        self.assertEqual(len(set(offsets)), 1, f"{text}\noffsets={offsets}")

    def test_cjk_alignment_differs_from_naive_padding(self) -> None:
        """Prove the width logic earns its keep: ``len()`` would misalign."""
        table = Table(["A", "B"])
        table.add("使用中", "1")
        table.add("ab", "2")
        lines = self._data_lines(table.render(), "A")

        # The width-aware renderer puts column B at one display offset...
        correct = {self._cell_offsets(ln, [c, d])[1]
                   for ln, c, d in [(lines[0], "使用中", "1"), (lines[1], "ab", "2")]}
        self.assertEqual(len(correct), 1, table.render())

        # ...whereas measuring by character count would not, because the CJK
        # cell is 3 characters but 6 columns wide.
        naive = {
            len(ln[: ln.index(d, len(c))]) for ln, c, d in
            [(lines[0], "使用中", "1"), (lines[1], "ab", "2")]
        }
        self.assertNotEqual(len(naive), 1, "the test data does not exercise CJK width")

    def test_empty_table_renders_a_header_only(self) -> None:
        table = Table(["A", "B"])
        text = table.render()
        self.assertIn("A", text)
        self.assertIn("B", text)

    def test_alignment_is_respected(self) -> None:
        table = Table(["L", "R"], aligns=("left", "right"))
        table.add("a", "1")
        table.add("bb", "22")
        lines = self._data_lines(table.render(), "L")
        # Right-aligned column: every row ends at the same display column.
        ends = [display_width(ln) for ln in lines]
        self.assertEqual(len(set(ends)), 1, table.render())

    def test_extend_adds_many_rows(self) -> None:
        table = Table(["A"])
        table.extend([["1"], ["2"], ["3"]])
        self.assertEqual(len(table), 3)

    def test_add_rejects_the_wrong_cell_count(self) -> None:
        table = Table(["A", "B"])
        with self.assertRaises(ValueError):
            table.add("only-one")

    def test_aligns_must_match_the_column_count(self) -> None:
        with self.assertRaises(ValueError):
            Table(["A", "B"], aligns=("left",))

    def test_rule_can_be_disabled(self) -> None:
        table = Table(["A"])
        table.add("1")
        self.assertNotIn("---", table.render(rule=False))

    def test_header_can_be_disabled(self) -> None:
        table = Table(["HEADERCOL"])
        table.add("1")
        self.assertNotIn("HEADERCOL", table.render(header=False))

    def test_none_cells_render_as_empty_not_the_string_none(self) -> None:
        table = Table(["A", "B"])
        table.add(None, "x")
        text = table.render()
        self.assertNotIn("None", text)
        self.assertIn("x", text)


class KeyValueTest(unittest.TestCase):
    def test_render_kv_aligns_values(self) -> None:
        text = render_kv([("Employee ID", "653124"), ("Organization", "体验项目")])
        self.assertIn("Employee ID", text)
        self.assertIn("653124", text)
        self.assertIn("体验项目", text)

    def test_render_kv_separator_column_is_aligned(self) -> None:
        text = render_kv([("A", "1"), ("LONGKEY", "2")])
        positions = {ln.index(":") for ln in text.splitlines() if ln}
        # Keys are padded by display width, so the colon lands in one column
        # for ASCII keys.
        self.assertEqual(len(positions), 1, text)

    def test_render_kv_with_cjk_keys(self) -> None:
        text = render_kv([("令牌", "30.6亿"), ("请求", "2.2万")])
        lines = [ln for ln in text.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 2)

    def test_render_kv_renders_missing_values_as_dash(self) -> None:
        self.assertIn(PLACEHOLDER, render_kv([("K", None)]))

    def test_render_kv_of_nothing_is_empty(self) -> None:
        self.assertEqual(render_kv([]), "")

    def test_section_wraps_a_title(self) -> None:
        self.assertEqual(section("My Tools"), "== My Tools ==")


class JsonOutputTest(unittest.TestCase):
    """``to_json`` must be valid, readable, and free of private fields."""

    def test_serialises_a_snapshot(self) -> None:
        client, _, _ = make_client()
        payload = to_json(client.get_my_tools())
        parsed = json.loads(payload)
        self.assertIsInstance(parsed, dict)

    def test_private_fields_are_dropped(self) -> None:
        client, _, _ = make_client()
        payload = to_json(client.get_my_tools())
        self.assertNotIn("_virtual_key", payload)
        self.assertNotIn("EXAMPLE00000000", payload)

    def test_non_ascii_is_preserved_not_escaped(self) -> None:
        """The site's own labels should stay readable in JSON output."""
        payload = to_json({"status": "使用中"})
        self.assertIn("使用中", payload)

    def test_datetimes_are_serialisable(self) -> None:
        client, _, _ = make_client()
        payload = to_json(client.get_my_tools())
        parsed = json.loads(payload)
        self.assertTrue(parsed)

    def test_round_trips_a_list(self) -> None:
        payload = to_json([{"a": 1}, {"b": 2}])
        self.assertEqual(json.loads(payload), [{"a": 1}, {"b": 2}])

    def test_nested_dataclasses_serialise(self) -> None:
        client, _, _ = make_client()
        prices = client.get_model_prices()
        parsed = json.loads(to_json(prices))
        self.assertEqual(len(parsed), len(prices))


class SnapshotRenderingTest(unittest.TestCase):
    """End-to-end: the values the CLI prints match the verified baseline."""

    def test_summary_numbers_render_as_the_site_shows_them(self) -> None:
        from opencsi.aggregation import summarise

        client, _, _ = make_client()
        summary = summarise(client.get_my_tools())
        self.assertEqual(format_count(summary.total_tokens), "30.6亿")
        self.assertEqual(format_count(summary.total_request_count), "2.2万")
        self.assertEqual(format_count(summary.added_lines_count), "3.1万")
        self.assertEqual(format_percent(summary.adoption_rate), "3.8%")

    def test_tool_status_renders_in_chinese(self) -> None:
        client, _, _ = make_client()
        snapshot = client.get_my_tools()
        rendered = {g.id: active_text(g.is_active) for g in snapshot.grants}
        self.assertEqual(rendered[5593], "使用中")
        self.assertEqual(rendered[1954], "使用中")
        self.assertEqual(rendered[1094], "已失效")


if __name__ == "__main__":
    unittest.main(verbosity=2)
