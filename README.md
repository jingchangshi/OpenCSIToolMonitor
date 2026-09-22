# opencsi — openCsiTool 只读命令行客户端

一个**独立**的 openCsiTool "My Tools"（我的工具）只读查询工具。

它不需要 DSH、不需要 AI Agent、不需要浏览器自动化框架，只依赖 **Python 标准库**。
认证方式是从**你自己已经登录的浏览器**里读取那个 HttpOnly 的会话 Cookie，
然后直接以普通 HTTP 请求访问站点自己的内部 Web API。

> **This project uses an internal web API observed from the user's own
> authenticated openCsiTool session. It is not an official openCsiTool public
> API client.**
>
> 本项目使用的是**从用户本人已认证会话中观测到的内部 Web API**，
> 它**不是** openCsiTool 官方公开 API 客户端。官方站点未提供公开 API 文档，
> 详见 [`docs/api-investigation.md`](docs/api-investigation.md)。

---

## 目录

- [它是什么，不是什么](#它是什么不是什么)
- [环境要求](#环境要求)
- [安装](#安装)
- [快速开始](#快速开始)
- [浏览器准备（重要）](#浏览器准备重要)
- [命令一览](#命令一览)
- [命令详解](#命令详解)
- [退出码](#退出码)
- [安全性](#安全性)
- [常见问题](#常见问题)
- [开发](#开发)

---

## 它是什么，不是什么

**是什么**

- 一个只读 CLI：`status` / `tools` / `usage` / `trend` / `prices` / `logs` / `doctor`
- 一个可被其他程序 import 的库：`from opencsi import OpenCsiToolClient`
- 纯标准库实现，`dependencies = []`，可在 Windows / Linux / macOS 上直接运行

**不是什么**

- ❌ 不是 GUI（图形界面属于后续阶段）
- ❌ 不是官方 API 客户端（见上方声明）
- ❌ 不会写任何数据 —— 客户端**只实现了 GET**，连 `post()` 方法都不存在
- ❌ 不访问任何管理端接口
- ❌ 不需要 Playwright / Selenium / Browser Use / LLM

### 架构原则

```
DSH 开发它。DSH 不运行它。
浏览器 认证它。浏览器 不查询它。
OpenCsiToolClient 查询它。CLI 暴露它。
```

核心 API 客户端**只认识 `CredentialProvider` 接口**，
完全不知道 Chrome、Edge、WebSocket、CDP 或 target 的存在。
浏览器只负责"提供 Cookie"，之后所有查询都是普通的 HTTPS 请求。

```
                    ┌──────────────────────┐
                    │   opencsi (CLI)      │  ← 用户入口
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │  OpenCsiToolClient   │  ← 纯 HTTP，只读
                    └──────────┬───────────┘
                               │  只认识这一个接口
                    ┌──────────▼───────────┐
                    │  CredentialProvider  │  ← 协议
                    └────┬────────────┬────┘
                         │            │
        ┌────────────────▼──┐   ┌─────▼──────────────┐
        │ CdpCookieProvider │   │ ManualCookieProvider│
        │ (读浏览器 Cookie) │   │ (手工输入/测试)     │
        └───────────────────┘   └────────────────────┘
```

---

## 环境要求

| 项目 | 要求 |
| --- | --- |
| Python | **3.10 或更高**（开发与验证使用 3.14.6） |
| 第三方依赖 | **无**（`dependencies = []`） |
| 操作系统 | Windows、Linux、macOS |
| 浏览器 | 一个已登录 openCsiTool 的 Chromium 系浏览器（Chrome / Edge / Brave） |

不需要 `pip install` 任何包。不需要 Node.js。不需要 Docker。

---

## 安装

### 方式一：直接运行（零安装，推荐先试这个）

```bash
git clone <this-repo>
cd OpenCSIToolMonitor

# Windows (PowerShell)
$env:PYTHONPATH = "$PWD\src"
python -m opencsi --help

# Linux / macOS
PYTHONPATH=src python -m opencsi --help
```

### 方式二：可编辑安装（获得 `opencsi` 命令）

```bash
pip install -e .
opencsi --help
```

如果你的环境里 `pip install -e .` 因为缺少 `setuptools` 而失败，可以用 `uv`：

```bash
uv pip install -e . --no-build-isolation
```

### 方式三：验证安装

```bash
opencsi --version     # opencsi 0.1.0
opencsi doctor        # 检查整条链路
```

### 方式四：独立 EXE（目标机器上没有 Python）

见下方[打包成独立 EXE](#打包成独立-exe无需安装-python)。

### 可选依赖

核心是**纯标准库**（`dependencies = []`），不装任何额外包也能用。
只有下面两个功能需要额外依赖：

| 额外依赖 | 装什么 | 用在哪 |
| --- | --- | --- |
| `opencsi[qr]` | Pillow, segno | `opencsi login --qr` 的图片解码与终端预览 |
| `opencsi[tray]` | pystray, Pillow | `opencsi tray` 托盘 |
| `opencsi[all]` | 以上全部 | 工作站安装 |

```bash
pip install "opencsi[all]"
```

缺哪个依赖时，命令会**明确告诉你装什么**，而不是抛一个 ImportError 让你自己猜。

> 注意：`pystray` 不在清华镜像上。如果用的是镜像源，需要指定
> `--index-url https://pypi.org/simple`。

---

## 快速开始

```bash
# 1. 先看当前会话状态（最快确认认证是否可用）
opencsi status

# 2. 看核心数据：token、请求数、PR、代码行
opencsi usage

# 3. 列出所有已授权的工具账号
opencsi tools
```

典型输出：

```
== openCsiTool session ==
API origin           : https://opencsitool.com
Credential source    : cdp
Credential available : yes
Cookie lifetime left : 58m12s
Session valid        : yes

== Signed in as ==
Display name : shijingchang
Login        : shijingchang
Employee ID  : 653124
Organization : 体验项目
Role view    : 普通用户
Roles        : VISITOR
```

```
== Personal data overview ==
Total tokens         : 30.6亿
Total tokens (exact) : 3,061,130,999
Total requests       : 2.2万
Pull requests        : 246
Added lines          : 3.1万
Adoption rate        : 3.8% (120/3,150)
Tool accounts        : 3 (2 使用中 / 1 已失效)
```

---

## 浏览器准备（重要）

这个工具**不启动浏览器、不操作页面**，它只是去读一个已经在运行的浏览器里的 Cookie。
所以你需要让浏览器把 DevTools 端口打开。

### 为什么不能直接用平时的浏览器？

**Chrome 147 及以上版本**改变了安全策略：如果你是通过 `chrome://inspect`
在**默认用户配置（default profile）**上打开远程调试的，那么：

- `/json/version`、`/json/list` 等接口会返回 **404**
- 浏览器级别的 WebSocket 握手会被**直接拒绝**

结果是端口开着，但读不到任何东西。这是浏览器的安全设计，不是本工具的缺陷。

### 正确做法：用一个专用配置目录

**Windows (PowerShell)**

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --user-data-dir="$env:LOCALAPPDATA\opencsi-cdp-profile" `
  https://opencsitool.com/myTools
```

**macOS**

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.opencsi-cdp-profile" \
  https://opencsitool.com/myTools
```

**Linux**

```bash
google-chrome \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.opencsi-cdp-profile" \
  https://opencsitool.com/myTools
```

> **注意**：这个专用配置目录是一个**全新的浏览器配置**，里面**还没有登录状态**。
> 请在弹出的窗口里**登录一次 openCsiTool**。
> 之后 Cookie 会保存在这个目录里，后续使用就不用再登录了。

Edge 和 Brave 同理，把可执行文件换成对应的即可（`msedge.exe` / `brave.exe`）。

### 验证浏览器准备好了

```bash
opencsi doctor
```

看到这些就说明成功了：

```
[ok]   devtools endpoint: http://127.0.0.1:9222
[ok]   credential: source=cdp, expires in 58m12s
[ok]   session: shijingchang (employeeId=653124)
```

### 如果不想开调试端口

也可以手工把 Cookie 喂进来（仅用于临时排查）：

```bash
opencsi login --manual
```

它会用 `getpass()` 从**标准输入**读取 Cookie 值。
出于安全考虑，**这个值永远不会通过命令行参数传入**（见 [安全性](#安全性)）。

---

## 命令一览

| 命令 | 作用 | 需要网络 |
| --- | --- | --- |
| `opencsi status` | 会话与凭据状态 | 是 |
| `opencsi tools` | 已授权的工具账号列表 | 是 |
| `opencsi usage` | 个人数据总览（token / 请求 / PR / 代码行） | 是 |
| `opencsi trend` | token 趋势序列 | 是 |
| `opencsi prices` | 模型与工具价目表 | 是 |
| `opencsi logs` | LLM 网关调用日志 | 是 |
| `opencsi doctor` | 诊断整条链路 | 部分 |
| `opencsi login` | 建立 / 续期会话（支持无浏览器扫码） | 是 |
| `opencsi tray` | Windows 11 托盘常驻监控 | 是 |
| `opencsi contract-check` | 校验线上 API 是否仍符合已验证契约 | 是 |

所有命令都支持 `--json`、`-v/--verbose`、`--cdp URL`、`--timeout SECONDS` 等通用选项。

### 通用选项

| 选项 | 作用 |
| --- | --- |
| `--json` | 以 JSON 输出（适合脚本 / Agent 调用），始终 UTF-8 |
| `-v`, `--verbose` | 打印请求路径、状态码、耗时（不含任何头部值） |
| `--cdp URL` | 指定 DevTools 端点，例如 `http://127.0.0.1:9222` |
| `--timeout SECONDS` | 单次请求超时，默认 15 |
| `--cache-ttl SECONDS` | 进程内业务数据缓存时长，默认 300 |
| `--no-cache` | 本次运行不使用缓存 |
| `--refresh` | 强制重新拉取，跳过缓存 |
| `--no-proxy` | **完全忽略系统代理**。见下方说明 |

#### `--no-proxy` 什么时候用

`urllib` 除了读 `HTTP_PROXY` / `HTTPS_PROXY` 环境变量，在 **Windows 上还会读注册表**
（`HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings`）。
`curl` 不读注册表，所以会出现这种让人困惑的情况：

```text
curl   https://opencsitool.com/...   ->  401  （正常）
python (urllib)                      ->  SSLEOFError
```

如果你的机器上装了 Clash / V2Ray 之类的本地代理，而它无法正确转发
`opencsitool.com`，就会看到 `SSLEOFError`。这时：

```bash
opencsi status --no-proxy
```

本工具在报错时会**指出走了哪个代理**，并在提示里给出这个选项。
代理 URL 中的 `user:password@` 会被剥离，不会出现在日志或输出里。

> 如果你**确实需要**代理才能上网，请不要用 `--no-proxy`。

---

## 命令详解

### `opencsi status`

默认显示会话状态和核心数据总览，适合作为脚本的第一步。

```bash
opencsi status
opencsi status --verbose        # 额外显示 userId / accountId / 组织 UUID
opencsi status --no-summary     # 只检查会话，少发一次请求
opencsi status --json
```

```
== openCsiTool ==
Session : OK
User     : shijingchang
Employee : 653124
Role     : 普通用户
Data updated : 2026-09-19 21:40 UTC+8

Tokens        : 3,061,130,999
Requests      : 21,632
PRs           : 246
Added lines   : 31,167
AI generated  : 3,150
AI adopted    : 120
Adoption rate : 3.8%

Active tools  : 2
Expired tools : 1
```

> 默认**不显示** `userId` / `accountId` / 组织 UUID —— 日常使用用不到，
> 而且截图或共享终端时会泄露身份。需要时用 `--verbose`。
>
> `Data updated` 显示的是**服务端**给出的时间，并保留它自带的时区偏移，
> 不做本地时区换算。

### `opencsi tools`

列出所有已授权的 AI 工具账号。**过滤全部在本地完成**——服务端没有搜索接口。

```bash
opencsi tools
opencsi tools --active                # 只看 使用中（--active-only 亦可）
opencsi tools --type API_BUNDLE       # 按 requestType 精确筛选
opencsi tools --search Triton          # 在账号名/工具名/申请单号/类型中模糊搜索
opencsi tools --show-key-mask         # 显示站点自身的掩码 sk-xxxxxxxx****
```

```
  ID  Request No.      Type        Account         Status  Tokens
----  ---------------  ----------  --------------  ------  ------
5593  REQ202608170007  API_BUNDLE  AI编程助手-002  使用中  14.0亿
1954  REQ202604160010  TRAE        AI编程助手-001  使用中   2.6亿
1094  REQ202603090022  API_BUNDLE  AI编程助手-001  已失效  14.0亿
```

> **密钥默认不显示**。加上 `--show-key-mask` 后也只显示站点自身的掩码
> （`sk-bM4LUSm****`）；**完整 `virtualKey` 永远不会出现在任何输出里**。
>
> `--type` 给了一个不存在的类型时是用法错误（退出码 2），并会列出实际存在的类型，
> 而不是静默返回空结果。

### `opencsi usage`

最重要的命令，对应网页上的"个人数据总览"卡片。

```bash
opencsi usage
opencsi usage --cost                     # 附加费用估算
opencsi usage --start-date 2026-08-20 --end-date 2026-09-19
```

> **关于日期**：`--start-date` / `--end-date` **只影响趋势序列 `tokenTrend`**，
> 工具账号列表 `requestList` 始终返回全部（这是服务端行为）。
> 两个日期必须**同时提供**。

费用估算说明：

- `TOKEN` 计费 → `tokens / 1,000,000 × 单价`
- `FLAT` 计费 → 只算月费，**不**按 token 计费
- 价目表里没有的条目（例如 `API_BUNDLE` 这种"套餐"）→ 显示 `UNKNOWN`，**不会**假装是 0

> 估算值仅供参考，**以服务端账单为准**。

### `opencsi trend`

token 趋势，含 prompt / completion 拆分。

```bash
opencsi trend                    # 按模型汇总
opencsi trend --group-by date    # 按日期汇总
opencsi trend --by-day           # 等价于 --group-by date
opencsi trend --days 7           # 最近 7 天（含今天）
opencsi trend --days 30
opencsi trend --from 2026-08-20 --to 2026-09-19
```

```
== Token trend by model ==
Key                     Display name             Tokens   Prompt  Completion  Share
----------------------  ----------------------  -------  -------  ----------  -----
GLM_5_3_FLASH           GLM-5.3-Flash             9.7亿    9.7亿     583.1万  69.6%
DEEPSEEK_V4_FLASH_0731  DeepSeek-V4-Flash-0731    4.2亿    4.1亿     290.4万  29.9%
```

> `--days N` 表示**今天加上之前 N-1 天**（`--days 1` 就是今天）。
> `--days` 不能与 `--start-date`/`--end-date` 同时使用——两者可能互相矛盾，
> 与其猜测不如直接报错。

### `opencsi prices`

```bash
opencsi prices              # 只显示已启用的
opencsi prices --all        # 显示全部 20 行
opencsi prices --bill-type TOKEN
```

> `Blended` / `In` / `Out` 是**每 100 万 token** 的价格；
> `Monthly` 是**包月费**，不是 token 单价 —— 两者不会混在一起。

### `opencsi logs`

LLM 网关调用日志。注意：**如果你的用量走的是套餐而不是网关，这个日志本来就是空的**，
这不是故障。

```bash
opencsi logs
opencsi logs --page 2 --page-size 50
opencsi logs --raw      # 显示每条记录的全部字段
opencsi logs --from 2026-08-01 --to 2026-09-01   # 等价于 --start-date/--end-date
```

### `opencsi doctor`

诊断整条链路，是遇到问题时**第一个应该运行的命令**。

```bash
opencsi doctor
opencsi doctor --skip-contract   # 跳过线上 API 检查，少发请求
```

它会逐项检查并给出 `[ok]` / `[warn]` / `[FAIL]`。**每个 API 端点单独一行**，
因为诊断的价值就在于定位到具体是哪一次调用坏了：

```
== opencsi doctor (0.1.0) ==
API origin : https://opencsitool.com
User-Agent : opencsi-cli/0.1.0

[ok]   python: 3.14.6 on Windows 11
[ok]   devtools endpoint: http://127.0.0.1:9222
[ok]   credential: source=cdp, expires in 58m12s
[ok]   session: shijingchang (employeeId=653124)
[ok]   getUserInfo: identity parsed
[ok]   personalQueueStatus envelope: code/data present
[ok]   personalQueueStatus.data.requestList: list present
[ok]   personalQueueStatus.data.tokenSummary: object present
[ok]   personalQueueStatus.data.tokenTrend: list present
[ok]   personalQueueStatus.data.syncStatus: object present
[ok]   requestList item fields: all required fields present
[ok]   ai/config/cost: 20 rows
[ok]   price rows have requestType: requestType present on every row
[ok]   call-logs shape: list/total present
[ok]   key-budget: parsed

All 15 checks passed.
```

失败时会打印**真正的原因**，而不只是一句"未登录"。例如当浏览器拒绝握手时
（注意提示紧跟在失败项之后，即使重定向到文件也保持这个顺序）：

```
[ok]   python: 3.14.6 on Windows 11
[ok]   devtools endpoint: http://127.0.0.1:9222
[FAIL] credential: source=cdp, the DevTools endpoint at http://127.0.0.1:9222
       answered, but its WebSocket could not be used (browser socket: WebSocketError)
       -> Port 9222 is open but the DevTools WebSocket handshake was refused.
          Chrome 147+ blocks remote debugging on the default profile when it was
          enabled from chrome://inspect. Close that browser and start a
          dedicated-profile instance instead: chrome.exe
          --remote-debugging-port=9222
          "--user-data-dir=%LOCALAPPDATA%\opencsi-cdp-profile"
          https://opencsitool.com/myTools  -- then sign in once in that window.
          See README 'Browser preparation'.
[warn] session: not attempted: no credential available
       -> resolve the credential check above first

1 check(s) failed, 1 warning(s).
```

> `doctor` 的提示输出到 **stdout**（而不是 stderr），因为提示本身就是这个命令的产物 ——
> 这样 `opencsi doctor > report.txt` 才不会丢掉修复方法。

### `opencsi login`

建立、查看或续期 openCsiTool 会话。这是**认证生命周期**的唯一入口。

```bash
opencsi login                 # 打开登录页，等待会话建立
opencsi login --status        # 只看会话与凭据剩余寿命，不做任何改动
opencsi login --renew         # 后台标签页静默续期，不打扰你
opencsi login --qr            # 用微信扫码登录 GitCode，无需浏览器
opencsi login --no-browser    # 不自动打开浏览器
opencsi login --wait 120      # 最多等待 120 秒
opencsi login --manual        # 从 stdin 读 Cookie（getpass）
```

#### 三种语义，不要混淆

| 概念 | 做什么 | 会不会弹窗 |
| --- | --- | --- |
| **凭据重载**（credential reload） | 从浏览器重新读一次 Cookie | 不会 |
| **会话续期**（session renewal） | 在后台标签页重跑 GitCode OAuth，换一个新的 `token` | 不会抢焦点 |
| **交互登录**（interactive login） | 需要你本人操作（扫码 / 输密码） | 会 |

`--renew` 是第二种。它**不会**打断你正在浏览的页面：续期在后台目标里完成，
用完即关。触发策略也很克制：

- 凭据剩余寿命 **> 5 分钟** → 什么都不做（`ALREADY_VALID`）；
- 剩余 **≤ 5 分钟** → 静默续期；
- API 返回 **401** → 先重载 Cookie，再尝试静默续期。

续期成功的判据不是"页面加载完了"，而是 **旧 token ≠ 新 token 且新过期时间更晚**，
并且服务端确实接受了它。

#### `--qr`：无浏览器登录（重要限制）

`opencsi login --qr` 走的是 GitCode 的微信扫码协议，**纯 HTTP + JSON 轮询**，
不需要 DevTools、不需要浏览器、不做任何 DOM 抓取。协议已实测复现，
细节见 [`docs/gitcode-qr-protocol.md`](docs/gitcode-qr-protocol.md)。

**但必须说清楚一个实测结论：GitCode 返回的 `qrcode` 字段不是二维码（QR code），
而是微信小程序码。** 依据（三重独立证据，可用
`python tools/verify_qr_render.py` 复现）：

1. 真实 QR 解码器（zxing-cpp）读不出来 —— 返回空；
2. 没有定位图案：二维码三个角必定有的回字形方块，实测暗像素占比全是 `0.000`；
3. 最细特征只有 **1 像素**（430 px 图内），而二维码最细特征是一个模块（约 7–20 px）。

第 3 点决定了**终端里画出来的一定扫不了**：终端宽度 80–120 列，
把 430 px 缩下去会摧毁亚像素细节。所以：

- **能扫的是文件**：命令会把原始 PNG 写到
  `%LOCALAPPDATA%\OpenCSI\login-code\`，你在屏幕上打开它再用微信扫。
- **终端里的图只是预览**：用灰度字符画出形状，方便你确认它加载出来了。
  它被明确标注为不可扫 —— 不会让你拿着手机对着一个永远读不出的图发呆。

还有一个诚实的边界：**openCsiTool 自己的 `token` Cookie 由它自己的 OAuth 回调签发，
那一步需要浏览器会话。** 单靠 GitCode 会话拿不到它。所以 `--qr` 的成功判据是
"GitCode 已登录"，命令会明确告诉你后面还需要什么。

### `opencsi tray`

Windows 11 通知区域（托盘）常驻监控。

```bash
opencsi tray                      # 启动托盘（阻塞）
opencsi tray --check              # 检查托盘是否可用，不发网络请求、不起线程
opencsi tray --once               # 只取一次数据并打印，不进托盘
opencsi tray --interval 600       # 刷新间隔（秒），默认 300
opencsi tray --renew-margin 300   # 剩余多久开始续期（秒），默认 300
opencsi tray --install-startup    # 注册开机自启（写 HKCU Run 键）
opencsi tray --remove-startup     # 取消开机自启
opencsi tray --startup-status     # 查看自启状态
opencsi tray --allow-multiple     # 允许多开（默认单实例）
opencsi tray --auto-recover-browser  # 浏览器没在跑时自动启动它（默认关闭）
```

托盘是**纯 UI**：它直接 import `MonitorService` / `OpenCsiToolClient`，
不通过 `subprocess` 调 `opencsi usage --json`。因此没有子进程、没有 JSON 二次解析、
也没有第二份认证逻辑。菜单里的操作只是把请求入队，阻塞工作都在工作线程里做 ——
否则取一次数据就会把图标卡住。

**`--auto-recover-browser` 为什么默认关闭。** 开机自启的场景下，Windows 会先把托盘拉起来，
而 Chrome 往往还没运行，于是托盘停在 `浏览器未运行`。打开这个开关后，服务会自己用专用
配置目录 + 调试端口启动浏览器，并**在同一轮内重试取数**，所以用户什么都不用做。
但它会在你的桌面上弹出一个窗口 —— 一个监控工具擅自开窗口，是用户有权反感的行为，
所以这必须由你显式选择，而不是因为它让主流程更顺就替你决定。启动带 600 秒冷却，
且同一轮只重试一次，避免反复弹窗或死循环。

开机自启用 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`：它是**每用户**的
（不需要管理员权限、不影响其他账户），而且 Windows 自己的任务管理器"启动"标签页
读的就是它 —— 在这里关和在系统里关是同一件事。**不显式执行 `--install-startup`
就不会写任何东西。**

托盘的文字是中文，数字用 `万` / `亿`：

```text
OpenCSI | 正常
36.3亿 tokens  2.7万 次请求  249 PR
更新于 0s 前 | 会话 53m
```

**tooltip 用 `亿`，菜单用完整数字**，这是刻意的：tooltip 一共只有 127 个字符
（Windows `NOTIFYICONDATA` 的硬限制），必须压缩；而菜单有空间，打开菜单看数字的
人要看的就是那个数字本身 —— 在菜单里显示 `36.3亿` 恰好丢掉了用户来找的精度。

状态文字（`正常` / `需要登录` / `浏览器未运行` / `离线` …）单独维护一份中文映射，**不改**
`MonitorState` 枚举本身：枚举值出现在 JSON 和日志里，是稳定契约；把两者合并，
要么机器输出变成中文，要么用户看到英文。

`需要登录` 与 `浏览器未运行` 是**两件事**，修复方式也不同：前者是你的 GitCode 登录态
没了，需要重新认证；后者是持有登录态的那个浏览器**没在运行**，需要把它启动起来。
早先两者被合并成"需要登录"，于是托盘让你去登录、登录动作却打开了一个没有调试端口的
浏览器 —— 你登录成功，Cookie 却写进了本工具读不到的地方，下一次轮询又是"需要登录"。
**点多少次都会回到原点。** 现在：

* `浏览器未运行` 的第一项菜单是 **`启动浏览器并登录`**，它会用专用配置目录 +
  `--remote-debugging-port` 启动 Chrome/Edge，再把登录页开在那里；
* 同一状态下 `opencsi login` 也会这么做，不再调用系统的默认浏览器。

**只在需要你处理时弹一次通知**（`需要登录` / `会话失效` / `浏览器未运行`），网络和服务端错误
**不弹** —— 网络抖动通常会自己恢复，不值得打断你。一直停留在该状态不会重复弹，
恢复正常后再次失效才会再弹一次。想彻底静音：**Windows 设置 → 系统 → 通知**
里单独关掉 `OpenCSI Monitor`，图标和 tooltip 照常工作。

### `opencsi contract-check`

校验线上 API 是否仍然符合本工具依赖的契约。当站点升级导致字段变化时，
这个命令能立刻告诉你**具体哪一项**对不上了。

```bash
opencsi contract-check
```

---

## 打包成独立 EXE（无需安装 Python）

```powershell
pip install "opencsi[build]"
python tools/build_exe.py
```

产出两个文件（各约 16 MB）：

| 文件 | 用途 |
| --- | --- |
| `dist\opencsi.exe` | 命令行版（有控制台） |
| `dist\opencsi-tray.exe` | 托盘版（无控制台，**不会闪黑窗**） |

托盘版接受与子命令相同的参数，`tray` 一词可以省略：

```powershell
dist\opencsi-tray.exe                      # 显示图标（无控制台窗口）
dist\opencsi-tray.exe --once               # 取一次快照，打印后退出
dist\opencsi-tray.exe --check              # 自检能否启动
dist\opencsi-tray.exe --startup-status     # 查看开机自启项
```

> 早期版本会**忽略**这些参数并直接常驻托盘，所以 `--once` 什么都不打印、
> 进程也永不退出。现在参数会被转发给 CLI 的 `tray` 子命令。

**为什么是两个而不是一个**：它们需求不同，而 PyInstaller 的 `--windowed`
是按二进制设置的。命令行版的全部意义就是输出文本；托盘版则绝不能在每次开机时
弹出一个控制台窗口。一个 EXE 无法同时满足这两点。

`packaging/opencsi.spec` 里的 `hiddenimports` 是关键。本项目的可选依赖
（pystray / Pillow / segno）都是**在函数内部惰性 import** 的 —— 这样
`opencsi --help` 在没有装任何额外依赖的机器上也能用 —— 但 PyInstaller 的
静态分析看不穿这一点。少了这份清单，托盘会**编译成功、然后在用户机器上崩掉**，
这是最糟糕的一类打包 bug，因为构建过程看起来完全正常。

`upx=False` 是刻意的：UPX 压缩是杀毒软件误报的常见诱因，而一个会读取用户
会话 Cookie 的工具，本来就更容易被盯上。

---

## 退出码

脚本可以依赖这些稳定的退出码：

| 码 | 含义 |
| --- | --- |
| `0` | 成功 |
| `1` | 未分类错误 |
| `2` | 参数或配置错误 |
| `10` | DevTools 端点不可用 |
| `11` | 找不到浏览器目标页面 |
| `12` | 未登录 / 找不到凭据 |
| `13` | 会话已过期（401） |
| `20` | 权限不足（403） |
| `30` | 网络错误 |
| `31` | 服务端错误（5xx） |
| `32` | 业务错误（HTTP 200 但 `code != 200`） |
| `33` | GitCode 扫码协议错误（响应结构不符合预期） |
| `130` | 被用户中断（Ctrl-C） |

`opencsi login --renew` 还会用一组独立的续期状态码，见
[`docs/authentication.md`](docs/authentication.md)：续期失败但会话仍可用时**不会**
返回非零码 —— 那是"这次没续上"，不是"命令失败了"。

示例：

```bash
opencsi usage --json || echo "exit=$?"
```

`--json` 模式下，失败会输出**单个** JSON 文档：

```json
{
  "ok": false,
  "error": {
    "error": "SESSION_EXPIRED",
    "message": "openCsiTool rejected the session cookie (HTTP 401)",
    "http_status": 401
  }
}
```

---

## 安全性

这个工具接触的是你的**真实会话凭据**，所以安全约束是硬性的：

### 绝不发送 `Authorization` 头

站点只认 Cookie。本工具的 `HttpTransport` **没有任何代码路径会设置
`Authorization` 头** —— 这一条有单元测试直接断言实际构造出的请求头。

（实测：任何 Bearer 头都会让服务端返回 `401 Invalid Authorization`，
而缺少 Cookie 时返回的是 `401 empty Authorization` —— 两者含义不同，
本工具会区分报告。）

### Cookie 不会泄漏到任何地方

- 不会出现在 `repr()`、`str()`、日志、异常消息、traceback、JSON 输出里
- `CredentialStatus` 这个结构体**根本没有能装 token 的字段**
- 日志过滤器 `RedactingFilter` 会在写入前清洗所有记录
- 密钥掩码规则与站点一致：`sk-bM4LUSm****`

### 命令行不接受密钥

```bash
opencsi login --token SECRET    # ❌ 会被拒绝
opencsi login --manual          # ✅ 用 getpass() 从 stdin 读
```

原因很简单：**argv 对同机器上的所有用户可见**（`ps`），而且会进 shell 历史。
所以没有任何命令接受 `--token` 之类的参数。

### 只读

- `HttpTransport` **只实现了 `get_json()`**，不存在 `post` / `put` / `patch` / `delete`
- 不访问任何管理端接口
- 没有守护进程、没有后台轮询

### 重试策略

遇到 `401` 时，**只重新读取一次凭据**然后重试一次；再失败就报错退出。
不会无限重试。

---

## 常见问题

### `opencsi doctor` 说 "WebSocket could not be used"

最常见的原因就是**用默认配置目录开了调试端口**。
请按 [浏览器准备](#浏览器准备重要) 用一个专用 `--user-data-dir` 重启浏览器。

### 报错 "no openCsiTool 'token' cookie"

浏览器连上了，但里面没有登录状态。请在**那个**浏览器窗口里登录一次
openCsiTool（注意：专用配置目录是全新的，需要重新登录）。

### 报错 `SSLEOFError` / `UNEXPECTED_EOF_WHILE_READING`

**不是服务端故障**，通常是本地代理。

`urllib` 在 Windows 上会读注册表里的系统代理，而 `curl` 不读 —— 所以
`curl` 能通、Python 不通。用 `--no-proxy` 绕过：

```bash
opencsi status --no-proxy
```

确认一下 Python 眼里有哪些代理：

```bash
python -c "import urllib.request; print(urllib.request.getproxies())"
```

详见 [docs/troubleshooting.md](docs/troubleshooting.md)。

### `opencsi logs` 是空的

正常现象。走套餐计费的账号，网关日志本来就是空的。

### 会话过期了怎么办

Cookie 有效期约 **0.97 小时**。过期后重新登录即可。
本工具会在 Cookie 快过期时主动重新读取一次。

### Windows 控制台显示乱码

本工具输出的是 UTF-8 中文。如果看到乱码，先设置：

```powershell
$env:PYTHONIOENCODING = "utf-8"
chcp 65001
```

CLI 本身已经做了防护：如果某个字符在当前控制台编码里无法表示
（例如服务端备注里的 emoji），它会降级成 `?` 而**不会崩溃**。

### `pip install -e .` 失败

你的环境可能缺少 `setuptools`。用 `PYTHONPATH` 方式直接运行，
或者用 `uv pip install -e . --no-build-isolation`。

更多排查见 [`docs/troubleshooting.md`](docs/troubleshooting.md)。

---

## 开发

### 运行测试

测试**完全离线**，不需要网络也不需要浏览器（CDP 用进程内的假 DevTools 服务器模拟）：

```bash
# 用 unittest（不需要任何第三方包）
cd tests
python -m unittest discover -s . -p "test_*.py" -t .

# 或者用 pytest（如果装了）
pytest
```

### 测试覆盖

| 文件 | 内容 |
| --- | --- |
| `test_mapping_regression.py` | 调查报告里 42 项已验证事实的回归 |
| `test_redaction.py` | 密钥脱敏（对抗性：repr / 日志 / 异常 / JSON） |
| `test_auth.py` | 凭据提供者协议 |
| `test_cdp.py` | CDP 读取，含 Chrome 147+ 失败模式 |
| `test_client.py` | 错误分类、401 单次重试、缓存 |
| `test_formatting.py` | CJK 宽度对齐、亿/万 数字规则 |
| `test_cli.py` | CLI 参数、退出码、输出契约 |

### 文档

- [`docs/architecture.md`](docs/architecture.md) —— 分层设计
- [`docs/authentication.md`](docs/authentication.md) —— 认证流程细节
- [`docs/troubleshooting.md`](docs/troubleshooting.md) —— 排查手册
- [`docs/api-investigation.md`](docs/api-investigation.md) —— API 调查报告（原始事实）

### 作为库使用

```python
from opencsi import CdpCookieProvider, OpenCsiToolClient

with OpenCsiToolClient(CdpCookieProvider()) as client:
    client.login_or_restore_session()
    snapshot = client.get_my_tools()
    print(snapshot.total_tokens, snapshot.total_request_count)
```

---

## 许可与免责声明

本项目通过**逆向观测内部 Web API** 实现，**不是** openCsiTool 官方产品，
也未获得其背书。接口可能随时变化而不再另行通知 —— 届时请运行
`opencsi contract-check` 定位差异。

请仅在你**本人已获授权**的账号上使用本工具，并遵守所在组织的规定。
