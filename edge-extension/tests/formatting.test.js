// tests/formatting.test.js — the display rules that keep missing values honest.
const test = require("node:test");
const assert = require("node:assert");

const f = require("../core/formatting.js");

test("中文万/亿格式", () => {
  assert.strictEqual(f.cnTokens(52_852_000), "5,285.2 万");
  assert.strictEqual(f.cnTokens(6_563_731_577), "65.64 亿");
  assert.strictEqual(f.cnTokens(9_999), "9,999");
  assert.strictEqual(f.cnTokens(0), "0");
});

test("缺失值是 --，绝不是 0", () => {
  assert.strictEqual(f.cnTokens(null), "--");
  assert.strictEqual(f.cnTokens(undefined), "--");
  assert.strictEqual(f.cost(null), "--");
  assert.strictEqual(f.fullNumber(null), null);
});

test("估算费用为人民币格式", () => {
  assert.strictEqual(f.cost(14.8), "¥14.80");
  assert.strictEqual(f.cost(23071.99999776), "¥23,072.00");
  assert.strictEqual(f.cost(3.2, "USD"), "USD 3.20");
});

test("完整数值供悬停展示", () => {
  assert.strictEqual(f.fullNumber(6_563_731_577), "6,563,731,577");
});
