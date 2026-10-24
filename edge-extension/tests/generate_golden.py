#!/usr/bin/env python3
"""Generate the golden fixture the JS aggregation tests compare against.

Run from the repository root:

    python edge-extension/tests/generate_golden.py

The same fixture cases are fed to the Python implementation
(:func:`opencsi.aggregation.estimate_usage_cost` + :func:`aggregate_trend`,
composed exactly as ``monitor.service.build_daily_usage`` composes them), and
the output is written to ``edge-extension/tests/golden.json``. The Node test
suite then asserts the JavaScript port reproduces it number for number — which
is what "JS 与 Python 对同一测试数据给出相同结果" means operationally.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from opencsi.aggregation import aggregate_trend, estimate_usage_cost  # noqa: E402
from opencsi.models import coerce_trend  # noqa: E402
from opencsi.models import coerce_prices  # noqa: E402

PRICES = [
    {"requestType": "glm-5.3-flash", "displayName": "GLM-5.3-Flash", "billType": "TOKEN", "enabled": 1, "blendedPrice": 2.24},
    {"requestType": "deepseek-v4-flash", "displayName": "DeepSeek-V4-Flash", "billType": "TOKEN", "enabled": 1, "blendedPrice": 4.72},
    {"requestType": "qwen3-max", "displayName": "Qwen3-Max", "billType": "TOKEN", "enabled": 1, "blendedPrice": 0.28},
    {"requestType": "trae-subscription", "displayName": "Trae 包月", "billType": "FLAT", "enabled": 1, "monthlyFee": 49.0},
    {"requestType": "mystery-metered", "displayName": "Mystery", "billType": "METER", "enabled": 1},
    {"requestType": "unpriced-token", "displayName": "NoPrice", "billType": "TOKEN", "enabled": 1},
]

D = "2026-10-24"

CASES = {
    # 当日有真实用量：三个 TOKEN 模型 + 一个 FLAT + 一个未知计费。
    "normal_day": [
        {"date": D, "requestType": "glm-5.3-flash", "tokens": 41_000_000},
        {"date": D, "requestType": "deepseek-v4-flash", "tokens": 11_852_000},
        {"date": D, "requestType": "qwen3-max", "tokens": 2_180_000},
        {"date": D, "requestType": "trae-subscription", "tokens": 340_000},
        {"date": D, "requestType": "mystery-metered", "tokens": 12_000},
        # 昨天的行必须被日期过滤掉。
        {"date": "2026-10-23", "requestType": "glm-5.3-flash", "tokens": 999_999_999},
    ],
    # 今日确定为零：服务端返回了今天的行，但都是 0。
    "confirmed_zero": [
        {"date": D, "requestType": "glm-5.3-flash", "tokens": 0},
        {"date": D, "requestType": "qwen3-max", "tokens": 0},
    ],
    # 今日数据未同步：tokenTrend 里没有今天的行。
    "not_synced": [
        {"date": "2026-10-23", "requestType": "glm-5.3-flash", "tokens": 123},
    ],
    # 部分模型价格未知：混合计费与无价模型，总额必须不完整。
    "partial_price_unknown": [
        {"date": D, "requestType": "glm-5.3-flash", "tokens": 1_000_000},
        {"date": D, "requestType": "unpriced-token", "tokens": 500_000},
        {"date": D, "requestType": "unknown-model-xyz", "tokens": 250_000},
    ],
    # 累计超百亿属于 summary 层；这里验证大数当日聚合不丢精度。
    "huge_day": [
        {"date": D, "requestType": "glm-5.3-flash", "tokens": 10_299_999_999},
    ],
    # 空响应：build_daily_usage 的空集语义（0.0，不是 null）。
    "empty_trend": [],
}


def main() -> int:
    prices = coerce_prices(PRICES)
    out = {"prices": PRICES, "cases": {}}
    for name, trend in CASES.items():
        # The server applies startDate/endDate to tokenTrend before the client
        # sees it; the JS port re-filters defensively. Mirror the server here:
        # only today's rows enter the aggregation.
        scoped = [row for row in trend if row["date"] == D]
        by_type = aggregate_trend(
            coerce_trend(
                [
                    {
                        "date": row["date"],
                        "requestType": row["requestType"],
                        "tokens": row["tokens"],
                        "promptTokens": 0,
                        "completionTokens": 0,
                    }
                    for row in scoped
                ]
            ),
            by_model=True,
        )
        estimate = estimate_usage_cost(by_type, prices)
        # build_daily_usage presents the lines biggest-first — the sort the
        # product applies on top of estimate_usage_cost's own ordering.
        ordered = sorted(
            estimate.lines,
            key=lambda ln: (-ln.tokens, ln.display_name, ln.request_type),
        )
        models = [
            {
                "requestType": ln.request_type,
                "displayName": ln.display_name,
                "billType": ln.bill_type,
                "tokens": ln.tokens,
                "cost": ln.estimated_cost,
                "monthlyFee": ln.monthly_fee,
                "note": ln.note,
            }
            for ln in ordered
        ]
        total_tokens = sum(m["tokens"] for m in models)
        complete = all(m["cost"] is not None for m in models if m["tokens"] > 0)
        total_cost = estimate.token_cost if complete else (0.0 if not models else None)
        out["cases"][name] = {
            "trend": trend,
            "today": D,
            "result": {
                "date": D,
                "totalTokens": total_tokens,
                "totalCost": total_cost,
                "currency": "CNY",
                "models": models,
                "knownData": bool(scoped),
            },
        }

    target = Path(__file__).resolve().parent / "golden.json"
    target.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {target} with {len(out['cases'])} cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
