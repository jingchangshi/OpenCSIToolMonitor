// panel/panel.js — the compact summary card's controller.
//
// Data still comes only from the service worker; the host page gets nothing.
// What changed this round is presentation: a single-layer card, two headline
// metrics, a model area whose layout follows the day's model count, and a
// ResizeObserver that reports the card's real height to the host page — the
// iframe is sized by content, never by a fixed constant.
//
// The resize message carries exactly one number (see core/resize.js); the
// target origin is the Magpie page and nothing else ever receives it.
(function () {
  "use strict";

  const Core = window.OpenCSICore;
  const REFRESH_MS = 5 * 60 * 1000;
  const CLICK_THROTTLE_MS = 3000;
  const MAGPIE_ORIGIN = "http://129.153.118.58:3430";

  const el = (id) => document.getElementById(id);
  const ui = {
    dot: el("dot"),
    state: el("state"),
    reason: el("reason"),
    todayTokens: el("todayTokens"),
    todayCost: el("todayCost"),
    subLine: el("subLine"),
    freshLine: el("freshLine"),
    modelRows: el("modelRows"),
    expandBtn: el("expandBtn"),
    updated: el("updated"),
    refreshBtn: el("refreshBtn"),
    loginBtn: el("loginBtn"),
  };

  let expanded = false;
  let lastRefreshClick = 0;
  let lastSnapshot = null;
  let lastSentHeight = 0;

  // ── height reporting ────────────────────────────────────────────────────
  function postHeight() {
    const height = Core.resize.clampPanelHeight(
      document.documentElement.scrollHeight
    );
    if (height === null || height === lastSentHeight) return; // no loop, no churn
    lastSentHeight = height;
    // One number, to one origin: the panel's data never crosses this boundary.
    window.parent.postMessage(
      { type: "OPENCSI_PANEL_RESIZE", height },
      MAGPIE_ORIGIN
    );
  }
  const resizeObserver = new ResizeObserver(postHeight);
  resizeObserver.observe(document.getElementById("panel"));

  // ── messaging ───────────────────────────────────────────────────────────
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

  // ── rendering ───────────────────────────────────────────────────────────
  function render(snapshot) {
    if (!snapshot) {
      ui.state.textContent = "暂不可用";
      ui.reason.textContent = "无法获得用量数据，请点击「刷新」重试";
      ui.reason.className = "reason error";
      postHeight();
      return;
    }
    lastSnapshot = snapshot;
    const meta = STATE_META[snapshot.state] || {
      label: snapshot.state,
      color: "var(--muted)",
    };
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
    ui.todayTokens.textContent = Core.formatting.cnTokens(today.tokens);
    ui.todayTokens.title = Core.formatting.fullNumber(today.tokens)
      ? `${Core.formatting.fullNumber(today.tokens)} tokens`
      : "";
    // 估算费用 is always named: tokens × price, never a billed amount.
    ui.todayCost.textContent = Core.formatting.cost(today.cost);
    ui.todayCost.title =
      today.cost === null || today.cost === undefined
        ? "费用无法完整估算"
        : "估算费用：tokens / 1e6 × 模型单价，非实际账单";

    renderModels(today);
    renderSubLine(snapshot, today);
    renderFooter(snapshot);

    const needsLogin = snapshot.state === "LOGIN_REQUIRED";
    ui.loginBtn.hidden = !needsLogin;
    postHeight();
  }

  /**
   * The model area's layout follows the day's model count:
   *   0     → "今日暂无模型使用记录" (only when the day is genuinely empty);
   *   1     → the model's name, no duplicate totals, no expand button;
   *   2–3   → the full ranking, no expand button;
   *   >3    → top 3 rows + an expand toggle; the expanded list scrolls
   *           internally so the card stays inside its height budget.
   */
  function renderModels(today) {
    const models = today.models || [];
    ui.modelRows.textContent = "";
    // The container starts `hidden` in the markup and its visibility is owned
    // here: a ranking is rendered for 2+ models only, and forgetting to unhide
    // would shrink the card while the rows quietly exist underneath.
    ui.modelRows.classList.toggle("hidden", models.length <= 1);
    if (models.length <= 1) return;

    if (models.length <= 3 && !expanded) {
      ui.modelRows.classList.remove("expanded");
      for (const model of models) ui.modelRows.appendChild(modelRow(model));
      return;
    }
    const rows = expanded ? models : models.slice(0, 3);
    ui.modelRows.classList.add("expanded");
    for (const model of rows) ui.modelRows.appendChild(modelRow(model));
  }

  function modelRow(model) {
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
    return row;
  }

  function renderSubLine(snapshot, today) {
    const models = today.models || [];
    const sub = ui.subLine;
    sub.classList.remove("hidden");

    // A confirmed-zero day says so in words; a day with models names them.
    // NOT_READY and friends carry their own note in the reason line instead of
    // borrowing this one.
    if (snapshot.state === "OK" && (today.tokens || 0) === 0) {
      sub.textContent = "今日暂无模型使用记录";
      return;
    }
    if (models.length === 1) {
      const only = models[0];
      // The totals are already the headline; repeating them for a single
      // model would say the same thing twice.
      const extra = only.note ? ` · ${only.note}` : "";
      sub.textContent = `${only.displayName || only.requestType} · 当前唯一使用模型${extra}`;
      return;
    }
    // Two or more models: the rows themselves are the sub-content.
    sub.classList.add("hidden");
  }

  function renderFooter(snapshot) {
    const time = snapshot.fetchedAtText || "--:--";
    const staleData = snapshot.today && snapshot.today.stale;
    let text;
    if (!snapshot.fetchedAt) {
      text = `最近更新：--:--`;
    } else if (staleData) {
      text = `上次成功查询 ${time}`;
    } else {
      text = `查询于 ${time}`;
    }
    text += ` · 统计时区 Asia/Shanghai`;
    ui.updated.textContent = text;

    // The server's own sync stamp is a different fact from our query time and
    // is labelled as such, shown only when it parses.
    const freshTime = snapshot.dataFreshTime
      ? Core.date.shanghaiClock(snapshot.dataFreshTime)
      : null;
    ui.freshLine.textContent =
      freshTime && freshTime !== "--:--" ? `服务端数据截至 ${freshTime}` : "";
    ui.freshLine.classList.toggle("hidden", !ui.freshLine.textContent);

    const models = (snapshot.today && snapshot.today.models) || [];
    const hasMore = models.length > 3;
    ui.expandBtn.hidden = !hasMore;
    ui.expandBtn.textContent = expanded ? `收起` : `展开全部（${models.length}）`;
  }

  // ── interactions ────────────────────────────────────────────────────────
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
    render(lastSnapshot); // re-render only: no API call, no iframe rebuild
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
