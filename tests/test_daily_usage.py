"""Daily usage aggregation and tray presentation."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

import helpers  # noqa: F401

from opencsi.models import ModelPrice, MyToolsSnapshot, TokenTrendPoint
from opencsi.monitor import DailyModelUsage, DailyUsage, MonitorSnapshot, MonitorState, build_daily_usage
from opencsi.tray.presenter import actions_for, tooltip_for


class DailyUsageAggregationTest(unittest.TestCase):
    def test_groups_token_trend_by_model_and_prices_it_like_the_site(self) -> None:
        snap = MyToolsSnapshot(
            token_trend=(
                TokenTrendPoint(date="2026-09-24", request_type="A", tokens=2_000_000),
                TokenTrendPoint(date="2026-09-24", request_type="A", tokens=1_000_000),
                TokenTrendPoint(date="2026-09-24", request_type="B", tokens=4_000_000),
            )
        )
        prices = (
            ModelPrice(request_type="A", display_name="Model A", bill_type="TOKEN", enabled=1, blended_price=0.5),
            ModelPrice(request_type="B", display_name="Model B", bill_type="TOKEN", enabled=1, blended_price=1.0),
        )
        daily = build_daily_usage(snap, prices, date="2026-09-24")
        self.assertEqual(daily.total_tokens, 7_000_000)
        self.assertAlmostEqual(daily.total_cost or 0.0, 5.5)
        self.assertEqual([m.display_name for m in daily.models], ["Model B", "Model A"])
        self.assertEqual([m.tokens for m in daily.models], [4_000_000, 3_000_000])

    def test_unknown_price_is_not_rendered_as_zero_cost(self) -> None:
        snap = MyToolsSnapshot(
            token_trend=(TokenTrendPoint(date="2026-09-24", request_type="UNKNOWN", tokens=123),)
        )
        daily = build_daily_usage(snap, (), date="2026-09-24")
        self.assertIsNone(daily.total_cost)
        self.assertIsNone(daily.models[0].cost)


class DailyUsageTrayTest(unittest.TestCase):
    def _snapshot(self) -> MonitorSnapshot:
        return MonitorSnapshot(
            state=MonitorState.OK,
            total_tokens=99_000_000,
            requests=100,
            prs=1,
            fetched_at=datetime.now(timezone.utc),
            daily_usage=DailyUsage(
                date="2026-09-24",
                total_tokens=7_000_000,
                total_cost=5.5,
                models=(
                    DailyModelUsage("B", "Model B", 4_000_000, 4.0),
                    DailyModelUsage("A", "Model A", 3_000_000, 1.5),
                ),
            ),
        )

    def test_tooltip_prefers_today_usage(self) -> None:
        text = tooltip_for(self._snapshot())
        self.assertIn("今日", text)
        self.assertIn("700.0万 tokens", text)
        self.assertIn("¥5.50", text)

    def test_menu_contains_total_and_each_model(self) -> None:
        actions = actions_for(self._snapshot())
        labels = [a.label for a in actions]
        self.assertTrue(any("今日: 7,000,000 tokens / ¥5.50" in x for x in labels))
        self.assertTrue(any("Model B: 4,000,000 / ¥4.00" in x for x in labels))
        self.assertTrue(any("Model A: 3,000,000 / ¥1.50" in x for x in labels))


if __name__ == "__main__":
    unittest.main()
