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
