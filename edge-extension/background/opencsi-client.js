// background/opencsi-client.js — the read-only OpenCSI API client for the
// service worker. Cookie auth only: every request goes out with
// `credentials: "include"` so the Edge profile's own opencsitool.com cookie is
// used, and nothing about the cookie is ever read, copied or stored.
//
// Outcomes are classified, never collapsed: the caller must be able to tell
// "the network broke" from "the server said no" from "the cookie was refused",
// because the remedies differ and treating a 500 as a sign-out would send the
// user to re-login for nothing.
(function (root) {
  "use strict";

  const BASE_URL = "https://opencsitool.com";
  const PATH_USAGE = "/opencsitool/rest/v1/ai/operations/personalQueueStatus";
  const PATH_PRICES = "/opencsitool/rest/v1/ai/config/cost";
  const DEFAULT_TIMEOUT_MS = 20000;

  /**
   * One GET with the profile's cookies. Returns a classified outcome:
   *   {kind:"http", status, body}      — an HTTP answer (body parsed only on 2xx)
   *   {kind:"contract", status, reason} — 2xx but not the shape this code reads
   *   {kind:"network", status:0, reason} — no HTTP answer at all, or timeout
   * No credential value ever appears in a reason: these strings can reach the UI.
   */
  async function fetchJson(path, { params, timeoutMs } = {}) {
    const url = new URL(BASE_URL + path);
    for (const [key, value] of Object.entries(params || {})) {
      url.searchParams.set(key, value);
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs || DEFAULT_TIMEOUT_MS);
    try {
      const response = await fetch(url.toString(), {
        method: "GET",
        credentials: "include",
        cache: "no-store",
        headers: { Accept: "application/json, text/plain, */*" },
        signal: controller.signal,
      });
      if (!response.ok) {
        return { kind: "http", status: response.status, body: null };
      }
      let body;
      try {
        body = await response.json();
      } catch {
        return {
          kind: "contract",
          status: response.status,
          reason: "响应不是 JSON（可能命中了站点外壳页）",
        };
      }
      return { kind: "http", status: response.status, body };
    } catch (error) {
      const name = (error && error.name) || "Error";
      return {
        kind: "network",
        status: 0,
        reason: name === "AbortError" ? "请求超时" : "网络不可达",
      };
    } finally {
      clearTimeout(timer);
    }
  }

  /** The dated account payload. Dates filter tokenTrend only (established). */
  async function fetchUsage(today) {
    const outcome = await fetchJson(PATH_USAGE, {
      params: { startDate: today, endDate: today },
    });
    if (outcome.kind === "http" && outcome.status === 200) {
      const data = outcome.body && outcome.body.data;
      if (!data || typeof data !== "object" || !Array.isArray(data.tokenTrend)) {
        return {
          kind: "contract",
          status: 200,
          reason: "personalQueueStatus 响应缺少 data.tokenTrend，接口结构可能已变化",
        };
      }
    }
    return outcome;
  }

  /** The model price table. Failure here degrades to COST_UNKNOWN, never a crash. */
  async function fetchPrices() {
    const outcome = await fetchJson(PATH_PRICES);
    if (outcome.kind === "http" && outcome.status === 200) {
      if (!Array.isArray(outcome.body)) {
        return { kind: "contract", status: 200, reason: "价格表响应不是数组" };
      }
    }
    return outcome;
  }

  const api = { BASE_URL, PATH_USAGE, PATH_PRICES, fetchJson, fetchUsage, fetchPrices };
  root.OpenCSIBackground = root.OpenCSIBackground || {};
  root.OpenCSIBackground.client = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof self !== "undefined" ? self : globalThis);
