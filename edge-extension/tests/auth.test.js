// tests/auth.test.js — the auth state machine against a scripted tabs API.
//
// These tests encode the hard-won rules: single-flight renewals, one automatic
// attempt per episode, a human page stops the automation, our tab is closed
// when the flow is done with it and kept when a human can still use it.
const test = require("node:test");
const assert = require("node:assert");

const authModule = require("../background/auth-manager.js");
const { AuthManager, AUTH_STATE, OAUTH_ENTRY } = authModule;

/** A chrome.tabs stand-in: created tabs, fired URL updates, call records. */
function fakeTabs() {
  const api = {
    created: [],
    removed: [],
    nextTabId: 500,
    listeners: [],
    onUpdated: {
      addListener(fn) {
        api.listeners.push(fn);
      },
      removeListener(fn) {
        api.listeners = api.listeners.filter((f) => f !== fn);
      },
    },
    async create(details) {
      const tab = { id: ++api.nextTabId, url: details.url, active: !!details.active };
      api.created.push(tab);
      return tab;
    },
    async remove(tabId) {
      api.removed.push(tabId);
    },
    async navigate(tabId, url) {
      for (const fn of [...api.listeners]) fn(tabId, { url });
    },
    lastCreated() {
      return api.created[api.created.length - 1];
    },
  };
  return api;
}

function makeManager(tabs) {
  return new AuthManager({ tabs, now: () => Date.now(), log: () => {} });
}

/** Flush the microtask chain so the awaited tab.create has resolved. */
const flush = () => new Promise((resolve) => setImmediate(resolve));

test("OAuth 入口 URL 是已验证的授权起点", () => {
  assert.strictEqual(
    OAUTH_ENTRY,
    "https://opencsitool.com/opencsitool/rest/v1/oauth2/authorization/gitcode?redirect=%2FmyTools"
  );
});

test("回调落地 opencsitool.com：VALID 且关闭自己的标签页", async () => {
  const tabs = fakeTabs();
  const manager = makeManager(tabs);
  const pending = manager.maybeRenew();
  await flush();
  const tab = tabs.lastCreated();
  assert.strictEqual(tab.active, false, "恢复标签页必须是后台标签页");
  await tabs.navigate(tab.id, "https://gitcode.com/oauth/authorize?x=1");
  await tabs.navigate(tab.id, "https://opencsitool.com/opencsitool/rest/v1/oauth2/authorization/callback/gitcode?code=x");
  assert.strictEqual(await pending, "VALID");
  assert.strictEqual(manager.state, AUTH_STATE.VALID);
  assert.ok(tabs.removed.includes(tab.id), "流程结束必须关闭自己的标签页");
});

test("GitCode 登录页：LOGIN_REQUIRED（需要人工）", async () => {
  const tabs = fakeTabs();
  const manager = makeManager(tabs);
  const pending = manager.maybeRenew();
  await flush();
  const tab = tabs.lastCreated();
  await tabs.navigate(tab.id, "https://gitcode.com/login?redirect=x");
  assert.strictEqual(await pending, "LOGIN_REQUIRED");
  assert.strictEqual(manager.state, AUTH_STATE.LOGIN_REQUIRED);
  assert.ok(manager.recovery.includes("重新登录"));
  assert.ok(tabs.removed.includes(tab.id));
});

test("授权页停留超过宽限期：CONSENT_REQUIRED 且保留标签页给人工", async () => {
  const tabs = fakeTabs();
  let now = 1_000_000; // 在默认冷却期之外，首次恢复才会真正启动
  const manager = new AuthManager({
    tabs,
    now: () => now,
    options: { consentGraceMs: 100, tabTimeoutMs: 10000 },
  });
  const pending = manager.maybeRenew();
  await flush();
  const tab = tabs.lastCreated();
  await flush(); // the watcher attaches in the continuation after create
  await tabs.navigate(tab.id, "https://gitcode.com/oauth/authorize?client_id=1");
  // 首次到达只记录时间。
  assert.strictEqual(manager.state, AUTH_STATE.RENEWING);
  now += 200; // 超过宽限期仍未离开
  await tabs.navigate(tab.id, "https://gitcode.com/oauth/authorize?client_id=1");
  assert.strictEqual(await pending, "CONSENT_REQUIRED");
  assert.strictEqual(manager.state, AUTH_STATE.CONSENT_REQUIRED);
  assert.strictEqual(
    tabs.removed.includes(tab.id),
    false,
    "人工可以在授权页上点击批准，标签页必须保留"
  );
});

test("单飞：并发恢复只开一个标签页", async () => {
  const tabs = fakeTabs();
  const manager = makeManager(tabs);
  const a = manager.maybeRenew();
  const b = manager.maybeRenew();
  const c = manager.maybeRenew();
  await flush();
  assert.strictEqual(tabs.created.length, 1, "三个并发请求只应创建一个标签页");
  const tab = tabs.lastCreated();
  await tabs.navigate(tab.id, "https://opencsitool.com/myTools");
  const results = await Promise.all([a, b, c]);
  assert.deepStrictEqual(results, ["VALID", "VALID", "VALID"]);
});

test("同一故障期只自动尝试一次（冷却期内不再开标签页）", async () => {
  const tabs = fakeTabs();
  let now = 1_000_000;
  const manager = new AuthManager({ tabs, now: () => now, options: { retryCooldownMs: 60000, tabTimeoutMs: 500 } });
  const first = await manager.maybeRenew(); // 超时 → FAILED
  assert.strictEqual(first, "FAILED");
  const createdAfterFirst = tabs.created.length;
  const second = await manager.maybeRenew();
  assert.strictEqual(second, "FAILED", "冷却期内直接失败，不再打开标签页");
  assert.strictEqual(tabs.created.length, createdAfterFirst);
  // 冷却期过后允许再试。
  now += 61000;
  const third = manager.maybeRenew();
  await flush();
  assert.strictEqual(tabs.created.length, createdAfterFirst + 1);
  const tab = tabs.lastCreated();
  await tabs.navigate(tab.id, "https://opencsitool.com/myTools");
  assert.strictEqual(await third, "VALID");
});

test("用户人工登录后立即解除冷却", async () => {
  const tabs = fakeTabs();
  let now = 1_000_000;
  const manager = new AuthManager({ tabs, now: () => now, options: { retryCooldownMs: 60000, tabTimeoutMs: 500 } });
  await manager.maybeRenew();
  manager.onUserAction();
  assert.strictEqual(manager.state, AUTH_STATE.VALID);
  now += 1; // 未过冷却期，但人工行为重置状态
  const pending = manager.maybeRenew();
  await flush();
  const tab = tabs.lastCreated();
  await tabs.navigate(tab.id, "https://opencsitool.com/myTools");
  assert.strictEqual(await pending, "VALID");
});

test("默认标签页超时收敛为 FAILED", async () => {
  const tabs = fakeTabs();
  const manager = new AuthManager({
    tabs,
    now: () => Date.now(),
    options: { tabTimeoutMs: 60, consentGraceMs: 30 },
  });
  const outcome = await manager.maybeRenew();
  assert.strictEqual(outcome, "FAILED");
  assert.strictEqual(manager.state, AUTH_STATE.SESSION_EXPIRED);
  // 超时后自己的标签页被关闭，不留孤儿标签。
  assert.ok(tabs.removed.length >= 1);
});
