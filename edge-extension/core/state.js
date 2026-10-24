// core/state.js — the snapshot the panel renders and the rules that keep it
// honest about what it knows.
//
// The states, and the rule each one guards:
//
// OK              the server answered and today's view is built on real rows.
// REFRESHING      a fetch is in flight; the last numbers stay on screen.
// RENEWING        a session recovery is in flight; ditto.
// LOGIN_REQUIRED  OpenCSI refused the credential and no recovery is running.
// CONSENT_REQUIRED recovery reached a page that needs a human to click.
// NOT_READY       the day has no trend rows *and* the server's own freshness
//                 stamp does not cover today — zero would be a lie.
// STALE           what is on screen is the last success's data, retained
//                 through a later failure, and labelled with its time.
// OFFLINE         the request never produced an HTTP answer.
// SERVER_ERROR    HTTP answered but said no: 429/5xx, or a body whose shape
//                 this code does not recognise. Never reported as a sign-out.
// COST_UNKNOWN    tokens are valid; the price table cannot price every model.
//
// An empty trend plus a freshness stamp that *does* cover today is the only
// case where "0" is claimed (OK with zero rows) — and even then the panel says
// the day is zero rather than just showing 0 silently.
(function (root) {
  "use strict";

  // Per-property fallbacks, not per-root: in Node each module is required
  // independently and earlier requires have already created root.OpenCSICore
  // without the later namespaces, so a root-only check would grab `undefined`.
  const models =
    root.OpenCSICore && root.OpenCSICore.models
      ? root.OpenCSICore.models
      : require("./models.js");
  const dateUtil =
    root.OpenCSICore && root.OpenCSICore.date
      ? root.OpenCSICore.date
      : require("./date.js");
  const aggregation =
    root.OpenCSICore && root.OpenCSICore.aggregation
      ? root.OpenCSICore.aggregation
      : require("./aggregation.js");

  const STATE = {
    OK: "OK",
    REFRESHING: "REFRESHING",
    RENEWING: "RENEWING",
    LOGIN_REQUIRED: "LOGIN_REQUIRED",
    CONSENT_REQUIRED: "CONSENT_REQUIRED",
    NOT_READY: "NOT_READY",
    STALE: "STALE",
    OFFLINE: "OFFLINE",
    SERVER_ERROR: "SERVER_ERROR",
    COST_UNKNOWN: "COST_UNKNOWN",
  };

  /** The classification of one HTTP/fetch outcome — never collapsed. */
  function classifyFetch(outcome) {
    if (!outcome) return STATE.OFFLINE;
    if (outcome.kind === "network") return STATE.OFFLINE;
    if (outcome.kind === "http") {
      const s = outcome.status;
      if (s === 401 || s === 403) return STATE.LOGIN_REQUIRED;
      if (s === 429) return STATE.SERVER_ERROR;
      if (s >= 500 && s < 600) return STATE.SERVER_ERROR;
      if (s >= 200 && s < 300) return STATE.OK;
      return STATE.SERVER_ERROR;
    }
    if (outcome.kind === "contract") return STATE.SERVER_ERROR;
    return STATE.SERVER_ERROR;
  }

  function emptySnapshot() {
    return {
      state: STATE.SERVER_ERROR,
      today: null,
      cumulative: null,
      fetchedAt: null,
      fetchedAtText: "--:--",
      dataFreshTime: null,
      statisticDate: null,
      lastError: null,
      recovery: null,
    };
  }

  /**
   * Build the snapshot the panel renders from a successful payload.
   * `today` is the Shanghai statistic date the fetch was scoped to.
   */
  function snapshotFromPayload(payload, today, nowMs) {
    const data = payload && payload.body && payload.body.data;
    const prices = models.coercePrices(payload.prices);
    const cumulative = models.summary(data);
    const daily = aggregation.buildDailyUsage(data, today, prices);
    const freshDate = dateUtil.shanghaiDateOf(models.dataFreshTime(data));

    let state = STATE.OK;
    let note = null;
    if (daily.totalTokens === 0 && daily.models.length === 0) {
      if (freshDate && freshDate >= today) {
        state = STATE.OK;
        note = "今日暂无用量（服务端数据已覆盖今日）";
      } else {
        state = STATE.NOT_READY;
        note = "服务端今日数据尚未同步，暂无可确认用量";
      }
    } else if (daily.totalCost === null && daily.totalTokens > 0) {
      state = STATE.COST_UNKNOWN;
      note = "部分模型无法按 Token 估价，费用不完整";
    }

    return {
      state,
      today: {
        date: today,
        tokens: daily.totalTokens,
        cost: daily.totalCost,
        models: daily.models,
        knownData: daily.knownData,
        note,
      },
      cumulative,
      fetchedAt: nowMs || Date.now(),
      fetchedAtText: "--:--", // rendered from fetchedAt in the panel's zone
      dataFreshTime: models.dataFreshTime(data),
      statisticDate: today,
      lastError: null,
      recovery: null,
    };
  }

  /**
   * Merge a failure into the last good snapshot: keep the numbers, mark them
   * stale, and carry the reason plus the recovery the user can act on.
   */
  function snapshotWithFailure(previous, state, reason, recovery, nowMs) {
    const base = previous ? { ...previous } : emptySnapshot();
    base.state = state;
    base.lastError = reason || null;
    base.recovery = recovery || null;
    base.fetchedAt = previous ? base.fetchedAt : null;
    if (previous && previous.today && previous.state === STATE.OK) {
      base.today = { ...previous.today, stale: true };
    } else if (previous && previous.today) {
      base.today = { ...previous.today, stale: previous.today.stale || state !== STATE.OK };
    }
    if (nowMs) base.failureAt = nowMs;
    return base;
  }

  /**
   * The transition after a renewal attempt ends. `outcome` is the auth
   * manager's verdict; the snapshot state follows it, not the other way.
   */
  function applyAuthOutcome(snapshot, outcome) {
    const base = { ...(snapshot || emptySnapshot()) };
    if (outcome === "VALID") {
      base.state = snapshot && snapshot.state !== STATE.OK ? STATE.REFRESHING : base.state;
      base.recovery = null;
      base.lastError = null;
    } else if (outcome === "LOGIN_REQUIRED" || outcome === "CONSENT_REQUIRED") {
      base.state = outcome;
      base.recovery =
        outcome === "LOGIN_REQUIRED"
          ? "点击「重新登录 OpenCSI」完成登录"
          : "在打开的授权页上点击一次批准";
    }
    return base;
  }

  const api = {
    STATE,
    classifyFetch,
    emptySnapshot,
    snapshotFromPayload,
    snapshotWithFailure,
    applyAuthOutcome,
  };
  root.OpenCSICore = root.OpenCSICore || {};
  root.OpenCSICore.state = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof self !== "undefined" ? self : globalThis);
