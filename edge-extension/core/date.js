// core/date.js — the company statistic day, in Asia/Shanghai, and freshness
// comparisons that never trust the browser's local zone.
//
// OpenCSI's own pages and the Python client both reason about the *company's*
// calendar day. The browser may sit in any timezone (and its clock may drift),
// so "today" is computed here from the Shanghai offset explicitly, and a
// server timestamp is converted into the same day before comparison.
(function (root) {
  "use strict";

  const TZ = "Asia/Shanghai";

  /** `YYYY-MM-DD` for the given instant in Shanghai; "now" by default. */
  function shanghaiToday(now) {
    const d = now instanceof Date ? now : new Date();
    return new Intl.DateTimeFormat("en-CA", {
      timeZone: TZ,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).format(d);
  }

  /** `HH:MM` in Shanghai for a server timestamp or Date. */
  function shanghaiClock(value) {
    if (!value) return "--:--";
    const d = value instanceof Date ? value : new Date(value);
    if (isNaN(d.getTime())) return "--:--";
    return new Intl.DateTimeFormat("en-GB", {
      timeZone: TZ,
      hour: "2-digit",
      minute: "2-digit",
    }).format(d);
  }

  /** The Shanghai calendar date of a server ISO timestamp, or null. */
  function shanghaiDateOf(value) {
    if (!value || typeof value !== "string") return null;
    const d = new Date(value);
    if (isNaN(d.getTime())) return null;
    return shanghaiToday(d);
  }

  /** Epoch seconds for a server ISO timestamp, or null when unparseable. */
  function epochOf(value) {
    if (!value) return null;
    const d = value instanceof Date ? value : new Date(value);
    return isNaN(d.getTime()) ? null : d.getTime() / 1000;
  }

  const api = { TZ, shanghaiToday, shanghaiClock, shanghaiDateOf, epochOf };
  root.OpenCSICore = root.OpenCSICore || {};
  root.OpenCSICore.date = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof self !== "undefined" ? self : globalThis);
