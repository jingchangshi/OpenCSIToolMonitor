// e2e/browser.e2e.js — the extension loaded in a real Chromium, driven over CDP.
//
// This harness verifies the quota-grid integration against Magpie's REAL
// layout machinery:
//
//   * the mock Magpie page loads the real `app.css` (vendored verbatim) and a
//     verbatim extraction of the real `packUsage` / `usagePack` /
//     `packUsageNow` functions from the vendored `app.js` (upstream commit
//     recorded in docs/magpie-quota-grid.md), driving a
//     `.subscription-usage` grid whose cards are built the way renderQuotas
//     builds them: replaceChildren + .subscription-card + dataset keys;
//   * a mock quota "API" refreshes through that same path, so the extension
//     card faces the exact lifecycle a real data refresh produces;
//   * the extension itself runs for real: service worker, content script,
//     iframe isolation, the storage-fixture data path (snapshots built by the
//     production state.js pipeline inside the service worker), and the auth
//     state machine;
//   * screenshots of the real rendering, for the delivery report.
//
// Run:  node e2e/browser.e2e.js   (from edge-extension/)
"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const puppeteer = require("puppeteer-core");

const EXT_DIR = path.resolve(__dirname, "..");
const OUT_DIR = path.resolve(__dirname, "out");
const MAGPIE_APP_JS = path.resolve(__dirname, "magpie-app.js");
const MAGPIE_APP_CSS = path.resolve(__dirname, "magpie-app.css");

const BROWSER_CANDIDATES = [
  // Edge is the delivery target, but this machine's Edge binary exits
  // silently under --headless=new + --load-extension; Chrome shares the
  // Chromium extension runtime. Branded builds 137+ also ignore
  // --load-extension, so the automated run prefers the downloaded Chromium
  // for Testing build, which still honours it. The user's real Edge
  // acceptance remains a manual step (the extension loads identically in
  // edge://extensions).
  path.resolve(__dirname, "chromium", "win64-1726484", "chrome-win", "chrome.exe"),
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
];

function findBrowser() {
  for (const candidate of BROWSER_CANDIDATES) {
    if (fs.existsSync(candidate)) return candidate;
  }
  throw new Error("no usable browser found; run: npx @puppeteer/browsers install chromium@latest");
}

/** The verbatim packUsage block, extracted from the real app.js. */
function extractPackUsage(appJs) {
  const start = appJs.indexOf("const USAGE_MIN = 260");
  const endMarker = "usagePack.observe(part);\n  }\n}";
  const end = appJs.indexOf(endMarker, start);
  if (start < 0 || end < 0) throw new Error("packUsage block not found in app.js");
  return appJs.slice(start, end + endMarker.length);
}

/** The mock page: real CSS + verbatim packUsage + a faithful quota lifecycle. */
function buildMockPage(appJs, appCss) {
  const packUsageCode = extractPackUsage(appJs);
  new Function("$", packUsageCode); // must parse before the page ever loads it
  const cards = [
    { provider: "opencode-go", name: "OpenCode Go", h: 120 },
    { provider: "codex", name: "Codex", h: 180 },
    { provider: "workbuddy", name: "WorkBuddy", h: 90 },
    { provider: "deepseek", name: "DeepSeek", h: 150 },
  ];
  return `<!doctype html><html><head><meta charset="utf-8"><title>magpie</title>
<link rel="stylesheet" href="app.css">
</head><body>
<nav style="padding:8px">
  <button data-view="agents">Agents</button>
  <button data-view="usage" class="on">Usage</button>
</nav>
<section id="view-agents" hidden><div style="height:200px">agents placeholder</div></section>
<section class="view" id="view-usage">
  <section class="subscription-usage" id="subscriptionUsage"></section>
  <div style="height:600px">filler makes the page scrollable</div>
</section>
<script>
  // ── verbatim from app.js — the real layout logic under test ─────────────
  const $ = (s) => document.querySelector(s);
  // stub: fitting quota bars is Magpie-internal; the mock cards carry fixed
  // heights so packUsage measures real, varied boxes without it.
  const fitQuota = () => {};
  ${packUsageCode}
  // ── a faithful renderQuotas-lite ─────────────────────────────────────────
  const MOCK_CARDS = ${JSON.stringify(cards)};
  let mockQuotas = MOCK_CARDS.map((c) => ({ ...c }));
  let usageOrder = [];
  function byUsageOrderLite(list) {
    const rank = (q) => { const i = usageOrder.indexOf(q.provider); return i < 0 ? usageOrder.length : i; };
    return list.map((q, i) => [q, i]).sort(([a, i], [b, j]) => rank(a) - rank(b) || i - j).map(([q]) => q);
  }
  function renderQuotasLite() {
    const list = $("#subscriptionUsage");
    list.replaceChildren(); // the real renderQuotas' wipe, verbatim behaviour
    for (const sub of byUsageOrderLite(mockQuotas)) {
      const card = document.createElement("div");
      card.className = "subscription-card";
      card.dataset.key = sub.provider;
      card.dataset.provider = sub.provider;
      const head = document.createElement("div");
      head.className = "subscription-head";
      const name = document.createElement("b");
      name.textContent = sub.name;
      head.appendChild(name);
      card.appendChild(head);
      const win = document.createElement("div");
      win.className = "quota-windows";
      win.style.height = sub.h + "px";
      card.appendChild(win);
      list.appendChild(card);
    }
    packUsageNow(list);
  }
  // mirrors app.js moveUsage: persist provider order, rebuild
  function moveUsageLite(key, to) {
    const keys = mockQuotas.map((q) => q.provider);
    const from = keys.indexOf(key);
    if (from < 0 || to < 0 || to >= keys.length || to === from) return;
    keys.splice(to, 0, ...keys.splice(from, 1));
    usageOrder = [...keys, ...usageOrder.filter((k) => !keys.includes(k))];
    mockQuotas.sort((a, b) => usageOrder.indexOf(a.provider) - usageOrder.indexOf(b.provider));
    renderQuotasLite();
  }
  // a native card's fold: its height changes, the grid re-packs
  function toggleNativeFold(provider) {
    const win = document.querySelector('#subscriptionUsage > [data-provider="' + provider + '"] .quota-windows');
    if (!win) return;
    win.style.height = win.style.height === "40px" ? "180px" : "40px";
    packUsage();
  }
  function showView(v) {
    document.getElementById("view-usage").hidden = v !== "usage";
    document.getElementById("view-agents").hidden = v !== "agents";
    document.querySelectorAll("nav button").forEach((x) => x.classList.toggle("on", x.dataset.view === v));
  }
  document.querySelectorAll("nav button").forEach((b) => {
    b.addEventListener("click", () => showView(b.dataset.view));
  });
  showView("usage");
  renderQuotasLite();
  window.__mock = { renderQuotasLite, moveUsageLite, toggleNativeFold, showView };
</script></body></html>`;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const appJs = fs.readFileSync(MAGPIE_APP_JS, "utf8");
  const appCss = fs.readFileSync(MAGPIE_APP_CSS, "utf8");
  const mockPageHtml = buildMockPage(appJs, appCss);

  fs.mkdirSync(OUT_DIR, { recursive: true });
  const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "opencsi-ext-e2e-"));
  const browser = await puppeteer.launch({
    executablePath: findBrowser(),
    headless: "new",
    userDataDir,
    args: [
      `--disable-extensions-except=${EXT_DIR}`,
      `--load-extension=${EXT_DIR}`,
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-gpu",
      "--window-size=1400,1000",
    ],
  });
  const results = [];
  const consoleErrors = [];
  const check = (name, ok, detail = "") => {
    results.push({ name, ok, detail });
    console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? " — " + detail : ""}`);
  };

  try {
    // ── the extension's service worker is running ─────────────────────────
    let worker = null;
    for (let attempt = 0; attempt < 20 && !worker; attempt++) {
      worker = browser.targets().find((t) => t.type() === "service_worker" &&
        t.url().includes("background/service-worker.js"));
      if (!worker) await sleep(500);
    }
    check("扩展加载：Service Worker 已启动", !!worker, worker && worker.url());
    const sw = await worker.worker();

    // ── the data path is deterministic: fixtures are pushed into
    // storage.session BEFORE the page loads, so every panel ask is a cache
    // hit — no real-network fetch, no renewal, no write race. The one
    // real-network section (auth) clears the fixture explicitly. ──────────
    async function pushFixtureSnapshot(models) {
      await sw.evaluate(async (modelRows) => {
        const prices = [
          { requestType: "glm-5.3-flash", displayName: "GLM-5.3-Flash", billType: "TOKEN", enabled: 1, blendedPrice: 2.24 },
          { requestType: "deepseek-v4", displayName: "DeepSeek-V4.1-Flash", billType: "TOKEN", enabled: 1, blendedPrice: 4.72 },
          { requestType: "qwen3-max", displayName: "Qwen3-Max", billType: "TOKEN", enabled: 1, blendedPrice: 0.28 },
          { requestType: "embed-v4", displayName: "Embedding-V4", billType: "TOKEN", enabled: 1, blendedPrice: 0.4 },
          { requestType: "glm-4.7-air", displayName: "GLM-4.7-Air", billType: "TOKEN", enabled: 1, blendedPrice: 1.1 },
        ];
        const today = new Intl.DateTimeFormat("en-CA", {
          timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
        }).format(new Date());
        const payload = {
          body: {
            data: {
              tokenTrend: modelRows.map((r) => ({ date: today, ...r })),
              tokenSummary: { totalTokens: 26_066_000, totalRequestCount: 1_284 },
              syncStatus: { dataFreshTime: "2026-10-25T16:10:00+08:00" },
            },
          },
          prices,
        };
        const snapshot = OpenCSICore.state.snapshotFromPayload(payload, today, Date.now());
        await chrome.storage.session.set({ opencsiSnapshot: snapshot });
      }, models);
    }
    async function clearFixture() {
      await sw.evaluate(async () => {
        await chrome.storage.session.remove("opencsiSnapshot");
      });
    }

    const page = await browser.newPage();
    page.on("console", (m) => {
      if (m.type() === "error") consoleErrors.push(m.text().slice(0, 160));
    });
    page.on("pageerror", (e) => consoleErrors.push("[pageerror] " + String(e).slice(0, 160)));

    await page.setRequestInterception(true);
    page.on("request", (request) => {
      const url = request.url();
      if (url === "http://129.153.118.58:3430/app.css") {
        request.respond({ status: 200, contentType: "text/css", body: appCss });
      } else if (url.startsWith("http://129.153.118.58:3430/") && !url.includes("panel/")) {
        request.respond({
          status: 200,
          contentType: "text/html; charset=utf-8",
          body: mockPageHtml,
        });
      } else {
        request.continue();
      }
    });
    await page.setViewport({ width: 1400, height: 1000 });
    await page.goto("http://129.153.118.58:3430/?view=usage", { waitUntil: "domcontentloaded" });
    await sleep(1500);

    // The panel's first ask runs a REAL fetch (401 here) and a silent renewal
    // (up to 45s); the renewal's failure write lands only when that whole
    // cycle finishes. Waiting on authState alone races the final write — so
    // wait for the written snapshot itself to be stable, THEN seed.
    for (let attempt = 0; attempt < 60; attempt++) {
      const settled = await sw.evaluate(async () => {
        const s = await chrome.storage.session.get("opencsiSnapshot");
        const snap = s.opencsiSnapshot;
        if (!snap) return false;
        // the failure write carries failureAt; once present and aged >3s the
        // whole first-load cycle has landed
        return typeof snap.failureAt === "number" && Date.now() - snap.failureAt > 3000;
      });
      if (settled) break;
      await sleep(1500);
    }

    // ── 1-3: the card is the grid's first child; native order unchanged ───
    const injected = await page.evaluate(() => {
      const grid = document.getElementById("subscriptionUsage");
      const kids = [...grid.children];
      return {
        gridExists: !!grid,
        total: kids.length,
        firstIsOurs: kids[0] && kids[0].id === "opencsi-usage-card",
        oursIsCard: kids[0] && kids[0].classList.contains("subscription-card"),
        hasExtAttr: kids[0] && kids[0].hasAttribute("data-opencsi-extension"),
        nativeSeq: kids.filter((k) => k.dataset.provider).map((k) => k.dataset.provider),
        hostCount: document.querySelectorAll("#opencsi-usage-card").length,
        frameSrc: kids[0] && kids[0].querySelector("iframe") ? kids[0].querySelector("iframe").src : null,
      };
    });
    check("OpenCSI 卡片位于额度网格第一格（原生风格 .subscription-card）",
      injected.gridExists && injected.firstIsOurs && injected.oursIsCard &&
      injected.hasExtAttr, JSON.stringify(injected));
    check("原生卡片相对顺序保持（OpenCode Go → Codex → WorkBuddy → DeepSeek）",
      JSON.stringify(injected.nativeSeq) ===
      JSON.stringify(["opencode-go", "codex", "workbuddy", "deepseek"]),
      JSON.stringify(injected.nativeSeq));
    check("单实例：全文档只有一张 OpenCSI 卡", injected.hostCount === 1);
    check("iframe 来自扩展（隔离边界保持）",
      (injected.frameSrc || "").startsWith("chrome-extension://"), injected.frameSrc);

    // ── fresh profile: no cookie → the honest login state ─────────────────
    async function panelFrame() {
      const fh = await page.evaluateHandle(() =>
        document.querySelector("#opencsi-usage-card iframe"));
      return fh.asElement().contentFrame();
    }
    const MULTI = [
      { requestType: "glm-5.3-flash", tokens: 16_800_000 },
      { requestType: "deepseek-v4", tokens: 7_900_000 },
      { requestType: "qwen3-max", tokens: 1_240_000 },
      { requestType: "embed-v4", tokens: 96_000 },
      { requestType: "glm-4.7-air", tokens: 30_000 },
    ];
    const TINY = [
      { requestType: "glm-5.3-flash", tokens: 1000 }, // ×2.24/M = ¥0.00224 < 分
      { requestType: "glm-4.7-air", tokens: 500_000 }, // priced normally
    ];
    const SINGLE = [{ requestType: "deepseek-v4", tokens: 7_970_000 }];
    const ZERO = [{ requestType: "glm-5.3-flash", tokens: 0 }];

    async function panelClick(elementId) {
      const f = await panelFrame();
      await f.evaluate((id) => document.getElementById(id).click(), elementId);
    }
    async function readPanel() {
      // Wait for the panel's first render: the state dot's tooltip is empty
      // until render() runs, so it doubles as the readiness signal.
      for (let attempt = 0; attempt < 30; attempt++) {
        const f = await panelFrame();
        const ready = await f.evaluate(() =>
          (document.getElementById("dot") || { title: "" }).title !== ""
        ).catch(() => false);
        if (ready) break;
        await sleep(400);
      }
      const f = await panelFrame();
      const [inner, geometry] = await Promise.all([
        f.evaluate(() => ({
          state: (document.getElementById("dot") || { title: "" }).title,
          tokens: document.getElementById("todayTokens").textContent,
          cost: document.getElementById("todayCost").textContent,
          updated: document.getElementById("updated").textContent,
          sub: document.getElementById("subLine").textContent,
          rowCount: document.querySelectorAll("#modelRows .model-row").length,
          expandText: document.getElementById("expandBtn").textContent,
          frameStyleHeight: (document.defaultView.frameElement || {}).style
            ? document.defaultView.frameElement.style.height
            : "",
        })),
        page.evaluate(() => {
          const card = document.getElementById("opencsi-usage-card");
          const iframe = card.querySelector("iframe");
          return {
            cardRect: card.getBoundingClientRect().height,
            frameRect: iframe.getBoundingClientRect().height,
            frameStyleHeight: iframe.style.height,
          };
        }),
      ]);
      return { ...inner, ...geometry };
    }
    async function gridGeometry() {
      return page.evaluate(() => {
        const grid = document.getElementById("subscriptionUsage");
        const cards = [...grid.children].map((c) => {
          const r = c.getBoundingClientRect();
          return { id: c.id || c.dataset.provider || "?", left: r.left, top: r.top, right: r.right, bottom: r.bottom };
        });
        const overlaps = [];
        for (let i = 0; i < cards.length; i++) {
          for (let j = i + 1; j < cards.length; j++) {
            const a = cards[i], b = cards[j];
            if (a.left < b.right - 1 && b.left < a.right - 1 &&
                a.top < b.bottom - 1 && b.top < a.bottom - 1) {
              overlaps.push(`${a.id}∩${b.id}`);
            }
          }
        }
        return { cards, overlaps };
      });
    }
    // ── multi-model through the pre-seeded cache ───────────────────────────
    await pushFixtureSnapshot(MULTI);
    await page.reload({ waitUntil: "domcontentloaded" });
    await sleep(1500);
    let view = await readPanel();
    check("今日 Tokens 与估算费用数值正确（与夹具一致）",
      view.tokens === "2,606.6 万" && view.cost === "¥75.34",
      view.tokens + " " + view.cost);
    check("查询时间真实显示", /查询于 \d{2}:\d{2}/.test(view.updated), view.updated);
    check("多模型：默认 Top 1 + 「另 4 个模型 · 查看详情」", view.rowCount === 1 &&
      view.expandText.includes("另 4 个模型") && view.expandText.includes("查看详情"),
      `rows=${view.rowCount} btn=${view.expandText}`);
    check("iframe 高度由内容驱动（不再是固定 300px）",
      view.frameStyleHeight !== "300px" && /^\d+px$/.test(view.frameStyleHeight),
      `style=${view.frameStyleHeight}`);
    fs.writeFileSync(path.join(OUT_DIR, "card-multi.png"),
      await page.screenshot({ clip: await cardClip(page) }));

    // ── P0-3: the footer must be fully visible inside the iframe ─────────
    const bottom = await (async () => {
      const f = await panelFrame();
      return f.evaluate(() => {
        const foot = document.querySelector(".foot");
        const fr = foot.getBoundingClientRect();
        const vh = window.innerHeight;
        const refresh = document.getElementById("refreshBtn");
        const diag = document.getElementById("diagLink");
        const rr = refresh.getBoundingClientRect();
        const dr = diag.getBoundingClientRect();
        return {
          footVisible: fr.bottom <= vh + 0.5 && fr.top >= -0.5,
          buttonsVisible:
            rr.bottom <= vh + 0.5 && rr.top >= -0.5 &&
            dr.bottom <= vh + 0.5 && dr.top >= -0.5,
          scrollVsDoc: document.documentElement.scrollHeight <= Math.ceil(vh) + 1,
          vh: Math.round(vh), sh: document.documentElement.scrollHeight,
        };
      });
    })();
    check("底部操作栏完整可见（无 iframe 裁剪）",
      bottom.footVisible && bottom.buttonsVisible && bottom.scrollVsDoc,
      JSON.stringify(bottom));

    // ── 4-5: rectangles pairwise disjoint; explicit grid areas applied ─────
    let geo = await gridGeometry();
    check("五张卡片矩形两两不重叠", geo.overlaps.length === 0,
      JSON.stringify(geo.overlaps));
    const areas = await page.evaluate(() =>
      [...document.getElementById("subscriptionUsage").children]
        .map((c) => c.style.gridArea));
    check("packUsage 显式网格定位应用于全部卡片（含 OpenCSI）",
      areas.every((a) => a && a.includes("span")), JSON.stringify(areas));
    fs.writeFileSync(path.join(OUT_DIR, "grid-layout.png"), await page.screenshot());

    // ── 10: OpenCSI model expand → the grid re-packs ───────────────────────
    // The growth is asserted on the IFRAME: packUsage stretches a column's
    // last card to align bottoms, so the article's height can legitimately
    // absorb content growth — the iframe is the honest measure of content.
    const frameBefore = view.frameRect;
    await panelClick("expandBtn");
    await sleep(900);
    view = await readPanel();
    check("OpenCSI 展开详情：全部 5 行 + 收起入口", view.rowCount === 5 &&
      view.expandText === "收起详情", `rows=${view.rowCount}`);
    check("展开后 iframe 高度随内容增长（网格随之重排）",
      view.frameRect > frameBefore + 40,
      `iframe ${Math.round(frameBefore)} → ${Math.round(view.frameRect)}, card ${Math.round(view.cardRect)}`);
    geo = await gridGeometry();
    check("展开后网格重排且仍不重叠", geo.overlaps.length === 0,
      JSON.stringify(geo.overlaps));
    fs.writeFileSync(path.join(OUT_DIR, "card-multi-expanded.png"),
      await page.screenshot({ clip: await cardClip(page) }));
    await panelClick("expandBtn");
    await sleep(500);

    // ── single-model and zero scenarios ─────────────────────────────────────
    await pushFixtureSnapshot(SINGLE);
    await page.reload({ waitUntil: "domcontentloaded" });
    await sleep(1200);
    view = await readPanel();
    check("单模型：只显示模型名，不重复总量", view.sub.includes("DeepSeek-V4.1-Flash") &&
      !view.sub.includes("万") && view.rowCount === 0, JSON.stringify({ sub: view.sub }));

    await pushFixtureSnapshot(ZERO);
    await page.reload({ waitUntil: "domcontentloaded" });
    await sleep(1200);
    view = await readPanel();
    check("零用量：0 tokens / ¥0.00 + 今日暂无模型用量",
      view.tokens === "0" && view.cost === "¥0.00" && view.sub.includes("今日暂无模型用量"),
      JSON.stringify({ tokens: view.tokens, sub: view.sub }));

    // ── small money: a positive sub-cent amount is ¥<0.01, never ¥0.00 ────
    await pushFixtureSnapshot(TINY);
    await page.reload({ waitUntil: "domcontentloaded" });
    await sleep(1400);
    view = await readPanel();
    await panelClick("expandBtn"); // the default shows only the top model
    await sleep(600);
    const tinyRow = await (async () => {
      const f = await panelFrame();
      return f.evaluate(() => {
        const rows = [...document.querySelectorAll("#modelRows .model-row")];
        return rows.map((r) => r.textContent);
      });
    })();
    await panelClick("expandBtn");
    await sleep(400);
    check("小金额：¥<0.01 而非免费的 ¥0.00",
      tinyRow.some((t) => t.includes("¥<0.01")) && !tinyRow.some((t) => /¥0\.00/.test(t)),
      JSON.stringify(tinyRow));

    // ── dual-column headline: no truncation at card width ──────────────────
    const cols = await (async () => {
      const f = await panelFrame();
      return f.evaluate(() => {
        const a = document.getElementById("todayTokens");
        const b = document.getElementById("todayCost");
        return {
          aClipped: a.scrollWidth > a.clientWidth + 1,
          bClipped: b.scrollWidth > b.clientWidth + 1,
          stacked: document.getElementById("numbers").classList.contains("stacked"),
        };
      });
    })();
    check("双列指标不截断（或已自动切换上下布局）",
      (!cols.aClipped && !cols.bClipped) || cols.stacked, JSON.stringify(cols));

    // ── P0-1: a REAL click on the refresh button triggers a real round trip.
    // With the fixture cleared, a forced refresh hits the real network and the
    // honest auth state must appear — proving the click→message→SW path. ────
    await clearFixture();
    const refreshClick = await (async () => {
      const f = await panelFrame();
      return f.evaluate(() => {
        const btn = document.getElementById("refreshBtn");
        btn.click(); // a genuine DOM click, not a function call
        return true;
      });
    })();
    let refreshOutcome = "";
    for (let attempt = 0; attempt < 60; attempt++) {
      const f = await panelFrame();
      refreshOutcome = await f.evaluate(() =>
        (document.getElementById("dot") || { title: "" }).title);
      if (refreshOutcome && refreshOutcome !== "正在刷新" &&
          refreshOutcome !== "正在恢复登录") break;
      await sleep(1500);
    }
    check("手动刷新按钮（真实点击）触发真实刷新并恢复状态",
      refreshClick && refreshOutcome.length > 0 && refreshOutcome !== "加载中",
      `outcome: ${refreshOutcome}`);
    check("刷新节流：按钮存在且可再次点击", await (async () => {
      const f = await panelFrame();
      return f.evaluate(() => {
        const btn = document.getElementById("refreshBtn");
        return btn && !btn.disabled;
      });
    })());

    // restore the fixture for the remaining checks
    await pushFixtureSnapshot(MULTI);
    await page.reload({ waitUntil: "domcontentloaded" });
    await sleep(1400);

    // ── 11: login-expired state inside the grid card ───────────────────────
    // Deterministic half: the failure snapshot built by the production state
    // pipeline renders 需要登录 + the recovery button.
    await sw.evaluate(async () => {
      const empty = OpenCSICore.state.emptySnapshot();
      const failure = OpenCSICore.state.snapshotWithFailure(
        empty,
        OpenCSICore.state.STATE.LOGIN_REQUIRED,
        "OpenCSI 返回 HTTP 401",
        "点击「重新登录 OpenCSI」完成登录后回到本页",
        Date.now()
      );
      await chrome.storage.session.set({ opencsiSnapshot: failure });
    });
    await page.reload({ waitUntil: "domcontentloaded" });
    await sleep(5000);
    view = await readPanel();
    check("登录过期：卡片内显示需要登录", view.state === "需要登录", view.state);

    // ── the real-network auth path: clear the fixture, reload, and give the
    // real fetch + renewal cycle a generous, polled window ──────────────────
    await clearFixture();
    await page.reload({ waitUntil: "domcontentloaded" });
    let liveState = "";
    for (let attempt = 0; attempt < 90; attempt++) {
      const f = await panelFrame();
      liveState = await f.evaluate(() =>
        (document.getElementById("dot") || { title: "" }).title).catch(() => "");
      if (liveState && liveState !== "加载中" && liveState !== "正在刷新" &&
          liveState !== "正在恢复登录") break;
      await sleep(1500);
    }
    check("真实网络链路：面板渲染出诚实的认证状态", liveState.length > 0,
      `live state: ${liveState || "(empty)"}`);
    await sleep(800); // render sets loginBtn last, after the dot
    check("恢复入口随状态出现", await (async () => {
      const f = await panelFrame();
      return f.evaluate(() => !document.getElementById("loginBtn").hidden).catch(() => false);
    })());

    // restore the data fixture for the remaining checks
    await pushFixtureSnapshot(MULTI);
    await page.reload({ waitUntil: "domcontentloaded" });
    await sleep(1200);

    // ── 6: three viewport widths, no overlap, order intact ─────────────────
    for (const width of [480, 900, 1400]) {
      await page.setViewport({ width, height: 1000, deviceScaleFactor: 1 });
      await sleep(700);
      geo = await gridGeometry();
      const firstId = await page.evaluate(() =>
        document.getElementById("subscriptionUsage").children[0].id);
      check(`宽度 ${width}px：不重叠且 OpenCSI 仍第一格`,
        geo.overlaps.length === 0 && firstId === "opencsi-usage-card",
        JSON.stringify({ overlaps: geo.overlaps, first: firstId }));
      if (width === 900) {
        fs.writeFileSync(path.join(OUT_DIR, "grid-900.png"), await page.screenshot());
      }
    }

    // ── theme follows Magpie's manual choice, not the OS ────────────────────
    // Force dark via Magpie's own attribute, then rebuild the card the way
    // a real quota refresh does (renderQuotas wipe → our guard re-inserts).
    // A page reload would drop the attribute here — the mock does not
    // persist it, while real Magpie re-applies its saved theme on load.
    await page.evaluate(() => {
      document.documentElement.dataset.theme = "dark";
    });
    await page.evaluate(() => window.__mock.renderQuotasLite());
    await sleep(1500);
    const themeAfterRebuild = await page.evaluate(() => {
      const f = document.querySelector("#opencsi-usage-card iframe");
      return { srcHasDark: f.src.includes("theme=dark") };
    });
    let panelThemeAttr = "";
    for (let attempt = 0; attempt < 15; attempt++) {
      const f = await panelFrame();
      panelThemeAttr = await f.evaluate(() =>
        document.documentElement.dataset.opencsiTheme || "").catch(() => "");
      if (panelThemeAttr) break;
      await sleep(400);
    }
    check("主题：Magpie 手动 dark → 重建卡片 URL 携带 theme=dark 且面板应用",
      themeAfterRebuild.srcHasDark && panelThemeAttr === "dark",
      JSON.stringify(themeAfterRebuild) + ` attr=${panelThemeAttr}`);
    // Live flip back to light without reload (postMessage relay)
    await page.evaluate(() => {
      document.documentElement.dataset.theme = "light";
    });
    await sleep(800);
    const liveTheme = await (async () => {
      const f = await panelFrame();
      return f.evaluate(() => document.documentElement.dataset.opencsiTheme || "");
    })();
    check("主题：实时切换 light（postMessage 中继，无需重载）",
      liveTheme === "light", `attr=${liveTheme}`);
    await page.evaluate(() => {
      delete document.documentElement.dataset.theme;
    });
    await sleep(300);

    // ── browser zoom: 125% and 150% scale factors keep the grid sane ──────
    for (const dsf of [1.25, 1.5]) {
      await page.setViewport({ width: 1400, height: 1000, deviceScaleFactor: dsf });
      await sleep(700);
      geo = await gridGeometry();
      const firstAtZoom = await page.evaluate(() =>
        document.getElementById("subscriptionUsage").children[0].id);
      check(`缩放 ${Math.round(dsf * 100)}%：不重叠且 OpenCSI 仍第一格`,
        geo.overlaps.length === 0 && firstAtZoom === "opencsi-usage-card",
        JSON.stringify(geo.overlaps));
    }
    await page.setViewport({ width: 1400, height: 1000, deviceScaleFactor: 1 });

    // ── 7: view switch away and back ────────────────────────────────────────
    await page.evaluate(() => window.__mock.showView("agents"));
    await sleep(300);
    const onAgents = await page.evaluate(() => ({
      usageHidden: document.getElementById("view-usage").hidden,
      cardThere: !!document.getElementById("opencsi-usage-card"),
    }));
    await page.evaluate(() => window.__mock.showView("usage"));
    await sleep(300);
    const backOnUsage = await page.evaluate(() => ({
      usageHidden: document.getElementById("view-usage").hidden,
      cardThere: !!document.getElementById("opencsi-usage-card"),
      count: document.querySelectorAll("#opencsi-usage-card").length,
    }));
    check("视图切换：面板随 Usage 隐藏并在返回时恢复（无重复）",
      onAgents.usageHidden === true && backOnUsage.cardThere &&
      backOnUsage.count === 1,
      JSON.stringify({ onAgents, backOnUsage }));

    // ── 8+14: native quota refreshes keep exactly one OpenCSI card, and the
    // observer singleton holds — 100 refreshes must not grow observers, cards
    // or DOM nodes (P0-2's regression test). ────────────────────────────────
    const baselineNodes = await page.evaluate(() => document.getElementsByTagName("*").length);
    const diagBefore = await page.evaluate(() => {
      const card = document.getElementById("opencsi-usage-card");
      return card
        ? { observerCount: Number(card.dataset.observerCount) }
        : { observerCount: -1 };
    });
    for (let i = 0; i < 100; i++) {
      await page.evaluate(() => window.__mock.renderQuotasLite());
      if (i % 20 === 19) await sleep(120); // let observers settle in batches
    }
    await sleep(500);
    const afterRefreshes = await page.evaluate((baseline) => {
      const grid = document.getElementById("subscriptionUsage");
      return {
        ours: document.querySelectorAll("#opencsi-usage-card").length,
        first: grid.children[0].id,
        native: [...grid.children].filter((k) => k.dataset.provider).map((k) => k.dataset.provider),
        nodeDelta: document.getElementsByTagName("*").length - baseline,
        diag: {
          observerCount: Number(grid.children[0].dataset.observerCount || -1),
        },
      };
    }, baselineNodes);
    check("100 次原生刷新后仍只有一张 OpenCSI 卡且在第一格",
      afterRefreshes.ours === 1 && afterRefreshes.first === "opencsi-usage-card" &&
      JSON.stringify(afterRefreshes.native) ===
      JSON.stringify(["opencode-go", "codex", "workbuddy", "deepseek"]),
      JSON.stringify(afterRefreshes));
    check("观察器注册受控（刷新不增长观察器）",
      afterRefreshes.diag.observerCount <= diagBefore.observerCount + 1,
      `before=${diagBefore.observerCount} after=${afterRefreshes.diag.observerCount}`);
    check("DOM 节点数不持续增长",
      afterRefreshes.nodeDelta <= 5,
      `delta=${afterRefreshes.nodeDelta} nodes over 100 refreshes`);

    // After the stress the grid must still respond to height changes.
    await panelClick("expandBtn");
    await sleep(700);
    await panelClick("expandBtn");
    await sleep(500);
    const stillPacking = await page.evaluate(() => {
      const areas = [...document.getElementById("subscriptionUsage").children]
        .map((c) => c.style.gridArea);
      return areas.every((a) => a && a.includes("span"));
    });
    check("压力刷新后网格仍响应高度变化", stillPacking);

    // ── 12: native reorder keeps OpenCSI first and unharmed ─────────────────
    await page.evaluate(() => window.__mock.moveUsageLite("deepseek", 0));
    await sleep(400);
    const afterMove = await page.evaluate(() => {
      const grid = document.getElementById("subscriptionUsage");
      return {
        first: grid.children[0].id,
        native: [...grid.children].filter((k) => k.dataset.provider).map((k) => k.dataset.provider),
        ours: document.querySelectorAll("#opencsi-usage-card").length,
      };
    });
    check("原生排序变更：原生顺序改变，OpenCSI 仍在第一格",
      afterMove.first === "opencsi-usage-card" &&
      JSON.stringify(afterMove.native) ===
      JSON.stringify(["deepseek", "opencode-go", "codex", "workbuddy"]) &&
      afterMove.ours === 1,
      JSON.stringify(afterMove));

    // ── 9: native card fold changes height → the grid re-packs ──────────────
    await page.evaluate(() => window.__mock.toggleNativeFold("codex"));
    await sleep(500);
    geo = await gridGeometry();
    check("原生卡片折叠后网格重排仍不重叠", geo.overlaps.length === 0,
      JSON.stringify(geo.overlaps));

    // ── 13: a quota refresh preserves the page's scroll position ────────────
    await page.evaluate(() => { window.scrollTo(0, 200); });
    await sleep(200);
    const scrollBefore = await page.evaluate(() => window.scrollY);
    await page.evaluate(() => window.__mock.renderQuotasLite());
    await sleep(500);
    const scrollAfter = await page.evaluate(() => window.scrollY);
    check("额度刷新保持页面滚动位置", Math.abs(scrollAfter - scrollBefore) <= 40,
      `${scrollBefore} → ${scrollAfter}`);

    // ── 15: expand/collapse cycles settle without a resize loop ─────────────
    for (let i = 0; i < 3; i++) {
      await panelClick("expandBtn");
      await sleep(350);
      await panelClick("expandBtn");
      await sleep(350);
    }
    view = await readPanel();
    check("反复展开/收起后高度稳定（无 ResizeObserver 循环）",
      /^\d+px$/.test(view.frameStyleHeight) && /查询于/.test(view.updated),
      `style=${view.frameStyleHeight}`);
    fs.writeFileSync(path.join(OUT_DIR, "grid-final.png"), await page.screenshot());

    check("页面无 Console Error / 未处理异常", consoleErrors.length === 0,
      consoleErrors.join(" | ").slice(0, 300));
  } finally {
    await browser.close();
  }

  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
  process.exit(failed.length ? 1 : 0);
}

async function cardClip(page) {
  const box = await page.evaluate(() => {
    const card = document.getElementById("opencsi-usage-card");
    const r = card.getBoundingClientRect();
    return { x: r.x, y: Math.max(0, r.y), width: r.width, height: Math.min(r.height, 560) };
  });
  return box;
}

main().catch((error) => {
  console.error("E2E harness error:", error);
  process.exit(2);
});
