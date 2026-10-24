// core/aggregation.js — the Python src/opencsi/aggregation.py semantics,
// ported line for line and verified against it by tests on shared fixtures.
//
// The rule that must never drift: TOKEN rows cost tokens / 1e6 * blendedPrice;
// FLAT rows keep their tokens but their cost is `null` (a monthly fee is never
// prorated into a day); an unknown model or bill type costs `null` — never 0.
// A day whose costs are not fully known has totalCost `null`, and the UI must
// not present a partial sum as the whole bill.
(function (root) {
  "use strict";

  const models = root.OpenCSICore ? root.OpenCSICore.models : require("./models.js");
  const TOKENS_PER_UNIT = 1_000_000;

  /** Sum trend tokens grouped by requestType — aggregation.aggregate_trend(by_model=True). */
  function aggregateTrendByModel(points) {
    const out = new Map();
    for (const p of points) {
      out.set(p.requestType, (out.get(p.requestType) || 0) + p.tokens);
    }
    return out;
  }

  /**
   * estimate_usage_cost: the per-model spend lines for a requestType→tokens map.
   * Returns lines sorted by tokens desc (the panel's default), each with
   * `cost: number | null` and `note` for the exceptional rows.
   */
  function estimateUsageCost(tokensByType, prices) {
    const byType = new Map(prices.map((p) => [p.requestType, p]));
    const lines = [];
    for (const requestType of [...tokensByType.keys()].sort()) {
      const tokens = tokensByType.get(requestType) || 0;
      const price = byType.get(requestType);
      if (!price) {
        lines.push({
          requestType,
          displayName: requestType,
          billType: "UNKNOWN",
          tokens,
          cost: null,
          monthlyFee: null,
          note: "no price configured for this request type",
        });
        continue;
      }
      const isToken = (price.billType || "").toUpperCase() === "TOKEN";
      const isFlat = (price.billType || "").toUpperCase() === "FLAT";
      if (isToken) {
        const unit = price.blendedPrice;
        if (unit === null || unit === undefined) {
          lines.push({
            requestType,
            displayName: price.displayName || requestType,
            billType: price.billType,
            tokens,
            cost: null,
            monthlyFee: null,
            note: "token-billed but no blended price configured",
          });
        } else {
          lines.push({
            requestType,
            displayName: price.displayName || requestType,
            billType: price.billType,
            tokens,
            cost: (tokens / TOKENS_PER_UNIT) * unit,
            monthlyFee: null,
            note: null,
          });
        }
      } else if (isFlat) {
        lines.push({
          requestType,
          displayName: price.displayName || requestType,
          billType: price.billType,
          tokens,
          cost: null,
          monthlyFee: price.monthlyFee,
          note: "flat monthly fee; not billed per token",
        });
      } else {
        lines.push({
          requestType,
          displayName: price.displayName || requestType,
          billType: price.billType || "UNKNOWN",
          tokens,
          cost: null,
          monthlyFee: price.monthlyFee,
          // The Python note, quoted identically so a UI can translate it and a
          // test can diff the two implementations line for line.
          note: `unsupported bill type '${price.billType}'`,
        });
      }
    }
    // The panel presents biggest-first; ties break by display name, matching
    // the tray's ordering rule.
    lines.sort(
      (a, b) =>
        b.tokens - a.tokens ||
        (a.displayName < b.displayName ? -1 : a.displayName > b.displayName ? 1 : 0)
    );
    return lines;
  }

  /**
   * Build today's usage view from a dated personalQueueStatus payload.
   *
   * `complete` is false — and totalCost null — whenever any model with tokens
   * has no computable cost, mirroring build_daily_usage: a partially known
   * bill is never presented as the total.
   */
  function buildDailyUsage(data, today, prices) {
    const trend = models.coerceTrend(data && data.tokenTrend);
    const todayPoints = today ? trend.filter((p) => p.date === today) : trend;
    const byType = aggregateTrendByModel(todayPoints);
    const lines = estimateUsageCost(byType, prices || []);
    let totalTokens = 0;
    // Python's completeness rule, verbatim: a model with tokens and no
    // computable cost makes the bill incomplete; an empty model set is
    // vacuously complete and totals 0.0 — not null, not "--".
    let complete = true;
    for (const line of lines) {
      totalTokens += line.tokens;
      if (line.tokens > 0 && line.cost === null) complete = false;
    }
    const totalCost = complete
      ? lines.reduce((acc, l) => acc + (l.cost || 0), 0)
      : null;
    return {
      date: today || null,
      totalTokens,
      totalCost,
      currency: "CNY",
      models: lines,
      knownData: todayPoints.length > 0,
    };
  }

  const api = { TOKENS_PER_UNIT, aggregateTrendByModel, estimateUsageCost, buildDailyUsage };
  root.OpenCSICore = root.OpenCSICore || {};
  root.OpenCSICore.aggregation = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof self !== "undefined" ? self : globalThis);
