// core/models.js — coerce raw OpenCSI JSON into the shapes the rest of the
// extension reads, mirroring src/opencsi/models.py field for field.
//
// Malformed rows are skipped, never defaulted: a trend point without a date
// must not silently become "0 tokens on the empty date". The one deliberate
// default is a *missing number* in a price row — absent blendedPrice/monthlyFee
// is `null`, which aggregation renders as "--", not ¥0.
(function (root) {
  "use strict";

  function asStr(v) {
    return typeof v === "string" ? v : "";
  }

  function asInt(v) {
    if (typeof v === "number" && Number.isFinite(v)) return Math.trunc(v);
    if (typeof v === "string" && /^-?\d+$/.test(v.trim())) return parseInt(v, 10);
    return 0;
  }

  function asOptFloat(v) {
    if (typeof v === "number" && Number.isFinite(v)) return v;
    if (typeof v === "string" && v.trim() !== "" && Number.isFinite(Number(v))) {
      return Number(v);
    }
    return null;
  }

  function asOptStr(v) {
    return typeof v === "string" && v ? v : null;
  }

  /** One data.tokenTrend point: a model's tokens on a date. */
  function trendPoint(raw) {
    raw = raw || {};
    return {
      date: asStr(raw.date),
      requestType: asStr(raw.requestType),
      tokens: asInt(raw.tokens),
      promptTokens: asInt(raw.promptTokens),
      completionTokens: asInt(raw.completionTokens),
    };
  }

  function coerceTrend(items) {
    const out = [];
    if (!Array.isArray(items)) return out;
    for (const item of items) {
      if (item && typeof item === "object") out.push(trendPoint(item));
    }
    return out;
  }

  /** One ai/config/cost row. */
  function modelPrice(raw) {
    raw = raw || {};
    return {
      requestType: asStr(raw.requestType),
      displayName: asStr(raw.displayName),
      billType: asStr(raw.billType),
      enabled: asInt(raw.enabled),
      blendedPrice: asOptFloat(raw.blendedPrice),
      monthlyFee: asOptFloat(raw.monthlyFee),
    };
  }

  function coercePrices(items) {
    const out = [];
    if (!Array.isArray(items)) return out;
    for (const item of items) {
      if (item && typeof item === "object") out.push(modelPrice(item));
    }
    return out;
  }

  /** The account-wide aggregate, read from data.tokenSummary only. */
  function summary(data) {
    const raw = data && typeof data === "object" ? data.tokenSummary : null;
    const s = raw && typeof raw === "object" ? raw : {};
    return {
      totalTokens: asInt(s.totalTokens),
      totalRequestCount: asInt(s.totalRequestCount),
    };
  }

  /** data.syncStatus.dataFreshTime — the server's own freshness stamp. */
  function dataFreshTime(data) {
    const s = data && typeof data === "object" ? data.syncStatus : null;
    const t = s && typeof s === "object" ? s.dataFreshTime : null;
    return asOptStr(t);
  }

  const api = {
    asStr,
    asInt,
    asOptFloat,
    asOptStr,
    trendPoint,
    coerceTrend,
    modelPrice,
    coercePrices,
    summary,
    dataFreshTime,
  };

  root.OpenCSICore = root.OpenCSICore || {};
  root.OpenCSICore.models = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof self !== "undefined" ? self : globalThis);
