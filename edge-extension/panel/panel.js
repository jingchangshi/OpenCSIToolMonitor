// panel/panel.js — the OpenCSI quota card's controller.
//
// The card lives inside Magpie's native quota grid: the .subscription-card
// frame around this content belongs to Magpie, so this stylesheet stays
// frame-less and this controller renders the content only. Data still comes
// only from the service worker; the host page receives exactly one number —
// the card's content height, via the resize protocol in core/resize.js — and
// Magpie's own layout observer re-packs the grid when it lands.
//
// Layout rules (Phase 1 of the card-refinement brief):
//   * headline metrics: two columns, baseline-aligned, wrapping to stacked
//     when the 260px card cannot fit both without squeezing;
//   * one model day: the model's name + a sub-note, no duplicate totals;
//   * multi-model day: the TOP model by default, the rest behind
//     「另 N 个模型 · 查看详情」; the expanded list carries name/tokens/
//     cost/bill-type and scrolls internally;
//   * money: ¥0.00 only for a genuine zero; a positive sub-cent amount is
//     ¥<0.01 — never rounded back to a "free" ¥0.00;
//   * state: the dot carries OK; any non-OK state ALSO shows visible text,
//     never colour alone.
(function () {
  "use strict";

  const Core = window.OpenCSICore;
  const REFRESH_MS = 5 * 60 * 1000;
  const CLICK_THROTTLE_MS = 3000;
  const MAGPIE_ORIGIN = "http://129.153.118.58:3430";

  const el = (id) => document.getElementById(id);
  const ui = {
    dot: el("dot"),
    stateText: el("stateText"),
    reason: el("reason"),
    numbers: el("numbers"),
    todayTokens: el("todayTokens"),
    todayCost: el("todayCost"),
    subLine: el("subLine"),
    modelRows: el("modelRows"),
    expandBtn: el("expandBtn"),
    updated: el("updated"),
    refreshBtn: el("refreshBtn"),
    diagLink: el("diagLink"),
    loginBtn: el("loginBtn"),
  };

  let expanded = false;
  let lastRefreshClick = 0;
  let lastSnapshot = null;
  let lastSentHeight = 0;

  // ── visual theme ────────────────────────────────────────────────────────
  // The card follows Magpie's theme, not the OS: the host page relays its
  // resolved light/dark (URL param for the first paint, postMessage for live
  // changes). Strictly validated and strictly visual — no other use.
  function applyTheme(theme) {
    if (theme !== "dark" && theme !== "light") return;
    document.documentElement.dataset.opencsiTheme = theme;
  }
  try {
    applyTheme(new URLSearchParams(location.search).get("theme"));
  } catch {
    /* no or malformed param: CSS falls back to prefers-color-scheme */
  }
  window.addEventListener("message", (event) => {
    if (event.source !== window.parent) return;
    if (event.origin !== "http://129.153.118.58:3430") return;
    const data = event.data;
    if (data && data.type === "OPENCSI_PANEL_THEME") applyTheme(data.theme);
  });

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
    OK: { label: "正常", color: "var(--ok)", text: "" },
    REFRESHING: { label: "正在刷新", color: "var(--busy)", text: "正在刷新" },
    RENEWING: { label: "正在恢复登录", color: "var(--busy)", text: "正在恢复登录" },
    LOGIN_REQUIRED: { label: "需要登录", color: "var(--error)", text: "需要重新登录" },
    CONSENT_REQUIRED: { label: "需要授权", color: "var(--warn)", text: "需要授权确认" },
    NOT_READY: { label: "数据未同步", color: "var(--warn)", text: "今日数据未就绪" },
    STALE: { label: "显示历史数据", color: "var(--warn)", text: "数据已过期" },
    OFFLINE: { label: "网络不可用", color: "var(--muted)", text: "网络不可用" },
    SERVER_ERROR: { label: "服务异常", color: "var(--warn)", text: "服务异常" },
    COST_UNKNOWN: { label: "费用不完整", color: "var(--warn)", text: "费用不完整" },
  };

  // ── rendering ───────────────────────────────────────────────────────────
  function render(snapshot) {
    if (!snapshot) {
      setDot("暂不可用", "var(--muted)");
      ui.stateText.textContent = "暂不可用";
      ui.stateText.classList.remove("hidden");
      ui.reason.textContent = "无法获得用量数据，请点击 ⟳ 重试";
      ui.reason.className = "reason error";
      postHeight();
      return;
    }
    lastSnapshot = snapshot;
    const meta = STATE_META[snapshot.state] || {
      label: snapshot.state, color: "var(--muted)", text: snapshot.state,
    };
    setDot(meta.label, meta.color);
    // OK shows only the green dot; every other state shows visible text too —
    // colour alone is not a status.
    ui.stateText.textContent = meta.text;
    ui.stateText.classList.toggle("hidden", !meta.text);

    const showReason =
      snapshot.lastError || snapshot.recovery || (snapshot.today && snapshot.today.note);
    ui.reason.textContent = showReason || "";
    ui.reason.className = "reason" + (showReason ? "" : " hidden");
    if (showReason) {
      ui.reason.classList.add(
        snapshot.state === "LOGIN_REQUIRED" || snapshot.state === "STALE" ? "error" : "warn"
      );
    }

    renderNumbers(snapshot.today || {});
    renderModels(snapshot);
    renderFooter(snapshot);

    const needsLogin = snapshot.state === "LOGIN_REQUIRED";
    ui.loginBtn.hidden = !needsLogin;
    postHeight();
  }

  function setDot(label, color) {
    ui.dot.style.background = color;
    ui.dot.title = label;
    ui.dot.setAttribute("aria-label", `状态：${label}`);
  }

  // ── headline metrics ────────────────────────────────────────────────────
  function renderNumbers(today) {
    ui.todayTokens.textContent = Core.formatting.cnTokens(today.tokens);
    ui.todayTokens.title = Core.formatting.fullNumber(today.tokens)
      ? `${Core.formatting.fullNumber(today.tokens)} tokens`
      : "";
    // 估算费用 is always named: tokens × price, never a billed amount.
    ui.todayCost.textContent = money(today.cost);
    ui.todayCost.title =
      today.cost === null || today.cost === undefined
        ? "部分模型无按 Token 价格，费用无法完整估算"
        : "估算费用：tokens / 1e6 × 模型单价，非实际账单";

    // Stack when both values cannot sit on one 260px line without squeezing:
    // measure the two rendered values against the available width.
    requestAnimationFrame(() => {
      const a = ui.todayTokens.getBoundingClientRect();
      const b = ui.todayCost.getBoundingClientRect();
      const available = ui.numbers.clientWidth;
      const need = a.width + b.width + 12;
      ui.numbers.classList.toggle("stacked", need > available);
      postHeight();
    });
  }

  /**
   * Money with the sub-cent rule: a genuine zero is ¥0.00; a POSITIVE amount
   * below one cent is ¥<0.01 (rounding it to 0.00 would call a paid service
   * free); unknown stays --.
   */
  function money(value) {
    if (value === null || value === undefined || !Number.isFinite(value)) return "--";
    if (value === 0) return "¥0.00";
    if (value > 0 && value < 0.01) return "¥<0.01";
    return Core.formatting.cost(value);
  }

  // ── model area ──────────────────────────────────────────────────────────
  function renderModels(snapshot) {
    const today = snapshot.today || {};
    const models = today.models || [];
    ui.modelRows.textContent = "";

    // Zero day: words, no empty list, no expand button (zero is only claimed
    // when the state machine confirmed it; NOT_READY carries its own text).
    // Checked by TOTAL tokens, because a confirmed-zero day can still carry
    // zero-token model rows.
    if (models.length === 0 ||
        (snapshot.state === "OK" && (today.tokens || 0) === 0)) {
      ui.modelRows.classList.add("hidden");
      ui.expandBtn.classList.add("hidden");
      ui.subLine.classList.remove("hidden");
      ui.subLine.textContent =
        snapshot.state === "OK" ? "今日暂无模型用量" : "";
      ui.subLine.classList.toggle("hidden", ui.subLine.textContent === "");
      return;
    }

    if (models.length === 1) {
      // A single model: its name plus a sub-note. The headline numbers ARE
      // its totals — repeating them would say the same thing twice.
      const only = models[0];
      ui.modelRows.classList.add("hidden");
      ui.expandBtn.classList.add("hidden");
      ui.subLine.classList.remove("hidden");
      const note = only.note ? ` · ${billNote(only)}` : "";
      ui.subLine.innerHTML = "";
      ui.subLine.append(only.displayName || only.requestType);
      const subNote = document.createElement("span");
      subNote.className = "sub-note";
      subNote.textContent = ` · 当前唯一使用模型${note}`;
      ui.subLine.append(subNote);
      return;
    }

    // Multi-model: the FIRST row is the day's top model (aggregation sorts
    // descending), shown by default with its numbers; the rest sit behind
    // the toggle. No sub line — the row already names it.
    ui.subLine.classList.add("hidden");

    const rows = expanded ? models : models.slice(0, 1);
    ui.modelRows.classList.remove("hidden");
    ui.modelRows.classList.toggle("expanded", expanded);
    for (const model of rows) ui.modelRows.appendChild(modelRow(model));
    ui.expandBtn.classList.remove("hidden");
    ui.expandBtn.textContent = expanded
      ? "收起详情"
      : `另 ${models.length - 1} 个模型 · 查看详情`;
  }

  function billNote(model) {
    if (model.cost === null && model.tokens > 0) {
      return model.monthlyFee !== null && model.monthlyFee !== undefined
        ? "套餐计费，不按 Token 计费"
        : "价格未知";
    }
    return model.note || "";
  }

  function modelRow(model) {
    const row = document.createElement("div");
    row.className = "model-row";
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = model.displayName || model.requestType;
    // Long names reach their full text through the tooltip.
    name.title = `${model.displayName || model.requestType}${
      model.note ? `（${model.note}）` : ""
    }`;
    const tok = document.createElement("span");
    tok.className = "tok";
    tok.textContent = Core.formatting.cnTokens(model.tokens);
    tok.title = Core.formatting.fullNumber(model.tokens) || "";
    const cst = document.createElement("span");
    cst.className = "cst";
    cst.textContent = money(model.cost);
    cst.title =
      model.cost === null && model.tokens > 0
        ? billNote(model)
        : "估算费用，非实际账单";
    row.append(name, tok, cst);
    if (model.cost === null && model.tokens > 0) {
      const bill = document.createElement("span");
      bill.className = "bill";
      bill.textContent = model.monthlyFee != null ? "套餐" : "未知";
      row.append(bill);
    }
    return row;
  }

  function renderFooter(snapshot) {
    const time = snapshot.fetchedAtText || "--:--";
    const staleData = snapshot.today && snapshot.today.stale;
    let text;
    if (!snapshot.fetchedAt) {
      text = "查询于 --:--";
    } else if (staleData) {
      text = `上次成功查询 ${time}`;
    } else {
      text = `查询于 ${time}`;
    }
    ui.updated.textContent = text;
    // The server's own sync stamp is a different fact from our query time and
    // is labelled as such — carried as the footer's tooltip, because the
    // narrow card has no separate line for it.
    const freshTime = snapshot.dataFreshTime
      ? Core.date.shanghaiClock(snapshot.dataFreshTime)
      : null;
    ui.updated.title =
      freshTime && freshTime !== "--:--"
        ? `服务端数据截至 ${freshTime} · 统计时区 Asia/Shanghai`
        : "统计时区 Asia/Shanghai";
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
    // In progress: the dot + the refresh icon spin; the numbers stay put.
    setDot("正在刷新", "var(--busy)");
    ui.refreshBtn.classList.add("spinning");
    ask(true).finally(() => ui.refreshBtn.classList.remove("spinning"));
  }

  ui.refreshBtn.addEventListener("click", manualRefresh);
  ui.expandBtn.addEventListener("click", () => {
    expanded = !expanded;
    render(lastSnapshot); // re-render only: no API call, no iframe rebuild
  });
  ui.loginBtn.addEventListener("click", () => send({ type: "openLogin" }));
  ui.diagLink.addEventListener("click", () => {
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
