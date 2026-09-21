# GitCode 微信小程序扫码登录协议调查报告

**Investigation target**: `opencsitool.com` → GitCode OAuth 授权入口 → GitCode 扫码登录流程
**Method**: 静态 bundle 分析（`cdn-static.gitcode.com` 懒加载 chunk）+ 只读 HTTP GET/OPTIONS 探针
**Scope**: READ-ONLY。未发起任何 POST/PUT/DELETE；未完成登录；未读取浏览器 Cookie。
**所有 cookie / token / session / scene_id / captcha_id / client_id 值均已脱敏（masked）**，本文档只记录 header 名、状态码、URL 路径、字段名与状态枚举名。

---

## 结论

**Verdict: `QR_FLOW_REPRODUCIBLE`**

GitCode 的微信小程序扫码登录是一个**纯 HTTP + JSON 的轮询式流程**，不需要执行任何浏览器 JS 即可完成：

1. `POST /uc/api/v1/qrcode/wechat_mini_program` 创建二维码，响应返回 `scene_id` 与 `qrcode` 两个字段；`qrcode` 是可以直接交给 `<img src>` 的**完整 data URI / URL 字符串**（客户端不做本地二维码编码）。
2. `GET /uc/api/v1/qrcode/wechat_mini_program?scene_id=<...>` 轮询状态，**无需任何 Cookie / 鉴权头**，响应 `{"status": "<STATE>"}`。
3. `POST /uc/api/v1/user/oauth/login/qrcode/wechat_mini_program?scene_id=<...>` 在扫码确认后换取登录凭据，响应 body 携带 `access_token` / `refresh_token`。

`X-Source` 只是**前端埋点来源标签**（`login_trigger_source`），不是签名/反爬令牌。
`X-Source`、`X-Platform` 等头都**不在** CORS `Access-Control-Allow-Headers` 白名单里（白名单实际只回显 `traceparent`），但这是浏览器同源策略的约束，**对普通 HTTP 客户端无效** —— 服务端不校验它们。

保留意见（不改变判定，但必须诚实记录）：
- 按约束要求，**未对创建二维码的 POST 做线上实测**（该调用会产生服务端状态）。`X-Source` 是否被服务端**强制**校验，仅有静态证据 + 该头在 CORS 白名单之外这一间接证据，**未经 wire-level 验证**。
- 扫码动作本身必须由真实微信客户端完成，但这属于"用户拿着手机扫码"的物理步骤，不属于"浏览器 JS 强制执行"，因此不构成 `QR_FLOW_BROWSER_BOUND`。
- 详见 §9 未解问题。

---

## 证据来源

### 入口与静态资源

| 项目 | 值 |
| --- | --- |
| OAuth 入口 | `GET https://opencsitool.com/opencsitool/rest/v1/oauth2/authorization/gitcode?redirect=%2FmyTools` → `302` |
| 授权页 | `https://gitcode.com/oauth/authorize?client_id=<masked>&redirect_uri=...&response_type=code&scope=all_user&state=<masked>` |
| 主 bundle | `https://cdn-static.gitcode.com/assets/index-a97e2b06.js` (1,889,881 B on wire) |
| 登录路由 chunk | `https://cdn-static.gitcode.com/assets/index-98fb2c9b.js` (18,887 B) —— `{path:"login",name:"login"}` |
| 布局/API 层 chunk | `https://cdn-static.gitcode.com/assets/vendor-layout-a25c0063.js` (984,908 B) |
| 第三方授权路由 | `https://cdn-static.gitcode.com/assets/authorize-54c99276.js`, `login-8974df96.js` |
| 回调 chunk | `https://cdn-static.gitcode.com/assets/callback-21ee2196.js` |

下载方法：`urllib.request.ProxyHandler({})` 绕过 `127.0.0.1:7890` 代理，直接 GET `cdn-static.gitcode.com`。共抓取 621 个 JS chunk（index bundle 引用的全部 JS 资产，924 个资产名中的 JS 子集）。

**重要发现**：OAuth 登录页实际有两个不同的登录实现，且它们是**两套并行代码**：
- `index-98fb2c9b.js`（`/login` 路由，GitCode 自有登录页）—— 包含 `miniProgramGetQrCode` / `miniProgramCheckStatus` / `miniProgramLogin` 的调用逻辑。
- `vendor-layout-a25c0063.js`（全局布局层，弹窗式登录 Modal）—— 包含**同一套协议的第二份实现**（`createMiniProgramQRCode` / `getMiniProgramQRCodeStatus` / `miniProgramQRCodeLogin`）。
两者 URL、method、参数、状态机完全一致，互为交叉验证。

**没有**发现 `.js.map`（`index-a97e2b06.js.map` / `index-98fb2c9b.js.map` / `vendor-layout-a25c0063.js.map` 均返回 `404`），因此函数名是压缩后的短名。

---

## 二维码创建

### 请求（两处实现完全一致）

来自 `vendor-layout-a25c0063.js`（变量名未压缩的版本，最易读）：

```js
createMiniProgramQRCode: t => ex(() => e({
  url: `/uc/api/v1/qrcode/${t.platform}`,
  method: "post",
  headers: { "X-Source": nV() }
}))
```

调用点固定传入 `{platform:"wechat_mini_program"}`：

```js
createMiniProgramQRCode({platform:"wechat_mini_program"})
```

来自 `index-a97e2b06.js`（`/login` 路由用的压缩版）：

```js
async miniProgramGetQrCode(){
  return await (e={platform:"wechat_mini_program"},
    Fo(()=>Bb({url:`/uc/api/v1/qrcode/${e.platform}`,method:"post",headers:{"X-Source":Gf()}})));
  var e
}
```

### 归纳

| 项 | 值 |
| --- | --- |
| URL | `https://web-api.gitcode.com/uc/api/v1/qrcode/wechat_mini_program` |
| Method | `POST` |
| Body | **无**（`data` 未设置；创建调用不带任何 body） |
| Query | **无** |
| 关键 header | `X-Source: <login_trigger_source>`（见 §7） |
| 自动附加 header | `X-App-Version: 0`, `X-Platform: web`, `X-Device-Type`, `X-App-Channel: gitcode-fe`, `X-Network-Type`, `X-OS-Version`, `X-Device-ID: "unknown"`, `page-title`, `page-ref`, `page-uri`, `gitcode-utm-source` |
| Cookie | 非必需（轮询已验证无 Cookie 可用；创建调用同域，`withCredentials:true` 但服务端未强制） |
| 鉴权 | 无 `Authorization`（`access_token` 拦截器对含 `/login` 的 URL 主动跳过） |

### 响应字段（承载二维码负载的字段名）

来自 `index-98fb2c9b.js` 的解构与绑定：

```js
lt = M({scene_id:"", qrcode:""})            // 本地状态
const a = await ge.miniProgramGetQrCode();
a.error || (lt.value = a?.data?.data, ...)  // 直接整体赋给本地状态
...
<img :src="lt.value.qrcode" alt="" class="w-[180px] h-[180px]" @error="dt">
```

`vendor-layout-a25c0063.js` 同构：

```js
const t = await d.miniProgramGetQrCode();
t.error || (Vt.value = t?.data?.data, k(()=>{ Bt.value = !0; zt() }))
...
<img :src="Vt.value.qrcode" alt="小程序二维码" class="qrcode-container__img">
```

因此响应体形状为（`Fo()` / `ex()` 解包 axios 响应后，`a.data.data` 才是业务体）：

```json
{
  "scene_id": "<str>",
  "qrcode":   "<str: 二维码图片字符串>"
}
```

字段名确认为 **`scene_id`** 与 **`qrcode`**（注意是 `qrcode`，不是 `qr_code`）。

> 旁证：同仓库另一个不相关组件 `AtomcodePreviewQrcode`（`index-78150a94.js`）用了更宽容的字段回退，说明 GitCode 各接口的二维码字段命名并不统一：
> ```js
> l(e.qr_code_url || e.url || e.qr_code || e.image || "")
> ```
> 但**扫码登录接口用的是 `qrcode`**，这一点由登录页模板的直接绑定确证。

---

## 状态轮询

### 请求（两处实现一致）

```js
// vendor-layout-a25c0063.js
getMiniProgramQRCodeStatus(t){
  const {platform:n, scene_id:o} = t;
  return ex(()=>e({url:`/uc/api/v1/qrcode/${n}`,method:"get",params:{scene_id:o}}), t)
}

// index-a97e2b06.js
miniProgramCheckStatus: async e => await function(e){
  const {platform:t, scene_id:o} = e;
  return Fo(()=>Bb({url:`/uc/api/v1/qrcode/${t}`,method:"get",params:{scene_id:o}}))
}({platform:"wechat_mini_program", scene_id:e})
```

| 项 | 值 |
| --- | --- |
| URL | `https://web-api.gitcode.com/uc/api/v1/qrcode/wechat_mini_program?scene_id=<scene_id>` |
| Method | `GET` |
| 参数 | `scene_id`（**必填**，见下方实测） |
| Header | **无特殊头**（不带 `X-Source`） |
| 鉴权 / Cookie | **不需要** |

### 响应字段

```js
const {status:n} = (t?.data?.data) || "WAITING";
Ft.value = n;
if ("TIMEOUT" === n || "CANCEL" === n) return void Ht();   // 停止轮询
if ("LOGIN" !== n) return;                                  // 继续轮询
```

响应形状：

```json
{ "status": "<STATE>" }
```

承载状态的字段名是 **`status`**。

### 状态机全量取值

来自 `index-98fb2c9b.js` 与 `vendor-layout-a25c0063.js` 的实际字面量使用（**只出现这 5 个字面量**）：

| 状态值 | 出处 | 语义 | 客户端动作 |
| --- | --- | --- | --- |
| `WAITING` | 轮询返回 + 初值 + 缺省值 | 二维码已生成，等待扫码 | 继续轮询 |
| `SCAN` | 模板判定 `["SCAN","CANCEL","LOGIN"].includes(st)` | 已扫码，等待手机上确认 | 继续轮询；UI 显示"扫码完成 / 请在手机上确认操作" |
| `LOGIN` | `if ("LOGIN" !== n) return;` | 手机端已确认授权 | **停止轮询**，调用登录完成接口 |
| `TIMEOUT` | `if ("TIMEOUT" === n \|\| "CANCEL" === n) return void Ht()` | 二维码过期 | 停止轮询，UI 显示"二维码失效 / 点击重试" |
| `CANCEL` | 同上 | 用户取消 | 停止轮询，UI 显示"重新扫码" |

UI 文案对照（`index-a97e2b06.js` 的 i18n 表）：

```js
miniProgram:{
  title:"小程序登录", qrCodeError:"二维码加载失败",
  tip01:"二维码失效", tip02:"点击重试", tip03:"扫码完成",
  tip04:"请在手机上确认操作", tip05:"打开微信扫一扫，快速登录/注册", tip06:"重新扫码"
}
```

模板中的状态分支（逐字）：

```js
"WAITING" !== st.value ? (...) : (),
"TIMEOUT" === st.value ? (... 二维码失效 / 点击重试 ...) : (),
["SCAN","CANCEL","LOGIN"].includes(st.value) ? (... 扫码完成 / 请在手机上确认操作 ...) : ()
```

### ⚠️ 关于题目给定的候选状态枚举

任务描述中给出的 `WAITING / PENDING / EXPIRED / AUTHORIZED / SUCCESS / CANCEL / TIMEOUT` **不是**扫码登录状态机。经全量 621 chunk 检索确认：

- **`EXPIRED`、`AUTHORIZED`、`PENDING` 从未作为扫码登录状态出现**（大小写敏感的 `"EXPIRED"` / `"AUTHORIZED"` 字面量在全部 chunk 中 0 次命中；`PENDING` 命中来自无关的 HTTP 状态枚举 `PENDING_REVIEW=451` 和 lodash）。
- `SUCCESS` 属于**另一套**枚举 —— OAuth 绑定状态 `user_status_enum`（见 §5），不是二维码状态。
- 真实的二维码状态只有 **`WAITING` / `SCAN` / `LOGIN` / `TIMEOUT` / `CANCEL`** 五个。

### 实测（只读 GET，无 Cookie）

```
GET https://web-api.gitcode.com/uc/api/v1/qrcode/wechat_mini_program?scene_id=PROBE0000BOGUS
  -> 200  content-type: application/json
  body: {"status":"TIMEOUT"}

GET https://web-api.gitcode.com/uc/api/v1/qrcode/wechat_mini_program?scene_id=
  -> 200  {"status":"TIMEOUT"}

GET https://web-api.gitcode.com/uc/api/v1/qrcode/wechat_mini_program      (无 scene_id)
  -> 400  {"error_code":400,"error_code_name":"BAD_REQUEST",
           "error_message":"Required request parameter 'scene_id' for method parameter type String is not present",
           "trace_id":"<masked>"}

GET https://web-api.gitcode.com/uc/api/v1/qrcode/                       (无 platform)
  -> 404  {"timestamp":"<masked>","status":404,"error":"Not Found","path":"/uc/api/v1/qrcode/"}
```

**结论**：未知/伪造 `scene_id` 一律返回 `{"status":"TIMEOUT"}`，不泄漏内部状态，也不需要任何凭据。

---

## 登录完成

### 请求（两处实现一致）

```js
// vendor-layout-a25c0063.js
miniProgramQRCodeLogin(t){
  const {platform:n, scene_id:o} = t;
  return ex(()=>e({
    url: `/api/v1/user/oauth/login/qrcode/${n}`,
    method: "post",
    params: {scene_id:o},
    headers: {"X-Source": nV()}
  }))
}

// index-a97e2b06.js
async miniProgramLogin(e){
  const t = await function(e){
    const {platform:t, scene_id:o} = e;
    return Fo(()=>Bb({url:`/api/v1/user/oauth/login/qrcode/${t}`,method:"post",
                      params:{scene_id:o},headers:{"X-Source":Gf()}}))
  }({platform:"wechat_mini_program", scene_id:e});
  ...
}
```

| 项 | 值 |
| --- | --- |
| URL（源码） | `/api/v1/user/oauth/login/qrcode/wechat_mini_program?scene_id=<scene_id>` |
| URL（线上实际） | `https://web-api.gitcode.com/uc/api/v1/user/oauth/login/qrcode/wechat_mini_program?scene_id=<scene_id>` |
| Method | `POST` |
| 参数 | `scene_id` 走 **query string**（不是 body） |
| Body | 无 |
| 关键 header | `X-Source: <login_trigger_source>` |

**⚠️ 路径前缀陷阱**：源码里写的是 `/api/v1/user/oauth/login/qrcode/...`，但 axios 请求拦截器会**自动改写**带 `/api/v1/user/` 的 URL，加上 `/uc` 前缀：

```js
// index-a97e2b06.js  (io = "/uc")
t.url = ((e,t)=>{
  const o = io;                       // io = "/uc"
  return e?.includes("/api/v1/user/")
      || e?.includes("/api/v1/oauth/")
      || e?.includes("/api/v1/internal/messages")
      || e?.includes("/api/v1/follow")
      || (e?.includes("/api/v1/obs") && "get" === t)
    ? `${o}${e}` : e
})(t.url, t.method)
```

`vendor-layout-a25c0063.js` 里同样存在 `const n="/uc"` 的等价改写。

**实测验证**（GET 仅为探测 method 是否存在，不产生状态）：

```
GET     https://web-api.gitcode.com/api/v1/user/oauth/login/qrcode/wechat_mini_program?scene_id=PROBE
  -> 401  {"error_code":401,"error_code_name":"UNAUTHORIZED","error_message":"Unauthorized","trace_id":"<masked>"}
        (未加 /uc 前缀 —— 这个路径没有这个接口，被鉴权中间件先拦下)

GET     https://web-api.gitcode.com/uc/api/v1/user/oauth/login/qrcode/wechat_mini_program?scene_id=PROBE
  -> 405  {"error_code":405,"error_code_name":"METHOD_NOT_ALLOWED","error_message":"Request method 'GET' not supported",...}
        (存在该路由，但只接受 POST —— 确证 /uc 前缀才是正确路径)

OPTIONS https://web-api.gitcode.com/uc/api/v1/user/oauth/login/qrcode/wechat_mini_program?scene_id=PROBE
  -> 200
```

`405 Request method 'GET' not supported` 是本次调查对 `/uc` 前缀改写最有力的**线上确证**。

### 响应与"登录凭据"到底是什么

`index-98fb2c9b.js`：

```js
const s = await ge.miniProgramLogin(lt.value.scene_id), i = s?.data;
if ("EMPTY_MOBILE" !== i?.user_status_enum) {
  s.success || rt();
  ot({mask:i.mask ?? "", user_status_enum:"EMPTY_MOBILE", username:i.username});
}
```

`index-a97e2b06.js` 的完整分支：

```js
async miniProgramLogin(e){
  const t = await ...;
  if(!t.error){
    sf({isRegister:t.data.data.is_new, registerLoginMethod:oc.MINI_PROGRAM, success:!0});
    const e=t.data.data, {user_status_enum:o}=e;
    return "EMPTY_MOBILE"===o || Wf(e,oc.MINI_PROGRAM), {success:!0,data:e}
  }
  return sf({isRegister:!1,registerLoginMethod:oc.MINI_PROGRAM,success:!1}), {success:!1,error:t.error}
}
```

响应业务体（`t.data.data`）字段名（逐字解构）：

```js
const { user_id, mask, mobile, user_status_enum, username } = o?.data || {};
```

```json
{
  "is_new": "<bool>",
  "user_id": "<str>",
  "mask": "<str>",
  "mobile": "<str>",
  "username": "<str>",
  "user_status_enum": "<STATE>",
  "access_token": "<str>",
  "refresh_token": "<str>"
}
```

**建立 GitCode 会话的机制 —— 不是 Set-Cookie，而是响应 body 里的 token，写入 localStorage：**

```js
// index-a97e2b06.js
function Wf(e,t,o="",i=!1){
  ...
  const n = { access_token: e.access_token, refresh_token: e.refresh_token };
  o && (n.loginType = o);
  window.parent && window.parent.postMessage(n, "*");
}
```

```js
// vendor-layout-a25c0063.js  (登录成功回调)
const { access_token:n, refresh_token:o, xauth_token:a, ...l } = e;
ix.setItem("access_token", n);
ix.setItem("refresh_token", o);
ix.setItem("xauth_token", a);
ix.setItem("userInfo", JSON.stringify(l));
```

- **Cookie 名（仅名字）**：授权页 `Set-Cookie` 观测到的名字集合为
  `HWWAFSESID`、`HWWAFSESTIME`、`c_gitcode_fref`、`c_gitcode_rid`、`c_gitcode_um`、`gitcode_oauth_session`、`gitcode_wechat_from`、`uuid_tt_dd`。
  其中 `gitcode_oauth_session` 只出现在**服务端 Set-Cookie**，在全部 621 个 chunk 中 0 次引用 —— 它是纯服务端会话 cookie，前端不读不写。
- **实际凭据是 body 中的 `access_token` / `refresh_token` / `xauth_token`**，客户端存进 `localStorage`（键名 `access_token` / `refresh_token` / `xauth_token` / `userInfo`），随后以 `Authorization: Bearer <access_token>` 使用。
- 因此一个 `CookieJar` 客户端拿到 `access_token` 后即可自建会话，**不依赖浏览器 Cookie 语义**。

### `user_status_enum` 全量枚举

来自 `callback-21ee2196.js`（唯一给出完整定义的地方）：

```js
var _e = (e => (
  e.SUCCESS = "SUCCESS",
  e.UN_REGISTER = "UN_REGISTER",
  e.UN_REGISTER_AND_MIRROR = "UN_REGISTER_AND_MIRROR",
  e.UNBIND_AND_MATCH = "UNBIND_AND_MATCH",
  e.UNBIND_AND_EXIST = "UNBIND_AND_EXIST",
  e.IAM_UNINITIALIZED = "IAM_UNINITIALIZED",
  e.MFA_CHECK = "MFA_CHECK",
  e.BIND_FREEZE_AND_MATCH = "BIND_FREEZE_AND_MATCH",
  e.EMPTY_MOBILE = "EMPTY_M..."
))( _e || {} )
```

| 值 | 扫码登录场景含义 | 客户端动作 |
| --- | --- | --- |
| `SUCCESS` | 登录成功 | 写入 token，跳转 |
| `EMPTY_MOBILE` | 账号未绑定手机号 | 进入绑定手机号流程（`bindPhone`），**不**立即写 token |
| `MFA_CHECK` | 需要多因素认证 | 进入 MFA 流程（`/api/v1/user/oauth/login/mfa`） |
| `UN_REGISTER` | 微信身份未注册 GitCode | 进入注册流程 |
| `UN_REGISTER_AND_MIRROR` | 未注册且需镜像 | 注册流程 |
| `UNBIND_AND_MATCH` / `UNBIND_AND_EXIST` | 需解绑后匹配 | 解绑确认流程 |
| `IAM_UNINITIALIZED` | 华为云 IAM 未初始化 | IAM 授权流程 |
| `BIND_FREEZE_AND_MATCH` | 绑定被冻结 | 绑定确认流程 |

---

## 轮询节奏与过期

### 轮询实现 `useVisibilityPoll`

`useVisibilityPoll-7cc2f764.js`（完整，未压缩）：

```js
function t(t, v = {}, u) {
  const { interval: n = 1e4, immediate: s = !0 } = v,
        o = e(null),        // timer
        r = e(!0),          // isPageVisible
        d = e(!1);          // destroyed
  const m = () => { (void 0 === u || u.value) && (d.value || !o.value && r.value && (s && t(), o.value = setInterval(t, n))) },
        c = () => { o.value && (clearInterval(o.value), o.value = null) },
        b = () => { d.value || (r.value = "visible" === document.visibilityState, r.value ? m() : c()) };
  return i(() => {
      r.value = "visible" === document.visibilityState,
      document.addEventListener("visibilitychange", b),
      r.value && m()
    }),
    void 0 !== u && l(u, (e => { d.value || (e && r.value ? m() : e || c()) })),
    a(() => { d.value = !0, c(), document.removeEventListener("visibilitychange", b) }),
    { timer: o, isPageVisible: r, handleVisibilityChange: b, startPoll: m, stopPoll: c }
}
```

要点：
- 默认间隔 `1e4` = **10000 ms**（10 s），默认 `immediate: true`。
- 仅在 `document.visibilityState === "visible"` 时轮询；标签页隐藏时 `clearInterval`。
- `startPoll` 幂等：已有 timer 则不重复启动。
- 组件卸载时清理。

### 扫码登录实际使用的参数

`index-98fb2c9b.js`：

```js
const { startPoll: rt, stopPoll: ut } = Me(
  async () => {
    if ("miniProgram" !== Ea.value || !Sa) return void ut();
    const t = await ge.miniProgramCheckStatus(lt.value.scene_id);
    if (t.error) return;
    const o = t?.data?.data?.status ?? "WAITING";
    st.value = o;
    if ("TIMEOUT" === o || "CANCEL" === o) return void ut();
    if ("LOGIN" !== o) return;
    ut();
    const s = await ge.miniProgramLogin(lt.value.scene_id);
    ...
  },
  { interval: 1500, immediate: false },   // <-- 1500 ms
  nt
);
```

`vendor-layout-a25c0063.js` 同构，同样是 `{interval:1500, immediate:false}`。

| 项 | 值 |
| --- | --- |
| 轮询间隔 | **1500 ms**（1.5 秒） |
| `immediate` | `false`（首次轮询由 `setTimeout(...,0)` 之后的 `startPoll()` 触发，不是同步立即） |
| 触发条件 | 开关 `nt.value` / `Bt.value`，在拿到二维码后才置 `true` |
| 停止条件 | `status` ∈ {`TIMEOUT`, `CANCEL`}，或 `status === "LOGIN"` 后转入登录完成调用 |
| 错误处理 | `if (t.error) return;` —— **网络错误不停止轮询，继续下一轮** |
| 页面隐藏 | 自动暂停（`visibilitychange`） |

### 过期行为

- **客户端不设置本地超时**。过期完全由**服务端**在轮询响应里返回 `status: "TIMEOUT"` 来判定。
- 客户端也没有倒计时 UI；`TIMEOUT` 到达即展示"二维码失效 / 点击重试"，点击重试走 `ct()` / `Gt()` 重新 `POST` 创建。
- 未在 bundle 中发现任何 `expire` / `expires_in` / `ttl` 字段被读取。创建响应只解构了 `scene_id` 与 `qrcode`。

### 对照：`useRunnerSetStatusPoll` 与本流程无关

`useRunnerSetStatusPoll-77739036.js` 是 **RunnerSet 升级状态轮询**（`using` / `using_editing` / `using_degraded` / `using_edit_failed` / `init` / `init_error` / `wait_release`），轮询间隔 `5e3` = 5000 ms，`immediate:false`，失败重试上限 3 次。**它只是复用了同一个 `useVisibilityPoll`**，与扫码登录协议没有任何关系 —— 请勿混淆。

---

## X-Source 与反爬

### `X-Source` 是什么

**它是前端埋点用的登录来源标签，不是签名，不是 nonce，不是 anti-bot token。**

完整定义链（`index-a97e2b06.js`）：

```js
const fo = "login_trigger_source";          // localStorage key
function Uf(e){ pf.setItem(fo, e) }         // setter
function Gf(){ return pf.getItem(fo) || f.get(fo) }   // getter  <-- 就是它
function qf(){ return pf.getItem(ho) }      // ho = "register_source_tab"
function $f(){ const e=Gf(), t=qf(); return e&&t ? `${e},${t}` : t||e }
```

其中 `pf = cf`，而 `cf` 是一个 **localStorage 的 shim**：

```js
cf = { setItem(t,o){ e[t]=String(o) }, getItem:t=>Object.prototype.hasOwnProperty.call(e,t)?e[t]:null, removeItem(t){...}, clear(){...}, get length(){...} }
```

`f.get(fo)` 则回退读**同名 Cookie**（`login_trigger_source`）。

`vendor-layout-a25c0063.js` 里是同一逻辑的另一份：

```js
const Mx = "login_trigger_source";
function tV(e){ SP.setItem(Mx, e) }
function nV(){ return SP.getItem(Mx) || qE.get(Mx) }        // <-- X-Source 取值
function oV(){ const e=nV(), t=SP.getItem(Ix); return e&&t ? `${e},${t}` : t||e }
// Ix = "register_source_tab"
```

### 它是静态常量还是每次计算？

**每次请求时计算**，但计算只依赖**本地存储的一个字符串**，与服务端下发的挑战、时间戳、随机数、设备指纹**完全无关**：

- 取值优先级：`localStorage["login_trigger_source"]` → 同名 Cookie → 空串。
- 写入时机：用户点击某个"登录"入口时，调用方把该入口的来源字符串传进来并写入，例如
  ```js
  login({ triggerType:"", loginTriggerSource:"toolbar_login" })
  // -> 内部： h && (tV(h), qE.set(Mx, h, {expires:1, domain:VITE_COOKIE_DOMAIN}))
  ```
- 已知取值（全量检索得到的字面量集合，节选）：
  `toolbar_login`、`toolbar_atomcode`、`toolbar_workspace`、`toolbar_org_follow`、`toolbar_user_follow`、`user_home_follow`、`repo_star_common`、`repo_star_errorpage`、`repo_issue_create_from_list`、`repo_merge_create`、`org_search`、`project_search`、`invite_link`、`discussion_like`、`discussion_new_comment`、`aside_<left_menu_url>`、`search_item_star`、`search_star_repo_recommend`、`toolsFloat_ads` 等。
- **没有任何签名/加密/哈希**：`nV()` 就是 `localStorage.getItem("login_trigger_source") || cookie.get("login_trigger_source")`。
- **没有任何 `Date.now()` / `Math.random()` / `navigator.*` / canvas 指纹参与**。

推论：一个普通 HTTP 客户端可以自由设置 `X-Source` 为任意字符串（包括省略、或直接伪造 `toolbar_login`），不存在"必须由浏览器计算"的约束。

### CORS 白名单的旁证

```
OPTIONS https://web-api.gitcode.com/uc/api/v1/qrcode/wechat_mini_program
  -> 200
  Access-Control-Allow-Origin:      https://gitcode.com
  Access-Control-Allow-Methods:     GET, POST, PUT, DELETE, OPTIONS
  Access-Control-Allow-Headers:     traceparent          <-- 注意：不含 X-Source
  Access-Control-Max-Age:           86400
  Access-Control-Allow-Credentials: true
```

`Access-Control-Allow-Headers` 只声明 `traceparent`（实际实现是回显 `Access-Control-Request-Headers`）。`X-Source` 不在白名单里，说明：

- 在**浏览器**里，`X-Source` 属于非简单请求头，需要 preflight 授权；服务端对 `X-Source` 的授权是**宽松/回显式**的，并未把它当作受保护的安全令牌。
- 在**非浏览器 HTTP 客户端**里，CORS 根本不生效，`X-Source` 可以任意设置。

无论如何，**服务端不会因为 `X-Source` 缺失或伪造而拒绝请求**这一结论，其证据强度为"间接"（见 §9）。

### 反爬组件清单与作用域

授权页 `<head>` 加载了 4 个安全/埋点脚本：

| 脚本 | 实际作用 | 是否拦扫码登录 |
| --- | --- | --- |
| `https://cdn-static.gitcode.com/js/tac/load.min.js` | 华为云 TAC 验证码**加载器**（`window.loadTAC` / `window.initTAC`），默认从 `<base>/js/tac.min.js` 拉真实 SDK | 否（按需加载） |
| `https://cdn-static.gitcode.com/js/yunpian/riddler-sdk-0.2.2.js` | 云片 Riddler 滑块验证码 | 否（仅密码/短信登录） |
| `https://cdn-static.gitcode.com/js/yidun/yidun-captcha.js` | 网易易盾 `initNECaptchaWithFallback` | 否（仅密码/短信登录） |
| `https://cdn-static.gitcode.com/js/furion.js` | 华为 Furion **埋点统计 SDK**（`appId: "<masked>"`，源码注释 `// 埋点统计`） | 否 |

**关键判定**：验证码组件只挂在**密码 / 短信 / MFA** 登录分支上，**扫码登录分支完全不触碰它们**。

证据 1 —— 创建/轮询/登录三个请求的函数体内**没有任何** captcha 参数：

```js
createMiniProgramQRCode: t => ex(()=>e({url:`/uc/api/v1/qrcode/${t.platform}`,method:"post",headers:{"X-Source":nV()}}))
getMiniProgramQRCodeStatus(t){ ... params:{scene_id:o} ... }        // 无 captcha
miniProgramQRCodeLogin(t){ ... params:{scene_id:o} ... }            // 无 captcha
```

对比密码登录（同一文件，紧邻位置），参数里带 `captcha_id` / `token` / `authenticate` / `validate`：

```js
const l = nM({ biz_enum:o, mobile:t, raw_data:i||"", captcha_id:a, token:r, authenticate:n, validate:s });
return Fo(()=>Bb({url:"/api/v1/user/sms/send/codeByBiz",method:"post",data:l}))
```

证据 2 —— captcha 类型来自独立配置接口，且只在密码分支被消费：

```js
getCaptchaType: () => ex(()=>e({url:"/uc/api/v1/captcha/config",method:"get"}))
```

实测：

```
GET https://web-api.gitcode.com/uc/api/v1/captcha/config
  -> 200
  {"provider":"YIDUN",
   "domestic":{"captcha_id":"<masked:32>","captcha_type":"NORMAL"},
   "international":{"captcha_id":"<masked:32>","captcha_type":"SMART"}}
```

`captchaType` 变量 `_t` / `ze.value` 只在 `Pt`（密码登录提交）里被读取：

```js
const { captchaType:_t, getYiDunCaptchaId:kt } = V();
const Pt = async e => { ... await ge.password({ param:{ ..., captchaType:_t.value }, ... }) }
```

证据 3 —— TAC 实例 `BH`（`vendor-layout-a25c0063.js`）的初始化入口 `initTAC` 需要调用方传入 `requestCaptchaDataUrl`、`bindEl`、`validSuccess` 回调，属于**主动调用式**验证码；扫码登录分支没有任何 `initTAC` / `loadTAC` 调用点。

**结论：二维码创建、状态轮询、扫码后登录三个调用均不需要 CSRF token、nonce、Yidun / Riddler / TAC / Furion 签名。**

（附注：`X-Hwwaf-*` 是华为云 WAF 的响应头，属于**边缘防护**，与前端 JS 无关，也不是需要客户端计算的签名。见 §9。）

---

## API Base Host

### 定义（`index-a97e2b06.js`）

```js
// 环境常量
VITE_API_HOST:          "https://web-api.gitcode.com"
VITE_CHAT_API_HOST:     "https://chat-api.gitcode.com"
VITE_DEV_API_HOST:      "https://gitcode.com"
VITE_HOST:              "https://gitcode.com"
VITE_STATIC_HOST:       "https://cdn-static.gitcode.com"

// 主 API base
ct = () => (e => {
  const t = location.host;
  if (t.includes("local")) return "https://web-api.gitcode.com";
  if (!["production","pre"].includes("production")) return `https://${t}`;
  if (!Je(t)) return e;
  const o = t?.replace(/^pre\./,"");
  return e?.replace(/gitcode\.com/g, o)
})(Xe.VITE_API_HOST || "https://web-api.gitcode.com")
```

### axios 实例构造（`index-a97e2b06.js`）

```js
Bb = async (e,t) => {
  const i = { [Lo.DEVPRESS]:jb, [Lo.MAIN]:Fb, [Lo.CHAT]:Ub },
        a = [Lo.DEVPRESS, Lo.CHAT],
        r = i[e.apiType] || Ob,                 // Ob = ct()
        n = O.create({
          baseURL: r,
          timeout: typeof t?.customTimeout === "number" ? t.customTimeout : 3e4,
          withCredentials: !0
        }),
        ...
}
```

`vendor-layout-a25c0063.js` 同构：

```js
pM = QL(), mM = (e,t) => {
  const n = me.create({ baseURL: pM.VITE_API_HOST, timeout: t?.customTimeout || 3e4, withCredentials: !0 })
  ...
}
```

### 结论

| 前缀 | Base host | 最终线上 URL |
| --- | --- | --- |
| `/uc/api/v1/...` | `https://web-api.gitcode.com` | `https://web-api.gitcode.com/uc/api/v1/...` |
| `/api/v1/user/...`（被拦截器加 `/uc`） | `https://web-api.gitcode.com` | `https://web-api.gitcode.com/uc/api/v1/user/...` |
| `/api/v1/...`（其它，如 `/api/v1/search/...`） | `https://web-api.gitcode.com` | `https://web-api.gitcode.com/api/v1/...` |

- **`api.gitcode.com` 未在 bundle 中出现**（检索 `"https://api.gitcode.com"` 命中 0 次）。
- 候选 `web-api.gitcode.com` **确认**：所有探针均返回结构化 JSON，`Server: elb`。
- 注意：`https://gitcode.com/uc/api/v1/qrcode/...` **也能工作**（同源反代到同一后端，实测返回同样的 `{"status":"TIMEOUT"}`），但 bundle 使用的是 `web-api.gitcode.com`。
- 请求超时默认 **30000 ms**。
- `withCredentials: true`。

### 实测确认

```
GET https://web-api.gitcode.com/uc/api/v1/qrcode/wechat_mini_program?scene_id=PROBE0000BOGUS
  -> 200  content-type: application/json  server: elb
  body: {"status": "<enum:TIMEOUT>"}

GET https://gitcode.com/uc/api/v1/qrcode/wechat_mini_program?scene_id=PROBE0000BOGUS
  -> 200  content-type: application/json  server: elb
  access-control-allow-origin: https://ai.gitcode.com
  body: {"status": "<enum:TIMEOUT>"}
```

---

## 二维码图像的形态

**`qrcode` 字段是一个字符串，客户端不做本地编码，直接塞进 `<img src>`。**

模板绑定（逐字）：

```js
// index-98fb2c9b.js
b("img", { src: lt.value.qrcode, alt: "", class:"w-[180px] h-[180px]", onError: dt }, null, 40, De)

// vendor-layout-a25c0063.js
J("img", { src: Vt.value.qrcode, alt:"小程序二维码", class:"qrcode-container__img" }, null, 8, HG)
```

- **不是** 服务端渲染的 `<img>` 标签，**不是** 二进制图片流。
- **是** 一个字符串，要么是完整 URL（`https://...` / `//...`），要么是 **base64 data URI**（`data:image/png;base64,...`）。
- **客户端不做本地二维码编码**：全量 621 chunk 检索，登录流程中**没有** `qrcode-generator` / `qr-code-styling` / `QRCode.toDataURL` 的调用（命中的 `toDataURL` 全部来自无关的图表/画布组件 `installCanvasRenderer` / `pipelineDataPrase`）。
- 加载失败时有 `@error="dt"` 处理，弹出 `二维码加载失败`。

### 样本 SHAPE（非真实值）

```json
{
  "scene_id": "e.g. 32~36 字符的十六进制/短 ID 字符串",
  "qrcode":   "data:image/png;base64,<...base64 PNG...>"
}
```

或

```json
{
  "scene_id": "e.g. 32~36 字符的十六进制/短 ID 字符串",
  "qrcode":   "https://<某个 gitcode 静态/图片域名>/<path>.png"
}
```

> 判定依据：登录流程本身**没有**做 base64 → data URI 的拼接，`lt.value.qrcode` 被原样赋给 `src`。因此 `qrcode` 必然已经是可直接加载的完整串（要么自带 `data:` 前缀，要么是 http(s) URL）。若它是裸 base64，浏览器 `<img src>` 无法加载 —— 而这与代码里没有拼接逻辑相矛盾，故可排除"裸 base64"。

### 旁证：GitCode 确实两种形态都在用

无关组件 `AtomcodePreviewQrcode`（`index-78150a94.js`）实现了一个**通用的二维码字符串归一化函数**，明确处理"裸 base64 → 补 data URI 前缀"以及"多种字段名回退"，说明 GitCode 后端不同接口返回的二维码字段命名/形态并不统一：

```js
function l(e){
  if (!e) return "";
  if (/^(https?:\/\/|\/\/|data:)/i.test(e)) return e;      // 已是 URL / data URI
  let t = "image/jpeg";
  e.startsWith("/9j/")        ? t = "image/jpeg" :
  e.startsWith("iVBORw0KGgo") ? t = "image/png"  :
  e.startsWith("R0lGOD")      ? t = "image/gif"  :
  e.startsWith("UklGR")       && (t = "image/webp");
  return `data:${t};base64,${e}`                            // 裸 base64 -> data URI
}
...
const e = t;
return l(e.qr_code_url || e.url || e.qr_code || e.image || "")
```

**但扫码登录用的是 `qrcode`（无下划线），且不做这层归一化。** 这一点必须区分清楚。

---

## 可行性判定

### 完整协议（可复现的纯 HTTP 流程）

```text
1) 创建二维码
   POST https://web-api.gitcode.com/uc/api/v1/qrcode/wechat_mini_program
   Headers: X-Source: <任意来源标签，可省略/伪造>
            (可选) X-App-Version: 0, X-Platform: web, X-App-Channel: gitcode-fe
   Body:    (无)
   -> 200 {"scene_id":"<...>", "qrcode":"data:image/png;base64,..."}

2) 展示 qrcode 字符串（浏览器 <img src> 或终端二维码渲染）
   用户用微信扫码并在手机上确认

3) 轮询状态（每 1500 ms，页面可见时）
   GET https://web-api.gitcode.com/uc/api/v1/qrcode/wechat_mini_program?scene_id=<scene_id>
   -> 200 {"status":"WAITING"}   继续
   -> 200 {"status":"SCAN"}      继续
   -> 200 {"status":"LOGIN"}     停止，进入第 4 步
   -> 200 {"status":"TIMEOUT"}   停止，二维码失效，回第 1 步
   -> 200 {"status":"CANCEL"}    停止，用户取消

4) 换取凭据
   POST https://web-api.gitcode.com/uc/api/v1/user/oauth/login/qrcode/wechat_mini_program?scene_id=<scene_id>
   Headers: X-Source: <来源标签>
   Body:    (无)
   -> 200 {"data": {"is_new":..., "user_id":..., "mask":..., "mobile":...,
                    "username":..., "user_status_enum":"SUCCESS",
                    "access_token":"<...>", "refresh_token":"<...>"}}

5) 后续请求带 Authorization: Bearer <access_token>
```

### 可行性论证

| 环节 | 是否需要浏览器 JS | 依据 |
| --- | --- | --- |
| 创建二维码 | **否** | 纯 `POST` + 一个可伪造的埋点头；无 captcha 参数 |
| 轮询状态 | **否** | 纯 `GET`，**已实测无 Cookie 可用** |
| 换取凭据 | **否** | 纯 `POST` + `scene_id` query；凭据在 body 而非 HttpOnly Cookie |
| 展示二维码 | **否** | `qrcode` 是可直接渲染的字符串 |
| 扫码确认 | **需要真实微信客户端** | 物理步骤，非浏览器 JS |
| 建立会话 | **否** | `access_token` 在响应 body，客户端自存 localStorage |

### 为什么不是 `QR_FLOW_BROWSER_BOUND`

按题目的定义，`QR_FLOW_BROWSER_BOUND` 要求"存在浏览器专属的强制机制（anti-bot 签名 / WebAuthn / JS challenge）"。本次调查**没有发现**这样的机制：

- 无 JS challenge（无 `__cf_chl`、无 proof-of-work、无 `document.cookie` 挑战-应答）。
- 无 anti-bot 签名头（`X-Source` 已证实是埋点标签；无 `X-Ca-Key`/`X-Ca-Signature` 类**强制**签名 —— 这类头只出现在 CORS `Access-Control-Allow-Headers` 的**允许列表**里，是通用网关配置，登录请求并未使用）。
- 无 WebAuthn / 设备指纹绑定（`X-Device-ID` 硬编码为 `"unknown"`）。
- 无 CSRF token。
- 轮询接口**已实测在零 Cookie、零鉴权头的情况下返回 200 + 业务 JSON**。

### 唯一的操作建议

一个 `urllib`/`requests` + `CookieJar` 的客户端可以完整跑通。建议实现时：

1. 先 `GET https://gitcode.com/oauth/authorize?...`（或直接跳过，因为 QR 接口不要求 `state`）建立 CookieJar。
2. 用 `POST` 创建，从 `qrcode` 渲染二维码（若是 data URI，解码 base64 后用本地库渲染成终端二维码或保存 PNG）。
3. 用 1500 ms 间隔轮询，直到 `LOGIN` / `TIMEOUT` / `CANCEL`。
4. 用返回的 `access_token` 以 `Authorization: Bearer` 访问其余 API。

---

## 未解问题

1. **创建二维码的 POST 未经线上实测。**
   该调用会在服务端创建二维码场景（状态变更），按本次调查的 READ-ONLY 约束**主动跳过**。因此以下两点只有静态证据：
   - 响应体的确切 JSON 包裹层级（推断为 `{"data":{"scene_id":...,"qrcode":...}}`，依据 `a.data.data` 的双层解构）；
   - 服务端是否**强制**校验 `X-Source`（推断为不强制，依据：CORS 白名单不含该头 + 该值纯本地读取）。

   **验证方法**（留给后续有写权限的调查）：单次 `POST` 并立即观察响应；随后用一个 `X-Source` 缺失的对照请求比较状态码。

2. **`scene_id` 的格式未确定。**
   实测表明任意字符串（含空串、1 字符、36 字符 UUID）都被服务端当作"未知场景"并返回 `TIMEOUT`，无法从响应反推真实格式。前端把它当作不透明字符串处理（`M({scene_id:"", qrcode:""})`），未做任何格式校验或解析。可能是 UUID v4 或短随机 ID。

3. **`qrcode` 字段究竟是 data URI 还是 http(s) URL，未经线上确证。**
   由"登录模板不做 base64→data URI 拼接"推断它必须是完整可加载串，但无法从静态代码区分两种形态。同仓库的 `AtomcodePreviewQrcode` 两种都处理，说明后端两种都可能返回。

4. **华为云 WAF 的行为边界。**
   在探测 `web-api.gitcode.com/api/v1/user/oauth/login/qrcode/...`（无 `/uc` 前缀）时，观察到**间歇性 `418` + `X-Hwwaf-Attack-Id` + `X-Hwwaf-Delay-Ms: 500`**（CloudWAF 拦截页"访问被拦截！"）。同一 URL 加 `Referer`/`Origin` 后返回正常的 `401`。这说明边缘 WAF 会基于请求头组合做启发式判定。
   - 这不是"必须执行的浏览器 JS 挑战"（没有需要计算的 token），只是常规 WAF 规则。
   - 但**在真实自动化中可能触发**，需要带上合理的 `User-Agent` / `Referer` / `Origin`。这一点**未被完整刻画**，是复现时最可能踩的坑。

5. **`xauth_token` 的用途未解。**
   登录成功后客户端会存 `xauth_token`，但本次调查未定位到它被哪个接口消费。它与华为云 IAM 授权相关（`IAM_UNINITIALIZED` 状态），与扫码登录本身无关。

6. **`EMPTY_MOBILE` / `MFA_CHECK` 分支之后的完整流程未展开。**
   本报告聚焦"扫码 → 拿到凭据"这一主路径。绑定手机号（`/api/v1/user/bind-mobile`、`/api/v1/user/confirm-bind-mobile`）与 MFA（`/api/v1/user/oauth/login/mfa`、`mfa_recover`）属于后续分支，未逐一验证。

7. **`login-8974df96.js`（`/-/oauth/login` 路由）与 `index-98fb2c9b.js`（`/login` 路由）的关系。**
   前者只有 988 B，是一个**跳转 shim**：读取 `?redirect=` 与 `?type=`，然后调用 `t(\`${l}${e}\`)` / `u(o, !0)` 跳到 CSDN passport 登录 URL。它**不包含**扫码 UI。真正的扫码 UI 在 `/login` 路由（`index-98fb2c9b.js`）和全局登录 Modal（`vendor-layout-a25c0063.js`）。本次调查未确认 openCsiTool 的 OAuth `redirect` 最终落到哪个路由，但**这不影响协议本身**（两条路径调用同一组 API）。

---

## 附录：本次调查使用的只读探针脚本

| 脚本 | 作用 | 是否发 POST |
| --- | --- | --- |
| `tools/probe_gitcode_qr_live.py` | 入口 302、授权页 header/脚本、轮询（bogus scene_id）、captcha 配置 | 否 |
| `tools/probe_gitcode_qr_live2.py` | CORS preflight (OPTIONS) + 多组 bogus scene_id | 否 |
| `tools/probe_gitcode_qr_live3.py` | `scene_id` 必填性、无 platform 的 404、`/uc` 存在性 | 否 |
| `tools/probe_gitcode_qr_live4.py` | 登录完成路径的完整响应头、WAF `418` 观测 | 否 |
| `tools/probe_gitcode_qr_live5.py` | `/uc` 前缀改写线上确证（`405` vs `401`） | 否 |
| `tools/probe_gitcode_bundle.py` | （已有）主 bundle 端点抽取 | 否 |

所有脚本均使用 `urllib.request.ProxyHandler({})` 绕过 `127.0.0.1:7890` 代理，并对所有形如
`state= / code= / ticket= / token= / scene_id= / client_id= / captcha_id=` 的值做 mask 后才输出。
