# 排查手册

遇到问题先跑：

```bash
opencsi doctor
```

它会把整条链路逐项检查一遍，并且给出**具体**的修复建议。

---

## 快速对照表

| 症状 | 退出码 | 最可能的原因 | 跳到 |
| --- | --- | --- | --- |
| `WebSocket could not be used` | 10 | 默认配置目录开了调试端口 | [§1](#1-websocket-could-not-be-used) |
| `no openCsiTool 'token' cookie` | 12 | 那个浏览器没登录 | [§2](#2-no-opencsitool-token-cookie) |
| `no openCsiTool session credential` | 12 | 完全没有凭据 | [§2](#2-no-opencsitool-token-cookie) |
| `rejected the session cookie (HTTP 401)` | 13 | Cookie 过期 | [§3](#3-会话过期-401) |
| `permission denied` | 20 | 账号权限不足 | [§4](#4-permission-denied-403) |
| `unexpected HTTP` / 网络错误 | 30 | 网络或代理 | [§5](#5-网络问题) |
| `SSLEOFError` / `UNEXPECTED_EOF` | 30 | 本地代理劫持了连接 | [§5b](#5b-ssleoferror--unexpected_eof_while_reading) |
| `server error` | 31 | 服务端故障 | [§6](#6-服务端错误-5xx) |
| `code=…` 业务错误 | 32 | 业务层拒绝 | [§7](#7-业务错误-code--200) |
| 扫码协议错误 | 33 | GitCode 改了响应结构 | [§14](#14-扫码登录-login---qr) |
| 控制台乱码 | — | 终端编码 | [§8](#8-中文乱码) |
| 表格没对齐 | — | 终端字体宽度 | [§9](#9-表格对齐) |
| `pip install -e .` 失败 | — | 缺 setuptools | [§10](#10-安装问题) |
| 表格被截断 | — | 终端太窄 | [§11](#11-输出被截断) |
| `tray: unavailable` | 2 | 没装 `opencsi[tray]` | [§13](#13-托盘问题) |
| 扫码登录卡住 / 扫不出来 | — | 扫的是终端图，不是文件 | [§14](#14-扫码登录-login---qr) |
| 续期报 `LOGIN_REQUIRED` | 13 | GitCode SSO 也过期了 | [§15](#15-续期失败) |
| 托盘显示 `浏览器未运行` | 10 | 没有带调试端口的浏览器在跑 | [§16](#16-浏览器未运行) |
| 托盘图标不出现 | — | 被折叠进溢出区 | [§13](#13-托盘问题) |

---

## 1. `WebSocket could not be used`

```
[FAIL] credential: source=cdp, the DevTools endpoint at http://127.0.0.1:9222
       answered, but its WebSocket could not be used (browser socket: WebSocketError)
```

### 这是什么

端口开着，但 DevTools 的 WebSocket 握手被拒绝了。

### 原因

**Chrome 147 及以上**在**默认用户配置**上限制远程调试
（如果你是通过 `chrome://inspect` 打开的话）。`/json/*` 会返回 404，
浏览器级 WebSocket 会被拒绝。

### 怎么修

关掉那个浏览器，用**专用配置目录**重启：

```powershell
# Windows
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --user-data-dir="$env:LOCALAPPDATA\opencsi-cdp-profile" `
  https://opencsitool.com/myTools
```

```bash
# macOS
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.opencsi-cdp-profile" \
  https://opencsitool.com/myTools
```

```bash
# Linux
google-chrome --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.opencsi-cdp-profile" \
  https://opencsitool.com/myTools
```

然后在**那个新窗口里登录一次**（专用目录是全新的，没有登录状态）。

### 怎么确认修好了

```bash
opencsi doctor
```

应该看到 `[ok] credential: source=cdp, expires in …`。

### 临时替代方案

如果实在开不了调试端口：

```bash
opencsi login --manual
```

---

## 2. `no openCsiTool 'token' cookie`

```
[FAIL] credential: source=cdp, the browser holds 12 cookie(s) for other sites,
       but no openCsiTool 'token'
```

### 这是什么

DevTools 连接**是好的**（这是好消息 —— 说明浏览器准备没问题），
但那个浏览器的 Cookie 库里没有 openCsiTool 的登录凭据。

### 原因

在那个浏览器窗口里**没有登录** openCsiTool，或者登录已经过期很久、
Cookie 被清掉了。

### 怎么修

1. 找到那个浏览器窗口（就是带 `--remote-debugging-port` 的那个）
2. 打开 <https://opencsitool.com/myTools>
3. 登录
4. 再跑 `opencsi doctor`

### 怎么确认

```bash
opencsi status
```

应该看到 `Session valid : yes` 和你的用户名。

### 注意

如果你有多个浏览器 / 多个配置目录，很容易**登录在 A 浏览器、
调试端口开在 B 浏览器**。检查一下 `doctor` 报的端口是不是你以为的那个。

---

## 3. 会话过期 (401)

```
error: openCsiTool rejected the session cookie (HTTP 401)
```

### 这是什么

Cookie 存在，但服务端拒绝了它。

### 原因

Cookie 有效期只有约 **58 分钟**。过期了。

### 怎么修

在浏览器里**刷新一下页面**（如果站点会自动续期），或者重新登录。

本工具已经做了两件事来减少这个问题的发生：

- Cookie 剩余寿命 < 30 秒时**主动**重新读取
- 收到 401 时**自动重试一次**（重新读 Cookie 再请求）

所以看到这个错误，说明浏览器里的 Cookie 本身也已经无效了。

### 怎么确认

```bash
opencsi status
```

看 `Cookie lifetime left`。如果是 `-` 或 `expired`，就是这个问题。

---

## 4. `permission denied` (403)

```
error: permission denied: this account cannot perform that action
```

### 这是什么

认证**成功了**，但服务端拒绝了这次访问。

### 原因

当前账号没有权限访问该数据。对只读接口来说，这通常意味着：

- 你的账号角色（例如 `VISITOR` / 访客）不包含这项数据
- 或者这个数据对你不适用

### 怎么修

这不是工具的问题，也不是配置问题。用 `opencsi status` 确认一下
当前账号和角色：

```
Role view : 普通用户
Roles     : VISITOR
```

如果角色不对，需要联系管理员。**不要**尝试绕过 —— 本工具也不会
提供任何绕过手段。

---

## 5. 网络问题

```
error: connection refused / timed out
```

### 检查顺序

1. **能不能直接访问站点？**
   ```bash
   curl -I https://opencsitool.com
   ```

2. **是不是在公司代理后面？**
   Python 的 `urllib` 会读 `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY`：
   ```powershell
   $env:HTTPS_PROXY = "http://proxy.example.com:8080"
   opencsi status
   ```

3. **是不是被 TLS 拦截了？** 有些企业代理会替换证书。
   本工具默认校验 TLS，不会静默接受无效证书。

4. **加大超时**（默认 15 秒）：
   ```bash
   opencsi usage --timeout 30
   ```

### 关于本地地址

如果 `127.0.0.1:9222` 连不上，注意某些代理配置会**错误地代理本地地址**。
确保 `NO_PROXY` 包含 `127.0.0.1,localhost`。

---

## 5b. `SSLEOFError` / `UNEXPECTED_EOF_WHILE_READING`

```
error: SSLEOFError contacting https://opencsitool.com/... (via proxy http://127.0.0.1:7890)
       -> this request went through the proxy http://127.0.0.1:7890. If that
          proxy cannot reach opencsitool.com, retry with --no-proxy ...
```

### 这是什么

TLS 握手在读到任何数据之前就被对端关闭了。**看起来像服务端故障，
其实不是。**

### 原因：`urllib` 和 `curl` 读代理的地方不同

这是一个真实踩过的坑。在本项目的开发机上：

| 方式 | 结果 |
| --- | --- |
| `curl https://opencsitool.com/...` | `401`（正确） |
| `python -c "urllib.request.urlopen(...)"` | `SSLEOFError` |

原因：

- **`curl`** 只读 `http_proxy` / `https_proxy` **环境变量**
- **`urllib`** 除了环境变量，在 Windows 上还读**注册表**：
  ```
  HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings
  ```

那台机器上装了 `127.0.0.1:7890` 的本地代理（Clash / V2Ray 之类），
注册表里 `ProxyEnable=1`。`urllib` 于是把请求发给了这个代理，
而这个代理无法正确转发 `opencsitool.com`，握手被它切断。

`curl` 完全不知道这个代理的存在，所以它是好的 —— 这也是为什么
"curl 能通但 Python 不通"会让人非常困惑。

### 怎么修

```bash
opencsi status --no-proxy
```

`--no-proxy` 让本工具**完全不使用任何代理**，直接连服务器。

### 怎么确认是这个问题

```bash
python -c "import urllib.request; print(urllib.request.getproxies())"
```

如果输出了你并不想要的代理，就是它。

### 其他方案

```powershell
# 临时清空代理（只影响当前 PowerShell 会话）
$env:HTTPS_PROXY = ""
$env:HTTP_PROXY = ""
opencsi status
```

或者把域名加进绕过列表：

```powershell
$env:NO_PROXY = "opencsitool.com,127.0.0.1,localhost"
```

### 本工具做了什么

1. 报错时**明确指出走了哪个代理**（而不是只说 SSL 失败）
2. 提示里有 `--no-proxy` 这个具体动作
3. 代理 URL 里的 `user:password@` 会被**剥掉**，不会进日志或输出
4. `NO_PROXY` 里列出的主机不会被误报成"走了代理"

> **注意**：如果你**确实**需要走公司代理才能上网，那就**不要**用
> `--no-proxy`。这时应该修代理配置，或者确认代理允许访问该域名。

---

## 6. 服务端错误 (5xx)

```
error: server error (HTTP 502)
```

### 这是什么

服务端自己出了问题。

### 怎么修

本工具已经对 `502` / `503` / `504` 做了**有退避的自动重试**。
如果还是失败，说明服务端确实在故障中。

- 等几分钟再试
- 用浏览器打开站点，确认网页本身是否也不可用
- 如果网页正常但 CLI 失败，用 `opencsi -v usage` 看请求路径

---

## 7. 业务错误 (`code != 200`)

```
error: the API reported failure (code=500): 内部错误
```

### 这是什么

HTTP 状态码是 **200**，但响应体里的业务码不是 200。

**这一点很重要**：只看 HTTP 状态会把这类错误当成成功。
本工具会检查信封里的 `code` 字段，并把它报成错误（退出码 32）。

### 怎么修

看 `message` 字段里的服务端说明。这通常是服务端的数据问题，
不是客户端能解决的。

---

## 8. 中文乱码

### 现象

```
ʹ����  5593
```

（本来应该是 `使用中  5593`）

### 原因

Windows 控制台的默认代码页是 **GBK**（936），而本工具输出 UTF-8。

### 怎么修

```powershell
$env:PYTHONIOENCODING = "utf-8"
chcp 65001
opencsi tools
```

或者用 Windows Terminal（默认 UTF-8）。

### 关于"无法表示的字符"

如果服务端返回了一个当前编码**无法表示**的字符（例如备注里的 emoji），
CLI 会把它降级成 `?` 而**不会崩溃**。

这是 `cli/app.py` 里 `_make_output_robust()` 做的：它把 stdout/stderr
的错误处理器设成 `replace`。有测试覆盖这一行为。

### JSON 输出不受影响

```bash
opencsi tools --json > tools.json
```

`--json` 的输出始终是 UTF-8，重定向到文件后中文完全正常。

---

## 9. 表格对齐

### 现象

```
使用中  5593
已失效     1094
```

（第二列没对齐）

### 原因

终端字体把中文渲染成了**非等宽**，或者用了不支持 CJK 双宽的字体。

### 说明

本工具**已经**按显示宽度计算（`使用中` = 6 列，不是 3 列），
所以这不是工具的 bug。问题在终端字体。

### 怎么修

换一个等宽且 CJK 宽度正确的字体：

- **Windows**：Cascadia Mono、Sarasa Mono SC
- **macOS**：Menlo、Sarasa Mono SC
- **Linux**：Noto Sans Mono CJK、Sarasa Mono SC

如果只是想要数据、不关心排版：

```bash
opencsi tools --json
```

---

## 10. 安装问题

### `pip install -e .` 失败

```
error: invalid command 'bdist_wheel'
```

或者提示缺少 `setuptools`。

**原因**：你的 Python 环境没有 `setuptools`（Python 3.12+ 不再默认捆绑）。

**方案 A** —— 不需要安装，直接用：

```powershell
$env:PYTHONPATH = "D:\path\to\OpenCSIToolMonitor\src"
python -m opencsi usage
```

**方案 B** —— 用 `uv`（如果可用）：

```bash
uv pip install -e . --no-build-isolation
```

**方案 C** —— 装上 setuptools：

```bash
python -m pip install setuptools wheel
pip install -e .
```

### `No module named 'opencsi'`

`src` 目录没有加到 `PYTHONPATH`。注意是 `src` 目录，不是仓库根目录。

### 我到底需不需要装？

不需要。这个工具是**纯标准库**的，`PYTHONPATH=src python -m opencsi`
就能跑。安装只是为了让 `opencsi` 这个命令名可用。

---

## 11. 输出被截断

### 现象

表格右边的列看不见。

### 原因

终端窗口太窄。本工具**不做**截断 —— 是终端在折行或裁剪。

### 怎么修

拉宽窗口，或者：

```bash
# 只要 JSON，宽度无关
opencsi tools --json

# 或者重定向到文件
opencsi tools > tools.txt
```

### 关于 `logs`

`logs` 是**分页**的：

```bash
opencsi logs --page 1 --page-size 50
```

`--raw` 会显示每条记录的**全部**字段，列可能非常多。
这种情况下建议用 `--json`。

---

## 12. 其他问题

### `opencsi logs` 是空的

**这是正常的。**

```
note: an empty log is normal when usage is billed through a bundled tool
      rather than the LLM gateway.
```

走套餐计费（`FLAT`）的账号，网关日志本来就是空的。
这**不是**故障。

### `prices` 显示的条目比网页少

默认只显示**已启用**的。加 `--all`：

```bash
opencsi prices --all
```

### `usage --cost` 显示 `UNKNOWN`

某些条目（例如 `API_BUNDLE` 这种套餐）**不在价目表里**，
因为它们是打包计费的。

本工具会显示 `UNKNOWN` 而**不是**假装成本为 0 ——
因为 0 会被误读成"不花钱"。

### `usage` 的费用和实际账单不一致

**这是预期的。** 输出里也写了：

> estimated from the published price list; the server-side billing record is
> authoritative.

费用是用**公开价目表**估算的，实际账单以服务端为准。

### 日期筛选看起来没生效

`--start-date` / `--end-date` **只影响 `tokenTrend`**，
工具账号列表 `requestList` **始终返回全部**。这是服务端的行为。

另外，两个日期必须**同时提供**：

```bash
opencsi usage --start-date 2026-08-20 --end-date 2026-09-19   # ✅
opencsi usage --start-date 2026-08-20                          # ❌ 报错
```

### `contract-check` 失败

```
[FAIL] api contract: personalQueueStatus.data.tokenSummary: object present
```

**这说明站点改了接口。** 这正是这个命令存在的意义。

请：

1. 记录完整的 `opencsi contract-check --json` 输出
2. 重新做一次 API 调查（见 `docs/api-investigation.md`）
3. 更新 `models.py` 的映射和 fixture

### 数据看起来是旧的

看 `usage` 末尾的"Data freshness"：

```
Data fresh time (server) : 2026-09-19T21:40:27+08:00
ETL time                 : 2026-09-20T05:39:58+08:00
Fetched at (this run)    : 2026-09-19 15:45:31
```

- **Data fresh time** 是服务端数据的新鲜度
- **Fetched at** 是本次请求的时间

两者不同是正常的。如果 `Data fresh time` 本身很旧，那是服务端 ETL 的问题。

### 我怎么确认数据是"真的"

```bash
opencsi contract-check
```

它验证 11 项契约。全通过说明接口形状没变。

---

## 13. 托盘问题

### `tray: unavailable`（退出码 2）

托盘需要两个可选依赖：

```bash
pip install "opencsi[tray]"
```

先确认到底缺什么：

```bash
opencsi doctor        # 看 "tray support" 那一行
opencsi tray --check  # 只看托盘可用性，不发网络请求、不起线程
```

`--check` 是刻意不碰网络的：判断"能不能显示托盘"不该依赖 API 是否可达。

> 如果用的是镜像源，`pystray` 可能不在上面。加
> `--index-url https://pypi.org/simple`。

### 托盘图标不出现

Windows 11 **默认把新图标折叠进溢出区**（任务栏那个 `^`）。
点开它，把图标拖到任务栏上就固定住了。

图标确实没出现的话，按顺序检查：

```bash
opencsi tray --once      # 业务逻辑能不能跑通？
opencsi tray             # 前台启动，看有没有报错
```

如果 `--once` 正常但托盘不显示，问题在 UI 层；如果 `--once` 就失败，
那是认证或网络问题，见前面几节。

### 托盘会不会一直弹通知

不会。**只在状态"进入"需要你处理的那一刻弹一次**，不会每次轮询都弹。

具体规则：

| 状态 | 弹通知吗 |
| --- | --- |
| 进入 `Login required` / `Session expired` | **弹一次** |
| 一直停留在该状态（每 5 分钟轮询） | 不弹 |
| 恢复正常后再次失效 | **再弹一次** |
| `Offline`（网络问题） | 不弹 |
| `Server error` | 不弹 |

网络抖动**不通知**是刻意的：它通常会自己恢复，不值得打断你。
只有"需要你登录"才值得。如果你觉得通知太吵，可以在
**Windows 设置 → 系统 → 通知**里单独关掉 `OpenCSI Monitor`，
图标和 tooltip 仍然会正常显示状态。

### 图标一直显示 "Renewing session"

说明续期卡住了。正常情况下续期完成后状态会立刻回到 `OK`。

```bash
opencsi login --status    # 看凭据剩余寿命和续期可用性
opencsi login --renew     # 手动续一次，看具体报什么
```

### 开了两个托盘图标

默认是**单实例**的（命名互斥量）。如果你看到两个，说明其中之一是用
`--allow-multiple` 启动的。

### 每次开机都闪一个黑窗口

说明自启项写的是 `python.exe` 而不是 `pythonw.exe`，或者用的是命令行版 EXE。
重新注册一次即可：

```bash
opencsi tray --remove-startup
opencsi tray --install-startup
opencsi tray --startup-status    # 确认命令里是 pythonw 或 opencsi-tray.exe
```

托盘版 EXE 是 `--windowed` 构建的，不会有控制台窗口。

### 怎么彻底关掉自启

```bash
opencsi tray --remove-startup
```

也可以在**任务管理器 → 启动应用**里禁用 `OpenCSIToolMonitor` ——
读的是同一个 `HKCU` 注册表键，两种方式等效。

---

## 14. 扫码登录（`login --qr`）

### 扫不出来 —— 这是预期行为，不是 bug

**GitCode 返回的不是二维码，是微信小程序码。** 它的点阵比终端字符格还细
（430 px 图里最细只有 1 像素），所以**终端里画出来的那个图一定扫不出来**。

正确做法：扫**文件**。

```bash
opencsi login --qr
# 输出里会有一行：
# Open this file and scan it with WeChat: C:\Users\...\opencsi-login-code-xxxx.png
```

在屏幕上打开那个 PNG，再用微信"扫一扫"。

终端里的图只是**预览**，用来确认码已经加载出来了。

### 为什么不用浏览器也能登录，但还是要浏览器

这是最容易误解的一点，所以写清楚：

| 步骤 | 需要浏览器吗 |
| --- | --- |
| 拿到 GitCode 会话 | **不需要**（纯 HTTP 轮询） |
| 拿到 openCsiTool 的 `token` Cookie | **需要**（它由自己的 OAuth 回调签发） |

所以 `login --qr` 成功后，命令会明确告诉你还需要跑一次 `opencsi login`
（在已登录 GitCode 的浏览器里）。**一次扫码不能替代 openCsiTool 的 OAuth 回调。**

### 码过期了

登录码寿命只有几分钟。命令会自动换一个新的（最多一次），
超时后重新运行即可：

```bash
opencsi login --qr --qr-wait 300
```

### 登录码文件会堆积吗

不会。只保留最新的 3 个，旧的自动清理。
文件在 `%LOCALAPPDATA%\OpenCSI\login-code\`。

---

## 15. 续期失败

`opencsi login --renew` 会报告一个明确的结果，而不是含糊的失败：

| 结果 | 含义 | 怎么办 |
| --- | --- | --- |
| `RENEWED` | 换到了新 token | 无需操作 |
| `ALREADY_VALID` | 剩余寿命还够，没动 | 无需操作 |
| `LOGIN_REQUIRED` | GitCode SSO 也过期了 | 跑 `opencsi login`（要浏览器） |
| `CDP_UNAVAILABLE` | 连不上调试端点 | 见 [§16](#16-浏览器未运行) |
| `OAUTH_FAILED` | OAuth 流程被拒 | 先确认浏览器里 GitCode 还是登录态 |
| `TIMEOUT` | 超时且**没**拿到新 token | 重试；仍失败就跑 `opencsi login` |
| `UNSUPPORTED` | 当前 provider 不支持续期 | 用 `opencsi login` |

**关键区分**：续期失败**不等于**命令失败。如果当前 Cookie 仍然可用，
命令会正常返回 —— 那是"这次没续上"，不是"你不能用了"。

只有 `LOGIN_REQUIRED` 才真的需要你本人操作。

### 为什么静默续期有时会失败

它依赖浏览器里**仍然有效的 GitCode SSO 会话**。如果那个也过期了
（比如很久没开过浏览器、或者手动登出了 GitCode），就只能交互登录。
这是设计使然 —— 本工具不会替你保存 GitCode 的长期凭据。

---

## 16. 浏览器未运行

```
state: BROWSER_UNAVAILABLE
note: no Chrome/Edge DevTools endpoint found. Tried: 127.0.0.1:9222, ...
```

退出码 **10**。托盘上显示 `浏览器未运行`（黄色图标 + 缺口）。

### 这是什么

**持有你登录态的那个浏览器没有在运行**，或者它没有开调试端口。
这和"需要登录"是两件事：

| 状态 | 真正的问题 | 修复方式 |
| --- | --- | --- |
| `需要登录` | GitCode 登录态没了 | 重新认证 |
| `浏览器未运行` | 持有登录态的浏览器没跑 | **把浏览器启动起来** |

### 为什么会这样

最常见的是**刚开机**：Windows 启动了托盘，但 Chrome 还没有运行。
这不是故障，是顺序问题。

### 怎么修

**直接点托盘菜单的第一项 `启动浏览器并登录`**，或者：

```powershell
opencsi login
```

两者现在都会用**专用配置目录 + `--remote-debugging-port`** 启动 Chrome/Edge，
并把登录页开在那里。这个专用配置目录里通常还留着你的 GitCode SSO 登录态，
所以多数情况下**连扫码都不需要**，直接就恢复了。

> **这个坑曾经是个死循环。** 早先"浏览器未运行"被当成"需要登录"报出来，
> 而登录动作调用的是系统默认浏览器（**没有**调试端口）。你登录成功，Cookie
> 却写进了本工具读不到的地方，下一次轮询又是"需要登录"。**点多少次都回到原点。**
> 现在菜单和 `opencsi login` 都会启动一个本工具能读的浏览器。

### 手动启动（等价做法）

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --user-data-dir="$env:LOCALAPPDATA\opencsi-cdp-profile" `
  https://opencsitool.com/myTools
```

### 怎么确认修好了

```powershell
opencsi doctor --no-proxy
```

`devtools endpoint` 一行应该变成 `[ok] http://127.0.0.1:9222`。

### 如果启动失败

报 `FAILED` 且提示 "already open" 时，说明**同一个专用配置目录已经被另一个
窗口占用**了。Chromium 会把新启动请求转发给那个进程，而那个进程没有调试端口，
所以端口永远不会出现。**把所有使用该配置目录的窗口关掉再重试。**

---

## 还是解决不了？

收集这些信息：

```bash
opencsi doctor --json > doctor.json
opencsi --version
python --version
```

`doctor --json` 的输出**已经过脱敏**，不含 Cookie 或完整密钥。
但提交前请还是**自己看一眼** —— 这是好习惯。
