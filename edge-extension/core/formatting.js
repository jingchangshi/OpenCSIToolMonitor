// core/formatting.js — number and cost rendering for the panel.
//
// Two rules travel with the numbers wherever they go: a missing value is "--"
// and never "0"; a cost is always labelled 估算 by the caller. Magnitudes use
// 中文 units so the headline numbers fit the panel at a glance, with the full
// digit string one hover away.
(function (root) {
  "use strict";

  function cnTokens(value) {
    if (value === null || value === undefined || !Number.isFinite(value)) return "--";
    const n = Math.trunc(value);
    if (Math.abs(n) >= 1e8) return (n / 1e8).toLocaleString("en-US", { maximumFractionDigits: 2 }) + " 亿";
    if (Math.abs(n) >= 1e4) return (n / 1e4).toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 }) + " 万";
    return n.toLocaleString("en-US");
  }

  function fullNumber(value) {
    if (value === null || value === undefined || !Number.isFinite(value)) return null;
    return Math.trunc(value).toLocaleString("en-US");
  }

  function cost(value, currency) {
    if (value === null || value === undefined || !Number.isFinite(value)) return "--";
    const prefix = (currency || "CNY").toUpperCase() === "CNY" ? "¥" : currency + " ";
    return prefix + value.toLocaleString("en-US", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }

  const api = { cnTokens, fullNumber, cost };
  root.OpenCSICore = root.OpenCSICore || {};
  root.OpenCSICore.formatting = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof self !== "undefined" ? self : globalThis);
