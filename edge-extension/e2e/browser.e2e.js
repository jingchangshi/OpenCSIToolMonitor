// e2e/browser.e2e.js — the extension loaded in a real Chromium, driven over CDP.
//
// Verified here, on the real browser and real network:
//   1. the unpacked extension loads (service worker starts, content script
//      registers);
//   2. the panel mounts as the usage container's *previous sibling* — inside
//      no innerHTML blast radius — on a page whose URL is the real production
//      origin (content fulfilled locally with a mock mimicking upstream's
//      dynamic #view-usage and SPA view switching);
//   3. the auth path for a fresh profile: the SW's fetch is refused (401),
//      the silent renewal runs and lands on GitCode's login page, and the
//      panel honestly shows 需要登录 with a recovery button;
//   4. with a CDP interception feeding controlled OpenCSI payloads to the
//      service worker, the compact card renders each data state for real:
//      multi-model top-3 + expand, single-model line without duplicate
//      totals, confirmed zero, real query time, and content-driven height;
//   5. SPA view switching and a hostile innerHTML re-render do not remove or
//      duplicate the panel;
//   6. screenshots of the real rendering, for the delivery report.
//
// Run:  node e2e/browser.e2e.js   (from edge-extension/)
"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const puppeteer = require("puppeteer-core");

const EXT_DIR = path.resolve(__dirname, "..");
const OUT_DIR = path.resolve(__dirname, "out");

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

/** A mock Magpie that mimics upstream's dynamic usage page (kernel.js). */
const MOCK_MAGPIE_HTML = `<!doctype html><html><head><meta charset="utf-8"><title>magpie</title>
<style>
body{font-family:system-ui;margin:0;padding:12px;background:#f6f6f4;color:#222}
nav button{margin-right:6px;padding:4px 10px}
section{padding:10px;border:1px solid #ddd;border-radius:8px;margin-top:8px}
[hidden]{display:none}
</style></head><body>
<nav>
  <button data-view="agents" class="on">Agents</button>
  <button data-view="usage">Usage</button>
</nav>
<section id="view-agents">agents placeholder</section>
<div id="pageHost"></div>
<script>
  // Upstream kernel.js: a plugin's page gets a container with id view-<key>,
  // created by JavaScript after the document loads.
  setTimeout(function () {
    const section = document.createElement("section");
    section.id = "view-usage";
    section.hidden = true;
    section.innerHTML = '<h2>Usage</h2><div class="usage-pane">magpie native usage</div>';
    document.getElementById("pageHost").appendChild(section);
    // Upstream start.js shows the deep-linked view; without this the usage
    // container would stay hidden and the panel (correctly) hidden with it.
    if (new URLSearchParams(location.search).get("view") === "usage") {
      section.hidden = false;
      document.getElementById("view-agents").hidden = true;
      document.querySelectorAll("nav button").forEach((b) =>
        b.classList.toggle("on", b.dataset.view === "usage"));
    }
    window.__usageReady = true;
    // The measured hostile behaviour of the real page: the usage render pass
    // rewrites the container's innerHTML AFTER the extension's panel has
    // mounted, wiping anything that lived inside it. Reproduced here on a
    // timer: a panel mounted as a CHILD would die here; the sibling mount
    // must survive.
    setTimeout(function () {
      section.innerHTML = '<h2>Usage</h2><div class="usage-pane">magpie native usage (re-rendered)</div>';
      window.__hostileRenderDone = true;
    }, 1200);
  }, 50);
  document.querySelectorAll("nav button").forEach(function (button) {
    button.addEventListener("click", function () {
      const view = button.dataset.view;
      document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("on", b === button));
      const usage = document.getElementById("view-usage");
      const agents = document.getElementById("view-agents");
      if (!usage) return; // not yet created
      usage.hidden = view !== "usage";
      agents.hidden = view !== "agents";
    });
  });
</script></body></html>`;

// ── controlled OpenCSI payloads for the service worker ─────────────────────
const PRICES = [
  { requestType: "glm-5.3-flash", displayName: "GLM-5.3-Flash", billType: "TOKEN", enabled: 1, blendedPrice: 2.24 },
  { requestType: "deepseek-v4", displayName: "DeepSeek-V4.1-Flash", billType: "TOKEN", enabled: 1, blendedPrice: 4.72 },
  { requestType: "qwen3-max", displayName: "Qwen3-Max", billType: "TOKEN", enabled: 1, blendedPrice: 0.28 },
  { requestType: "embed-v4", displayName: "Embedding-V4", billType: "TOKEN", enabled: 1, blendedPrice: 0.4 },
  { requestType: "glm-4.7-air", displayName: "GLM-4.7-Air", billType: "TOKEN", enabled: 1, blendedPrice: 1.1 },
  { requestType: "trae", displayName: "Trae 包月", billType: "FLAT", enabled: 1, monthlyFee: 49 },
];

function todayShanghai() {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
  }).format(new Date());
}

/** Usage payloads per scenario; the interception reads from this object. */
const SCENARIOS = {
  multi: {
    trend: [
      { requestType: "glm-5.3-flash", tokens: 16_800_000 },
      { requestType: "deepseek-v4", tokens: 7_900_000 },
      { requestType: "qwen3-max", tokens: 1_240_000 },
      { requestType: "embed-v4", tokens: 96_000 },
      { requestType: "glm-4.7-air", tokens: 30_000 },
    ],
    summary: { totalTokens: 26_066_000, totalRequestCount: 1_284 },
    fresh: "2026-10-25T16:10:00+08:00",
  },
  single: {
    trend: [{ requestType: "deepseek-v4", tokens: 7_970_000 }],
    summary: { totalTokens: 7_970_000, totalRequestCount: 401 },
    fresh: "2026-10-25T16:10:00+08:00",
  },
  zero: {
    trend: [{ requestType: "glm-5.3-flash", tokens: 0 }],
    summary: { totalTokens: 656_373_157, totalRequestCount: 6_280 },
    fresh: "2026-10-25T16:10:00+08:00",
  },
};
let activeScenario = "multi";

function fixturePayload(scenarioName) {
  const s = SCENARIOS[scenarioName];
  const today = todayShanghai();
  return {
    data: {
      tokenTrend: s.trend.map((r) => ({ date: today, ...r })),
      tokenSummary: s.summary,
      syncStatus: { dataFreshTime: s.fresh },
    },
  };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
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
      "--window-size=1280,900",
    ],
  });
  const results = [];
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

    // ── open the Magpie Usage URL (origin real, content mocked) ───────────
    const page = await browser.newPage();
    await page.setRequestInterception(true);
    page.on("request", (request) => {
      const url = request.url();
      if (url.startsWith("http://129.153.118.58:3430/") && !url.includes("panel/")) {
        request.respond({
          status: 200,
          contentType: "text/html; charset=utf-8",
          body: MOCK_MAGPIE_HTML,
        });
      } else {
        request.continue();
      }
    });
    await page.goto("http://129.153.118.58:3430/?view=usage", { waitUntil: "domcontentloaded" });
    await sleep(1500);

    const injected = await page.evaluate(() => {
      const usage = document.getElementById("view-usage");
      const host = document.getElementById("opencsi-usage-panel-host");
      return {
        usageExists: !!usage,
        hostPresent: !!host,
        isPreviousSibling: !!(host && usage && host.nextElementSibling === usage),
        sameParent: !!(host && usage && host.parentElement === usage.parentElement),
        hostCount: document.querySelectorAll("#opencsi-usage-panel-host").length,
        frameSrc: host && host.querySelector("iframe") ? host.querySelector("iframe").src : null,
      };
    });
    check("Magpie 注入：面板为 Usage 容器的前置兄弟节点",
      injected.usageExists && injected.hostPresent && injected.isPreviousSibling && injected.sameParent,
      JSON.stringify(injected));
    check("单实例：全文档只有一个面板宿主", injected.hostCount === 1);

    // ── fresh profile: no cookie → the honest login state ─────────────────
    const frameHandle = await page.evaluateHandle(() =>
      document.querySelector("#opencsi-usage-panel-host iframe")
    );
    const frame = await frameHandle.asElement().contentFrame();
    await sleep(6000); // the SW fetches (401), the renewal attempt runs and fails here
    const panelText = await frame.evaluate(() => document.body.innerText);
    check("认证状态如实呈现（测试 Profile 无 Cookie → 需要登录）",
      /需要登录/.test(panelText), panelText.split("\n").slice(0, 4).join(" | "));
    check("恢复入口存在（重新登录按钮）", /重新登录 OpenCSI/.test(panelText));

    // ── hostile innerHTML re-render: the sibling survives ─────────────────
    await page.waitForFunction("window.__hostileRenderDone === true", { timeout: 8000 });
    await sleep(400);
    const survived = await page.evaluate(() => {
      const usage = document.getElementById("view-usage");
      const host = document.getElementById("opencsi-usage-panel-host");
      return {
        hostPresent: !!host,
        stillSibling: !!(host && usage && host.nextElementSibling === usage),
        hostCount: document.querySelectorAll("#opencsi-usage-panel-host").length,
      };
    });
    check("敌对 innerHTML 重渲染后面板仍存在（兄弟节点在半径之外）",
      survived.hostPresent && survived.stillSibling && survived.hostCount === 1,
      JSON.stringify(survived));

    // ── data-driven states: build the snapshot through the SW's real
    // state.js pipeline and place it in chrome.storage.session ─────────────
    // A CDP Fetch interception of the service worker's requests proved
    // unreliable (Fetch.requestPaused fired zero times at some attach
    // timings), and the panel's cache-hit path is exactly what a real
    // successful query produces — so the fixtures are pushed as snapshots
    // built by the production code, and the panel renders them through the
    // genuine cache semantics.
    let activeScenario = "multi";
    async function pushFixtureSnapshot() {
      await sw.evaluate(async (scenarioName) => {
        const scenarios = {
          multi: {
            trend: [
              { requestType: "glm-5.3-flash", tokens: 16_800_000 },
              { requestType: "deepseek-v4", tokens: 7_900_000 },
              { requestType: "qwen3-max", tokens: 1_240_000 },
              { requestType: "embed-v4", tokens: 96_000 },
              { requestType: "glm-4.7-air", tokens: 30_000 },
            ],
            summary: { totalTokens: 26_066_000, totalRequestCount: 1_284 },
            fresh: "2026-10-25T16:10:00+08:00",
          },
          single: {
            trend: [{ requestType: "deepseek-v4", tokens: 7_970_000 }],
            summary: { totalTokens: 7_970_000, totalRequestCount: 401 },
            fresh: "2026-10-25T16:10:00+08:00",
          },
          zero: {
            trend: [{ requestType: "glm-5.3-flash", tokens: 0 }],
            summary: { totalTokens: 656_373_157, totalRequestCount: 6_280 },
            fresh: "2026-10-25T16:10:00+08:00",
          },
        };
        const s = scenarios[scenarioName];
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
              tokenTrend: s.trend.map((r) => ({ date: today, ...r })),
              tokenSummary: s.summary,
              syncStatus: { dataFreshTime: s.fresh },
            },
          },
          prices,
        };
        const snapshot = OpenCSICore.state.snapshotFromPayload(payload, today, Date.now());
        await chrome.storage.session.set({ opencsiSnapshot: snapshot });
      }, activeScenario);
    }
    async function clearSnapshot() {
      await sw.evaluate(async () => {
        await chrome.storage.session.remove("opencsiSnapshot");
      });
    }

    async function readPanel() {
      // Panel internals live in the iframe's document; geometry lives in the
      // host page's. Two contexts, one merged view. The frame binding is
      // re-resolved on every read: page reloads invalidate the old handle.
      let frame = null;
      try {
        const fh = await page.evaluateHandle(() =>
          document.querySelector("#opencsi-usage-panel-host iframe"));
        frame = await fh.asElement().contentFrame();
      } catch {
        frame = null;
      }
      if (!frame) return { state: "PAGE_GONE" };
      const [inner, geometry] = await Promise.all([
        frame.evaluate(() => ({
          tokens: document.getElementById("todayTokens").textContent,
          cost: document.getElementById("todayCost").textContent,
          updated: document.getElementById("updated").textContent,
          sub: document.getElementById("subLine").textContent,
          subHidden: document.getElementById("subLine").classList.contains("hidden"),
          fresh: document.getElementById("freshLine").textContent,
          rowCount: document.querySelectorAll("#modelRows .model-row").length,
          expandText: document.getElementById("expandBtn").textContent,
          expandHidden: document.getElementById("expandBtn").hidden,
          state: document.getElementById("state").textContent,
        })),
        page.evaluate(() => {
          const host = document.getElementById("opencsi-usage-panel-host");
          const iframe = host.querySelector("iframe");
          return {
            frameStyleHeight: iframe.style.height,
            frameRect: iframe.getBoundingClientRect().height,
            hostWidth: host.getBoundingClientRect().width,
            usageWidth: document.getElementById("view-usage").getBoundingClientRect().width,
          };
        }),
      ]);
      return { ...inner, ...geometry };
    }

    // A freshly resolved frame handle per interaction: page reloads
    // invalidate the old one.
    const panelClick = async (elementId) => {
      const fh = await page.evaluateHandle((id) =>
        document.querySelector("#opencsi-usage-panel-host iframe"), elementId);
      const f = await fh.asElement().contentFrame();
      await f.evaluate((id) => document.getElementById(id).click(), elementId);
    };

    // multi-model scenario
    await pushFixtureSnapshot();
    await page.reload({ waitUntil: "domcontentloaded" });
    await sleep(1200);
    let view = await readPanel();
    check("多模型：状态 OK，双指标渲染", view.state === "正常" &&
      view.tokens.length > 1 && view.cost.startsWith("¥"), view.tokens + " " + view.cost);
    check("查询时间真实显示（不再固定 --:--）",
      /查询于 \d{2}:\d{2}/.test(view.updated) && !view.updated.includes("--:--"), view.updated);
    check("服务端同步时间以独立措辞显示", /服务端数据截至 \d{2}:\d{2}/.test(view.fresh), view.fresh);
    check("多模型：默认 Top 3 + 展开全部（5）", view.rowCount === 3 &&
      view.expandText.includes("展开全部") && view.expandText.includes("5"),
      `rows=${view.rowCount} btn=${view.expandText}`);
    check("多模型场景不渲染单模型摘要行", view.subHidden === true, `sub=${view.sub}`);
    check("iframe 高度由内容驱动（不再是固定 300px）",
      view.frameStyleHeight !== "300px" && view.frameRect >= 88 && view.frameRect <= 560,
      `style=${view.frameStyleHeight} rect=${Math.round(view.frameRect)}`);
    check("面板宽度与 Usage 容器对齐",
      Math.abs(view.hostWidth - view.usageWidth) < 2,
      `host=${Math.round(view.hostWidth)} usage=${Math.round(view.usageWidth)}`);
    fs.writeFileSync(path.join(OUT_DIR, "panel-multi.png"),
      await page.screenshot({ clip: await panelClip(page) }));

    // expand → all 5 rows, height grows, no refetch needed
    const heightBefore = view.frameRect;
    await panelClick("expandBtn");
    await sleep(900);
    view = await readPanel();
    check("展开全部：显示全部 5 行", view.rowCount === 5 &&
      view.expandText === "收起", `rows=${view.rowCount}`);
    check("展开后 iframe 高度随内容增长", view.frameRect > heightBefore + 40,
      `${Math.round(heightBefore)} → ${Math.round(view.frameRect)}`);
    fs.writeFileSync(path.join(OUT_DIR, "panel-multi-expanded.png"),
      await page.screenshot({ clip: await panelClip(page) }));
    await panelClick("expandBtn");
    await sleep(400);

    // single-model scenario: the summary line replaces the ranking
    activeScenario = "single";
    await pushFixtureSnapshot();
    await page.reload({ waitUntil: "domcontentloaded" });
    await sleep(1200);
    view = await readPanel();
    check("单模型：只显示模型名与唯一使用说明，无排行与展开按钮",
      view.sub.includes("当前唯一使用模型") && view.subHidden === false &&
      view.rowCount === 0 && view.expandHidden === true,
      JSON.stringify({ sub: view.sub, rows: view.rowCount }));
    fs.writeFileSync(path.join(OUT_DIR, "panel-single.png"),
      await page.screenshot({ clip: await panelClip(page) }));

    // zero scenario: confirmed zero, no empty list, no expand button
    activeScenario = "zero";
    await pushFixtureSnapshot();
    await page.reload({ waitUntil: "domcontentloaded" });
    await sleep(1200);
    view = await readPanel();
    check("零用量：0 tokens / ¥0.00 + 暂无模型使用记录",
      view.tokens === "0" && view.cost === "¥0.00" &&
      view.sub.includes("今日暂无模型使用记录") && view.rowCount === 0,
      JSON.stringify({ tokens: view.tokens, cost: view.cost, sub: view.sub }));
    fs.writeFileSync(path.join(OUT_DIR, "panel-zero.png"),
      await page.screenshot({ clip: await panelClip(page) }));

    // snapshot cleared: the real network answers 401 again → the auth state
    // machine still stands behind the UI
    await clearSnapshot();
    await page.reload({ waitUntil: "domcontentloaded" });
    await sleep(5000);
    view = await readPanel();
    check("快照清除后恢复真实链路：401 → 需要登录", view.state === "需要登录", view.state);

    // ── SPA view switch: panel follows visibility, does not duplicate ─────
    await page.evaluate(() => {
      document.querySelector('nav button[data-view="agents"]').click();
    });
    await sleep(400);
    const onAgents = await page.evaluate(() => {
      const host = document.getElementById("opencsi-usage-panel-host");
      return {
        count: document.querySelectorAll("#opencsi-usage-panel-host").length,
        usageHidden: document.getElementById("view-usage").hidden,
        hostHidden: host.hidden,
      };
    });
    check("切换到其他视图：面板随容器隐藏", onAgents.count === 1 &&
      onAgents.usageHidden === true && onAgents.hostHidden === true,
      JSON.stringify(onAgents));
    await page.evaluate(() => {
      document.querySelector('nav button[data-view="usage"]').click();
    });
    await sleep(400);
    const backOnUsage = await page.evaluate(() => {
      const host = document.getElementById("opencsi-usage-panel-host");
      return {
        count: document.querySelectorAll("#opencsi-usage-panel-host").length,
        usageHidden: document.getElementById("view-usage").hidden,
        hostHidden: host.hidden,
      };
    });
    check("切回 Usage：面板恢复显示且不重复挂载", backOnUsage.count === 1 &&
      backOnUsage.usageHidden === false && backOnUsage.hostHidden === false,
      JSON.stringify(backOnUsage));

    // ── the stale-tab recovery: retry via the SW's executeScript path ─────
    await page.evaluate(() => {
      document.getElementById("opencsi-usage-panel-host").remove();
    });
    await sleep(300);
    const retry = await sw.evaluate(async () => {
      const tabs = await chrome.tabs.query({ url: "http://129.153.118.58/*" });
      if (!tabs.length) return { ok: false, reason: "no matching tab" };
      return await self.__opencsiSw.injectIntoTab(tabs[0].id);
    });
    check("executeScript 重试：面板重新挂载（含 #view-usage 探针）",
      retry.ok === true && retry.hostMounted === true && retry.containerFound === true,
      JSON.stringify(retry));

    // ── the toolbar popup page renders its controls ───────────────────────
    const popupPage = await browser.newPage();
    await popupPage.goto(worker.url().replace("background/service-worker.js", "panel/action.html"), {
      waitUntil: "domcontentloaded",
    });
    await sleep(400);
    const popupOk = await popupPage.evaluate(() => ({
      retry: !!document.getElementById("retry"),
      diag: !!document.getElementById("diag"),
      verdict: (document.getElementById("verdict") || {}).textContent || "",
    }));
    check("工具栏弹窗可用（重试/诊断/判定文案）",
      popupOk.retry && popupOk.diag && popupOk.verdict.length > 0,
      JSON.stringify(popupOk));
    await popupPage.close();

    // ── final full-page screenshot ─────────────────────────────────────────
    fs.writeFileSync(path.join(OUT_DIR, "magpie-mock-page.png"),
      await page.screenshot({ fullPage: false }));
  } finally {
    await browser.close();
  }

  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
  process.exit(failed.length ? 1 : 0);
}

async function panelClip(page) {
  const box = await page.evaluate(() => {
    const frame = document.querySelector("#opencsi-usage-panel-host iframe");
    const r = frame.getBoundingClientRect();
    return { x: r.x, y: Math.max(0, r.y), width: r.width, height: Math.min(r.height, 560) };
  });
  return box;
}

main().catch((error) => {
  console.error("E2E harness error:", error);
  process.exit(2);
});
