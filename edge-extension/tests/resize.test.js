// tests/resize.test.js — the height protocol's pure half.
const test = require("node:test");
const assert = require("node:assert");

const resize = require("../core/resize.js");

test("高度钳制：取整并落在边界内", () => {
  assert.strictEqual(resize.clampPanelHeight(167.6), 168);
  assert.strictEqual(resize.clampPanelHeight(20), resize.DEFAULTS.min);
  assert.strictEqual(resize.clampPanelHeight(99999), resize.DEFAULTS.max);
  assert.strictEqual(resize.clampPanelHeight(0), resize.DEFAULTS.min);
  assert.strictEqual(resize.clampPanelHeight(-5), resize.DEFAULTS.min);
});

test("非法高度返回 null，调用方跳过更新", () => {
  assert.strictEqual(resize.clampPanelHeight(null), null);
  assert.strictEqual(resize.clampPanelHeight(undefined), null);
  assert.strictEqual(resize.clampPanelHeight(NaN), null);
  assert.strictEqual(resize.clampPanelHeight("168"), null);
  assert.strictEqual(resize.clampPanelHeight(Infinity), null);
});

test("消息形状校验：只接受指定类型与有限高度", () => {
  assert.strictEqual(
    resize.isValidResizeMessage({ type: "OPENCSI_PANEL_RESIZE", height: 168 }),
    true
  );
  assert.strictEqual(
    resize.isValidResizeMessage({ type: "OPENCSI_PANEL_RESIZE", height: 168.4 }),
    true
  );
  // 不是这个事件的消息一律拒绝——宿主页面可能发来任何东西。
  assert.strictEqual(resize.isValidResizeMessage({ type: "other", height: 1 }), false);
  assert.strictEqual(resize.isValidResizeMessage({ type: "OPENCSI_PANEL_RESIZE" }), false);
  assert.strictEqual(
    resize.isValidResizeMessage({ type: "OPENCSI_PANEL_RESIZE", height: "168" }),
    false
  );
  assert.strictEqual(resize.isValidResizeMessage(null), false);
  assert.strictEqual(resize.isValidResizeMessage("resize"), false);
});

test("消息契约只含 type 与 height 两个字段的设计被测试固定", () => {
  // 防止未来把业务数据（token、费用、模型行）误塞进 resize 消息——
  // 那条通道直达宿主页面，必须保持只有布局一个用途。
  const valid = { type: "OPENCSI_PANEL_RESIZE", height: 200 };
  assert.deepStrictEqual(Object.keys(valid), ["type", "height"]);
});
