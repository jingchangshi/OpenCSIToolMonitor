# Magpie 额度网格集成调查（Phase 0）

**上游 commit**: `b97e0aa2a2578ce65bd69feed4d310f9218139a8`（2026-10-24）
**方法**: 上游源码精读 + 本地真实 CSS/真实 `packUsage` 的浏览器 E2E。
**注意**: 任务书引用的 `internal/gui/tests/usage-pack.test.cjs`、`usage-arrange.test.cjs`、
`usage-card-refresh.test.cjs` 与 `docs/subsystems/gui-shell.md` 在上游 main 已不存在
（HTTP 404）；usage 插件在近期重构中精简为 `client.js`(9.2KB) + `page.js`(183KB) + `i18n.js`。
本文以实际存在的源码为准。

## 调用链（renderQuotas → 卡片 → 布局）

```
数据到达/视图返回/排序结束
  └─ renderQuotas(lazy)                       app.js:15568
       ├─ keepQuotaFlash(box)                 保存 .flash 动画状态（按 [data-provider] 选择器）
       ├─ subscriptions.replaceChildren()     ★ 清空全部子节点（包括外部卡片）
       ├─ usageArranging? → usageRenderPending=true; return   （拖拽中：清空后不重建）
       ├─ for (sub of byUsageOrder(keyCards(quotas)))  按 UsageOrder 排序后逐卡构建
       │     card = div.subscription-card
       │     card.dataset.key / dataset.provider / [data-card] / [data-balCard]
       ├─ markUpstream()                      只认 [data-provider]（无则跳过）
       ├─ packUsageNow(list)                  packUsage + 重挂 RO（list/每卡/每卡子节点）
       └─ restoreFlash() / restoreRefresh()
```

## 十个问题的答案（全部有源码行号）

1. **何时创建**：`plugins/usage/client.js:39` —— 插件页模板中的静态
   `<section class="subscription-usage" id="subscriptionUsage">`，随插件页注册一次性创建。
2. **谁清空子节点**：只有 `renderQuotas`（`app.js:15572` 的 `replaceChildren()`）。
   触发源：额度数据到达、`storage` 事件（跨标签页设置同步）、拖拽排序提交
   （`moveUsage` → `renderQuotas()`）、卡片折叠/展开后的部分重绘仍走整次 renderQuotas。
3. **packUsage 如何收集卡片**：`app.js:15949` —— `const cards = [...list.children]`，
   **没有任何过滤**。外部卡片天然参与。
4. **是否只处理 API 卡片**：收集不分；但**后续按属性分流**——
   `markUpstream`（5900）、`keepQuotaFlash`（16764，选择器 `[data-provider=…]`）、
   拖拽提交 `moveUsage(key=provider, to)`（16060，按 provider key 持久化
   `state.settings.usageOrder`）全部只认 `[data-provider]`。无 provider 的卡片
   进得去布局、进不去持久化。
5. **布局计算**：`packUsage`（15943）——先清 `minHeight` 再量每张卡
   `getBoundingClientRect().height`；列数 `n ∈ {most, most-1}`
   （`most = ⌊(width+gap)/(260+gap)⌋`），按"下一张放进当前最矮的列"贪心放置，
   以列尾空白总量选优；每卡写 `gridArea = "{top+1}/{col+1}/span h/span 1"`；
   **每列最后一张卡 `min-height` 拉伸到最高列底**（内容需顶部对齐）。
6. **加入后触发重布局**：无需调用任何函数。`usagePack`（16012）是挂在
   `#subscriptionUsage` 上的 ResizeObserver（rAF 去抖）——外部卡片插入改变容器
   高度即触发重打包。
7. **外部卡片高度变化后**：同理。卡片高度 → 容器高度 → RO → repack。
   我们的高度协议（iframe postMessage → content script 改 iframe style.height）
   是唯一需要自己实现的半边。
8. **拖拽与 UsageOrder**：`dragCards`（2987）在拖起时快照 `[...list.children]`
   并只对被拖的原生卡施加 transform；提交时 `moveUsage(sub.provider, to)`
   只持久化 provider 顺序。外部卡片无 `us-handle`（不可拖）、无 data-provider
   （不入 UsageOrder）；`to` 索引会计入外部卡片的位置，但外部卡片随后总是
   重插回首格，原生卡之间的相对顺序始终由 UsageOrder 决定。
9. **刷新后恢复**：`replaceChildren` 清空 → 扩展的 childList 观察器重插回首格。
   拖拽进行中（容器带 `.sorting`）主动跳过重插，等 class 移除再挂——
   避免拖拽期间向被快照的 children 数组里插入节点。
10. **能否不改源码稳定容纳**：能。唯一的写入时机约束是避开 `.sorting` 窗口；
    其余全部复用 Magpie 自身的布局与观察器。`packUsage` 的列尾拉伸会作用于
    外部卡片——内容保持顶部对齐即可，多出的空间留白与原生卡片行为一致。

## 方案判定

**采用 Phase 1 纯扩展方案**（不修改 Magpie 源码）：

```html
<section id="subscriptionUsage" class="subscription-usage">
  <article class="subscription-card" data-opencsi-extension>
    <iframe src="chrome-extension://…/panel/index.html" title="OpenCSI"></iframe>
  </article>
  <!-- 原生卡片，UsageOrder 相对顺序不变 -->
</section>
```

- 外部卡片：`class="subscription-card"`（继承真实内边距/边框/背景/网格尺寸）、
  `data-opencsi-extension` 标识、**无 data-provider / data-key / us-handle**
  （不进持久化、不可拖、不被 markUpstream 触碰）。
- 挂载位置：容器**第一格**；每次 renderQuotas 后由 childList 观察器重插，
  `.sorting` 期间挂起。
- 高度：iframe 内容 → ResizeObserver → postMessage（仅高度数字，目标 origin
  固定为 Magpie 页）→ content script 校验后写 iframe style.height → 容器
  高度变化 → Magpie 自己的 usagePack RO → repack。
