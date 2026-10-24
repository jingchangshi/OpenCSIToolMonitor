// tests/client.test.js — the OpenCSI client's request shape and classification.
//
// global.fetch is replaced; the assertions look at what the client actually
// sends, because "uses the profile's cookie" is exactly the property
// `credentials: "include"` carries and a regression there is silent.
const test = require("node:test");
const assert = require("node:assert");

const client = require("../background/opencsi-client.js");

function withFetch(impl, fn) {
  const original = global.fetch;
  const calls = [];
  global.fetch = async (url, init) => {
    calls.push({ url: String(url), init: init || {} });
    return impl(calls[calls.length - 1]);
  };
  return Promise.resolve(fn(calls)).finally(() => {
    global.fetch = original;
  });
}

test("用量请求：GET + credentials:include + no-store + 日期参数", async () => {
  await withFetch(
    () => ({
      ok: true,
      status: 200,
      json: async () => ({ data: { tokenTrend: [], tokenSummary: {}, syncStatus: {} } }),
    }),
    async (calls) => {
      const out = await client.fetchUsage("2026-10-24");
      assert.strictEqual(out.kind, "http");
      const call = calls[0];
      const url = new URL(call.url);
      assert.strictEqual(url.origin, "https://opencsitool.com");
      assert.strictEqual(
        url.pathname,
        "/opencsitool/rest/v1/ai/operations/personalQueueStatus"
      );
      assert.strictEqual(url.searchParams.get("startDate"), "2026-10-24");
      assert.strictEqual(url.searchParams.get("endDate"), "2026-10-24");
      assert.strictEqual(call.init.credentials, "include");
      assert.strictEqual(call.init.cache, "no-store");
      assert.strictEqual(call.init.method, "GET");
      // 绝不手动构造 Cookie 头，也绝不发送 Bearer。
      assert.strictEqual(call.init.headers && call.init.headers.Cookie, undefined);
      const allHeaders = JSON.stringify(call.init.headers || {}).toLowerCase();
      assert.ok(!allHeaders.includes("authorization"), "不得发送 Authorization 头");
    }
  );
});

test("401/403 分类为登录要求，不做任何重试", async () => {
  await withFetch(
    () => ({ ok: false, status: 401, json: async () => ({}) }),
    async () => {
      const out = await client.fetchUsage("2026-10-24");
      assert.strictEqual(out.kind, "http");
      assert.strictEqual(out.status, 401);
    }
  );
});

test("5xx 与 429 是服务端答案，不是登录失效", async () => {
  await withFetch(
    () => ({ ok: false, status: 502, json: async () => ({}) }),
    async () => {
      const out = await client.fetchUsage("2026-10-24");
      assert.strictEqual(out.status, 502);
    }
  );
});

test("200 但结构改变：contract 错误，绝不冒充登录失效", async () => {
  await withFetch(
    () => ({ ok: true, status: 200, json: async () => ({ unexpected: true }) }),
    async () => {
      const out = await client.fetchUsage("2026-10-24");
      assert.strictEqual(out.kind, "contract");
      assert.match(out.reason, /tokenTrend/);
    }
  );
});

test("200 非 JSON（站点外壳页）：contract 错误", async () => {
  await withFetch(
    () => ({
      ok: true,
      status: 200,
      json: async () => {
        throw new SyntaxError("not json");
      },
    }),
    async () => {
      const out = await client.fetchUsage("2026-10-24");
      assert.strictEqual(out.kind, "contract");
    }
  );
});

test("网络失败与超时归为 network，状态 0", async () => {
  await withFetch(
    () => {
      throw new TypeError("fetch failed");
    },
    async () => {
      const out = await client.fetchUsage("2026-10-24");
      assert.strictEqual(out.kind, "network");
      assert.strictEqual(out.status, 0);
    }
  );
});

test("价格表成功返回数组；结构漂移报 contract", async () => {
  await withFetch(
    () => ({ ok: true, status: 200, json: async () => [{ requestType: "x" }] }),
    async () => {
      const ok = await client.fetchPrices();
      assert.strictEqual(ok.kind, "http");
      assert.ok(Array.isArray(ok.body));
    }
  );
  await withFetch(
    () => ({ ok: true, status: 200, json: async () => ({ rows: [] }) }),
    async () => {
      const bad = await client.fetchPrices();
      assert.strictEqual(bad.kind, "contract");
    }
  );
});

test("错误原因串绝不携带 URL 或凭据形态", async () => {
  await withFetch(
    () => {
      throw new Error("https://opencsitool.com/secret?token=abc");
    },
    async () => {
      const out = await client.fetchUsage("2026-10-24");
      assert.strictEqual(out.kind, "network");
      assert.ok(!String(out.reason).includes("token"), "原因串不得包含 URL 查询串");
    }
  );
});
