// panel/panel.js — the iframe's own controller. It talks only to the service
// worker through chrome.runtime.sendMessage; the host page gets nothing.
//
// The refresh cycle is deliberately page-owned: ask on load, ask again on
// becoming visible (when the cache is older than the interval — the service
// worker answers from cache or refreshes), and a 5-minute timer that only
// runs while the page is visible. Close the tab and everything here dies
// with it; no background collection continues.
(function () {
  "use strict";

  const Core = window.OpenCSICore;
  const REFRESH_MS = 5 * 60 * 1000;
  const CLICK_THROTTLE_MS = 3000;

  const el = (id) => document.getElementById(id);
  const ui = {
    dot: el("dot"),
    state: el("state"),
    reason: el("reason"),
    todayTitle: el("todayTitle"),
    todayNote: el("todayNote"),
    todayTokens: el("todayTokens"),
    todayCost: el("todayCost"),
    modelRows: el("modelRows"),
    expandBtn: el("expandBtn"),
    updated: el("updated"),
    refreshBtn: el("refreshBtn"),
    loginBtn: el("loginBtn"),
  };

  let expanded = false;
  let lastRefreshClick = 0;
  let lastSnapshot = null;

  function send(message) {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage(message, (response) => {
          void chrome.runtime.lastError; // the worker may be starting; resolve empty
          resolve(response || null);
        });
      } catch {
        resolve(null);
      }
    });
  }

  const STATE_META = {
    OK: { label: "正常", color: "var(--ok)" },
    REFRESHING: { label: "正在刷新", color: "var(--busy)" },
    RENEWING: { label: "正在恢复登录", color: "var(--busy)" },
    LOGIN_REQUIRED: { label: "需要登录", color: "var(--error)" },
    CONSENT_REQUIRED: { label: "需要授权", color: "var(--warn)" },
    NOT_READY: { label: "数据未同步", color: "var(--warn)" },
    STALE: { label: "显示历史数据", color: "var(--warn)" },
    OFFLINE: { label: "网络不可用", color: "var(--muted)" },
    SERVER_ERROR: { label: "服务异常", color: "var(--warn)" },
    COST_UNKNOWN: { label: "费用不完整", color: "var(--warn)" },
  };

  function render(snapshot) {
    if (!snapshot) {
      ui.state.textContent = "暂不可用";
      ui.reason.textContent = "无法获得用量数据，请点击「立即刷新」重试";
      ui.reason.className = "reason error";
      return;
    }
    lastSnapshot = snapshot;
    const meta = STATE_META[snapshot.state] || { label: snapshot.state, color: "var(--muted)" };
    ui.dot.style.background = meta.color;
    ui.state.textContent = meta.label;
    ui.state.style.color = meta.color;

    const showReason =
      snapshot.lastError || snapshot.recovery || (snapshot.today && snapshot.today.note);
    ui.reason.textContent = showReason || "";
    ui.reason.className = "reason" + (showReason ? "" : " hidden");
    if (showReason) {
      ui.reason.classList.add(
        snapshot.state === "LOGIN_REQUIRED" || snapshot.state === "STALE" ? "error" : "warn"
      );
    }

    const today = snapshot.today || {};
    ui.todayTitle.textContent = today.date ? `今日用量 · ${today.date}` : "今日用量";
    ui.todayTokens.textContent = Core.formatting.cnTokens(today.tokens);
    ui.todayTokens.title = Core.formatting.fullNumber(today.tokens)
      ? `${Core.formatting.fullNumber(today.tokens)} tokens`
      : "";
    // The estimate is always named: it is tokens × price, never a billed amount.
    ui.todayCost.textContent = Core.formatting.cost(today.cost);
    ui.todayCost.title =
      today.cost === null || today.cost === undefined
        ? "费用无法完整估算"
        : "估算费用：tokens / 1e6 × 模型单价，非实际账单";
    ui.todayNote.textContent = today.stale
      ? `上次成功更新 ${snapshot.fetchedAtText}`
      : today.note || "";

    renderModels(today);

    ui.updated.textContent = `最近更新：${
      snapshot.fetchedAtText || "--:--"
    }　统计时区：Asia/Shanghai`;
    const needsLogin = snapshot.state === "LOGIN_REQUIRED";
    ui.loginBtn.hidden = !needsLogin;
  }

  function renderModels(today) {
    const models = today.models || [];
    const rows = expanded ? models : models.slice(0, 3);
    ui.modelRows.textContent = "";
    ui.modelRows.classList.toggle("expanded", expanded);
    for (const model of rows) {
      const row = document.createElement("div");
      row.className = "model-row" + (model.cost === null && model.tokens > 0 ? " flat" : "");
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = model.displayName || model.requestType;
      name.title = model.note || "";
      const tok = document.createElement("span");
      tok.className = "tok";
      tok.textContent = Core.formatting.cnTokens(model.tokens);
      tok.title = Core.formatting.fullNumber(model.tokens) || "";
      const cst = document.createElement("span");
      cst.className = "cst";
      cst.textContent = Core.formatting.cost(model.cost);
      row.append(name, tok, cst);
      ui.modelRows.appendChild(row);
    }
    const hasMore = models.length > 3;
    ui.expandBtn.hidden = !hasMore;
    ui.expandBtn.textContent = expanded ? "收起" : `展开全部（${models.length}）`;
  }

  async function ask(force) {
    const snapshot = await send(force ? { type: "refresh" } : { type: "getSnapshot" });
    render(snapshot);
  }

  function manualRefresh() {
    const now = Date.now();
    if (now - lastRefreshClick < CLICK_THROTTLE_MS) return; // no API hammering
    lastRefreshClick = now;
    ui.state.textContent = "正在刷新";
    ask(true);
  }

  ui.refreshBtn.addEventListener("click", manualRefresh);
  ui.loginBtn.addEventListener("click", () => send({ type: "openLogin" }));
  ui.expandBtn.addEventListener("click", () => {
    expanded = !expanded;
    render(lastSnapshot);
  });
  el("diagLink").addEventListener("click", () => {
    window.open(chrome.runtime.getURL("panel/diag.html"), "_blank");
  });

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") ask(false);
  });
  setInterval(() => {
    if (document.visibilityState === "visible") ask(false);
  }, REFRESH_MS);

  ask(false);
})();
