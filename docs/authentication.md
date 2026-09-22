# 认证

本文说明 opencsi 如何取得会话凭据，以及围绕凭据的安全约束。

---

## 结论先行

站点**只认 HttpOnly Cookie**，不认 `Authorization` 头。

| 请求 | 服务端响应 |
| --- | --- |
| 带有效 `token` Cookie | `200` |
| 不带 Cookie | `401 empty Authorization` |
| 带任意 `Bearer …` 头 | `401 Invalid Authorization` |

注意这两种 401 的**含义不同**：

- `empty Authorization` → **缺少 Cookie**（没登录）
- `Invalid Authorization` → **发了一个错误的头**

本工具永远不发送 `Authorization`，所以如果看到第二种，说明有代码 bug，
`client.py` 会把它报成 `BadAuthHeaderError` 而不是"会话过期"。

---

## 凭据的来源

`CredentialProvider` 协议只有一个核心问题：**"Cookie 值是什么？"**

```
CredentialProvider (Protocol)
├── get_token() -> str | None      返回 Cookie 值；源存在但不可用时抛异常
├── invalidate() -> None           丢弃缓存，下次重新读取
├── refresh() -> str | None        强制重新读取，绕过 TTL
└── status() -> CredentialStatus   脱敏状态，供 doctor 显示
```

两个实现：

| 实现 | 用途 | `invalidate()` 语义 |
| --- | --- | --- |
| `CdpCookieProvider` | 从运行中的浏览器读 | **只丢缓存**，源仍可读 → 重试有意义 |
| `ManualCookieProvider` | 手工输入 / 测试 | **永久清除**，没有源可以重读 |

这个差异是刻意的，而且有测试断言。它直接决定了 401 重试是否有效：
CDP 的 `invalidate()` 之后再 `get_token()` 会**重新去浏览器读一次**，
而浏览器的 Cookie 可能已经被站点刷新过了 —— 所以重试有真实成功的机会。

---

## 从浏览器读取 Cookie 的流程

```
discover_cdp_endpoint()
  │
  ├─ 1. --cdp URL              （显式指定，最高优先级）
  ├─ 2. $OPENCSI_CDP_URL       （环境变量）
  ├─ 3. 端口扫描 9222/9223/9224 （探测 /json/version）
  └─ 4. DevToolsActivePort 标记文件（Chrome 147+ 的关键）
        │
        ▼
   _read_cookies()
        │
        ├─ 策略 A：页面级 WebSocket
        │    /json/list → 找 type=page 的目标 → 连它的 webSocketDebuggerUrl
        │    → Network.getCookies
        │
        └─ 策略 B：浏览器级 WebSocket（A 失败时）
             browser_ws_url() → Storage.getCookies
             → 若为空：Target.getTargets → Target.attachToTarget(flatten)
               → 在 session 上 Network.getCookies
        │
        ▼
   select_token_cookie(cookies)
        │
        └─ 在 opencsitool.com 域上找 name == "token" 的 Cookie
           （优先未过期的；只有过期的也返回，让服务端给出准确的 401）
```

### `DevToolsActivePort` 标记文件

Chrome 在用户配置目录里写一个 `DevToolsActivePort`，内容是两行：

```
9222
/devtools/browser/1a2b3c4d-...
```

第一行是端口，第二行是浏览器级 WebSocket 的路径。

**为什么需要它**：Chrome 147+ 在默认配置上不提供 `/json/*`，
所以拿不到 `webSocketDebuggerUrl`。这个标记文件是唯一能恢复出
WebSocket 路径的途径。

读取的配置目录（按平台）：

| 平台 | 路径 |
| --- | --- |
| Windows | `%LOCALAPPDATA%\Google\Chrome\User Data`、`…\Microsoft\Edge\User Data`、`…\BraveSoftware\Brave-Browser\User Data` |
| macOS | `~/Library/Application Support/Google/Chrome` 等 |
| Linux | `$XDG_CONFIG_HOME/google-chrome` 等 |

---

## Chrome 147+ 的问题

这是实际部署时**最常遇到**的障碍。

### 现象

- `9222` 端口是开着的
- `DevToolsActivePort` 存在，而且不是陈旧的
- 但 `/json/version` 返回 **404**
- 浏览器级 WebSocket 握手被**直接拒绝**

### 原因

如果你是通过 `chrome://inspect` 的 "Allow remote debugging" 在
**默认用户配置**上打开调试的，Chrome 147+ 会限制 `/json/*` 接口
和浏览器级 WebSocket。这是安全设计。

### 解决

用一个**专用配置目录**重新启动浏览器：

```powershell
chrome.exe --remote-debugging-port=9222 `
           --user-data-dir="$env:LOCALAPPDATA\opencsi-cdp-profile" `
           https://opencsitool.com/myTools
```

用 `--user-data-dir` 显式指定端口启动时，Chrome 会正常提供 `/json/*`。

> **注意**：专用目录是全新的，**没有登录状态**。需要在新窗口里登录一次。

### 本工具如何报告

`_upgrade_hint()` 会生成包含完整命令的提示，并且 `doctor` 会打印它
（输出到 stdout，所以重定向到文件也不会丢）。

---

## 安全约束

### 1. 绝不发送 `Authorization`

`transport.py` 的 `_headers()` 只构造 `Cookie`、`Accept`、`User-Agent`。

有测试**直接断言实际构造出的 header 字典**里没有 `authorization` 键 ——
而不是去 grep 源码（那样会被一行注释骗过）。

### 2. Cookie 不进入任何输出通道

| 通道 | 防护 |
| --- | --- |
| `repr()` / `str()` | `Secret` 包装、`repr=False` 字段 |
| 日志 | `RedactingFilter` 在 handler 上 |
| 异常消息 | `OpenCsiError.__init__` 里调 `scrub_text()` |
| traceback | 日志过滤器清洗格式化后的文本 |
| JSON 输出 | `to_json()` 丢 `_` 前缀字段 + `redact_mapping()` |
| `CredentialStatus` | **结构上**没有能装 token 的字段 |
| 命令行 | 没有任何命令接受 `--token` |

### 3. 命令行不接受密钥

```bash
opencsi login --token SECRET   # ❌ argparse 直接拒绝
opencsi login --manual         # ✅ getpass() 从 stdin 读
```

**为什么**：argv 对同机器上所有用户可见（`ps aux`），
而且会写进 shell 历史（`.bash_history` / PSReadLine）。
一条测试遍历所有子命令的所有选项，断言不存在 `--token` / `--cookie` /
`--secret` / `--password` / `--key` / `--auth`。

### 4. 脱敏正则

`redaction.py` 覆盖这些形态：

- `Cookie: token=…` / `Set-Cookie: token=…`
- `Authorization: Bearer …`
- URL 里的 `?token=…`
- `"virtualKey": "sk-…"`
- `sk-` 前缀的长串
- JWT（`eyJ….eyJ….…`）
- 任何 ≥48 字符的不透明串

另外维护一个**注册表**：读到真实 Cookie 时会 `register_secret(value)`，
之后这个值在任何文本里都会被替换成 `<redacted>`。
注册表有上限（防止内存无限增长）并会淘汰最旧的条目。

> 短于 8 字符的值不会被注册 —— 否则注册一个 `"a"` 会把所有文本里的
> 每个 `a` 都变成 `<redacted>`。

### 5. 密钥掩码规则

与站点一致，只显示前 10 个字符：

```python
f"{key[:10]}****"     # sk-bM4LUSm****
```

原始密钥保存在 `ToolGrant._virtual_key`，是 `repr=False` 的私有字段，
**不出现在 `repr`、`str` 或 JSON 里**。唯一的出口是 `virtual_key_masked`。

---

## 会话生命周期

| 项目 | 值 |
| --- | --- |
| Cookie 名 | `token` |
| 域 | `opencsitool.com` |
| HttpOnly | 是 |
| 有效期 | 约 **0.97 小时**（约 58 分钟） |
| CSRF token | **无** |
| 速率限制头 | **无** |
| Origin 校验 | **无** |

### 过期处理

1. `CdpCookieProvider` 在 Cookie 剩余寿命 < **30 秒**时主动重新读取
2. 请求收到 `401` → `invalidate()` → 重新读取 → **重试一次**
3. 再失败 → `SessionExpiredError`，退出码 **13**

401 响应会带 `Set-Cookie: token=; Expires=Thu, 01 Jan 1970`，
但本工具**不使用**这个来清除自己的缓存 —— 它以浏览器为准，
而不是以某个响应头为准。

---

## 会话生命周期管理（`SessionManager`）

上面第 2 步的"重新读取"是**凭据重载**，不是**会话续期**。这两件事必须分开，
因为它们的成本和成功率完全不同：重载只是再读一次浏览器里的 Cookie，
而续期要重跑一次 OAuth。

### 三种语义

| 概念 | 做什么 | 要用户参与 | 成功率 |
| --- | --- | --- | --- |
| **凭据重载** `credential reload` | 重新读浏览器 Cookie | 否 | 只要浏览器还在就必成 |
| **会话续期** `session renewal` | 后台标签页重跑 GitCode OAuth，签发新 `token` | 否 | 取决于 GitCode SSO 会话是否还在 |
| **交互登录** `interactive login` | 用户本人操作（扫码 / 输密码） | **是** | —— |

把这三件事混为一谈是原始设计的问题所在：`token` 只有约 58 分钟寿命，
而原来的 `CdpCookieProvider.refresh()` **只是重载**。浏览器里的 Cookie 一旦
真的过期，重载多少次都没用，只会得到同一个死 Cookie。

### 架构

```
                     ┌──────────────────────────────┐
                     │        SessionManager        │
                     │  needs_renewal()             │
                     │  ensure_valid()              │
                     │  renew(force=False)          │
                     │  reload_then_renew()         │
                     │  login()                     │
                     └───────┬──────────┬───────────┘
                             │          │
              ┌──────────────┘          └───────────────┐
              ▼                                         ▼
   ┌────────────────────┐                  ┌────────────────────────┐
   │ CredentialProvider │                  │    SessionRenewer      │
   │  (协议)            │                  │     (协议)             │
   │                    │                  │                        │
   │ get_credential()   │                  │ renew(before=None)     │
   │ invalidate()       │                  │  -> RenewalResult      │
   │ describe()         │                  └───────────┬────────────┘
   └─────────┬──────────┘                              │
             │                                         │
   ┌─────────▼──────────┐                  ┌───────────▼────────────┐
   │ CdpCookieProvider  │                  │ BrowserOAuthRenewer    │
   │  从 CDP 读 Cookie  │                  │  后台标签页跑 OAuth    │
   └────────────────────┘                  └────────────────────────┘

   另一条路（无需浏览器）：
   ┌────────────────────────────┐
   │ InteractiveAuthenticator   │  GitCodeQrAuthenticator
   │   login(...)               │  纯 HTTP 轮询，微信扫码
   └────────────────────────────┘
```

### 续期触发策略

刻意保守，避免任何形式的循环：

| 条件 | 动作 |
| --- | --- |
| `expires_in > 5 分钟` | **什么都不做** → `ALREADY_VALID` |
| `expires_in <= 5 分钟` | 静默续期 |
| API 返回 `401` | 先**重载** Cookie，再尝试静默续期 |

`renew()` 内部最多重试一次，**不会无限循环**。续期失败也不会让一个仍然
可用的会话变成错误：如果续期没成功但当前 Cookie 还能用，命令正常返回 ——
那是"这次没续上"，不是"命令失败了"。

### 续期成功的判据

**不是** "页面加载完了"。`Page.loadEventFired` 只说明导航发生，
不说明认证成功。真正的判据是：

- 旧 token ≠ 新 token，**且**
- 新过期时间 > 旧过期时间，**且**
- 服务端确实接受了它（`getUserInfo` → `200`）

冷启动（本来就没有 Cookie）是一个特例：此时没有"旧 token"可比，
所以用**第三个信号** —— `credential_appeared`（从"无凭据"变成"有凭据"）。
缺了它，一个全新的 3598 秒 token 会被误判成"什么都没发生"。

超时是另一个特例：如果续期**超过了截止时间但成功了**，报告 `TIMEOUT` 是错的。
所以 `_evaluate()` 接收 `timed_out` 标志，最终**以 Cookie jar 的实际状态为准**；
只有确实超时且**没有**拿到 token 时才报 `TIMEOUT`。

### 浏览器是凭据存储，不是认证步骤

这一节解释一个容易看反的设计，也是"浏览器无法被移除"这个说法最后的落点。

**认证不需要浏览器**（见上）。但**凭据存放**需要它 —— 而这是两回事。

本项目的硬约束是**凭据不落盘**：`token` 只存内存、不写配置文件、不写日志。
那么 `opencsi usage` 作为**另一个进程**启动时，从哪里拿到凭据？
答案一直是浏览器：`CdpCookieProvider` 从运行中的 Chrome/Edge 读 cookie。
这就是为什么 `opencsi usage` 能在没有共享状态的前提下工作。

纯 HTTP 续期打破了这一点，而且方式很隐蔽：它在**内存里**铸造了一个真实会话，
但那个浏览器**从来不知道**这个新 cookie 存在。于是：

```text
opencsi login --renew   ->  RENEWED        （进程 A 内存里有 token）
opencsi usage           ->  没有 token     （进程 B 去问浏览器，浏览器没有）
```

一次**成功的续期**，报成了失败。这是"把部分成功描述成完整成功"的镜像：
这里是**把完整成功描述成失败**，同样是报告与事实不符。

**修法只能是浏览器，不能是文件。** 把 cookie 写到磁盘会直接违反上面的硬约束，
而且会把一个活的凭据放到项目承诺过绝不放的地方。

所以续期成功后，`HttpOAuthRenewer._install_into_browser()` 会把这个 cookie
通过 CDP 写回浏览器（`CdpCookieProvider.install_token()`）。写之前先验证了
`Storage.setCookies` 在这个 Chrome 版本上确实可用 —— 用
`tools/probe_cookie_write.py`（写一个合成标记再删掉），而不是假设它可用。

几个刻意的边界：

- **只写一个 cookie**：`token`，域 `opencsitool.com`。不碰任何其他 cookie，
  也不删除任何东西。
- **保持 `HttpOnly` / `Secure`**：被替换的那个 cookie 有这两个标志，
  一次悄悄降级它们的安全属性的"修复"是安全回归。
- **写入失败不算续期失败**。续期**确实**成功了，只有持久化这一步失败，
  两者混为一谈会把一次可用的续期报成错误。失败记录在 `last_persisted` 上，
  且 `None`（该 provider 没有写入能力）与 `False`（尝试了但失败）是**不同的事实**。
- **`expires_in` 传的是相对秒数**，转成绝对时间由 provider 做。CDP 要的是绝对
  epoch；传相对值会把过期时间设到 1970 年，cookie 立刻被丢弃 —— 那看起来
  和"写入被拒绝"一模一样。

### 静默续期不抢焦点

续期用 `Target.createTarget` 开一个**后台**目标，完成后 `Target.closeTarget` 关掉。
**绝不导航用户当前正在看的页面** —— 那会在用户阅读时把页面换掉。

### 为什么保留两条路

`CdpCookieProvider` + `BrowserOAuthRenewer` 在 `login --qr` 可用之后**仍然不删除**，
但保留的**理由变了**，这一点值得写清楚，因为旧理由已经站不住：

~~理由：openCsiTool 的 `token` 由它自己的 OAuth 回调签发，那一步需要浏览器会话。~~

**这个理由是错的。** OAuth 回调那一步是纯 HTTP：`checkOrAuthorize` 返回授权码，
`GET` 回调即签发 `token` cookie。`getUserInfo` 会接受它，全程无需浏览器引擎。
详见 [`oauth-spa-investigation.md`](oauth-spa-investigation.md) 与
[`gitcode-qr-protocol.md`](gitcode-qr-protocol.md) §9.1 的错因分析。

现在的理由有两条，都是具体的：

1. **首次授权需要人点一次。** `checkOrAuthorize` 只在授权**已存在**时返回授权码；
   从未批准过的账号会拿到 `401`，SPA 随后加载批准页。此时报 `CONSENT_REQUIRED`，
   由浏览器把页面呈现给人 —— 批准第三方授权是用户的决定，不是本工具的决定。
   提交授权的接口（`POST /uc/api/v1/oauth/authorize`）在本项目中**从未被调用**。
2. **凭据只能从浏览器读到的环境。** 有些部署里 GitCode 会话只存在于某个浏览器 profile 中，
   `CdpCookieProvider` 是唯一能拿到它的途径。

默认顺序由 `FallbackRenewer` 决定，且**纯 HTTP 优先**：
HTTP 路径无人值守、不需要装浏览器；浏览器路径是它覆盖不到时的兜底。
`FallbackRenewer` 在 `CONSENT_REQUIRED` / `LOGIN_REQUIRED` 上**立即停止**，
不再尝试下一个 —— 这两种情况都需要人，换一个机制解决不了，
继续尝试只会拖慢一条用户本来就需要看到的消息。

### 自动续期（托盘常驻路径）

`opencsi login --renew` 是**手动**路径：它强制续期一次，用来验证机制本身。

用户真正依赖的是**自动**路径 —— 托盘自己按计划续期，没人去点它。
这是**两条不同的代码路径**，手动那条通过并不代表自动那条也对：

| 路径 | 入口 | 触发方式 |
| --- | --- | --- |
| 手动 | `opencsi login --renew` | `session.renew(force=True)`，一条直线 |
| **自动** | `MonitorService._maybe_renew` | 由 `needs_renewal(margin=...)` 把关，从计划 tick 进入 |

自动路径曾经坏过两次，而手动路径一直是绿的 —— 这正是"只测强制路径"的风险：

1. **续期成功后图标卡在 "Renewing session"。**
   `_tick_once` 在尝试续期后就提前返回，而 `_maybe_renew` 发布了 `RENEWING`
   却没发布结果。一个两秒就完成的续期，会让托盘显示"进行中"直到下一次
   计划刷新 —— 最多 5 分钟。

2. **`tick()` 根本不存在**，尽管类文档一直声称可以用它同步驱动。
   只有私有的 `_tick_once`。这不只是不整洁：它意味着自动路径**只能靠等真实定时器**
   才能被跑到，而这正是问题 1 得以存活的原因。现在 `tick()` 是公开的。

两个可复现的验证脚本：

```bash
# 证明自动路径会触发并成功（不需要等一小时）
python tools/probe_autonomous_renewal.py

# 跨真实过期观察（默认 70 分钟，观察若干次续期）
python tools/probe_renewal_soak.py --minutes 75 --renewals 2
```

`probe_autonomous_renewal.py` 构建**真实对象图**（真的 CDP provider、真的
OAuth renewer、真的 monitor），用真实调度驱动，**不强制**任何东西 ——
如果策略不决定续期，它就报失败。

实测结果：

```text
renewal margin set to   : 3629s (lifetime is 3569s)
state                   : OK
renewal status          : RENEWED
token changed           : True
lifetime after          : 3598s
server accepted it      : yes (shijingchang)
business data           : 3,634,063,175 tokens
```

---

## 为什么不自动启动浏览器

`CdpCookieProvider` 会连接一个**已经在运行**的调试端点；
如果找不到，它会抛出一个带操作指引的 `CdpUnavailableError`，
而**不会**自己启动浏览器。

**为什么**：

1. **不打扰用户** —— 自动弹窗会抢焦点、可能打断正在进行的操作
2. **没有意义** —— 新启动的浏览器（无论哪个配置目录）都不会有登录状态，
   所以自动启动**并不能**让认证成功，只会让用户困惑
3. **需要用户参与** —— 无论如何用户都得登录一次，那么由用户自己
   选择用哪个浏览器、哪个配置目录，比工具替他们决定更好

所以：**浏览器由用户启动，凭据由本工具读取。**
