// tests/date.test.js — the company statistic day and its boundary crossings.
const test = require("node:test");
const assert = require("node:assert");

const dateUtil = require("../core/date.js");

test("Asia/Shanghai 的当天：UTC 16:00 起进入上海次日", () => {
  // 北京时间 2026-10-25 00:00 = UTC 2026-10-24 16:00
  assert.strictEqual(
    dateUtil.shanghaiToday(new Date("2026-10-24T15:59:59Z")),
    "2026-10-24"
  );
  assert.strictEqual(
    dateUtil.shanghaiToday(new Date("2026-10-24T16:00:00Z")),
    "2026-10-25"
  );
});

test("北京时间 00:00 跨日：昨日行不再计入今日", () => {
  // 23:59:59 上海时间仍是 10-24；00:00:00 已是 10-25。
  const before = dateUtil.shanghaiToday(new Date("2026-10-24T15:59:59Z"));
  const after = dateUtil.shanghaiToday(new Date("2026-10-24T16:00:01Z"));
  assert.notStrictEqual(before, after);
  assert.strictEqual(before, "2026-10-24");
  assert.strictEqual(after, "2026-10-25");
});

test("服务器时间戳的上海日期与解析失败处理", () => {
  assert.strictEqual(
    dateUtil.shanghaiDateOf("2026-10-24T07:58:29+08:00"),
    "2026-10-24"
  );
  // UTC 2026-10-24 17:00 = 上海 10-25 01:00。
  assert.strictEqual(
    dateUtil.shanghaiDateOf("2026-10-24T17:00:00Z"),
    "2026-10-25"
  );
  assert.strictEqual(dateUtil.shanghaiDateOf("不是时间"), null);
  assert.strictEqual(dateUtil.shanghaiDateOf(null), null);
});

test("时钟显示与无效值", () => {
  assert.strictEqual(dateUtil.shanghaiClock("2026-10-24T00:35:00+08:00"), "00:35");
  assert.strictEqual(dateUtil.shanghaiClock(null), "--:--");
  assert.strictEqual(dateUtil.shanghaiClock("garbage"), "--:--");
});
