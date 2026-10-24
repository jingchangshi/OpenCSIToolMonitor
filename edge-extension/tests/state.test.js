// tests/state.test.js — the snapshot's honesty rules and the fetch classifier.
const test = require("node:test");
const assert = require("node:assert");

const state = require("../core/state.js");
const dateUtil = require("../core/date.js");

const TODAY = "2026-10-24";
const PRICES = [
  { requestType: "glm", displayName: "GLM", billType: "TOKEN", enabled: 1, blendedPrice: 2.0 },
];

function payload(trend, freshTime) {
  return {
    body: {
      data: {
        tokenTrend: trend,
        tokenSummary: { totalTokens: 6_563_731_577, totalRequestCount: 62_808 },
        syncStatus: { dataFreshTime: freshTime },
      },
    },
    prices: PRICES,
  };
}

test("正常数据：OK，今日 Token 与费用由 trend 聚合", () => {
  const snap = state.snapshotFromPayload(
    payload([{ date: TODAY, requestType: "glm", tokens: 2_000_000 }], "2026-10-24T07:00:00+08:00"),
    TODAY,
    1000
  );
  assert.strictEqual(snap.state, state.STATE.OK);
  assert.strictEqual(snap.today.tokens, 2_000_000);
  assert.strictEqual(snap.today.cost, 4.0);
  assert.strictEqual(snap.cumulative.totalTokens, 6_563_731_577);
  assert.strictEqual(snap.statisticDate, TODAY);
});

test("trend 为空且服务端新鲜度未覆盖今日：NOT_READY，不显示 0", () => {
  const snap = state.snapshotFromPayload(payload([], "2026-10-23T23:00:00+08:00"), TODAY, 1000);
  assert.strictEqual(snap.state, state.STATE.NOT_READY);
  assert.match(snap.today.note, /尚未同步/);
});

test("trend 为空且新鲜度覆盖今日：OK 且确实为零", () => {
  const snap = state.snapshotFromPayload(payload([], "2026-10-24T09:00:00+08:00"), TODAY, 1000);
  assert.strictEqual(snap.state, state.STATE.OK);
  assert.strictEqual(snap.today.tokens, 0);
});

test("无新鲜度信号时同样 NOT_READY", () => {
  const snap = state.snapshotFromPayload(payload([], null), TODAY, 1000);
  assert.strictEqual(snap.state, state.STATE.NOT_READY);
});

test("费用无法完整估算：COST_UNKNOWN", () => {
  const snap = state.snapshotFromPayload(
    {
      body: {
        data: {
          tokenTrend: [{ date: TODAY, requestType: "mystery", tokens: 500_000 }],
          tokenSummary: { totalTokens: 1 },
          syncStatus: { dataFreshTime: "2026-10-24T08:00:00+08:00" },
        },
      },
      prices: PRICES,
    },
    TODAY,
    1000
  );
  assert.strictEqual(snap.state, state.STATE.COST_UNKNOWN);
  assert.strictEqual(snap.today.cost, null);
});

test("服务器时钟与本地不一致时按上海时区判定新鲜度", () => {
  // dataFreshTime 的日期是上海口径：UTC 17:00 已是上海次日。
  const snap = state.snapshotFromPayload(payload([], "2026-10-24T17:00:00Z"), "2026-10-25", 1000);
  assert.strictEqual(snap.state, state.STATE.OK);
});

test("失败合并：保留旧数据并标记 STALE 与原因", () => {
  const good = state.snapshotFromPayload(
    payload([{ date: TODAY, requestType: "glm", tokens: 3_000_000 }], "2026-10-24T08:00:00+08:00"),
    TODAY,
    1000
  );
  const failed = state.snapshotWithFailure(
    good,
    state.STATE.SERVER_ERROR,
    "OpenCSI 返回 HTTP 502",
    "稍后自动重试",
    2000
  );
  assert.strictEqual(failed.state, state.STATE.SERVER_ERROR);
  assert.strictEqual(failed.today.tokens, 3_000_000);
  assert.strictEqual(failed.today.stale, true);
  assert.strictEqual(failed.lastError, "OpenCSI 返回 HTTP 502");
  assert.strictEqual(failed.recovery, "稍后自动重试");
});

test("分类矩阵：登录失效、限流、服务故障、断网绝不互相冒充", () => {
  const S = state.STATE;
  assert.strictEqual(state.classifyFetch({ kind: "http", status: 401 }), S.LOGIN_REQUIRED);
  assert.strictEqual(state.classifyFetch({ kind: "http", status: 403 }), S.LOGIN_REQUIRED);
  assert.strictEqual(state.classifyFetch({ kind: "http", status: 429 }), S.SERVER_ERROR);
  assert.strictEqual(state.classifyFetch({ kind: "http", status: 502 }), S.SERVER_ERROR);
  assert.strictEqual(state.classifyFetch({ kind: "http", status: 200 }), S.OK);
  assert.strictEqual(state.classifyFetch({ kind: "network", status: 0 }), S.OFFLINE);
  assert.strictEqual(state.classifyFetch({ kind: "contract", status: 200 }), S.SERVER_ERROR);
  assert.strictEqual(state.classifyFetch(null), S.OFFLINE);
});

test("恢复结论回写快照：登录要求带出可执行动作", () => {
  const snap = state.applyAuthOutcome(state.emptySnapshot(), "LOGIN_REQUIRED");
  assert.strictEqual(snap.state, state.STATE.LOGIN_REQUIRED);
  assert.match(snap.recovery, /重新登录/);
});
