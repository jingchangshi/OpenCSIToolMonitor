# GitCode `/oauth/authorize` SPA —— 纯 HTTP OAuth 可行性调查

**调查对象**：`opencsitool.com` OAuth 入口 → `gitcode.com/oauth/authorize` SPA → openCsiTool `token` cookie
**方法**：静态 bundle 分析 + CDP 线上抓包 + 纯 `urllib` 复现实验
**结论**：**`PURE_HTTP_OAUTH_FEASIBLE`**

本文件的所有请求/响应值均已脱敏：cookie 值、`code`、`state`、`xauth_token` 只记录**名称、长度、SHA-256 前 12 位**，
URL 一律去掉 query 后再打印；`client_id` 按本仓库既有约定写作 `<client_id>`（它是公开的 OAuth 客户端标识，
不是密钥，但既有文档一律 mask，此处保持一致）。全程**未调用任何业务接口**，**未调用授权同意（consent）接口**。

---

## 1. Method

四支探针，全部可重复运行。前两支只读，第三支会产生一次认证副作用，第四支只读。

| 探针 | 作用 | 安全标签 |
| --- | --- | --- |
| `tools/probe_oauth_spa_static.py` | 下载并静态分析 authorize SPA 的 bundle，还原 API 调用点与压缩别名 | LIVE / NETWORK / read-only / NO_AUTH |
| `tools/probe_oauth_spa_dynamic.py` | 用 CDP 抓 `Network` 事件，记录页面真实发出的请求序列 | LIVE / NETWORK / AUTH_SIDE_EFFECT / GET only |
| `tools/probe_oauth_spa_pure_http.py` | 用纯 `urllib` + `CookieJar` 复现整条流程，验证无需 JS | LIVE / NETWORK / AUTH_SIDE_EFFECT / read-only on business data |
| `tools/probe_oauth_spa_proxy.py` | 隔离测量"浏览器能否到达 opencsitool.com"与系统代理的关系 | LIVE / NETWORK / read-only / writes nothing |

复现命令：

```powershell
python tools/probe_oauth_spa_static.py     --json "$env:TEMP\oauth-spa\report.json"
python tools/probe_oauth_spa_dynamic.py    --port 9333 --wait 30 --json "$env:TEMP\oauth-spa\dynamic.json"
python tools/probe_oauth_spa_pure_http.py  --port 9333
python tools/probe_oauth_spa_proxy.py      --json "$env:TEMP\oauth-spa\proxy.json"
```

### 1.1 三个环境事实（与调查前提不符，必须先记录）

**(a) 给定的 9333 profile 实测是"已登录"状态。** `%LOCALAPPDATA%\OpenCSI\auth-test-profile` 中存在
`GITCODE_ACCESS_TOKEN` / `GITCODE_REFRESH_TOKEN` / `GitCodeUserName`，有效期约 **4320 小时（180 天）**；
`gitcode.com/` 实测跳转到 `/dashboard`。任务描述称该 profile "已登出 GitCode"，**该描述在本次测量时不成立**。
原因是前一轮的 bridge 实验（`tools/probe_gitcode_sso_bridge2.py`）向该 profile 植入了 GitCode 会话 cookie ——
即 profile 创建时确实是登出的，是被那次实验登录的。这是记录，不是矛盾。

因此 **配置 C（GitCode 登出）** 用一个新建的一次性 profile
`%LOCALAPPDATA%\OpenCSI\spa-signedout-profile` 测得，未触碰原 profile。

**(b) 9333 上的 headless Chrome 一开始完全无法访问 opencsitool.com。** Chrome 继承 Windows 系统代理
`127.0.0.1:7890`，而该代理**恰好只对 opencsitool.com 失败**。详见 §4.4。这不是本次调查引入的问题，
而是**产品自身的静默续期在这台机器上会直接失败**。修复方式是给浏览器加 `--no-proxy-server`。

**(c) bundle 会轮换。** 静态分析基于 `index-a97e2b06.js`（该 URL 现在仍可下载，且其配套的
`authorize-54c99276.js` 也仍在 CDN 上，二者自洽）。抓包当天线上外壳已指向更新版
`index-e12962d8.js` / `authorize-6870efd9.js`。**已对当前版本复核**：`checkOrAuthorize`、
`/api/v1/oauth/client/${e}`、`/uc/api/v1/oauth/authorize` 及全部 consent 字段名在新版中完全一致，
结论不依赖旧 hash。

---

## 2. Bundles

`GET https://gitcode.com/login` 与 `GET <authorize URL>` 返回的**是同一个外壳**（均为 5527 B，
sha256 `cede48c3925b`）：GitCode 的 `/oauth/authorize` 不是服务端渲染的授权页，而是一个
`<div id="app">` 空壳加一个 Vue 路由表，路由项为 `{path:"/oauth/authorize", name:"authorize"}`。

| 资源 | 大小 (B) | 说明 |
| --- | --- | --- |
| `assets/index-a97e2b06.js` | 1,889,881 | 主 bundle：路由表 + axios 封装 + 全部 API 工厂函数 |
| `assets/vendor-a7fa94b7.js` | 562,288 | 第三方库 |
| `assets/vendor-layout-a25c0063.js` | 984,908 | 布局与 API 层 |
| `assets/authorize-54c99276.js` | 11,398 | **授权页路由 chunk**，含 consent 表单构造 |
| `assets/index-0ccfd32c.js` | 1,263 | authorize chunk 依赖 |
| `assets/login-8974df96.js` | 988 | 登录页 chunk |

**Source maps：不存在。** 三个 bundle 都以 `//# sourceMappingURL=*.js.map` 结尾，但对应 `.map`
全部返回 `404`（`application/xml` 错误页）。因此**没有还原出原始函数名**——§3 表中的函数名
（`Gv`、`Nv`、`Af` 等）是**压缩后的符号**，通过主 bundle 末尾的 `export{...}` 映射表反查得到，
不是源码名。这一点必须如实标注。

**别名解析方法**：`authorize-54c99276.js` 写 `import{j9 as P}from"./index-a97e2b06.js"`，
即取主 bundle 的**导出名** `j9` 并本地叫 `P`。主 bundle 末尾 `export{...Gv as j9...}` 建立
`j9 -> Gv` 的反查。忽略"导出名 / 本地别名"的区别会得到错误的函数（第一版探针即如此，
把 `C` 错配成 `/api/v2/projects/${e}/repository/pre_archive`）。修正后 17/17 全部解析成功。

---

## 3. Endpoints recovered

`observed on the wire` = CDP 抓到；`read from the bundle` = 只在 JS 里读到，**未调用**。

| # | method | path | parameters | purpose | evidence |
| --- | --- | --- | --- | --- | --- |
| 1 | GET | `opencsitool.com/opencsitool/rest/v1/oauth2/authorization/gitcode` | query `redirect` | OAuth 入口，`302` 到 GitCode；同时 `Set-Cookie: gitcode_oauth_session` | observed |
| 2 | GET | `gitcode.com/oauth/authorize` | query `client_id`, `redirect_uri`, `response_type=code`, `scope=all_user`, `state` | 返回 SPA 外壳（5527 B），授权判定由 JS 发起 | observed |
| 3 | **POST** | `web-api.gitcode.com/uc/api/v1/oauth/checkOrAuthorize` | multipart: `client_id`, `state`, `redirect_uri`, `response_type` | **核心**：判定"已有授权→直接发 code"还是"需要同意页" | observed |
| 4 | GET | `web-api.gitcode.com/uc/api/v1/oauth/client/{client_id}` | query `redirect_uri` | 应用元数据（名称/logo/scopes），用于渲染同意页 | observed |
| 5 | GET | `opencsitool.com/opencsitool/rest/v1/oauth2/authorization/callback/gitcode` | query `client_id`, `code`, `response_type`, `scopes`, `state` | 用 `code` 换 openCsiTool `token` cookie | observed |
| 6 | POST | `web-api.gitcode.com/uc/api/v1/oauth/authorize` | form: `client_id`, `state`, `redirect_uri`, `response_type`, `scopes`, `access`(bool), `prompt` | **同意提交**。原生 `<form>` POST，非 XHR | **read from the bundle —— 未调用** |
| 7 | POST | `web-api.gitcode.com/uc/api/v1/oauth/reauth-transactions/{id}/authorization-codes` | 路径参数 `transactionId` | reauth 场景下签发 code（`Af`） | read from the bundle |
| 8 | GET | `web-api.gitcode.com/uc/api/v1/user/oauth/token` | — | 返回 `access_token` / `refresh_token`（`checkIsLogin`） | read from the bundle |
| 9 | GET | `web-api.gitcode.com/uc/api/v1/user/oauth/userInfo` | — | 当前用户信息（`xv`） | read from the bundle |

### 3.1 两条 URL 重写规则（复现时最容易写错的地方）

bundle 里写的是 `url:"/api/v1/oauth/checkOrAuthorize"`，但**线上真实 URL 是**
`https://web-api.gitcode.com/uc/api/v1/oauth/checkOrAuthorize`。中间叠了**两层**改写：

1. **baseURL**：`ct()` 返回 `https://web-api.gitcode.com`（源码常量 `VITE_API_HOST`）。
2. **前缀补全**：axios 请求拦截器把满足条件的路径前面加上 `io`，而 `io = "/uc"`：

```js
t.url = ((e, t) => {
  const o = io;                                   // "/uc"
  return e?.includes("/api/v1/user/") ||
         e?.includes("/api/v1/oauth/") ||         // checkOrAuthorize 命中这一条
         e?.includes("/api/v1/internal/messages") ||
         e?.includes("/api/v1/follow") ||
         (e?.includes("/api/v1/obs") && "get" === t)
    ? `${o}${e}` : e;
})(t.url, t.method)
```

**只做第一层会得到 404**，必须两层都做。

### 3.2 `checkOrAuthorize` 的请求与响应

请求（multipart/form-data，字段名与顺序按 SPA 的 `FormData` 构造顺序）：

```
client_id, state, redirect_uri, response_type
```

响应 JSON **顶层键**（无嵌套信封——SPA 读 `response.data.data`，但 axios 的 `.data` 就是 HTTP body）：

| 键 | 类型 | 含义 |
| --- | --- | --- |
| `redirect_uri` | string | **已含授权 code 的回调地址**（有授权时） |
| `reauth_required` | null / bool | reauth 场景标志 |
| `reauth_transaction_id` | null / string | reauth 事务 id |

无 GitCode 会话时该接口返回 **`401`**，body 键为
`error_code`(num), `error_code_name`(str), `error_message`(str), `trace_id`(str)。

### 3.3 应用元数据接口的响应

`GET /uc/api/v1/oauth/client/{client_id}?redirect_uri=...` 响应键：

`object_id`, `client_name`, `client_index`, `logo`, `client_desc`, `scopes`(数组), `additional_information`

---

## 4. Request sequences

### 4.1 配置 A —— 已有授权 / 已登录（**已到达**）

序列极短，**只有一次后端调用**：

```
GET  opencsitool.com/.../authorization/gitcode?redirect=%2FmyTools
      302 -> gitcode.com/oauth/authorize?client_id=..&redirect_uri=..&response_type=code&scope=all_user&state=..
GET  gitcode.com/oauth/authorize                       200  text/html      (SPA 外壳)
GET  cdn-static.gitcode.com/assets/*                   (bundle，无鉴权)
POST web-api.gitcode.com/uc/api/v1/oauth/checkOrAuthorize   200  application/json
      -> {"redirect_uri": ".../callback/gitcode?...&code=..&state=..", ...}
GET  opencsitool.com/.../authorization/callback/gitcode     200
      -> Set-Cookie: token   (len=333)
GET  opencsitool.com/                                    200  (进入 /apps/welcome)
```

**页面自己就完成了回调**，`location.replace(o.redirect_uri)` 由 JS 执行。
整个流程**没有任何 consent 交互**。

### 4.2 配置 B —— 无授权 / 需要同意（**未到达**）

**未到达。** 要构造"GitCode 已登录但 openCsiTool 尚未获得授权"的状态，需要先撤销已有授权，
而撤销是账户持有者的操作，本次调查没有做，也**没有**去调用同意接口来制造该状态。
因此 §3 表中第 6 项（consent 提交）**只有 bundle 证据，没有线上证据**。

从 bundle 可以确定同意页的完整形状（`authorize-54c99276.js`，函数 `Ie`）：
构造 `client_id, redirect_uri, state, response_type, scopes`，另加 `i.access = ("agree" === e)`，
再 append `prompt`（仅当 `prompt=login`）；用**原生 `<form method="POST">`** 提交到
`${ct()}/uc/api/v1/oauth/authorize`。按钮绑定为 `Ie("agree")` 与 `Ie("cancel")`。

**按约束第 2 条，该接口未被调用。** 这是刻意的：同意第三方应用授权是用户决定，不能自动化。

### 4.3 配置 C —— GitCode 登出（**已到达**，一次性 profile）

```
GET  opencsitool.com/.../authorization/gitcode           302 -> gitcode.com/oauth/authorize
GET  gitcode.com/oauth/authorize                         200  text/html   (SPA 外壳)
GET  cdn-static.gitcode.com/assets/*                     (bundle)
POST web-api.gitcode.com/uc/api/v1/oauth/checkOrAuthorize   401  application/json
      -> {"error_code":..,"error_code_name":..,"error_message":..,"trace_id":..}
GET  cdn-static.gitcode.com/assets/authorize-54c99276.js (同意页 chunk 被加载)
GET  web-api.gitcode.com/uc/api/v1/oauth/client/<client_id>   200
GET  web-api.gitcode.com/uc/api/v1/captcha/config        200
POST web-api.gitcode.com/uc/api/v1/qrcode/wechat_mini_program   200
GET  web-api.gitcode.com/uc/api/v1/task/v2/sign_status   200
```

**流程停在登录页**（最终落点 `gitcode.com/login`）。注意 `401` 之后 SPA **才**去加载
`authorize-54c99276.js` —— 这直接说明：**授权页 chunk 是"需要同意"分支才加载的**，
"已有授权"分支根本不会去取它。

### 4.4 附加测量 —— 系统代理与浏览器可达性

这是独立于 OAuth 的**产品缺陷**，单列。四组配置，每组 3 个 host：

| 配置 | opencsitool.com | gitcode.com | web-api.gitcode.com |
| --- | --- | --- | --- |
| baseline（继承系统代理 7890） | **FAIL** `net::ERR_CONNECTION_CLOSED` | ok | ok |
| `--no-proxy-server` | **ok** | ok | ok |
| `--proxy-bypass-list=opencsitool.com` | **FAIL**（3/3 次） | ok | ok |
| `--proxy-server=http://127.0.0.1:7890 --proxy-bypass-list=opencsitool.com` | **ok** | ok | ok |
| 死代理控制组 `--proxy-server=http://127.0.0.1:1` | FAIL `ERR_PROXY_CONNECTION_FAILED` | FAIL | FAIL |
| 死代理 + `--proxy-bypass-list=opencsitool.com` | **ok** | FAIL | FAIL |

结论：

- **失败是 host-specific 的，不是全局的。** `gitcode.com` 与 `web-api.gitcode.com` 走同一个代理
  完全正常；只有 `opencsitool.com` 失败。
- **失败模式是 TLS 握手被切断**，不是 DNS、不是拒连。`curl -v -x 127.0.0.1:7890` 显示代理
  `CONNECT` 返回 `200 Connection established`（隧道建立成功），随后
  `schannel: failed to receive handshake`；直连同一 URL 返回 `200`。因此是**代理在隧道内转发
  opencsitool.com 的 TLS 时失败**（代理侧对该 host 的 upstream 有问题），Chrome 将其报为
  `ERR_CONNECTION_CLOSED`。
- **`--proxy-bypass-list` 单独使用无效**（3/3 复现），**必须与显式 `--proxy-server` 同时给出才生效**：
  显式指定 `--proxy-server=http://127.0.0.1:7890` + bypass 时 `opencsitool.com` 变为 ok。
  即：bypass 列表对"从操作系统继承来的代理"不被采纳。
- 死代理控制组证明 bypass 的语义**确实是只豁免列出的 host**：死代理下只有
  `opencsitool.com` 成功，另外两个仍失败。所以 bypass 不是"顺带关闭了代理"。

**推荐：`--no-proxy-server`。** 理由：该浏览器是**专用自动化 profile**，唯一用途是访问
gitcode.com 与 opencsitool.com 完成 OAuth；给它一条已知会破坏 TLS 的代理路径没有收益。
`--proxy-server` + `--proxy-bypass-list` 虽然也能修好，但要求代码知道系统代理地址、
并在"代理本身坏了"时仍然依赖它——多一个故障点。若将来确实需要让浏览器走代理访问其他站点，
再退回 bypass 方案（且必须显式带上 `--proxy-server`）。

**未改动用户任何代理设置**，未修改 `src/`。

### 4.5 纯 HTTP 复现（决定性实验）

`probe_oauth_spa_pure_http.py` 不执行任何 JS，只用 `urllib` + `CookieJar`：

1. 从浏览器读出 GitCode cookie，装入 `CookieJar`，**排除** openCsiTool 的 `token`；
2. `GET` OAuth 入口，**不跟随重定向**，读出 `302` 的 `Location`；
3. `POST` `checkOrAuthorize`（multipart，字段同上）；
4. 跟随响应里的 `redirect_uri`，检查是否出现 `token` cookie。

**cookie 范围矩阵**（每次都是全新 `CookieJar`，`token` 始终排除）：

| 范围 | 装入的 GitCode cookie | 是否签发 `token` |
| --- | --- | --- |
| `all` | 全部 21 个（含 WAF `HWWAFSESID`/`HWWAFSESTIME`/`BENSESSCC_TAG`、统计 cookie） | **是** |
| `sso` | 3 个：`GITCODE_ACCESS_TOKEN`, `GITCODE_REFRESH_TOKEN`, `GitCodeUserName` | **是** |
| `access` | **仅** `GITCODE_ACCESS_TOKEN` | **是** |

`token` cookie 长度 333，每次值不同（说明是新签发的，不是被搬运进来的）。

**关于 `access` 范围的精确表述**：在本次实测的这三个范围下，**单独一个
`GITCODE_ACCESS_TOKEN` cookie 就足以完成整条流程并换到新的 `token`**。这证明
`GITCODE_ACCESS_TOKEN` 本身就是 GitCode 侧的有效会话凭据，`GITCODE_REFRESH_TOKEN`、
`GitCodeUserName`、WAF cookie、浏览器 UA、`Origin`/`Referer` 头**都不是必要条件**。
这是**在这三个范围上测得的结论**，不外推到其他 host 或未来的服务端改动。

---

## 5. Verdict

### `PURE_HTTP_OAUTH_FEASIBLE`

**证据**：一个不执行任何 JavaScript 的 `urllib` 客户端，仅凭 GitCode 会话 cookie，
在三个 cookie 范围下**各完成一次**完整流程并签发出真实的 openCsiTool `token` cookie（§4.5）。

完整序列只有三步，全部有线上证据：

```
GET  opencsitool.com/opencsitool/rest/v1/oauth2/authorization/gitcode?redirect=%2FmyTools
POST web-api.gitcode.com/uc/api/v1/oauth/checkOrAuthorize
       multipart: client_id, state, redirect_uri, response_type
GET  <响应中的 redirect_uri>            # opencsitool callback
```

### 边界：这只覆盖"续期"，不覆盖"首次授权"

必须明确区分，否则会把结论用错：

- **已有授权时**，`checkOrAuthorize` **直接返回带 `code` 的 `redirect_uri`**。这是本次实测的路径，
  也是静默续期走的路径。**续期可以完全无浏览器。**
- **尚无授权时**，同一接口返回 `401`，SPA 转而加载同意页并需要用户点击。
  提交同意的接口是 `POST /uc/api/v1/oauth/authorize`（§3 表第 6 项），
  **本次调查只从 bundle 读到它，从未调用**。
- 因此：**"续期无浏览器"为真；"首次授权被自动化"为假，且不应被自动化。**

### 与既有文档的冲突（需要修订）

`docs/gitcode-qr-protocol.md` §9.1 判定"第二段是 browser-bound，原因是 `/oauth/authorize`
是客户端渲染的 SPA 外壳"，并据此写下"把浏览器完全去掉在第二段被阻断"。

**该判定应被推翻，其推理有一步跳跃**：SPA 外壳只证明"外壳本身不是服务端渲染的授权结果"，
**不证明**"SPA 背后的后端接口无法被非 JS 客户端调用"。§9.1 的探针用 `urllib` **跟随重定向**，
于是永远停在外壳上；它从未尝试调用外壳**之后**的那一个接口。本次调查把那一层补上了，
流程即打通。这不是"SPA 因此变成可行的"，而是"原来的实验少做了一步"。

---

## 6. Browser-bound evidence

**No —— 没有发现任何浏览器专属的强制机制。**

对主 bundle 与 authorize chunk 逐项检查，全部为 0 命中：

| 机制 | 命中数 |
| --- | --- |
| `crypto.subtle` / `SubtleCrypto` / `generateKey` / `importKey` | 0 |
| `navigator.credentials` / `PublicKeyCredential` / WebAuthn | 0 |
| `attestation` / `Attestation` | 0 |
| `deviceId` / `device_id` / `hwid` / `machineId` | 0 |
| `challenge` | 1（无关：字符串常量，非挑战-应答） |

`X-Device-ID` 命中 1 次，值为**硬编码字符串 `"unknown"`**：

```js
{"X-App-Version":0,"X-Platform":yg()?"mobile_web":"web","X-Device-Type":Db(),
 "X-App-Channel":w?"gitcode_ai":"gitcode-fe","X-Network-Type":Lb(),
 "X-OS-Version":xb(),"X-Device-ID":"unknown"}
```

它既不是指纹，也不被服务端强制校验——§4.5 的纯 HTTP 客户端**完全没有发送这些头**，
流程依然成功。

`X-Source` 是埋点来源标签（与既有 `docs/gitcode-qr-protocol.md` 的结论一致），非签名。

**"页面是 SPA" 本身不构成 browser-bound 证据**，本次调查正是这一点的实例：
外壳是 JS 渲染的，而外壳背后的接口是普通 multipart POST。

**唯一真正的物理约束**（不构成 browser-bound）：首次授权需要用户点击同意，
这是**人的决定**，不是浏览器的技术限制。

---

## 7. What remains unknown

1. **同意页分支的线上行为未验证。** 未构造"GitCode 已登录 + openCsiTool 无授权"状态，
   未调用 `POST /uc/api/v1/oauth/authorize`。该接口的**响应**（是 `302` 到 callback 还是
   `200` + JSON）只有 bundle 证据：从 `Ie` 用原生 `<form>.submit()` 推断是**导航式 `302`**，
   但这是**推断，不是观测**。
2. **`reauth` 事务流程未验证。** `POST /uc/api/v1/oauth/reauth-transactions/{id}/authorization-codes`
   的存在、路径形状与字段来自 bundle（`Af`），未触发。
3. **`401` 之后 `checkOrAuthorize` 的重试语义未知**：是否 `401` 一律意味着需要同意页，
   还是会先触发 token 刷新，未测。
4. **服务端是否会在 cookie 失效后仍接受 `checkOrAuthorize`** 未知。本次用的
   `GITCODE_ACCESS_TOKEN` 有效期内（180 天）未做过期实验。
5. **代理故障的归属未知**：只测得 `127.0.0.1:7890` 对 `opencsitool.com` 的 TLS 转发失败，
   未判断是该代理的规则、上游线路，还是 opencsitool.com 对该出口 IP 的拒绝。
   代理本身不在本次调查范围内。
6. **未验证 openCsiTool 是否对 `state` 做服务端校验。** 本次每次都原样回传入口给出的 `state`，
   未测试空值或伪造值——**也不建议测试**，那是安全性探测而非可行性探测。
7. **bundle hash 会继续轮换**，§3 的路径与字段名在 `index-a97e2b06` 与 `index-e12962d8`
   两个版本上一致，但不能保证下一个版本仍一致；实现方应把"接口形状"当作可失效的外部契约。
8. **`GITCODE_REFRESH_TOKEN` 的作用未单独定位。** 已知仅 `GITCODE_ACCESS_TOKEN` 即可完成流程，
   因此 refresh token 在**这条**路径上是冗余的；它在 GitCode 自身的续期中的作用未测。

---

## 附：对实现的直接含义

1. **静默续期可以去掉浏览器。** 三步 HTTP 序列，见 §5。前提是持有一个有效的
   `GITCODE_ACCESS_TOKEN`。
2. **浏览器仍然需要用于首次授权**（用户点击同意），以及用于获取最初的 GitCode 凭据。
   浏览器从"必需"降级为"仅首次 + 作为 cookie 来源"。
3. **若浏览器仍被使用，必须处理 §4.4 的代理问题**，否则续期在带该代理配置的机器上会直接失败，
   且失败信息（`ERR_CONNECTION_CLOSED`）看起来像服务端问题，极易误诊。推荐 `--no-proxy-server`。
4. **复现时必须做两层 URL 改写**（§3.1），否则请求打到不存在的路径。
5. **不要自动化同意接口。** 首次授权是账户持有者的决定。
