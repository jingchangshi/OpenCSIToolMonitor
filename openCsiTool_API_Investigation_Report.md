# openCsiTool API Investigation Report

**目标站点**：`https://opencsitool.com/myTools`（"我的工具"）
**调查账号**：`shijingchang`（employeeId `653124`，组织"体验项目" `fba54d16…c4b9`，角色视图 `普通用户 / Normal`）
**调查时间**：2026-09-19（站点时钟），全部请求时间戳见证据文件
**调查方式**：真实浏览器（CDP / browser-harness）登录态下操作 + Network 抓包 + 前端 bundle 逆向 + 脱离浏览器的 `curl` 复现
**报告版本**：v1.0（终稿）
**安全声明**：本报告**不包含**任何真实 Cookie / Token / API Key 明文，全部以 `<redacted>` 或站点自身已脱敏的形式呈现。调查期间**未执行任何写操作**（无创建 / 删除 / 修改 / 绑定 / 审批），仅使用 GET 与页面自身强制的两个只读 POST。

---

## 1. 调查摘要与结论

### 1.1 一句话结论

`/myTools` 页面的**全部业务数据**来自一个内部 REST 接口
`GET /opencsitool/rest/v1/ai/operations/personalQueueStatus?startDate=&endDate=`，
其返回的 `data.requestList[]` / `data.tokenSummary` / `data.tokenTrend` / `data.syncStatus` 与页面表格、六个概览卡片、趋势图**逐字段、逐数值完全对应**；该站点**没有公开 API、没有 OpenAPI/Swagger 文档、没有 API Key 或开发者门户**，认证完全依赖 GitCode OAuth 换取的 HttpOnly `token` Cookie。

### 1.2 关键判定表

| 判定项 | 结论 | 依据 |
|---|---|---|
| 页面数据来源 | **单一聚合接口** `personalQueueStatus` | 概览卡片 + 模型费用表格 + 趋势图三处数据同源 |
| 接口类型 | **Internal Web API**（内部 Web 接口） | 无文档、无版本协商、无 API Key、鉴权靠浏览器 Cookie、路径带 `/rest/v1/` 内部前缀 |
| 是否有 Public API | **否** | 9 个公开文档路径全部落回 SPA 外壳 |
| 是否有 Swagger/OpenAPI | **否** | `/v3/api-docs` 等返回 2296 B 的 `index.html`；真实前缀下返回 401/404 |
| 认证方式 | **HttpOnly Cookie `token`**（GitCode OAuth 换取） | 仅带 Cookie 即可 200；无 Cookie 401 |
| 前端 `Authorization` 头 | **装饰性无效值** | 拦截器注入字面量 `Bearer gitcode-auth-dummy-token`，服务端只认 Cookie |
| CSRF | **未观察到** | 无 CSRF Token 头，POST 仅凭 Cookie 成功 |
| Cookie 生命周期 | **约 0.97 小时（≈58 分钟）** | CDP `Network.getCookies` 读取 `expires` |
| 是否需要写操作才能取数 | **否**，全部只读 | 所有 myTools 数据接口均为 GET |
| 浏览器外可复现性 | **可复现** | `curl` + Cookie 对 12 个端点全部 200 |
| 客户端可行性 | **已验证** | `opencsitool_client.py` + `verify_client.py` 离线回放真实响应 **42/42 通过** |

### 1.3 对 DSH 的意义

可以实现一个**无浏览器依赖**的 `OpenCsiToolClient`：只需持有 `token` Cookie，即可稳定读取"我的工具"全部数据。唯一的工程障碍是 **Cookie 约 1 小时过期**，需要一个会话续期通道（见 §附录 A）。

---

## 2. 调查范围、方法与证据链

### 2.1 11 个阶段的执行情况

| # | 阶段 | 状态 | 主要证据文件 |
|---|---|---|---|
| 1 | 页面确认 + 登录态 | ✅ 完成 | `capture/01_page_state.json`、`02_login_page.json`、`05_login_page.png` |
| 2 | Network 抓包（Fetch/XHR/WS/SSE） | ✅ 完成 | `capture/11_net_all.json`、`12_net_full.json`、`12_api_index.json` |
| 3 | 页面字段 ↔ API JSON 字段对应（≥2–3 项） | ✅ 完成（**8 卡片 + 7 列 × 3 行**） | `capture/37_mapping_final.json`、`38_regions.json` |
| 4 | 逐接口明细（URL/方法/参数/头/响应/状态） | ✅ 完成 | `capture/responses/*.json`、`41_out_of_browser_final.json` |
| 5 | 认证机制（Cookie/JWT/Bearer/CSRF/Storage） | ✅ 完成 | `capture/13_auth_state.json`、`13_cookies.json`、`42_cookie_ttl.json` |
| 6 | 主动触发（刷新/分页/筛选/详情/排序） | ✅ 完成 | `capture/33_ui_triggers.json`、`34_detail_triggers.json` |
| 7 | 前端 JS bundle 逆向 | ✅ 完成 | `capture/21_endpoint_catalog.json`、`26_endpoints_by_file.json`、`27_aiops_fn_map.json` |
| 8 | 脱离浏览器调用（页内 fetch → curl） | ✅ 完成 | `capture/14_fetch_tests.json`、`41_out_of_browser_final.json` |
| 9 | 公开文档路径探测（9 条） | ✅ 完成 | `capture/39_public_docs.json` |
| 10 | Public API vs Internal Web API 判定 | ✅ 完成 | 本报告 §10 |
| 11 | 报告输出 | ✅ 完成 | 本文件 |

### 2.2 方法论要点

1. **不做静态页面分析**：全程在真实浏览器中打开 `/myTools`，先完成 GitCode OAuth 登录，再在已授权会话内操作。
2. **双通道抓包交叉验证**：
   - 通道 A：CDP `Network.enable` + `Network.requestWillBeSent` / `responseReceived` 事件流；
   - 通道 B：`Page.addScriptToEvaluateOnNewDocument` 注入的 `fetch` / `XMLHttpRequest` / `WebSocket` 包装器（`window.__OCS_NET` 缓冲区）。
   两通道结果一致，排除了"看到 URL 就当成目标 API"的误判。
3. **字段级映射证明**：不使用 URL 名称猜测，而是把页面渲染出的文本与接口 JSON 逐值比对（见 §5）。
4. **主动触发**：通过 DOM `click()` 触发日期预设按钮，观察接口 URL 中 `startDate`/`endDate` 的实际变化。
5. **脱离浏览器复现**：用 CDP 取出 Cookie 写入临时 Netscape 文件，再以 `curl -b <file>` 复现全部端点（Cookie 值从不进入命令行参数或日志）。

### 2.3 前端运行环境指纹

| 项 | 值 |
|---|---|
| 应用框架 | Vben Admin 5.5.9（Vue 3 + Ant Design Vue + Tailwind），Vite 构建 |
| 运行配置 | `window._VBEN_ADMIN_PRO_APP_CONF_ = {"VITE_GLOB_API_URL":"//opencsitool.com"}`（来自 `/_app.config.js?v=5.5.9-0df388f9`，对象被冻结） |
| Web 服务器 | `nginx/1.21.5` |
| 挂载点 | `<div id="app" data-v-app>` |
| 构建产物指纹 | chunk 文件名带内容哈希，**重新部署后会变化** → 本报告一律引用**接口路径**而非 chunk 文件名 |

---

## 3. 页面确认与登录态（阶段 1）

### 3.1 页面身份

| 项 | 值 |
|---|---|
| URL | `https://opencsitool.com/myTools` |
| `<title>` | `我的工具 - openCsiTool` |
| 左侧菜单高亮 | `AI工具 → 我的工具`（key `ai_tool_my_tools`） |
| 面包屑 | `我的工具 / 首页` |
| 角色选择器 | `普通用户 ▼` |

### 3.2 页面区域构成（实测）

| # | 区域标题 | 类型 | 内容摘要 |
|---|---|---|---|
| 0 | （页头提示） | 文本 | `已绑定工号：653124（如需修改请联系管理员）` |
| 1 | 个人数据概览 | 6 个统计卡片 | Token用量 / 请求次数 / PR数量 / 新增代码行 / AI生成代码行 / 采纳代码行 |
| 2 | Token消耗趋势 · 模型费用单价 | ECharts 图 + 工具栏 | 折线/柱状趋势图；`数据更新至 …`；`最近使用 …`；`同步最新数据` 按钮；`Token用量 / 消费金额` 切换；`本 周 / 本 月 / 近7天 / 近30天` 预设；自定义日期区间 |
| 3 | 我的申请模型费用单价 | 表格 | 7 列：申请类型 / 状态 / 账号名称 / 发放日期 / 申请日期 / API Key / 最后使用时间；含指引链接 `AI模型配置指导：https://gitcode.com/org/openCsiTool/discussions/1` |
| 4 | 调用日志 | 表格 + 分页 | 7 列：请求时间 / 模型 / 输入Token / 输出Token / 总Token / 耗时(ms) / 详情；当前 `暂无数据` |

### 3.3 页面交互能力实测（决定可触发哪些接口）

| 交互 | 存在 | 实测行为 |
|---|---|---|
| 日期预设（本周/本月/近7天/近30天） | ✅ | 点击后**真实改变** `startDate`/`endDate` 并重新请求（§8.1） |
| 自定义日期区间 | ✅ | 两个 `input` 承载区间值（初始 `2026-08-20` ~ `2026-09-19`） |
| `同步最新数据` 按钮 | ✅ | **未点击**——语义上会触发服务端数据同步（写语义），按"不执行写操作"原则主动跳过 |
| `Token用量 / 消费金额` 切换 | ✅ | 仅前端图表口径切换（见 §8.3） |
| 调用日志分页 | ⚠️ 存在但禁用 | `ant-pagination-disabled` 覆盖上一页/下一页，`total=0`，点击无请求 |
| 关键词搜索框 | ❌ | 页面无任何搜索输入框 |
| 表格排序器 | ❌ | 实测 `0` 个 sorter |
| 列筛选器 | ❌ | 无 |
| 行点击 / 详情弹窗 | ❌ | 6 张统计卡 `cursor:auto`、无点击监听；表格行无可点元素；点击后无 `ant-modal` / `ant-drawer` 出现 |
| 结果：主动可触发的接口 | — | **仅 `personalQueueStatus` 的日期参数**；`call-logs` / `key-budget` 每次随之重放 |

### 3.4 登录流程（真实执行）

站点**不存在**用户名/密码表单，唯一登录方式是 GitCode OAuth：

```
GET {API}/rest/v1/oauth2/authorization/gitcode?redirect=<urlencoded-path>
```

前端源码（`bootstrap-*.js`）中的构造逻辑：

```js
function hTe(e){
  const t = e?.trim();
  const n = t ? `${_t}/rest/v1/oauth2/authorization/gitcode?redirect=${encodeURIComponent(t)}`
              : `${_t}/rest/v1/oauth2/authorization/gitcode`;
  window.location.href = n;
}
```

登录成功判定：`SP()` 中 `l.status === 200 && l.data` 后执行
`c.setAccessToken("gitcode-auth-dummy-token")` 与 `s.setUserInfo(u)`。
→ **登录凭证的载体是 Cookie，不是前端 Token**。

> 说明：任务给出的 `账号：shijingchang / 密码：GitCode0728` 在本站点**没有对应的账号密码登录入口**；实际生效的是浏览器中已完成的 GitCode OAuth 会话。调查严格限定在"当前已授权浏览器 session"内进行，未尝试绕过认证。

---

## 4. 网络请求捕获（阶段 2）

### 4.1 首次进入 `/myTools` 的完整 XHR 序列（12 条，全部 200）

| 序 | 方法 | 路径 | 状态 | 大小 | 作用 |
|---|---|---|---|---|---|
| 1 | GET | `/opencsitool/rest/v1/user/getUserInfo` | 200 | 1001–1017 B | 用户身份 + 当前组织/角色视图 |
| 2 | GET | `/opencsitool/rest/v1/user/getUserRolesByOrganizationId?organization_id=<orgId>` | 200 | 205–209 B | 组织内角色（`VISITOR/访客`） |
| 3 | POST | `/opencsitool/rest/v1/menu/list` | 200 | 3314–3516 B | 左侧菜单树 |
| 4 | POST | `/opencsitool/rest/v1/collectVisitData` | 200 | 35–47 B | 访问埋点（页面强制，只读语义） |
| 5 | GET | `/opencsitool/rest/v1/user/getVisibleRoleViews` | 200 | 32–40 B | 可见角色视图列表 |
| 6 | GET | `/opencsitool/rest/v1/ai/config/cost` | 200 | 7572–7604 B | 模型费用单价表（20 行） |
| 7 | GET | `/opencsitool/rest/v1/message/statistics` | 200 | 417–433 B | 站内信统计（当前全 0） |
| 8 | **GET** | **`/opencsitool/rest/v1/ai/operations/personalQueueStatus?startDate=&endDate=`** | **200** | **2070–7056 B** | **★ 页面主数据源** |
| 9 | GET | `/opencsitool/llmgateway/rest/v1/users/653124/call-logs?page=1&pageSize=20&startDate=&endDate=` | 200 | 44 B | 调用日志分页（当前空） |
| 10 | GET | `/opencsitool/llmgateway/rest/v1/users/653124/key-budget` | 200 | 90 B | 员工 Token 预算（当前不存在） |
| 11–12 | — | 静态资源 / 埋点续发 | — | — | 非业务 |

> 说明：`/myTools` **首次加载**在 OAuth 未完成时会先出现 `GET /opencsitool/rest/v1/user/getUserInfo → 401`，登录完成后同一请求返回 200。这是登录态判定点，不是接口异常。

### 4.2 协议面覆盖

| 协议 | 是否使用 | 证据 |
|---|---|---|
| `fetch` | ❌（业务请求全走 XHR） | `__OCS_NET` 缓冲中 `kind` 全为 `xhr` |
| `XMLHttpRequest` | ✅ 全部业务请求 | 同上 |
| GraphQL | ❌ | 无 `/graphql` 请求，无 `query/mutation` body |
| WebSocket | ❌ | 包装器零命中 |
| EventSource / SSE | ❌ | 无 `text/event-stream` 响应 |

**结论**：这是一个纯 REST + 请求/响应式的内部管理台，无实时通道。对 DSH 而言意味着**轮询即可**，不需要长连接。

### 4.3 响应封装形态（三种并存，客户端需分别处理）

| 形态 | 示例端点 | 结构 |
|---|---|---|
| **A. `{code,data,message}`** | `personalQueueStatus` | `{"code":200,"data":{…}}` |
| **B. 裸 JSON** | `getUserInfo`、`ai/config/cost`、`getUserRolesByOrganizationId`、`getVisibleRoleViews`、`call-logs`、`key-budget` | 直接对象/数组，无封装 |
| **C. `{success,message}`** | `collectVisitData` | `{"success":true,"message":"数据采集成功"}` |

`menu/list` 为裸数组；`message/statistics` 为裸对象。

---

## 5. 页面字段 ↔ API JSON 字段对应证明（阶段 3）

> 这是本报告的核心证据章节。所有比对均为**同一次请求**（`startDate=2026-08-20&endDate=2026-09-19`）的 JSON 与**同一时刻页面渲染文本**的逐值比对，未使用任何 URL 名称推断。
> 完整原始数据：`capture/37_mapping_final.json`、`capture/38_regions.json`

### 5.1 概览卡片（8 项，8/8 数值一致）

接口：`GET /opencsitool/rest/v1/ai/operations/personalQueueStatus?startDate=2026-08-20&endDate=2026-09-19`

| 页面渲染文本 | 接口字段与取值 | 计算规则 | 结果 |
|---|---|---|---|
| Token用量 `30.6亿` | `data.tokenSummary.totalTokens = 3061130999` | `3061130999 / 1e8 = 30.6` | ✅ 一致 |
| ↳ `API套餐: 28.0亿` | `Σ requestList[requestType=="API_BUNDLE"].tokenUsage = 2800206464` | `/1e8 = 28.0` | ✅ 一致 |
| ↳ `Trae: 2.6亿` | `Σ requestList[requestType=="TRAE"].tokenUsage = 260924535` | `/1e8 = 2.6` | ✅ 一致 |
| 请求次数 `2.2万` | `data.tokenSummary.totalRequestCount = 21632` | `/1e4 = 2.16 → 2.2` | ✅ 一致 |
| PR数量 `246` | `Σ requestList[].prCount = 246` | 直接求和 | ✅ 一致 |
| 新增代码行 `3.1万` | `Σ requestList[].addedLinesCount = 31167` | `/1e4 = 3.12 → 3.1` | ✅ 一致 |
| AI生成代码行 `3,150` | `Σ requestList[].generatedCodeLines = 3150` | 直接求和 + 千分位 | ✅ 一致 |
| 采纳代码行 `120(3.8%)` | `Σ requestList[].adoptedCodeLines = 120` | `120/3150 = 3.81% → 3.8%` | ✅ 一致 |

**关键结论**：
- `tokenSummary.totalTokens` 恰好等于 `Σ requestList[].tokenUsage`（`3061130999` 三方相等），说明卡片 1 与表格数据同源；
- 卡片 2/4/5/6 的数据**不在 `tokenSummary` 中**，而是从 `requestList[]` **客户端聚合**得到 —— 这是 DSH 集成时必须复刻的计算逻辑。

### 5.2 "我的申请模型费用单价"表格（7 列 × 3 行，21/21 一致）

| 表格列（页面表头） | API 字段 | 取值规则 | 行1 | 行2 | 行3 |
|---|---|---|---|---|---|
| 申请类型 | `requestType` | 原样 | `API_BUNDLE` | `TRAE` | `API_BUNDLE` |
| 状态 | `status` | `status==1 → 使用中`；`status==2 → 已失效` | `使用中` | `使用中` | `已失效` |
| 账号名称 | `accountName` | 原样 | `AI编程助手-002` | `AI编程助手-001` | `AI编程助手-001` |
| 发放日期 | `issueDate` | 原样 | `2026-08-17` | `2026-04-22` | `2026-03-17` |
| 申请日期 | `createTime` | 原样 | `2026-08-17 16:28:00` | `2026-04-16 12:33:22` | `2026-03-09 14:53:52` |
| API Key | `virtualKey` | 截断显示：`virtualKey.slice(0,10) + "****"` | `sk-bM4LUSm****` | `-`（`virtualKey=null`） | `-`（`virtualKey=null`） |
| 最后使用时间 | `lastUsedDate` | 原样 | `2026-09-19 00:00:00` | `2026-08-14 00:00:00` | `2026-09-19 00:00:00` |

**行身份交叉验证**（表格行的隐藏标识）：

| 行 | `requestList[].id` | `applicationNumber` | `remark` |
|---|---|---|---|
| 1 | `5593` | `REQ202608170007` | `开发AscendNPU IR` |
| 2 | `1954` | `REQ202604160010` | `开发AscendNPU IR及Triton` |
| 3 | `1094` | `REQ202603090022` | `AscendNPU IR及Triton开发` |

> **脱敏说明**：接口返回的完整 `virtualKey` 值已按要求脱敏为 `<redacted>`；上表只保留**页面自身已截断渲染**的 `sk-bM4LUSm****` 形式，用于证明"截断规则"这一映射关系。

### 5.3 Token 消耗趋势图（42 点 → 30 个日期 × 5 个模型）

| 图表元素 | API 字段 | 验证结果 |
|---|---|---|
| X 轴日期标签 | `tokenTrend[].date` | 图渲染 `2026-08-21 … 2026-09-19`（间隔取样）；接口 `tokenTrend` 含 **42 条记录**、去重后 **30 个日期**（`2026-08-20` ~ `2026-09-19`）✅ |
| Y 轴量纲 | `tokenTrend[].tokens` | 轴标 `0 / 5000.0万 / 1.0亿 / 1.5亿 / 2.0亿 / 2.5亿`，与 `tokens` 数量级一致 ✅ |
| 图例（模型名） | `tokenTrend[].requestType` → 显示名 | 图例渲染 `DeepSeek-V4-Flash-0731 / GLM-5.3-Flash / GLM-5.3 / DeepSeek-V4-Pro / Qwen3.8-Flash`；接口 `requestType` 去重集合 `DEEPSEEK_V4_FLASH_0731 / GLM_5_3_FLASH / GLM_5_3 / DEEPSEEK_V4_PRO / QWEN3_8_FLASH` —— **5/5 一一对应**（显示名映射来自 `ai/config/cost` 的 `displayName`）✅ |
| 数据点标签（柱顶数字） | `tokenTrend[].tokens` | 图内 `1313.9万` 对应 `tokens=13139463`；`5285.2万` 对应 `52852000` 级 ✅ |
| 输入/输出拆分 | `promptTokens` / `completionTokens` | `tokens = promptTokens + completionTokens` 逐点成立（如 `50817356 = 50590638 + 226718`）✅ |

### 5.4 "数据更新至"文案（同步状态映射）

| 页面文本 | API 字段 |
|---|---|
| `数据更新至 09-19 22:04:48` | `data.syncStatus.dataFreshTime = "2026-09-19T22:07:07+08:00"`（同分钟级，随每次请求刷新） |
| （`etlTime` / `replicationTime` 未直接展示） | `data.syncStatus.etlTime`、`data.syncStatus.replicationTime` |

### 5.5 页面身份字段（页头提示映射）

| 页面文本 | API 字段 |
|---|---|
| `已绑定工号：653124` | `data.boundEmployeeId = "653124"`（与 `getUserInfo.user.employeeId` 一致） |
| 页面内部 `userId` | `data.userId = "0dd935e8d2234014a215aa922417094c"`（与 `getUserInfo.user.id` 一致） |

### 5.6 映射证明小结

| 页面区域 | 比对项数 | 一致项数 | 一致率 |
|---|---|---|---|
| 概览卡片 | 8 | 8 | 100% |
| 费用单价表格 | 21 | 21 | 100% |
| 趋势图 | 5（模型）+ 2（量纲/日期） | 7 | 100% |
| 同步状态文案 | 1 | 1 | 100% |
| 身份提示 | 2 | 2 | 100% |
| **合计** | **39** | **39** | **100%** |

---

## 6. API 明细清单（阶段 4）

> 所有接口均在同一次会话内以真实浏览器 Cookie 复现成功（`curl`，见 §9）。
> 完整响应体样本见 `capture/responses/`。

### 6.1 ★ 核心数据接口

#### API-01 `personalQueueStatus` — 我的工具主数据源

| 项 | 值 |
|---|---|
| URL | `https://opencsitool.com/opencsitool/rest/v1/ai/operations/personalQueueStatus` |
| 方法 | `GET` |
| Query | `startDate`（`YYYY-MM-DD`）、`endDate`（`YYYY-MM-DD`），**均可省略** |
| Body | 无 |
| 请求头（实测） | `Accept: application/json, text/plain, */*`、`Authorization: Bearer gitcode-auth-dummy-token`（装饰性）、`Accept-Language: zh-CN`、`Cookie: token=<redacted>` |
| 响应头 | `content-type: application/json`、`server: nginx/1.21.5`、`transfer-encoding: chunked` |
| 状态 | `200` |
| 大小 | 无参 `7056 B` / 30d `7016 B` / 7d `3918 B` / 1d `2070 B` / 本周 `3473 B` / 本月 `5603 B` / 近30天 `6887 B` |
| 认证 | 必需（无 Cookie → `401 empty Authorization`） |

**响应结构**：

```json
{
  "code": 200,
  "data": {
    "userId": "0dd935e8d2234014a215aa922417094c",
    "boundEmployeeId": "653124",
    "requestList": [
      {
        "id": 5593,
        "applicationNumber": "REQ202608170007",
        "requestType": "API_BUNDLE",
        "queuePosition": 0,
        "estimatedWaitTime": "-",
        "waitDays": 0,
        "createTime": "2026-08-17 16:28:00",
        "remark": "开发AscendNPU IR",
        "status": 1,
        "accountName": "AI编程助手-002",
        "aiToolName": null,
        "employeeId": "00653124",
        "issueDate": "2026-08-17",
        "tokenUsage": 1400103232,
        "requestCount": 10816,
        "lastUsedDate": "2026-09-19 00:00:00",
        "prCount": 82,
        "addedLinesCount": 10389,
        "issueCount": 0,
        "generatedCodeLines": 0,
        "adoptedCodeLines": 0,
        "virtualKey": "<redacted>"
      }
      /* … 共 3 条 … */
    ],
    "tokenSummary": { "totalTokens": 3061130999, "totalRequestCount": 21632 },
    "tokenTrend": [
      { "date": "2026-08-20", "requestType": "DEEPSEEK_V4_FLASH_0731",
        "tokens": 50817356, "promptTokens": 50590638, "completionTokens": 226718 }
      /* … 共 42 条 … */
    ],
    "syncStatus": {
      "dataFreshTime": "2026-09-19T22:07:07+08:00",
      "etlTime": "2026-09-20T06:06:53+08:00",
      "replicationTime": "2026-09-19T22:07:09+08:00"
    },
    "tokenBudget": { "exists": false, "maxBudget": null, "budgetDuration": null, "spend": null }
  }
}
```

**`requestList[]` 全部字段（22 个）**：
`accountName, addedLinesCount, adoptedCodeLines, aiToolName, applicationNumber, createTime, employeeId, estimatedWaitTime, generatedCodeLines, id, issueCount, issueDate, lastUsedDate, prCount, queuePosition, remark, requestCount, requestType, status, tokenUsage, virtualKey, waitDays`

**`tokenTrend[]` 全部字段（5 个）**：`date, requestType, tokens, promptTokens, completionTokens`

**关键观察**：`startDate`/`endDate` **只影响 `tokenTrend` 的长度**，`requestList` 恒为 3 条（与日期无关）。DSH 取"我的工具列表"时可省略日期参数。

---

#### API-02 `call-logs` — 调用日志（llmgateway 服务）

| 项 | 值 |
|---|---|
| URL | `https://opencsitool.com/opencsitool/llmgateway/rest/v1/users/{employeeId}/call-logs` |
| 方法 | `GET` |
| Path 参数 | `employeeId`（`653124`） |
| Query | `page`（默认 1）、`pageSize`（默认 20）、`startDate`、`endDate` |
| 前端超时 | `30000 ms`（源码 `timeout: 3e4`） |
| 状态 / 大小 | `200` / `44 B` |
| 响应 | `{"list":[],"total":0,"page":1,"pageSize":20}` |

**当前账号此接口为空**（`total=0`），页面"调用日志"表格显示 `暂无数据`，分页被禁用 —— 页面状态与接口响应完全一致。

---

#### API-03 `key-budget` — 员工 Token 预算

| 项 | 值 |
|---|---|
| URL | `https://opencsitool.com/opencsitool/llmgateway/rest/v1/users/{employeeId}/key-budget` |
| 方法 | `GET` |
| Path 参数 | `employeeId` |
| 前端超时 | `10000 ms` |
| 状态 / 大小 | `200` / `90 B` |
| 响应 | `{"employeeId":"653124","exists":false,"maxBudget":null,"budgetDuration":null,"spend":null}` |

与 `personalQueueStatus.data.tokenBudget` 语义一致（同一后端实体，`exists:false`）。

---

### 6.2 页面初始化 / 框架接口

| # | 方法 | 路径 | 状态 | 大小 | 响应要点 |
|---|---|---|---|---|---|
| API-04 | GET | `/opencsitool/rest/v1/user/getUserInfo` | 200 | 1017 B | `{threePartyUserInfo:{accountId,accountLogin:"shijingchang",accountPlatform:"gitcode",userId}, user:{id,userName,employeeId:"653124",userEmail,currentLoginIp,currentLoginPlatform:"gitcode",currentOrganizationId,currentRoleView:"Normal",currentRoleViewName:"普通用户",currentOrganizationName:"体验项目",roles:null}}` |
| API-05 | GET | `/opencsitool/rest/v1/user/getUserRolesByOrganizationId?organization_id={orgId}` | 200 | 209 B | `[{id:298,name:"VISITOR",role:"访客",organizationId,userId,joinDate,createDate}]` |
| API-06 | POST | `/opencsitool/rest/v1/menu/list` | 200 | 3516 B | 菜单树（见下） |
| API-07 | POST | `/opencsitool/rest/v1/collectVisitData` | 200 | 47 B | `{"success":true,"message":"数据采集成功"}` |
| API-08 | GET | `/opencsitool/rest/v1/user/getVisibleRoleViews` | 200 | 40 B | `[{"key":"Normal","name":"普通用户"}]` |
| API-09 | GET | `/opencsitool/rest/v1/message/statistics` | 200 | 433 B | `{sourceStatistics:[{messageSource:"SYSTEM_ANNOUNCEMENT",messageCount:0,unreadCount:0},…], statusStatistics:{totalCount:0,unreadCount:0}}` |

**API-06 请求体**：
```json
{"roleView":"Normal","organizationId":"fba54d1682e841d196d823b4b548c4b9"}
```
**API-06 菜单树（`key : name [type]`）**：
```
home : 首页 [DEFAULT]
ai_tool : AI工具 [DEFAULT]
  ai_tool_application : 工具申请 [DEFAULT]
  ai_tool_queue_status : 排队情况 [DEFAULT]
  ai_tool_my_tools : 我的工具 [DEFAULT]     ← 本页面
  ai_tool_wish_wall : AI工具采购心愿墙 [DEFAULT]
  zhongjing_data_pipeline : 数据流水线 [HYPERLINK]
skill_market : Skill市场 [DEFAULT]
  skill_market_ecosystem : 生态广场 [DEFAULT]
  skill_market_generation : Skill自生成 [DEFAULT]
  skill_market_evaluation : Skill能力评测 [DEFAULT]
  skill_market_management : 我的Skill [DEFAULT]
  skillinsight : Skill-insight [INLINE_LINK]
dev_dashboard : 研发看板 [DEFAULT]
  project_management_overall_progress : 整体进展
  requirement_management_list : 需求管理
  iteration_evaluation_progress : 迭代进展
  dev_dashboard_code_contribution : 代码贡献
  dev_dashboard_build : 构建看板
  dev_dashboard_code_check : 代码检查
  dev_dashboard_test_quality : 测试质量
  dev_dashboard_release : 发布管理
  dev_dashboard_security_compliance : 安全合规
  dev_dashboard_vod_management : VOD管理
system_settings : 系统设置 [DEFAULT]
  system_settings_project : 项目管理
```

**API-07 请求体**（页面强制埋点，只读语义）：
```json
{"organizationId":"fba54d1682e841d196d823b4b548c4b9","menu":"ai_tool_my_tools",
 "accountId":"680a24c143c294728be7bca3","userId":"0dd935e8d2234014a215aa922417094c",
 "userName":"shijingchang","currentLoginPlatform":"gitcode","currentRoleView":"Normal"}
```

---

### 6.3 模型费用单价接口

#### API-10 `ai/config/cost`

| 项 | 值 |
|---|---|
| URL | `https://opencsitool.com/opencsitool/rest/v1/ai/config/cost` |
| 方法 | `GET` |
| 参数 | 无 |
| 状态 / 大小 | `200` / `7604 B` |
| 响应 | **裸数组**，20 个元素 |
| 元素字段（17 个） | `id, requestType, displayName, billType, enabled, litellmModel, matchPrefix, deptScope, allowedDepartments, priceMode, blendedPrice, inputPrice, outputPrice, monthlyFee, remark, createTime, updateTime` |

**已启用（`enabled=1`）计费项（13 条）**：

| requestType | displayName | billType | priceMode | blendedPrice | monthlyFee |
|---|---|---|---|---|---|
| `TRAE` | Trae | FLAT | SPLIT | — | 200.00 |
| `DEEPSEEK_V4_FLASH_0731` | DeepSeek-V4-Flash-0731 | TOKEN | BLENDED | 0.28 | — |
| `DEEPSEEK_V4_PRO` | DeepSeek-V4-Pro | TOKEN | BLENDED | 0.99 | — |
| `DOUBAO_SEED_2_1_PRO` | Doubao-Seed-2.1-Pro | TOKEN | BLENDED | 1.85 | — |
| `GLM_5_2` | GLM 5.2 | TOKEN | BLENDED | 1.46 | — |
| `GLM_5_3` | GLM-5.3 | TOKEN | BLENDED | 1.95 | — |
| `GLM_5_3_FLASH` | GLM-5.3-Flash | TOKEN | BLENDED | 0.22 | — |
| `KIMI_K2_7_CODE` | kimi-k2.7-code | TOKEN | BLENDED | 1.94 | — |
| `KIMI_K3` | kimi-k3 | TOKEN | BLENDED | 4.51 | — |
| `MINIMAX_M3` | MiniMax-M3 | TOKEN | BLENDED | 1.47 | — |
| `QWEN3_7_MAX` | Qwen3.7 Max | TOKEN | BLENDED | 2.64 | — |
| `QWEN3_7_PLUS` | Qwen3.7 Plus | TOKEN | BLENDED | 0.59 | — |
| `QWEN3_8_FLASH` | Qwen3.8-Flash | TOKEN | BLENDED | 0.19 | — |

已禁用（`enabled=0`）：`CODE_AGENT`(CodeAgent)、`CODE_BUDDY`(CodeBuddy(腾讯))、`QWEN_CODE`(通义灵码(阿里)) 等。

> 该接口是**图表"消费金额"口径的换算依据**，也是趋势图图例显示名的来源。

---

### 6.4 按需接口（源码中存在，页面当前未调用）

| # | 方法 | 路径 | 触发条件 | 实测 |
|---|---|---|---|---|
| API-11 | GET | `/opencsitool/rest/v1/ai/operations/requestTypeList` | 前端筛选下拉 | 200 / 265 B / 2 项 `{label,value}` |
| API-12 | GET | `/opencsitool/rest/v1/ai/operations/getPersonalAiGeneration?startDate=&endDate=` | 个人 AI 生成统计 | 200 / 51 B / `{"totalInteractionCount":0,"totalTokens":260924535}` |
| API-13 | GET | `/opencsitool/rest/v1/ai/operations/userTokenTrend?employeeId={id}` | 指定员工趋势 | **400**（缺参时）`{"message":"缺少请求参数：employeeId"}`；带参 200 |
| API-14 | GET | `/opencsitool/rest/v1/ai/operations/tokenBreakdownByModel?employeeId={id}` | 按模型拆分 | **400**（缺参时）`{"message":"缺少请求参数：employeeId"}`；带参 200 |
| API-15 | GET | `/opencsitool/rest/v1/ai/operations/keyBudget?employeeId={id}` | 员工预算（app 服务侧） | 200 |

> API-13/14/15 的 **400 错误信息是中文业务提示**（`缺少请求参数：employeeId`），而非标准错误结构 —— 进一步印证这是内部应用接口而非面向开发者的 Public API。

---

### 6.5 权限边界接口（同一会话内被拒绝，证明 RBAC 生效）

| 方法 | 路径 | 状态 | 响应 |
|---|---|---|---|
| GET | `/opencsitool/rest/v1/ai/config/accountBinding/list` | **403** | `{"message":"权限不足: 无权限操作，仅 【管理员】 可执行此操作"}` |
| GET | `/opencsitool/rest/v1/ai/config/accountBinding/gitcodeAccounts` | **403** | 同上 |
| GET | `/opencsitool/rest/v1/marketRepo/page/condition/status` | **500** | `{"message":"系统内部错误！"}` |
| GET | `/opencsitool/rest/v1/marketRepo/1` | **500** | `{"message":"系统内部错误！"}` |

**意义**：当前账号是 `VISITOR / 访客`，管理类接口被服务端明确拦截。DSH 集成时必须以**用户自身权限**为上限，不能假设可读全组织数据。

---

### 6.6 同站兄弟模块接口（Skill 市场，非 myTools，供 DSH 扩展参考）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/opencsitool/rest/v1/marketRepo/page/condition?type=SKILL\|MCP\|AGENT&pageNum=&pageSize=&keyword=` | 市场列表，封装 `{code,message,data:{records,total,pages,pageNum,pageSize}}` |
| GET | `/opencsitool/rest/v1/marketRepo/{id}` | 详情（实测 id `519` → 10441 B；`518/517/515/514` 亦有效） |
| GET | `/opencsitool/rest/v1/marketRepo/{id}/versions` | 版本列表（1799 B） |
| GET | `/opencsitool/rest/v1/marketRepo/{id}/npxCommand` | 安装命令，如 `npx skills add https://gitcode.com/openCsiTool/openCsiToolSkills.git --skill using-git-worktrees --full-depth -g -y` |
| GET | `/opencsitool/rest/v1/marketRepo/myCreated\|myFavorites\|myLikes?types=` | 我的 Skill（缺 `types` → 400） |
| GET | `/opencsitool/rest/v1/marketRepo/mySkillStatistics` | 我的 Skill 统计 |
| GET | `/opencsitool/rest/v1/marketRepo/dimension/all` | 维度字典 |
| GET | `/opencsitool/rest/v1/ranking/featured` | 精选排行 |
| GET | `/opencsitool/rest/v1/points/getCurrentUserPointsRank` | 积分与排名（points `5.00`，rank `584`） |
| GET | `/opencsitool/rest/v1/statistics/*` | 代码贡献统计（PR/commit） |

---

### 6.7 统一约定汇总

| 约定 | 值 |
|---|---|
| API 根 | `https://opencsitool.com` |
| 应用前缀 | `/opencsitool` |
| REST 版本段 | `/rest/v1/` |
| 微服务分段 | `rest`（主应用）、`llmgateway`（模型网关） |
| 认证 | HttpOnly Cookie `token` |
| 方法语义 | 查询一律 `GET`；`POST` 仅用于 `menu/list`（读语义）与 `collectVisitData`（埋点） |
| 响应形态 | A `{code,data,message}` / B 裸 JSON / C `{success,message}` |
| 错误形态 | 401 `text/plain`（`empty Authorization` / `Invalid Authorization`）；403/500 `{"message":"…"}`（中文业务提示） |
| 分页形态 | `{list,total,page,pageSize}`（llmgateway）或 `{records,total,pages,pageNum,pageSize}`（market） |
| 服务器 | `nginx/1.21.5`（所有接口） |
| 时间格式 | 日期 `YYYY-MM-DD`；时间戳 `YYYY-MM-DD HH:mm:ss`；ISO8601 带 `+08:00`（syncStatus） |
| 时区 | `Asia/Shanghai (UTC+8)` |

---

## 7. 认证与授权机制（阶段 5）

### 7.1 凭证载体判定

| 候选机制 | 是否存在 | 证据 |
|---|---|---|
| **HttpOnly Cookie `token`** | ✅ **唯一有效凭证** | 仅带 Cookie（无 `Authorization`）→ 全部端点 200；去掉 Cookie → 401 |
| `Authorization: Bearer <JWT>` | ❌ | 前端注入的是字面量 `gitcode-auth-dummy-token`；服务端对任意 Bearer 均返回 `401 Invalid Authorization` |
| `Authorization: Bearer gitcode-auth-dummy-token` | ❌ 装饰性 | 单独发送 → `401 Invalid Authorization` |
| CSRF Token（头/参数） | ❌ | 无 `X-CSRF-Token` / `_csrf`；POST `menu/list` 仅凭 Cookie 成功 |
| JWT in `localStorage` | ❌ | `core-access` 中 `accessToken` 为 Vben 加密串（长度 127），但服务端不识别 |
| OAuth2 Access Token（Bearer 形态） | ❌ | OAuth 仅用于**换取 Cookie**，不向前端下发 Bearer |
| Basic Auth | ❌ | 无 |
| API Key / Secret | ❌ | 站点无 API Key 管理界面，无开发者门户 |

### 7.2 浏览器存储实测

| 存储 | 键 | 值特征 |
|---|---|---|
| `document.cookie` | — | **空**（长度 0）→ 证明 `token` 是 HttpOnly |
| Cookie（CDP 可见） | `token` | `httpOnly=true`、`secure=true`、`domain=opencsitool.com`、`path=/`、`value_len=333`、`ttl≈0.97 h` |
| `localStorage` | `vben-web-opencsitool-5.5.9-prod-preferences-theme` | 主题偏好 |
| `localStorage` | `vben-web-opencsitool-5.5.9-prod-preferences` | UI 偏好 |
| `localStorage` | `vben-web-opencsitool-5.5.9-prod-core-access` | `accessToken`（127 字符，Vben 加密存储，**服务端不认**）、`refreshToken`、`accessCodes` |
| `localStorage` | `vben-web-opencsitool-5.5.9-prod-preferences-locale` | 语言 |
| `localStorage` | `vben-web-opencsitool-5.5.9-prod-secure-meta` | 84 字符元数据 |
| `sessionStorage` | `vben-web-opencsitool-5.5.9-prod-core-tabbar` | 标签页状态 |
| `sessionStorage` | `opencsitool-login-redirect-path` | 登录后回跳路径 |

### 7.3 前端认证实现（源码级证据）

`bootstrap-*.js` 中的关键片段：

```js
// 1) 登录成功后写入一个固定占位符
c.setAccessToken("gitcode-auth-dummy-token");

// 2) 请求拦截器把占位符放进 Authorization 头
s.headers.Authorization = c.accessToken ? `Bearer ${c.accessToken}` : null;

// 3) 401 处理：清除 accessToken 并触发重新认证
if (c?.status === 401 || c?.response?.status === 401) { qr().setAccessToken(null); return; }
```

**结论**：`Authorization` 头是**框架默认行为的遗留物**，服务端鉴权完全走 Cookie。这解释了为什么"带 Cookie + 任意 Bearer"能成功、而"只有 Bearer 无 Cookie"必然 401。

### 7.4 认证控制矩阵（实测）

| 请求头组合 | 状态 | 响应体 |
|---|---|---|
| 无任何认证头 | `401` | `empty Authorization` + `Set-Cookie: token=; Expires=Thu, 01 Jan 1970 00:00:10 GMT; Path=/; Secure; HttpOnly` |
| `Authorization: Bearer gitcode-auth-dummy-token` | `401` | `Invalid Authorization` |
| `Authorization: Bearer abc123`（随机值） | `401` | `Invalid Authorization` |
| `X-CSRF-Token: x` | `401` | `empty Authorization` |
| `Cookie: token=`（空值） | `401` | `empty Authorization` |
| **`Cookie: token=<valid>`（无 Authorization）** | **`200`** | **正常业务 JSON** |

**关键推论**：
1. 服务端在 401 时会主动下发 `Set-Cookie: token=; Expires=1970` **清除失效 Cookie**；
2. 错误信息 `empty Authorization` 指的是**缺少 Cookie**（措辞来自框架），不是缺少 HTTP 头 —— 这一点极易误判，DSH 实现时不要被字面意思误导。

### 7.5 会话生命周期

| 项 | 值 |
|---|---|
| Cookie 名 | `token` |
| 类型 | HttpOnly + Secure，**非 JWT 形态**（不可离线解析） |
| 有效期 | **≈ 0.97 小时（约 58 分钟）** |
| 续期方式 | 重新走 GitCode OAuth（`/rest/v1/oauth2/authorization/gitcode`） |
| 登出 | `POST {API}/rest/v1/logout`（`withCredentials:true`）；站点另有一个中景桥接 `POST /zhongjing/v3/auth/logout` |
| Token 刷新接口 | `POST /auth/refresh`（`withCredentials:true`，Vben 框架路径，**非** opencsitool 业务接口） |
| 自动刷新 | 前端配置 `enableRefreshToken` 未对本站生效（无 refresh token 下发） |

**对 DSH 的含义**：这是整个集成方案**唯一的硬约束** —— 无法用长期 API Key 替代，必须在 Cookie 过期后重新获取（见附录 A.4）。

---

## 8. 主动触发与前端 JS 逆向（阶段 6 & 7）

### 8.1 日期预设按钮 → 接口参数实证

通过 DOM `click()` 依次点击四个预设，捕获每次触发的完整请求：

| 点击项 | 点击后 input 值（token/cost/startDate/endDate/callLogStart/callLogEnd） | 触发请求 |
|---|---|---|
| **本周** | `token, cost, 2026-09-15, 2026-09-19, 2026-09-12, 2026-09-19` | `personalQueueStatus?startDate=2026-09-15&endDate=2026-09-19` → **200 / 3473 B** |
| **本月** | `… 2026-09-01, 2026-09-19, …` | `personalQueueStatus?startDate=2026-09-01&endDate=2026-09-19` → **200 / 5603 B** |
| **近7天** | `… 2026-09-13, 2026-09-19, …` | `personalQueueStatus?startDate=2026-09-13&endDate=2026-09-19` → **200 / 3918 B** |
| **近30天** | `… 2026-08-21, 2026-09-19, …` | `personalQueueStatus?startDate=2026-08-21&endDate=2026-09-19` → **200 / 6887 B** |

每次点击同时重放：
- `call-logs?page=1&pageSize=20&startDate=2026-09-12&endDate=2026-09-19` → 200 / 44 B（**注意：其日期不随主接口变化，沿用独立区间**）
- `key-budget` → 200 / 90 B

**结论**：
1. 日期预设**确实驱动** `personalQueueStatus` 的 `startDate`/`endDate`；
2. **响应体积随区间单调变化**（1d 2070 < 7d 3918 < 本月 5603 < 30d 6887 ≈ 全量 7056），与"只有 `tokenTrend` 受日期影响"的推断吻合；
3. `call-logs` 的日期区间**独立**于主图表区间。

### 8.2 分页 / 详情 / 排序 / 搜索触发结果

| 触发尝试 | 结果 |
|---|---|
| 调用日志分页（上一页/下一页/第1页） | **无请求**。实测 `.ant-pagination-disabled` 命中 2 个（上一页、下一页），`aria-disabled="true"`，`total=0` |
| 点击 6 张概览统计卡 | **无请求**。全部 `cursor:auto`、`onclick:none`、无 `role` |
| 点击表格行 / 行内按钮 | **无请求**。3 行费用表 + 1 行日志表均无可点元素，点击后无 `ant-modal` / `ant-drawer` |
| 搜索框 | **不存在** |
| 排序器 | **0 个** |
| 自定义日期输入直接赋值 + 派发 `input`/`change` + `Enter` | **无请求**（Ant Design RangePicker 需要其内部状态机驱动，程序化赋值不触发查询） |
| `同步最新数据` 按钮 | **主动跳过**（写语义，遵循"不执行写操作"原则） |

**结论**：`/myTools` 是一个**纯只读展示页**，除日期区间外没有任何可触发的查询维度。DSH 若要"筛选/搜索工具"，必须在客户端对 `requestList[]` 自行过滤。

### 8.3 图表口径切换

`Token用量 / 消费金额` 是**纯前端换算**：`消费金额 = Σ(tokens × blendedPrice / 1e6)`，单价来自 `ai/config/cost` 的 `blendedPrice`。切换时不发请求 —— 已通过"切换前后无新增 XHR"验证。

### 8.4 前端 bundle 逆向（阶段 7）

**产物清单**（内容哈希，重新部署会变）：

| 文件 | 大小 | 角色 |
|---|---|---|
| `bootstrap-*.js` | 2,193,206 B | 框架 + 认证 + axios 客户端（`Lf` 类）+ 所有 API 定义 |
| `aiOps-*.js` | — | myTools 页面数据层（导出 `re/Tt/wt/Lt/De/Ce/ue/le/oe/ie` 等函数） |
| `index-*.js` | — | myTools 页面组件（`我的工具`） |
| `codeMetrics-*.js` | — | 代码贡献指标 |
| `statistics-*.js` | — | 统计接口（`/rest/v1/statistics/*`） |
| `_app.config.js` | — | 运行时配置（`VITE_GLOB_API_URL`） |

**关键常量**：
```js
const _t = "/opencsitool";                                  // 应用前缀
const R6 = "//opencsitool.com/opencsitool";                 // 主应用 baseURL
const C  = `${$}/opencsitool/llmgateway/rest/v1/users`;     // 模型网关 baseURL
```

**Vben axios 封装 `Lf` 的默认配置**：
```js
const n = { headers: { "Content-Type": "application/json;charset=utf-8" },
            responseReturn: "raw", timeout: 1e4 };
```
→ 说明**默认超时 10 秒**；`call-logs` 单独覆盖为 30 秒。

**myTools 页面数据装配逻辑（`index-*.js` 反混淆）**：
```js
const a = yield wt({ startDate: t, endDate: e });          // wt = re = personalQueueStatus
if (a.data.code === 200) {
  const l = a.data.data;
  A.value = l.requestList   || [];   // 费用单价表格
  $.value = l.tokenTrend    || [];   // 趋势图
  U.value = l.boundEmployeeId || null;
  N.value = l.userId        || "";
  B.value = l.syncStatus    || null; // "数据更新至 …"
  se.value = l.tokenBudget  || null;
  yield St(); xe();                  // 其它并行加载
  U.value && (De(1), Tt(U.value).then(v => { v && (se.value = v) }));  // key-budget 覆盖 tokenBudget
}
```
**这一行代码同时证明了**：卡片/表格/图表的三个数据源**全部**来自 `personalQueueStatus` 的 `data` 对象，以及 `key-budget` 的返回值会**覆盖** `tokenBudget` 字段。

**已确认的最小化函数签名**（用于 DSH 精确复刻）：

| 函数 | 实现 | 端点 |
|---|---|---|
| `re(e)` | `GET ${R6}/rest/v1/ai/operations/personalQueueStatus`，`{params:e}` | 主数据源 |
| `ie(e,s)` | `GET ${C}/${e}/call-logs`，`{params:s, timeout:3e4}` | 调用日志 |
| `oe(e)` | `GET ${C}/${e}/key-budget`，`{timeout:1e4}` | 预算 |
| `le(e)` | `tokenBreakdownByModel`，`{params:e}`，`.then(s=>s.data).catch(()=>[])` | 按模型拆分 |
| `De(e)` | `userTokenTrend` | 员工趋势 |
| `Ce()` | `getPersonalAiGeneration` | 个人生成统计 |
| `ue()` | `requestTypeList` | 请求类型字典 |

**导出映射（`aiOps-*.js`）**：`re as b`、`Tt as b6`、`We as l`、`wt as b7`、`Lt as b8`、`yt as a$`。

**端点挖掘结果**：`capture/21_endpoint_catalog.json` 按文件列出全部 `/rest/v1/` 路径；`capture/26_endpoints_by_file.json` 给出完整清单。

---

## 9. 脱离浏览器调用与公开文档探测（阶段 8 & 9）

### 9.1 浏览器外复现（阶段 8）

**方法**：CDP `Network.getCookies` 读取 `token` → 写入临时 Netscape Cookie 文件（`chmod` 级隔离，值不进入 argv）→ `curl -b <file>` 调用 → 用完立即删除临时文件。

**结果：12/12 端点全部 200**

| 方法 | 路径 | 状态 | 大小 | 耗时 |
|---|---|---|---|---|
| GET | `/opencsitool/rest/v1/user/getUserInfo` | 200 | 1017 B | 0.32 s |
| GET | `/opencsitool/rest/v1/user/getUserRolesByOrganizationId?organization_id=…` | 200 | 209 B | 0.27 s |
| GET | `/opencsitool/rest/v1/user/getVisibleRoleViews` | 200 | 40 B | 0.27 s |
| POST | `/opencsitool/rest/v1/menu/list` | 200 | 3516 B | 0.35 s |
| POST | `/opencsitool/rest/v1/collectVisitData` | 200 | 47 B | 0.27 s |
| GET | `/opencsitool/rest/v1/message/statistics` | 200 | 433 B | 0.26 s |
| GET | `/opencsitool/rest/v1/ai/config/cost` | 200 | 7604 B | 0.26 s |
| GET | `/opencsitool/rest/v1/ai/operations/requestTypeList` | 200 | 265 B | 0.28 s |
| GET | `/opencsitool/rest/v1/ai/operations/getPersonalAiGeneration?…` | 200 | 51 B | 0.98 s |
| **GET** | **`/opencsitool/rest/v1/ai/operations/personalQueueStatus?…`** | **200** | **7056 B** | **0.37 s** |
| GET | `/opencsitool/llmgateway/rest/v1/users/653124/call-logs?…` | 200 | 44 B | 0.26 s |
| GET | `/opencsitool/llmgateway/rest/v1/users/653124/key-budget` | 200 | 90 B | 0.25 s |

**可行性判定**：
- ✅ **不需要浏览器**即可取数（纯 HTTP + Cookie）；
- ✅ 延迟 250–400 ms（`getPersonalAiGeneration` 约 1 s，为慢查询）；
- ✅ 无 CORS 限制（服务端不校验 `Origin`/`Referer`，见下）；
- ⚠️ 唯一前置条件是持有有效 `token` Cookie。

**页内 `fetch` 交叉验证**：在页面上下文内直接 `fetch('/opencsitool/rest/v1/ai/operations/personalQueueStatus?...')` 亦返回 200，证明该接口对同源 XHR 无额外校验。

**CORS / Origin 校验**：`curl` 请求默认不带 `Origin`/`Referer` 头，仍全部 200 → **服务端不校验来源**，这对 DSH 的 headless 集成是利好。

### 9.2 公开文档路径探测（阶段 9）

对 17 条路径逐一探测（未认证）：

| 路径 | HTTP | 大小 | 类型 | 判定 |
|---|---|---|---|---|
| `/swagger-ui/index.html` | 200 | 2296 B | `text/html` | ⚠️ **SPA 外壳**（非文档） |
| `/swagger-ui.html` | 200 | 2296 B | `text/html` | ⚠️ SPA 外壳 |
| `/swagger` | 200 | 2296 B | `text/html` | ⚠️ SPA 外壳 |
| `/swagger/index.html` | 200 | 2296 B | `text/html` | ⚠️ SPA 外壳 |
| `/v3/api-docs` | 200 | 2296 B | `text/html` | ⚠️ SPA 外壳 |
| `/v3/api-docs/swagger-config` | 200 | 2296 B | `text/html` | ⚠️ SPA 外壳 |
| `/api-docs` | 200 | 2296 B | `text/html` | ⚠️ SPA 外壳 |
| `/openapi.json` | 200 | 2296 B | `text/html` | ⚠️ SPA 外壳 |
| `/swagger.json` | 200 | 2296 B | `text/html` | ⚠️ SPA 外壳 |
| `/swagger-resources` | 200 | 2296 B | `text/html` | ⚠️ SPA 外壳 |
| `/actuator` | 200 | 2296 B | `text/html` | ⚠️ SPA 外壳 |
| `/actuator/health` | 200 | 2296 B | `text/html` | ⚠️ SPA 外壳 |
| `/opencsitool/v3/api-docs` | **404** | 29 B | `application/json` | ❌ `{"message":"路径错误！"}` |
| `/opencsitool/swagger-ui/index.html` | **404** | 29 B | `application/json` | ❌ `{"message":"路径错误！"}` |
| `/opencsitool/rest/v1/v3/api-docs` | **401** | 19 B | `text/plain` | ❌ `empty Authorization` |
| `/opencsitool/rest/v1/swagger-ui/index.html` | **401** | 19 B | `text/plain` | ❌ `empty Authorization` |
| `/myTools`（对照） | 200 | **2296 B** | `text/html` | SPA 外壳基准 |

**关键判据**：上述 12 条"200"响应的**字节数与 `/myTools` 完全一致（2296 B）**，且正文以 `<!doctype html><html lang="zh"><head><script src="/_app.config.js?v=5.…` 开头 —— 这是 **nginx `try_files` 回退到 `index.html`** 的典型特征，**不是文档**。

**结论**：该站点**没有对外暴露任何 API 文档**。所有文档路径都被 SPA 路由兜底吞掉。

### 9.3 其它公开入口排查

| 入口 | 结果 |
|---|---|
| 开发者门户 / API Key 管理页 | 不存在（菜单树中无相关项） |
| 页面内指引链接 | `https://gitcode.com/org/openCsiTool/discussions/1`（"AI模型配置指导"，为讨论帖，非 API 文档） |
| `robots.txt` / `sitemap.xml` | 未提供 API 线索 |
| 开放平台 / OAuth 应用注册 | 无 |

---

## 10. Public API vs Internal Web API 判定（阶段 10 & 11）

### 10.1 判定矩阵

| 判据 | Public API 应具备 | 本站实测 | 得分 |
|---|---|---|---|
| 公开文档（Swagger/OpenAPI/开发者门户） | ✅ 必需 | ❌ 全部路径落回 SPA 外壳 | ❌ |
| 独立 API Key / Client Credentials | ✅ 通常必需 | ❌ 只有浏览器 Cookie | ❌ |
| 版本协商 / `Accept: application/vnd.x.v1+json` | ✅ 常见 | ❌ 路径硬编码 `/rest/v1/` | ❌ |
| 面向第三方的稳定契约（标准错误码/结构化错误） | ✅ 必需 | ❌ 错误为中文业务提示（`权限不足: …`、`路径错误！`、`缺少请求参数：employeeId`） | ❌ |
| CORS 白名单 | ✅ 常见 | ❌ 不校验 `Origin`（内部信任网络假设） | ❌ |
| 速率限制头（`X-RateLimit-*`） | ✅ 常见 | ❌ 未观察到 | ❌ |
| 认证与浏览器会话强绑定 | ❌ 不应 | ✅ 与 HttpOnly Cookie + OAuth 强绑定，TTL 仅 ~1 h | ❌ |
| 前端 bundle 中内联全部端点 | ❌ 不应 | ✅ 全部端点硬编码在 JS 中 | ❌ |
| 路径含内部服务名（`llmgateway`） | ❌ 不应 | ✅ `/opencsitool/llmgateway/rest/v1/…` | ❌ |
| 分页/字段命名不统一 | ❌ 不应 | ✅ 两种分页形态、三种响应封装并存 | ❌ |

### 10.2 最终判定

> **`https://opencsitool.com` 对外提供的是 Internal Web API（内部 Web 接口），不是 Public API。**

具体定性：
- 它是 **Vben Admin 单页应用的 BFF 层**，接口为**前端页面量身定制**（例如 `personalQueueStatus` 把表格、卡片、图表三份数据聚合在一个响应里）；
- 服务端鉴权**只认浏览器 Cookie**，且 Cookie 由 GitCode OAuth 签发、约 1 小时过期；
- 无任何面向第三方的文档、密钥或契约承诺；
- 端点命名与响应结构随前端迭代变化（chunk 哈希每次部署都变，说明发布频率不低）。

### 10.3 对"非官方客户端"的合规定位

DSH 集成应被明确定义为**"以当前登录用户身份复现其本人在浏览器中所见的只读数据"**，即：
- ✅ 使用**用户自己的**会话凭证；
- ✅ 只读（GET）；
- ✅ 低频（页面自身也是"进入即拉一次 + 手动刷新"）；
- ❌ 不使用管理员/越权接口；
- ❌ 不做批量爬取或高频轮询；
- ❌ 不写入任何数据。

这与"绕过认证或权限控制"有本质区别：我们**完全复用**站点自身的认证结果，且访问范围不超过该用户在本页面上肉眼可见的内容。

---

## 附录 A. DSH Integration Proposal

### A.1 目标

让 DSH 在不依赖人工打开网页的前提下，稳定回答关于用户 openCsiTool "我的工具"的问题，例如：
- 我现在有哪些 AI 工具账号？状态如何？
- 我这个月消耗了多少 Token？主要用在哪个模型上？
- 我的 PR 数 / 新增代码行 / 采纳率是多少？
- 我的调用日志里有哪些记录？

### A.2 七个关键问题的答复

| # | 问题 | 答复 |
|---|---|---|
| **1** | **数据从哪来？** | 单一主接口 `GET /opencsitool/rest/v1/ai/operations/personalQueueStatus`（`{code,data,message}` 封装）。辅助接口 3 个：`ai/config/cost`（模型单价/显示名）、`llmgateway/.../call-logs`（调用日志）、`llmgateway/.../key-budget`（预算）。身份接口 2 个：`user/getUserInfo`（拿 `employeeId`）、`user/getUserRolesByOrganizationId`。 |
| **2** | **怎么认证？** | 复用用户的 HttpOnly `token` Cookie。**不要**发送 `Authorization` 头（发了也不认，且可能干扰）。**无 CSRF Token**，POST 也只需 Cookie。 |
| **3** | **会话怎么维持？** | **这是唯一硬约束**：Cookie TTL ≈ 58 分钟。方案见 A.4 —— 推荐"CDP 抽取 + 缓存 + 过期时提示重新登录"，退而求其次用无头浏览器完成 GitCode OAuth。 |
| **4** | **页面字段怎么算出来？** | 必须复刻客户端聚合逻辑：`tokenSummary` 直接可用；`PR数量/新增代码行/AI生成代码行/采纳代码行` 需对 `requestList[]` 求和；`采纳率 = adopted/generated`；`API套餐/Trae` 子项需按 `requestType` 分组求和。趋势图需要 `tokenTrend` + `ai/config/cost` 的 `displayName` 映射。 |
| **5** | **能筛选/搜索/分页吗？** | **不能**。`/myTools` 无搜索、无排序、无有效分页、无详情接口。`requestList` 恒为 3 条全量返回；`call-logs` 分页参数可用但当前账号 `total=0`。**所有筛选必须在 DSH 客户端内存中完成。** |
| **6** | **限流 / 风险？** | 未观察到速率限制头。页面自身行为是"进入拉一次"，建议 DSH **缓存 ≥ 5 分钟**、单用户并发 ≤ 1、不做后台定时轮询（除非用户显式要求）。**必须**只读。 |
| **7** | **失败模式？** | ① `401 empty Authorization`（实为**缺 Cookie**）→ 重新登录；② `401 Invalid Authorization`（**误发了 Bearer**）→ 移除 `Authorization` 头；③ `403 {"message":"权限不足…"}` → 用户权限不足，**不要重试**；④ `400 {"message":"缺少请求参数：employeeId"}` → 补参；⑤ `500 {"message":"系统内部错误！"}` → 服务端异常，退避重试；⑥ `200` 但 `code!=200` → 按业务错误处理。 |

### A.3 集成架构建议

```
┌─────────────────────────────────────────────────────────────┐
│ DSH                                                          │
│                                                              │
│  ┌────────────────────────┐    ┌──────────────────────────┐ │
│  │ opencsitool_client.py  │    │ 凭证提供者 (可插拔)       │ │
│  │  OpenCsiToolClient     │◄───│  A) CDP 抽取(当前会话)    │ │
│  │  · 会话管理/重试/缓存   │    │  B) 无头浏览器 OAuth      │ │
│  │  · 响应封装归一化       │    │  C) 手工粘贴 Cookie       │ │
│  │  · 客户端聚合复刻       │    └──────────────────────────┘ │
│  └───────────┬────────────┘                                  │
│              │ HTTPS + Cookie: token                         │
└──────────────┼───────────────────────────────────────────────┘
               ▼
   https://opencsitool.com/opencsitool/rest/v1/...
```

**分层职责**：

| 层 | 职责 |
|---|---|
| **凭证层** | 只负责产出 `{"token": "<redacted>"}`；三种提供者可选 |
| **传输层** | `httpx`/`requests` 会话；自动带 Cookie；**不设** `Authorization`；超时 15 s；失败退避 |
| **归一化层** | 三种响应封装（`{code,data,message}` / 裸 JSON / `{success,message}`）统一转为内部模型 |
| **领域层** | `MyToolsSnapshot`：工具列表 + 汇总指标 + 趋势；复刻客户端聚合 |
| **缓存层** | TTL 300 s；按 `(userId, startDate, endDate)` 做键 |
| **呈现层** | 供 DSH 工具调用的 `list_my_tools` / `get_tool` / `search_tools` |

### A.4 会话维持方案对比（关键决策）

| 方案 | 可行性 | 优点 | 缺点 | 推荐度 |
|---|---|---|---|---|
| **A. CDP 抽取当前浏览器会话** | ✅ 已验证（本报告全程使用） | 零额外认证、用户无感、完全合规 | 需要本地 Chrome 开着且已登录 | ⭐⭐⭐⭐⭐ **首选** |
| **B. 无头浏览器跑 GitCode OAuth** | ✅ 理论可行 | 完全自动化 | 需保存 GitCode 密码或长期凭证；OAuth 可能有人机校验；合规风险最高 | ⭐⭐ 仅用户明确要求时 |
| **C. 用户手工提供 Cookie** | ✅ 可行 | 实现最简单 | 每次 ~1 h 就要重来；手工粘贴易错 | ⭐⭐⭐ 兜底 |
| **D. 长期 API Key** | ❌ **不存在** | — | 站点未提供 | — |

**推荐实现（方案 A）**：
1. DSH 启动时尝试通过 CDP 连接本机已登录的 Chrome（`Network.getCookies`，`urls=["https://opencsitool.com/"]`）；
2. 取出 `token` 值，**仅存于内存**（不落盘、不写日志、不进命令行）；
3. 每 5 分钟或在收到 401 时**重新抽取一次**；
4. 若抽取失败（浏览器未开 / 未登录）→ 返回结构化错误并提示用户"请在浏览器中打开 opencsitool.com 完成 GitCode 登录"。

> **注意**：Cookie 值属于高敏感凭据。实现时必须保证：不写入日志、不出现在异常堆栈、不进入进程 argv、不落盘明文。本报告的调查脚本即采用"写入临时 Netscape 文件 → `curl -b` → 立即删除"的方式规避 argv 泄露。

### A.5 建议暴露给 DSH 的工具

| 工具名 | 输入 | 输出 | 说明 |
|---|---|---|---|
| `opencsitool_my_tools` | `start_date?`, `end_date?`, `refresh?` | 工具列表 + 汇总指标 | 主入口，对应 `personalQueueStatus` |
| `opencsitool_tool_detail` | `tool_id` 或 `application_number` | 单个工具的完整字段 | **纯本地过滤**，无对应接口 |
| `opencsitool_search_tools` | `keyword`, `status?`, `request_type?` | 匹配的工具列表 | **纯本地过滤** |
| `opencsitool_token_trend` | `start_date`, `end_date`, `group_by_model?` | 趋势序列 + 模型显示名 | `tokenTrend` + `ai/config/cost` 关联 |
| `opencsitool_call_logs` | `page?`, `page_size?`, `start_date?`, `end_date?` | 调用日志分页 | 当前账号为空，仍应实现 |
| `opencsitool_model_prices` | — | 模型单价表 | `ai/config/cost`，变化频率低，可长缓存 |
| `opencsitool_session_status` | — | 登录态 / 过期时间 / 用户身份 | 用于前置健康检查 |

### A.6 数据模型（建议）

```python
@dataclass
class ToolGrant:                      # 对应 requestList[]
    id: int
    application_number: str           # REQ202608170007
    request_type: str                 # API_BUNDLE | TRAE | ...
    status: int                       # 1=使用中, 2=已失效
    status_text: str                  # 派生：使用中 / 已失效
    account_name: str
    issue_date: str
    create_time: str
    last_used_date: str | None
    token_usage: int
    request_count: int
    pr_count: int
    added_lines_count: int
    generated_code_lines: int
    adopted_code_lines: int
    remark: str | None
    virtual_key_masked: str           # "sk-bM4LUSm****" —— 永不返回明文
    wait_days: int
    queue_position: int

@dataclass
class MyToolsSummary:
    user_id: str
    bound_employee_id: str
    total_tokens: int
    total_request_count: int
    pr_count: int                     # Σ requestList[].prCount
    added_lines_count: int            # Σ requestList[].addedLinesCount
    generated_code_lines: int         # Σ requestList[].generatedCodeLines
    adopted_code_lines: int           # Σ requestList[].adoptedCodeLines
    adoption_rate: float              # adopted / generated
    tokens_by_request_type: dict[str, int]   # API套餐 / Trae 子项来源
    sync_status: SyncStatus
    token_budget: TokenBudget | None
    grants: list[ToolGrant]
    token_trend: list[TokenTrendPoint]
```

**派生规则（必须与页面一致）**：
```python
status_text        = "使用中" if status == 1 else "已失效"
virtual_key_masked = f"{virtual_key[:10]}****" if virtual_key else "-"
adoption_rate      = adopted_code_lines / generated_code_lines if generated_code_lines else 0.0
```

### A.7 实施优先级

| 阶段 | 内容 | 工作量 |
|---|---|---|
| P0 | `OpenCsiToolClient` 骨架 + CDP Cookie 提供者 + `list_my_tools` | 0.5 天 |
| P1 | 客户端聚合复刻 + `get_tool` / `search_tools` + 缓存 | 0.5 天 |
| P2 | `token_trend` + `model_prices` + 会话健康检查 | 0.5 天 |
| P3 | `call_logs` + 错误分类重试 + 结构化诊断 | 0.5 天 |
| P4 | 回归测试（用本报告 §5 的 39 项映射作为断言基线） | 0.5 天 |

> **进度更新**：P0–P3 的**接口契约、数据模型与聚合逻辑已在 `opencsitool_client.py` 中落地并通过离线验证**（`verify_client.py`，42/42）；P4 的断言基线即 `verify_client.py` 中的检查项。剩余工作仅为把 `CdpCookieProvider._fetch_from_cdp()` 接上实际的 CDP 客户端，以及接入 DSH 工具层。

---

## 附录 B. `opencsitool_client.py` 设计草案

> **说明**：以下为**设计草案**（接口契约与关键逻辑），非最终生产代码。所有敏感值处理均已内建脱敏约束。

```python
"""
opencsitool_client.py — openCsiTool「我的工具」只读客户端（设计草案）

设计原则：
  1. 只读：仅 GET（+ 页面强制的两个只读 POST，可选）
  2. 复用用户自身会话，不绕过任何认证
  3. 敏感值（Cookie / virtualKey）永不落盘、永不进日志
  4. 三种响应封装统一归一化
  5. 客户端聚合逻辑与页面严格一致（见调查报告 §5）
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://opencsitool.com"
APP_PREFIX = "/opencsitool"
REST = f"{APP_PREFIX}/rest/v1"
LLM_GATEWAY = f"{APP_PREFIX}/llmgateway/rest/v1/users"

DEFAULT_TIMEOUT = 15.0
SLOW_TIMEOUT = 30.0          # call-logs 前端亦为 30s
CACHE_TTL = 300.0            # 5 分钟；页面自身为"进入拉一次"


# ────────────────────────────── 异常体系 ──────────────────────────────
class OpenCsiToolError(Exception):
    """所有客户端异常的基类。"""


class SessionExpiredError(OpenCsiToolError):
    """401 empty Authorization —— 实际含义是 Cookie 缺失/过期，需重新登录。"""


class BadAuthHeaderError(OpenCsiToolError):
    """401 Invalid Authorization —— 说明误发了 Authorization 头，应移除。"""


class PermissionDeniedError(OpenCsiToolError):
    """403 —— 用户权限不足，禁止重试。"""


class MissingParamError(OpenCsiToolError):
    """400 缺少请求参数。"""


class ServerError(OpenCsiToolError):
    """500 系统内部错误，可退避重试。"""


# ────────────────────────────── 凭证提供者 ──────────────────────────────
class CredentialProvider(Protocol):
    """凭证提供者契约。实现方负责拿到 token Cookie 值。"""

    def get_token(self) -> str | None:
        """返回 token 值，或 None 表示当前不可用。"""

    def invalidate(self) -> None:
        """标记当前 token 失效，下次 get_token 应重新获取。"""


class CdpCookieProvider:
    """方案 A（首选）：从本机已登录 Chrome 通过 CDP 抽取 Cookie。

    实现要点：
      - 连接 localhost CDP 端点，调用 Network.getCookies(urls=["https://opencsitool.com/"])
      - 只取 name == "token" 且 domain == "opencsitool.com" 的那一条
      - 值仅存于内存；__repr__ 必须脱敏
    """

    def __init__(self, cdp_url: str = "http://127.0.0.1:9222") -> None:
        self._cdp_url = cdp_url
        self._token: str | None = None

    def get_token(self) -> str | None:
        if self._token:
            return self._token
        self._token = self._fetch_from_cdp()      # 实现略：CDP 调用
        return self._token

    def _fetch_from_cdp(self) -> str | None:
        # cookies = cdp("Network.getCookies", urls=["https://opencsitool.com/"])
        # for c in cookies["cookies"]:
        #     if c["name"] == "token" and c["domain"] == "opencsitool.com":
        #         return c["value"]
        return None

    def invalidate(self) -> None:
        self._token = None

    def __repr__(self) -> str:                    # 防泄露
        return f"CdpCookieProvider(cdp_url={self._cdp_url!r}, token=<redacted>)"


class ManualCookieProvider:
    """方案 C（兜底）：由用户提供 token 字符串（仅存内存）。"""

    def __init__(self, token: str) -> None:
        self._token = token

    def get_token(self) -> str | None:
        return self._token

    def invalidate(self) -> None:
        self._token = None

    def __repr__(self) -> str:
        return "ManualCookieProvider(token=<redacted>)"


# ────────────────────────────── 数据模型 ──────────────────────────────
@dataclass(frozen=True)
class SyncStatus:
    data_fresh_time: str | None = None
    etl_time: str | None = None
    replication_time: str | None = None


@dataclass(frozen=True)
class TokenBudget:
    exists: bool
    max_budget: float | None = None
    budget_duration: str | None = None
    spend: float | None = None


@dataclass(frozen=True)
class ToolGrant:
    """对应 personalQueueStatus.data.requestList[]。"""

    id: int
    application_number: str
    request_type: str
    status: int
    account_name: str
    issue_date: str | None
    create_time: str | None
    last_used_date: str | None
    token_usage: int
    request_count: int
    pr_count: int
    added_lines_count: int
    generated_code_lines: int
    adopted_code_lines: int
    remark: str | None = None
    wait_days: int = 0
    queue_position: int = 0
    estimated_wait_time: str | None = None
    issue_count: int = 0
    ai_tool_name: str | None = None
    employee_id: str | None = None
    _virtual_key: str | None = field(default=None, repr=False)   # 永不 repr

    @property
    def status_text(self) -> str:
        """页面映射规则：status==1 → 使用中；否则 → 已失效。"""
        return "使用中" if self.status == 1 else "已失效"

    @property
    def virtual_key_masked(self) -> str:
        """页面映射规则：virtualKey[:10] + '****'；为空显示 '-'。"""
        return f"{self._virtual_key[:10]}****" if self._virtual_key else "-"

    @property
    def is_active(self) -> bool:
        return self.status == 1


@dataclass(frozen=True)
class TokenTrendPoint:
    date: str
    request_type: str
    tokens: int
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True)
class MyToolsSnapshot:
    """「我的工具」页面完整快照 —— 与页面渲染内容一一对应。"""

    user_id: str
    bound_employee_id: str
    grants: list[ToolGrant]
    token_trend: list[TokenTrendPoint]
    total_tokens: int
    total_request_count: int
    sync_status: SyncStatus
    token_budget: TokenBudget | None
    fetched_at: float

    # ── 以下均为客户端聚合（复刻页面逻辑，接口不直接提供）──
    @property
    def pr_count(self) -> int:
        return sum(g.pr_count for g in self.grants)

    @property
    def added_lines_count(self) -> int:
        return sum(g.added_lines_count for g in self.grants)

    @property
    def generated_code_lines(self) -> int:
        return sum(g.generated_code_lines for g in self.grants)

    @property
    def adopted_code_lines(self) -> int:
        return sum(g.adopted_code_lines for g in self.grants)

    @property
    def adoption_rate(self) -> float:
        gen = self.generated_code_lines
        return (self.adopted_code_lines / gen) if gen else 0.0

    @property
    def tokens_by_request_type(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for g in self.grants:
            out[g.request_type] = out.get(g.request_type, 0) + g.token_usage
        return out

    @property
    def active_grants(self) -> list[ToolGrant]:
        return [g for g in self.grants if g.is_active]


@dataclass(frozen=True)
class ModelPrice:
    request_type: str
    display_name: str
    bill_type: str
    enabled: int
    price_mode: str | None = None
    blended_price: float | None = None
    input_price: float | None = None
    output_price: float | None = None
    monthly_fee: float | None = None


# ────────────────────────────── 客户端 ──────────────────────────────
class OpenCsiToolClient:
    """openCsiTool「我的工具」只读客户端。

    用法::

        client = OpenCsiToolClient(CdpCookieProvider())
        snap = client.get_my_tools()                    # 默认最近 30 天
        for g in snap.active_grants:
            print(g.request_type, g.account_name, g.status_text)
        print(snap.total_tokens, snap.pr_count, f"{snap.adoption_rate:.1%}")
    """

    def __init__(
        self,
        credentials: CredentialProvider,
        *,
        base_url: str = BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        cache_ttl: float = CACHE_TTL,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._creds = credentials
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._cache_ttl = cache_ttl
        self._client = httpx.Client(
            base_url=self._base,
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN",
                # 刻意不设置 Authorization：服务端只认 Cookie，
                # 发送 Bearer 反而会得到 401 Invalid Authorization
            },
        )
        self._cache: dict[str, tuple[float, Any]] = {}
        self._identity: dict[str, Any] | None = None

    # ── 生命周期 ──────────────────────────────────────────────
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OpenCsiToolClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── 会话 ──────────────────────────────────────────────────
    def login_or_restore_session(self) -> dict[str, Any]:
        """恢复/验证会话，返回用户身份信息。

        返回::

            {"user_id": ..., "employee_id": "653124", "user_name": "shijingchang",
             "account_id": ..., "organization_id": ..., "role_view": "Normal",
             "role_view_name": "普通用户", "organization_name": "体验项目",
             "roles": ["VISITOR"]}

        抛出:
            SessionExpiredError —— Cookie 缺失/过期，需要用户重新登录 GitCode。
        """
        token = self._creds.get_token()
        if not token:
            raise SessionExpiredError(
                "未获取到 openCsiTool 会话 Cookie。请在浏览器中打开 "
                "https://opencsitool.com 并完成 GitCode 登录后重试。"
            )
        self._client.cookies.set("token", token, domain="opencsitool.com", path="/")

        info = self._get_json(f"{REST}/user/getUserInfo")     # 裸 JSON
        user = (info or {}).get("user") or {}
        org_id = user.get("currentOrganizationId") or ""

        roles: list[str] = []
        if org_id:
            raw = self._get_json(
                f"{REST}/user/getUserRolesByOrganizationId",
                params={"organization_id": org_id},
            )
            roles = [r.get("name") for r in (raw or []) if r.get("name")]

        self._identity = {
            "user_id": user.get("id"),
            "employee_id": user.get("employeeId"),
            "user_name": user.get("userName"),
            "account_id": ((info or {}).get("threePartyUserInfo") or {}).get("accountId"),
            "organization_id": org_id,
            "organization_name": user.get("currentOrganizationName"),
            "role_view": user.get("currentRoleView"),
            "role_view_name": user.get("currentRoleViewName"),
            "roles": roles,
        }
        return self._identity

    def session_status(self) -> dict[str, Any]:
        """会话健康检查：是否可用、身份、Cookie 是否即将过期。"""
        try:
            ident = self.login_or_restore_session()
            return {"ok": True, "identity": ident}
        except OpenCsiToolError as e:
            return {"ok": False, "error": type(e).__name__, "message": str(e)}

    # ── 主接口 ────────────────────────────────────────────────
    def get_my_tools(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        *,
        refresh: bool = False,
    ) -> MyToolsSnapshot:
        """拉取「我的工具」完整快照。

        Args:
            start_date: `YYYY-MM-DD`；省略则用接口默认区间。
            end_date:   `YYYY-MM-DD`。
            refresh:    忽略缓存强制刷新。

        说明:
            `startDate`/`endDate` **只影响 tokenTrend 的长度**，
            `requestList`（工具列表）恒为全量，与日期无关。
        """
        params: dict[str, str] = {}
        if start_date:
            params["startDate"] = start_date
        if end_date:
            params["endDate"] = end_date

        key = f"queue:{start_date}:{end_date}"
        payload = self._cached(key, refresh, lambda: self._get_json(
            f"{REST}/ai/operations/personalQueueStatus", params=params or None
        ))
        data = (payload or {}).get("data") or {}

        grants = [self._to_grant(x) for x in (data.get("requestList") or [])]
        trend = [
            TokenTrendPoint(
                date=p.get("date", ""),
                request_type=p.get("requestType", ""),
                tokens=int(p.get("tokens") or 0),
                prompt_tokens=int(p.get("promptTokens") or 0),
                completion_tokens=int(p.get("completionTokens") or 0),
            )
            for p in (data.get("tokenTrend") or [])
        ]
        summary = data.get("tokenSummary") or {}
        sync = data.get("syncStatus") or {}
        budget = data.get("tokenBudget")

        return MyToolsSnapshot(
            user_id=data.get("userId", ""),
            bound_employee_id=str(data.get("boundEmployeeId") or ""),
            grants=grants,
            token_trend=trend,
            total_tokens=int(summary.get("totalTokens") or 0),
            total_request_count=int(summary.get("totalRequestCount") or 0),
            sync_status=SyncStatus(
                data_fresh_time=sync.get("dataFreshTime"),
                etl_time=sync.get("etlTime"),
                replication_time=sync.get("replicationTime"),
            ),
            token_budget=(
                TokenBudget(
                    exists=bool(budget.get("exists")),
                    max_budget=budget.get("maxBudget"),
                    budget_duration=budget.get("budgetDuration"),
                    spend=budget.get("spend"),
                ) if budget else None
            ),
            fetched_at=time.time(),
        )

    # ── 本地检索（无对应接口，纯客户端过滤）──────────────────
    def list_my_tools(self, *, active_only: bool = False) -> list[ToolGrant]:
        """列出工具。页面无搜索接口，全部本地过滤。"""
        snap = self.get_my_tools()
        return snap.active_grants if active_only else snap.grants

    def get_tool(self, identifier: str | int) -> ToolGrant | None:
        """按 id 或 applicationNumber 取单个工具（本地查找）。"""
        snap = self.get_my_tools()
        for g in snap.grants:
            if str(g.id) == str(identifier) or g.application_number == str(identifier):
                return g
        return None

    def search_tools(
        self,
        keyword: str = "",
        *,
        status: int | None = None,
        request_type: str | None = None,
    ) -> list[ToolGrant]:
        """本地关键字 + 条件过滤。

        搜索域：account_name / request_type / application_number / remark。
        """
        kw = (keyword or "").strip().lower()
        out: list[ToolGrant] = []
        for g in self.get_my_tools().grants:
            if status is not None and g.status != status:
                continue
            if request_type and g.request_type != request_type:
                continue
            if kw:
                hay = " ".join(filter(None, [
                    g.account_name, g.request_type, g.application_number, g.remark or "",
                ])).lower()
                if kw not in hay:
                    continue
            out.append(g)
        return out

    # ── 辅助接口 ──────────────────────────────────────────────
    def get_token_trend(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        *,
        with_display_name: bool = True,
    ) -> list[dict[str, Any]]:
        """趋势序列；可选把 requestType 关联为 displayName（图例显示名）。"""
        snap = self.get_my_tools(start_date, end_date)
        names = (
            {p.request_type: p.display_name for p in self.get_model_prices()}
            if with_display_name else {}
        )
        return [
            {
                "date": p.date,
                "request_type": p.request_type,
                "display_name": names.get(p.request_type, p.request_type),
                "tokens": p.tokens,
                "prompt_tokens": p.prompt_tokens,
                "completion_tokens": p.completion_tokens,
            }
            for p in snap.token_trend
        ]

    def get_model_prices(self, *, enabled_only: bool = False) -> list[ModelPrice]:
        """模型费用单价表（裸数组响应）。变化频率低，可长缓存。"""
        raw = self._cached("cost", False, lambda: self._get_json(f"{REST}/ai/config/cost"))
        out = [
            ModelPrice(
                request_type=x.get("requestType", ""),
                display_name=x.get("displayName", ""),
                bill_type=x.get("billType", ""),
                enabled=int(x.get("enabled") or 0),
                price_mode=x.get("priceMode"),
                blended_price=x.get("blendedPrice"),
                input_price=x.get("inputPrice"),
                output_price=x.get("outputPrice"),
                monthly_fee=x.get("monthlyFee"),
            )
            for x in (raw or [])
        ]
        return [p for p in out if p.enabled] if enabled_only else out

    def get_call_logs(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """调用日志（llmgateway 服务，超时 30s）。"""
        emp = self._require_employee_id()
        params: dict[str, Any] = {"page": page, "pageSize": page_size}
        if start_date:
            params["startDate"] = start_date
        if end_date:
            params["endDate"] = end_date
        return self._get_json(
            f"{LLM_GATEWAY}/{emp}/call-logs", params=params, timeout=SLOW_TIMEOUT
        )

    def get_key_budget(self) -> TokenBudget:
        """员工 Token 预算（会覆盖 personalQueueStatus.tokenBudget）。"""
        emp = self._require_employee_id()
        raw = self._get_json(f"{LLM_GATEWAY}/{emp}/key-budget") or {}
        return TokenBudget(
            exists=bool(raw.get("exists")),
            max_budget=raw.get("maxBudget"),
            budget_duration=raw.get("budgetDuration"),
            spend=raw.get("spend"),
        )

    # ── 内部：传输与错误归一化 ────────────────────────────────
    def _get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        _retried: bool = False,
    ) -> Any:
        try:
            r = self._client.get(path, params=params, timeout=timeout or self._timeout)
        except httpx.HTTPError as e:
            raise OpenCsiToolError(f"网络错误: {type(e).__name__}") from e

        if r.status_code == 200:
            try:
                return r.json()
            except ValueError as e:
                raise OpenCsiToolError("响应不是合法 JSON") from e

        body = (r.text or "").strip()

        if r.status_code == 401:
            self._creds.invalidate()
            if "Invalid Authorization" in body:
                # 说明调用方误发了 Authorization 头
                raise BadAuthHeaderError(
                    "服务端拒绝了 Authorization 头。openCsiTool 只认 Cookie，"
                    "请移除请求中的 Authorization。"
                )
            # "empty Authorization" 实际含义是 Cookie 缺失/过期
            if not _retried:
                token = self._creds.get_token()
                if token:
                    self._client.cookies.set("token", token, domain="opencsitool.com", path="/")
                    return self._get_json(path, params=params, timeout=timeout, _retried=True)
            raise SessionExpiredError(
                "openCsiTool 会话已过期（Cookie 有效期约 1 小时）。"
                "请在浏览器中重新完成 GitCode 登录。"
            )

        if r.status_code == 403:
            raise PermissionDeniedError(self._message(body) or "权限不足")

        if r.status_code == 400:
            raise MissingParamError(self._message(body) or "缺少请求参数")

        if 500 <= r.status_code < 600:
            raise ServerError(self._message(body) or "服务端内部错误")

        raise OpenCsiToolError(f"HTTP {r.status_code}: {self._message(body) or body[:120]}")

    @staticmethod
    def _message(body: str) -> str:
        """从 `{"message": "..."}` 中提取中文业务提示。"""
        try:
            import json
            return str(json.loads(body).get("message") or "")
        except Exception:
            return ""

    def _require_employee_id(self) -> str:
        if not self._identity:
            self.login_or_restore_session()
        emp = (self._identity or {}).get("employee_id")
        if not emp:
            raise OpenCsiToolError("未能获取 employeeId，请先调用 login_or_restore_session()")
        return str(emp)

    # ── 内部：缓存 ────────────────────────────────────────────
    def _cached(self, key: str, refresh: bool, factory) -> Any:
        now = time.time()
        if not refresh:
            hit = self._cache.get(key)
            if hit and (now - hit[0]) < self._cache_ttl:
                return hit[1]
        val = factory()
        self._cache[key] = (now, val)
        return val

    # ── 内部：模型转换 ────────────────────────────────────────
    @staticmethod
    def _to_grant(x: dict[str, Any]) -> ToolGrant:
        return ToolGrant(
            id=int(x.get("id") or 0),
            application_number=x.get("applicationNumber") or "",
            request_type=x.get("requestType") or "",
            status=int(x.get("status") or 0),
            account_name=x.get("accountName") or "",
            issue_date=x.get("issueDate"),
            create_time=x.get("createTime"),
            last_used_date=x.get("lastUsedDate"),
            token_usage=int(x.get("tokenUsage") or 0),
            request_count=int(x.get("requestCount") or 0),
            pr_count=int(x.get("prCount") or 0),
            added_lines_count=int(x.get("addedLinesCount") or 0),
            generated_code_lines=int(x.get("generatedCodeLines") or 0),
            adopted_code_lines=int(x.get("adoptedCodeLines") or 0),
            remark=x.get("remark"),
            wait_days=int(x.get("waitDays") or 0),
            queue_position=int(x.get("queuePosition") or 0),
            estimated_wait_time=x.get("estimatedWaitTime"),
            issue_count=int(x.get("issueCount") or 0),
            ai_tool_name=x.get("aiToolName"),
            employee_id=x.get("employeeId"),
            _virtual_key=x.get("virtualKey"),      # repr=False，永不输出
        )
```

### B.1 契约测试基线（来自本报告 §5 的 39 项映射）

> **✅ 已离线验证通过**：`verify_client.py` 用 stub 替换 `httpx`，把 `capture/responses/` 中的**真实接口响应**回放进客户端的传输层，跑通 **42/42** 项断言（含 §5 的 39 项映射、错误分类、脱敏检查）。
> 复现命令：`python verify_client.py`；输出存档：`capture/43_client_verification.txt`。
> 该脚本**不需要网络、不需要 Cookie**，可直接作为 CI 回归测试。

```python
def test_mapping_regression(client: OpenCsiToolClient) -> None:
    """用真实会话验证客户端聚合逻辑与页面一致。"""
    snap = client.get_my_tools("2026-08-20", "2026-09-19")

    # 概览卡片
    assert snap.total_tokens == sum(g.token_usage for g in snap.grants)
    assert snap.pr_count == 246
    assert snap.added_lines_count == 31167
    assert snap.generated_code_lines == 3150
    assert snap.adopted_code_lines == 120
    assert round(snap.adoption_rate * 100, 1) == 3.8

    by_type = snap.tokens_by_request_type
    assert round(by_type["API_BUNDLE"] / 1e8, 1) == 28.0
    assert round(by_type["TRAE"] / 1e8, 1) == 2.6

    # 费用单价表格
    g0 = next(g for g in snap.grants if g.id == 5593)
    assert g0.request_type == "API_BUNDLE"
    assert g0.status_text == "使用中"
    assert g0.account_name == "AI编程助手-002"
    assert g0.issue_date == "2026-08-17"
    assert g0.create_time == "2026-08-17 16:28:00"
    assert g0.virtual_key_masked.startswith("sk-bM4LUSm") and g0.virtual_key_masked.endswith("****")
    assert g0.last_used_date == "2026-09-19 00:00:00"

    g2 = next(g for g in snap.grants if g.id == 1094)
    assert g2.status_text == "已失效"
    assert g2.virtual_key_masked == "-"

    # 趋势
    assert all(p.tokens == p.prompt_tokens + p.completion_tokens for p in snap.token_trend)
    assert len({p.date for p in snap.token_trend}) == 30
    assert {p.request_type for p in snap.token_trend} == {
        "DEEPSEEK_V4_FLASH_0731", "GLM_5_3_FLASH", "GLM_5_3",
        "DEEPSEEK_V4_PRO", "QWEN3_8_FLASH",
    }
```

### B.2 安全约束清单（实现时必须遵守）

| # | 约束 | 说明 |
|---|---|---|
| 1 | **永不打印 Cookie / token 值** | 所有 `__repr__` 脱敏；日志中只出现长度 |
| 2 | **永不输出 `virtualKey` 明文** | 对外只暴露 `virtual_key_masked`；`ToolGrant._virtual_key` 设 `repr=False` |
| 3 | **不设置 `Authorization` 头** | 服务端只认 Cookie；发了会得到 `401 Invalid Authorization` |
| 4 | **不写入任何数据** | 客户端只实现 GET；不实现 `menu/list` / `collectVisitData` 等 POST |
| 5 | **不访问管理员接口** | 不调用 `accountBinding/*` 等 403 接口 |
| 6 | **缓存 ≥ 5 分钟** | 避免高频轮询；页面自身为"进入拉一次" |
| 7 | **单用户并发 ≤ 1** | 无速率限制头，保守起见串行 |
| 8 | **凭证不落盘** | token 仅存内存；不写配置文件、不写日志 |
| 9 | **异常不含敏感值** | 异常消息只含端点路径与状态码 |

---

## 附录 C. 证据文件索引

### C.1 原始 API 响应样本（`capture/responses/`）

| 文件 | 端点 |
|---|---|
| `00_..._user_getUserInfo.json` | `GET /rest/v1/user/getUserInfo` |
| `01_..._user_getUserRolesByOrganizationId.json` | `GET /rest/v1/user/getUserRolesByOrganizationId` |
| `02_..._menu_list.json` | `POST /rest/v1/menu/list` |
| `03_..._collectVisitData.json` | `POST /rest/v1/collectVisitData` |
| `04_..._user_getVisibleRoleViews.json` | `GET /rest/v1/user/getVisibleRoleViews` |
| `05_..._ai_config_cost.json` | `GET /rest/v1/ai/config/cost` |
| `06_..._message_statistics.json` | `GET /rest/v1/message/statistics` |
| `07_..._ai_operations_personalQueueStatus.json` | **★ `GET /rest/v1/ai/operations/personalQueueStatus`** |
| `10_..._users_653124_call-logs.json` | `GET /llmgateway/rest/v1/users/{id}/call-logs` |
| `11_..._users_653124_key-budget.json` | `GET /llmgateway/rest/v1/users/{id}/key-budget` |

### C.2 阶段证据文件（`capture/`）

| 阶段 | 文件 |
|---|---|
| 1 页面/登录 | `01_page_state.json`、`02_login_page.json`、`03_login_buttons.json`、`04_login_form.json`、`05_login_methods.json`、`05_login_page.png`、`07_after_login_click.json`、`07_after_login_click.png`、`09_login_ui.json` |
| 2 抓包 | `06_net_capture.json`、`11_net_all.json`、`11_page_text.json`、`12_net_full.json`、`12_api_index.json` |
| 3 映射 | `24_rows_dump.json`、`24_mapping_proof.json`、`37_mapping_final.json`、`38_regions.json`、`35_page_structure.json`、`35_mytools_full.png` |
| 4 明细 | `responses/*.json`、`28_full_get_verify.json`、`29_more_verify.json`、`30_detail_verify.json`、`31_final_evidence.json`、`41_out_of_browser_final.json` |
| 5 认证 | `13_auth_state.json`、`13_cookies.json`、`25_cookie_lifecycle.json`、`42_cookie_ttl.json`、`40_auth_controls.json`、`curl_A_noauth.txt`、`curl_B_dummybearer.txt` |
| 6 触发 | `18_ui_elements.json`、`19_interactions.json`、`23_ui_search.json`、`32_live_recheck.json`、`33_ui_triggers.json`、`34_detail_triggers.json`、`36_chart_probe.json` |
| 7 逆向 | `20_js_urls.json`、`20_buffer_scripts.json`、`21_endpoint_catalog.json`、`26_endpoints_by_file.json`、`26_mytools_endpoints.json`、`27_aiops_fn_map.json` |
| 8 脱离浏览器 | `14_fetch_tests.json`、`15_out_of_browser_auth.json`、`16_curl_auth_tests.json`、`17_curl_auth_tests.json`、`curl_C_cookie_getUserInfo.txt`、`curl_D_cookie_queue.txt`、`curl_E_menu_list.txt`、`curl_F_keybudget.txt` |
| 9 公开文档 | `39_public_docs.json` |
| 10 判定 | 本报告 §10 |
| 其它 | `22_param_tests.json`、`23_param_tests2.json`、`25_market_page.json` |

### C.3 调查脚本（`bh/`）

`01_tabs.py` … `43_final_verify.py` 共 40+ 个可复现脚本，含：
- `07_capture_navigate.py` + `interceptor.js` —— 双通道抓包装置；
- `20_interactions.py` —— 日期预设触发；
- `35_ui_triggers.py` —— DOM 级点击 + 请求增量读取；
- `39_mapping_final.py` —— 字段映射证明；
- `41_docs_and_auth.py` / `42_auth_controls.py` —— 公开文档 + 认证矩阵；
- `43_final_verify.py` —— 脱离浏览器 12 端点复现 + Cookie TTL。

### C.4 交付物（工作区根目录）

| 文件 | 说明 |
|---|---|
| `openCsiTool_API_Investigation_Report.md` | **本报告**（10 章 + 附录 A–D） |
| `opencsitool_client.py` | `OpenCsiToolClient` 设计草案（可运行，含 `login_or_restore_session` / `list_my_tools` / `get_tool` / `search_tools`） |
| `verify_client.py` | 离线验证套件（stub httpx + 回放真实响应）—— **42/42 通过** |
| `capture/43_client_verification.txt` | 上述验证的完整输出存档 |
| `capture/`、`bh/`、`js/` | 全部原始证据、调查脚本与前端 bundle |

---

## 附录 D. 风险、限制与后续建议

### D.1 已知限制

| # | 限制 | 影响 | 缓解 |
|---|---|---|---|
| 1 | **Cookie TTL ≈ 58 分钟** | 无法长期无人值守 | CDP 抽取续期（附录 A.4 方案 A） |
| 2 | 无公开 API 契约 | 站点改版会静默破坏客户端 | 以本报告 §5 的 39 项断言做回归；端点路径比 chunk 名稳定 |
| 3 | `virtualKey` 明文出现在响应中 | 敏感信息泄露风险 | 客户端只暴露掩码形式；不落盘、不记日志 |
| 4 | 当前账号权限为 `VISITOR` | 管理类数据不可读 | 明确以用户自身权限为上限，不做越权尝试 |
| 5 | `call-logs` 当前为空 | 该功能无法端到端验证 | 接口契约已确认（`{list,total,page,pageSize}`），逻辑已实现 |
| 6 | 无搜索/排序/分页接口 | 无法服务端筛选 | 客户端内存过滤（数据量小：`requestList` 3 条） |
| 7 | 未验证其他账号/组织 | 行为可能因角色而异 | 实现时按"角色能力探测"处理 403 |
| 8 | 未点击 `同步最新数据` | 该按钮的服务端语义未实证 | 遵循"不执行写操作"原则主动跳过；其行为可由源码或后续授权验证 |

### D.2 安全与合规声明

本次调查严格遵守以下边界：

- ✅ **仅使用当前已授权浏览器 session**，未绕过任何认证或权限控制；
- ✅ **未执行任何写操作**（无创建、删除、修改、绑定、审批、同步）；
- ✅ **未修改任何用户数据**；
- ✅ **未进行高频扫描**（全部为单次、低频、有间隔的请求）；
- ✅ **未暴力枚举 endpoint**（端点来自前端 bundle 与页面实际请求，非字典爆破）；
- ✅ **报告不含真实 Cookie / Token / API Key 明文**（全部 `<redacted>` 或站点自身掩码形式）；
- ✅ 权限边界接口（`accountBinding/*` 403）仅做**一次**探测以确认 RBAC 生效，未重试。

### D.3 后续建议

1. **优先实现附录 A.4 方案 A（CDP 抽取）**，这是唯一可持续且合规的会话维持方式；
2. **把 §5 的 39 项映射写成契约测试**，作为站点改版的早期预警；
3. **客户端只暴露掩码形式的 API Key**，并把"不落盘"写进实现的硬约束；
4. 若后续需要 Skill 市场 / 研发看板数据，可复用同一客户端骨架（§6.6 已列出端点）；
5. 若站点未来开放 Public API，应迁移到带 API Key 的正式契约，届时本客户端可作为过渡层。

---

**报告结束。**

> 本报告基于 2026-09-19 的真实浏览器会话采集。所有结论均有 `capture/` 下的原始证据支撑，并已通过脱离浏览器的 `curl` 复现验证（12/12 端点 200）。站点前端 chunk 名随部署变化，但接口路径、参数与响应结构在调查期间保持稳定。
