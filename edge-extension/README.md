# OpenCSI Usage for Magpie（Edge 扩展）

在公司 Magpie Web 的 Usage 页面内嵌一个只读的「公司 OpenCSI 用量」面板：
今日 Token、估算费用、模型明细，数据来自当前 Edge Profile 已登录的
OpenCSI 会话 Cookie。**不向 VPS 上报任何数据，不改动 Magpie 本身。**

## 安装（Edge，加载解压扩展）

1. 打开 `edge://extensions`，开启左下角「开发人员模式」；
2. 点「加载解压缩的扩展」，选择本目录（`edge-extension/`）；
3. 确认本 Edge Profile 已登录 OpenCSI（打开一次
   <https://opencsitool.com/myTools> 能看到数据即可，之后可以关掉该标签页）；
4. 打开 Magpie Usage 页：<http://129.153.118.58:3430/?view=usage>，
   页面顶部出现「公司 OpenCSI 用量」面板。

**如果 Magpie 页面是在加载扩展之前就打开的**，旧标签页不会自动获得面板
（清单式 content script 只对之后的导航生效）。两种补救，任选其一：

- 刷新那个 Magpie 标签页；或
- 点工具栏里的扩展图标 → 「在此标签页重试注入」——按钮会回报精确结果
  （注入成功 / 当前页不匹配 / 找不到 `#view-usage` 容器），不再是静默失败。

安装加载扩展时若已有打开的 Magpie 标签页，扩展会在 `onInstalled` 里对
它们自动补注入，通常无需手动操作。扩展管理页显示「服务工作进程（非活动）」
是 MV3 的正常待机态：事件驱动的 worker 空闲即挂起，打开一次 Magpie Usage
页（或点扩展图标）就会唤醒。

## 会话过期与恢复

OpenCSI 的会话 Cookie 约 1 小时过期。过期后：

- 面板先显示「需要登录」；
- 扩展自动尝试一次静默恢复：在后台标签页中走一遍 OpenCSI 的 GitCode
  OAuth 入口。若本 Profile 的 GitCode 登录态仍然有效，会话自动恢复，
  面板回到「正常」，全程无需操作；
- 若 GitCode 也要求重新登录（或出现应用授权确认页），自动流程停止：
  前者显示「重新登录 OpenCSI」按钮，点击后在打开的页面里完成登录，
  回到 Magpie 刷新即可；后者直接在已打开的授权页上点击一次批准。
- 授权确认（Consent）永远由用户本人点击，扩展不会代为提交。

服务器故障（429/5xx）、断网、接口结构变化分别显示为「服务异常 /
网络不可用」，不会要求你重新登录。数据不可用时显示 `--`；显示的是
历史数据时标注「上次成功更新」的时间。

## 面板形态

OpenCSI 用量现在是 **Magpie 原生额度网格（`#subscriptionUsage`）中的第一张
卡片**——与 OpenCode Go、Codex、WorkBuddy 等原生额度卡并排，由 Magpie 自己
的 `packUsage` 瀑布流布局排版（调查依据见 `docs/magpie-quota-grid.md`，
上游 commit `b97e0aa2`）。卡片参与网格但不进入 UsageOrder 拖拽排序与持久
化；Magpie 额度数据刷新（`renderQuotas` 清空重建容器）后自动重新插回首格。

卡片内部：标题行 `OpenCSI` + `公司模型服务` + 状态点；今日 Tokens 与估算费
用双指标垂直排列；模型区按当日模型数呈现（默认 Top 2 + 「查看全部」）；底
部为查询时间与刷新/诊断小图标。高度由内容决定，变化时通过高度消息通知宿
主页，由 Magpie 自身的 ResizeObserver 触发网格重排：

- 单模型：仅显示模型名与「当前唯一使用模型」，不重复总量数字；
- 2–3 个模型：紧凑排行（按 Token 降序）；
- 超过 3 个：默认 Top 3 + 「展开全部（N）」，展开后内部滚动，收起恢复；
- 确认零用量：`0 tokens / ¥0.00` + 「今日暂无模型使用记录」；
- 数据未就绪/失败：`--` 与状态说明，绝不把缺失渲染成零。

展开与收起不重新请求 API。面板通过 ResizeObserver 把自身内容高度
（仅一个数字）报给注入器，iframe 随内容伸缩；消息经来源、origin 与
形状三重校验，不携带任何业务数据。诊断入口在右上角低强调文字。

## 刷新策略

- 打开 Usage 页立即查询；页面可见期间每 5 分钟刷新；
- 切回挂起的标签页时若缓存已超过 5 分钟则立即刷新；
- 「立即刷新」无视间隔，但限制连续点击；
- 关闭 Magpie 页面后不再采集；浏览器退出后下次打开重新查询。

## 诊断

面板右上角「诊断」打开诊断页：显示扩展版本、认证状态、最近错误与
`BROWSER_COOKIE_REUSE` 等 POC 判定，一键复制脱敏报告（不含 Cookie、
Token 或 API 原始响应值）。认证或注入出问题时，把该报告发出来即可。

## 目录

```text
manifest.json            MV3 清单：权限仅 opencsitool.com + Magpie 主机 + storage/tabs
background/
  service-worker.js      事件驱动协调：消息路由、快照缓存（storage.session）
  opencsi-client.js      只读 API 客户端（credentials:include，绝不手动带 Cookie 头）
  auth-manager.js        认证状态机 + 后台标签页静默 SSO 恢复（单飞/冷却/一次一集）
core/                    纯逻辑：models / aggregation / date / state / formatting
content/magpie-inject.js  runtime origin 校验 + #view-usage 发现 + 单实例挂载
panel/                   用量面板 iframe（index/panel.js/panel.css）+ 诊断页 + 工具栏弹窗
tests/                   Node 内建测试（node --test tests/），含 Python 黄金对照
e2e/browser.e2e.js       真实浏览器端到端（Chromium for Testing + mock Magpie DOM）
```

## 开发

```bash
# 单元测试（无浏览器依赖）
node --test tests/
# 重新生成 JS/Python 黄金对照（改了 Python 聚合语义之后）
python tests/generate_golden.py
# 真实浏览器端到端（首次需 npx @puppeteer/browsers install chromium@latest）
node e2e/browser.e2e.js
```

安全边界：扩展不读取、不复制、不持久化任何 Cookie 或 Token；不向
Magpie 页面或 Oracle VPS 发送用量与凭据；面板是扩展自己的 iframe，
Magpie 页面脚本读不到其中的数据。
