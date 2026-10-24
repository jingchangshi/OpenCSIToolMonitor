// background/service-worker.js — the event-driven coordinator.
//
// MV3 service workers die when idle; nothing here may rely on staying alive.
// All state that survives a restart lives in chrome.storage.session (the
// redacted snapshot) or chrome.storage.local (UI settings), and every cache
// decision is made from the stored timestamps, not from module globals.
//
// Pages drive the work: a Magpie page asks for a snapshot when it opens or
// becomes visible, and again every five minutes while it stays visible. No
// alarms, no background polling after the last Magpie page closes.
"use strict";

// In a classic service worker the core files are loaded via importScripts()
// (declared below by this file's importScripts call at the top). The UMD
// guards in core/*.js attach them to self.OpenCSICore.
importScripts(
  "../core/models.js",
  "../core/aggregation.js",
  "../core/date.js",
  "../core/state.js",
  "../core/formatting.js",
  "opencsi-client.js",
  "auth-manager.js"
);

const OpenCSICore = self.OpenCSICore;
const BG = self.OpenCSIBackground;

const REFRESH_INTERVAL_MS = 5 * 60 * 1000;
const SNAPSHOT_KEY = "opencsiSnapshot";

const client = BG.client;
const auth = new BG.auth.AuthManager({
  tabs: chrome.tabs,
  log: (msg) => console.info(`[opencsi-auth] ${msg}`),
});

let inflight = null; // single-flight refresh across concurrent page asks

// ── snapshot storage (survives service-worker restarts) ────────────────────
async function readSnapshot() {
  try {
    const stored = await chrome.storage.session.get(SNAPSHOT_KEY);
    return stored[SNAPSHOT_KEY] || null;
  } catch {
    return null;
  }
}

async function writeSnapshot(snapshot) {
  try {
    await chrome.storage.session.set({ [SNAPSHOT_KEY]: snapshot });
  } catch {
    // A storage failure must not crash the worker; the next refresh retries.
  }
}

// ── the refresh flow ───────────────────────────────────────────────────────
/**
 * Get the current snapshot, refreshing when the cache is out of date or
 * `force` (the manual button). Single-flight: concurrent callers share one
 * refresh instead of hammering the API from several Magpie tabs.
 */
function getSnapshot({ force = false } = {}) {
  if (inflight) return inflight;
  inflight = (async () => {
    const now = Date.now();
    const today = OpenCSICore.date.shanghaiToday();
    let cached = await readSnapshot();

    // A new Shanghai day invalidates yesterday's "today" card immediately.
    const cacheValid =
      cached &&
      cached.fetchedAt &&
      cached.statisticDate === today &&
      now - cached.fetchedAt < REFRESH_INTERVAL_MS;

    if (!force && cacheValid) return cached;

    let usage = await client.fetchUsage(today);

    // The one authentication fork: 401/403 means the cookie was refused.
    // Recovery is attempted once per episode; concurrent and subsequent
    // requests within this refresh share the outcome.
    if (usage.kind === "http" && (usage.status === 401 || usage.status === 403)) {
      auth.onSessionExpired(`OpenCSI 返回 HTTP ${usage.status}`);
      const outcome = await auth.maybeRenew();
      if (outcome === "VALID") {
        usage = await client.fetchUsage(today);
      }
    }

    if (usage.kind === "http" && usage.status === 200) {
      // Prices failing is a degraded snapshot, not a failed one: tokens stay
      // valid and the cost becomes 估算不完整 (COST_UNKNOWN) in the state rules.
      const pricesOutcome = await client.fetchPrices();
      const prices =
        pricesOutcome.kind === "http" && pricesOutcome.status === 200
          ? pricesOutcome.body
          : [];
      const snapshot = OpenCSICore.state.snapshotFromPayload(
        { body: usage.body, prices },
        today,
        Date.now()
      );
      if (auth.state !== BG.auth.AUTH_STATE.VALID) auth.onUserAction();
      await writeSnapshot(snapshot);
      return snapshot;
    }

    // Not OK: classify honestly, keep the last good numbers, name the remedy.
    const state = OpenCSICore.state.classifyFetch(usage);
    let reason;
    if (usage.kind === "http") reason = `OpenCSI 返回 HTTP ${usage.status}`;
    else reason = usage.reason || "请求失败";

    let recovery = null;
    if (state === OpenCSICore.state.STATE.LOGIN_REQUIRED) {
      recovery = "点击「重新登录 OpenCSI」完成登录后回到本页";
    } else if (state === OpenCSICore.state.STATE.SERVER_ERROR) {
      recovery = "稍后自动重试，或点击「立即刷新」";
    } else if (state === OpenCSICore.state.STATE.OFFLINE) {
      recovery = "检查网络后点击「立即刷新」";
    }

    const snapshot = OpenCSICore.state.snapshotWithFailure(
      cached,
      state,
      reason,
      recovery,
      Date.now()
    );
    // Auth-facing states win over the fetch classification: if a renewal is
    // waiting for a human, the panel must say so rather than "server error".
    if (
      auth.state === BG.auth.AUTH_STATE.RENEWING ||
      auth.state === BG.auth.AUTH_STATE.LOGIN_REQUIRED ||
      auth.state === BG.auth.AUTH_STATE.CONSENT_REQUIRED
    ) {
      const after = OpenCSICore.state.applyAuthOutcome(snapshot, auth.state === BG.auth.AUTH_STATE.RENEWING ? "RENEWING" : auth.state);
      after.recovery = auth.recovery || after.recovery;
      after.lastError = after.lastError || auth.lastError;
      await writeSnapshot(after);
      return after;
    }
    await writeSnapshot(snapshot);
    return snapshot;
  })().finally(() => {
    inflight = null;
  });
  return inflight;
}

// ── messages ───────────────────────────────────────────────────────────────
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  (async () => {
    switch (message && message.type) {
      case "getSnapshot":
        sendResponse(await getSnapshot({ force: !!message.force }));
        break;
      case "refresh":
        sendResponse(await getSnapshot({ force: true }));
        break;
      case "openLogin":
        // The human path: a real tab on the real login flow. When it returns,
        // the next snapshot decides the truth — nothing is assumed here.
        auth.onUserAction();
        await chrome.tabs.create({
          url: "https://opencsitool.com/myTools",
          active: true,
        });
        sendResponse({ ok: true });
        break;
      case "authInfo":
        sendResponse(auth.describe());
        break;
      case "renewNow":
        auth.onSessionExpired("手动触发恢复");
        sendResponse({ outcome: await auth.maybeRenew() });
        break;
      default:
        sendResponse({ error: "unknown message" });
    }
  })().catch((error) => {
    sendResponse({ error: String((error && error.message) || error) });
  });
  return true; // asynchronous sendResponse
});
