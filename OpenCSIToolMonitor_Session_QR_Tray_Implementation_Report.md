# OpenCSIToolMonitor 会话续期 + 扫码登录 + 系统托盘 实施报告

本报告覆盖 `jingchangshi/OpenCSIToolMonitor`（`master` 分支）第三阶段的工作：
把一个"可以用浏览器登录"的只读 CLI，演进为**能让自己保持登录**、**可以完全不用
浏览器登录**、并且**常驻 Windows 11 桌面**的工具。

架构原则延续上一阶段，并做了扩展：

```
DSH 负责开发。              DSH 不负责运行。
浏览器可以完成认证。        浏览器不负责查询。
OpenCsiToolClient 查询 API。CLI 与托盘负责展示。
托盘只是视图。              它绝不 subprocess 调用 CLI。
```

---

## 1. 结论

**已完成，并且针对真实站点做了实测验证。**

| 目标 | 状态 | 证据 |
| --- | --- | --- |
| 拆分 *credential reload* 与 *session renewal* | **完成** | `SessionManager` + 三个协议；提交 `cf34c1b` |
| 过期会话的静默自动续期 | **完成，实测证明** | 浸泡探针观测到真实过期点 `02:13:34`，`token_changed=True`，服务端接受新会话（`shijingchang`） |
| 续期触发策略，且不存在无限循环 | **完成** | `expires_in > margin` → 不动作；`<= margin` → 静默续期；401 → 先 reload，再续期一次 |
| 静默续期不抢占用户焦点 | **完成** | `Target.createTarget` 创建**后台** target，完成后 `Target.closeTarget`；绝不导航用户当前页面 |
| GitCode 纯 CLI / 扫码登录可行性调研 | **完成** | `docs/gitcode-qr-protocol.md` —— 结论 `QR_FLOW_REPRODUCIBLE` |
| 实现 CLI 扫码登录 | **完成，实测验证** | `opencsi login --qr` 真实创建了 challenge、写出图片，未扫码时如实返回 `TIMEOUT`（退出码 13） |
| Windows 11 托盘 v1 | **完成，实测验证** | `opencsi tray --once` 打印真实快照；`--check` 报告 `tray: ok`，6 个菜单项 |
| 测试 | **完成** | **656 项测试**（655 通过、1 跳过），`pytest` 与 `unittest` 双跑全绿 |
| Windows 实机验证 | **完成** | 实测 CLI、扫码、托盘、冻结二进制、入口点 |
| 文档 | **完成** | 5 份文档 + README + 本报告 |
| 规范提交 | **完成** | 自 `cf34c1b` 起 29 个提交；其中 3 个修复了通过**运行真实产物**才发现的缺陷 |

一处必须如实声明的**非结论**：**扫码流程的最后一步无法机器验证。** 它需要真人用手
机扫描一个微信小程序码。本报告交付的代码证明了该物理动作之前的每一步，并且把超时
如实报告为可区分的退出码，而不是假装成功。

---

## 2. 最终 HEAD

最后一个修改**源码或测试**的提交：

```
542fe4f  fix(packaging): make the frozen tray honour its own arguments
```

其后都是纯文档提交，包括承载本报告的提交。在这里写出那些提交是循环的——一个提交
无法包含它自己的 SHA——所以锚点取最后一个行为变更，这才是读者真正需要检出的东西。

本阶段的基线是 `cf34c1b~1`（`4739898`）。工作区干净；无未跟踪的临时文件；无残留进程；
注册表未留下 `Run` 项。

**二进制产物**（在最后一次源码变更后重新构建并重新验证）：

| 产物 | 大小 | 用途 |
| --- | --- | --- |
| `dist/opencsi.exe` | 16.3 MB | 命令行版 |
| `dist/opencsi-tray.exe` | 16.3 MB | 托盘版（无控制台窗口） |

做成两个二进制是刻意的：PyInstaller 的 `--windowed` 是按二进制设置的开关，而托盘
程序在开机自启时闪出一个黑色控制台窗口是不可接受的。

---

## 3. 根因

本阶段由三个真实缺陷驱动。三个缺陷都在修复前被复现，且每个修复都有能在旧代码上
失败的测试。

### 3.1 会话约 58 分钟后失效，而没有任何机制续期

openCsiTool 的 `token` cookie 的 TTL ≈ 0.97 小时（实测 3,472 秒）。原有的
`CdpCookieProvider.refresh()` **只是重新从浏览器读取 cookie** —— 它并不会让服务端
签发一个*新的* cookie。因此，把工具挂着跑的用户会发现它在一小时后静默退化为未认证
状态，而唯一的恢复手段是手动用浏览器重新登录。

"refresh" 这个词同时承担了两种不同操作的语义，这种混淆就是根因：

| 操作 | 做了什么 | 对生命周期的效果 |
| --- | --- | --- |
| **credential reload**（凭据重载） | 从浏览器 profile 重新读取 cookie 值 | 无——同一个 token，同一个过期时间 |
| **session renewal**（会话续期） | 重跑 GitCode OAuth，让服务端签发*新* cookie | 延长整整一个 TTL |
| **interactive login**（交互式登录） | 打开登录页并等待真人操作 | 同样延长，但需要人 |

### 3.2 `opencsi login` 硬依赖打开浏览器

当时不存在任何不启动浏览器窗口、不驱赶真人操作网页的认证路径。在无头环境、受限
环境，或者用户单纯不希望浏览器被劫持时，工具不可用。

### 3.3 当时只有 CLI

没有任何常驻存在。想知道会话是否还活着，必须手动跑一条命令；桌面上也没有任何东西
能告诉你这个工具正在退化。

---

## 4. SessionManager 架构

§3.1 中的三种操作现在被拆成三个独立协议，由同一个对象统一协调。代码库中不再有
其它地方决定何时续期。

```
                    ┌──────────────────────────────────────────────┐
                    │              SessionManager                  │
                    │  renew_margin    renew_cooldown  last_renewal│
                    │                                              │
                    │  needs_renewal()   -> bool                   │
                    │  ensure_valid()    -> RenewalResult          │
                    │  renew(force)      -> RenewalResult          │
                    │  reload_then_renew()-> RenewalResult         │
                    │  login()           -> LoginResult            │
                    │  describe()        -> dict                   │
                    └───┬──────────────┬───────────────┬───────────┘
                        │              │               │
        ┌───────────────▼──┐  ┌────────▼────────┐  ┌───▼────────────────────┐
        │ CredentialProvider│  │ SessionRenewer  │  │InteractiveAuthenticator│
        │   （已存在）      │  │   （Protocol）  │  │      （Protocol）      │
        │                   │  │                 │  │                        │
        │  get_credentials()│  │  renew()        │  │  login()               │
        │  refresh()        │  │  can_renew()    │  │  describe()            │
        │  invalidate()     │  │  describe()     │  │                        │
        └───────────────────┘  └────────┬────────┘  └──────────┬─────────────┘
                                        │                      │
                    ┌───────────────────▼───┐      ┌───────────▼────────────┐
                    │ BrowserOAuthRenewer   │      │ GitCodeQrAuthenticator │
                    │  （静默，走 CDP）      │      │  （完全不用浏览器）     │
                    └───────────────────────┘      └────────────────────────┘
```

`RenewalStatus` 是一个 `str` 枚举，**按身份比较**，绝不从字符串解析：
`RENEWED`、`ALREADY_VALID`、`LOGIN_REQUIRED`、`CDP_UNAVAILABLE`、`OAUTH_FAILED`、
`TIMEOUT`、`UNSUPPORTED`。

### 续期触发策略

```
expires_in  >  renew_margin (300 秒)   ->  ALREADY_VALID，不动作
expires_in  <= renew_margin            ->  静默续期（只尝试一次）
API 返回 401                           ->  reload_then_renew()：
                                           先重载一次 cookie，再续期一次
续期失败                                ->  LOGIN_REQUIRED；绝不循环
```

冷却时间（`renew_cooldown`）防止一次失败的续期在每次 tick 时被反复重试。整条路径上
不存在任何无界重试。

---

## 5. 静默 OAuth 续期

`BrowserOAuthRenewer` 通过 Chrome DevTools Protocol 重跑 GitCode OAuth 授权码流程，
复用已经存在于专用浏览器 profile 中的 SSO 会话。该 SSO 会话的生命周期长于 openCsiTool
cookie，这正是它能在无需用户干预的情况下工作的原因。

**绝不抢占焦点。** 续期器创建一个**后台** target，完成后关闭它：

```
Target.createTarget  (background: true)   <- 不激活该标签页
   -> 驱动 OAuth 重定向链
   -> 通过 Storage.getCookies（浏览器作用域）读取产生的 cookie
Target.closeTarget
```

用户当前页面绝不会被导航、绝不会被聚焦、绝不会被关闭。`Network.getCookies` /
`Network.deleteCookies` 是**页面作用域**的，会要求触碰用户页面；`Storage.getCookies`
是**浏览器作用域**的，这才是正确的域。

**成功是被证明的，不是被假设的。** 只有以下三条**全部**成立，一次续期才被计为成功：

1. `old token != new token`，**且**
2. `new expiry > old expiry`，**且**
3. 服务端接受新会话（`getUserInfo` → `200`）。

到达 `Page.loadEventFired` 只能证明一个页面加载完了。那不是认证，实现也没有把它当作
认证。

### 实测证据 —— 真实过期点，无需任何用户操作完成续期

`tools/probe_renewal_soak.py --minutes 75 --renewals 1 --interval 20`，针对一个还剩
3,472 秒寿命的会话启动，并使用**生产环境**的续期余量（300 秒）：

```
start lifetime : 3472s
renew margin   : 300s (production value)
observing for  : 75 min, every 20s

[01:20:23] OK               lifetime   3472s
[02:12:28] OK               lifetime    347s      <- 即将越过余量线
[02:13:34] RENEWED  token_changed=True

renewals observed: 1
final check    : server accepted the session (shijingchang)

VERIFIED: 1 silent renewals across real expiries, with no
user interaction and no interactive login required.
```

这是本报告中最重要的一条证据：会话被放任从整整一小时掉到不足五分钟，然后它自行完成
了续期。没有任何人碰过这台机器。

第二个探针 `tools/probe_autonomous_renewal.py` 通过生产的 `MonitorService.tick()` 驱动
**定时**路径，并使用放宽的余量，断言同样的四条性质，因此定时器驱动的路径被独立于
直接调用 `renew()` 之外单独覆盖。

---

## 6. GitCode 扫码登录调研

完整报告：`docs/gitcode-qr-protocol.md`（905 行）。

**结论：`QR_FLOW_REPRODUCIBLE`（扫码流程可复现）。** GitCode 的微信小程序登录是一个
纯 HTTP + JSON 轮询流程，不需要浏览器 JS：

| 步骤 | Method | Path |
| --- | --- | --- |
| 创建二维码 | `POST` | `/uc/api/v1/qrcode/wechat_mini_program` |
| 轮询状态 | `GET` | `/uc/api/v1/qrcode/wechat_mini_program?scene_id=…` |
| 换取凭据 | `POST` | `/uc/api/v1/user/oauth/login/qrcode/wechat_mini_program?scene_id=…` |

方法：对 `cdn-static.gitcode.com` 上惰性加载的 chunk 做静态 bundle 分析，加上只读的
`GET`/`OPTIONS` 探测。调研本身**没有发出过任何 `POST`**。

三个改变了实现方式的发现：

1. **`X-Source` 不是签名。** 它只是一个前端埋点标签（`login_trigger_source`）。它不在
   CORS 的 `Access-Control-Allow-Headers` 白名单里（该白名单只回显 `traceparent`），
   但那只是浏览器侧的同源策略约束——服务端并不校验它。在 `--qr` 存在之后，通过线路层
   测试得到了确认。
2. **轮询不需要 cookie 或认证头。** 状态接口是匿名的。
3. **`qrcode` 字段不是二维码——它是微信小程序码。** 这是一个硬性的渲染约束，并用三种
   独立方式证明：`zxing-cpp` 返回 `[]`（无 QR 符号）；三个 QR 定位图案角点的暗色占比
   均为 `0.000`；430 px 图片中最细的暗色连续段只有 **1 px**，而一个 QR 模块本该有
   好几个像素宽。

第 3 条发现正是 CLI **不**声称自己画出了可扫描二维码的原因。它写出一张 PNG，让用户去
扫*那张图片*，并明确说明终端里的图形只是预览、其点阵比终端字符格更细。早先的行为——
打印一个终端图形并称之为二维码——是一个谎言，提交 `36cc305` 移除了它。

我**没有**发明 GitCode Device Code API。没有任何 `/oauth/device/code` 端点被凭空编造；
该流程被证实并不存在，因此没有被使用。

---

## 7. 扫码登录实现

`src/opencsi/auth/gitcode_qr.py`（577 行）—— `GitCodeQrAuthenticator`、`QrChallenge`、
`QrLoginResult`、`QrStatus`、`QrLoginStatus`、`QrProtocolError`（退出码 33）。

* `MAX_QR_REFRESHES = 1` —— 过期的码只重新创建**一次**，绝不循环。
* `DEFAULT_POLL_INTERVAL = 1.5 秒`，`DEFAULT_MAX_WAIT = 180 秒`。
* `scene_id` 是与凭据相邻的敏感值：它以 `field(repr=False)` 声明，并配有自定义
  `__repr__`，打印 `scene_id=<redacted>`，同时把图片载荷截断到 32 个字符，因此它无法
  经由 traceback 或日志行泄漏。

`src/opencsi/auth/qr_render.py` 在不引入运行时依赖的前提下渲染该码：`segno` 与
`Pillow` 仅在存在时才使用，否则渲染器回退到亮度渐变预览（`" .:-=+*#%@"`）。已保存的
登录码只保留最新的 3 个（`KEEP_CODES = 3`），因此本地目录不会无界增长。

### 实测运行

```
$ opencsi login --qr --qr-wait 6 --no-proxy
Requesting a GitCode login code...

Sign in to GitCode by scanning this with WeChat (微信扫一扫):
   <terminal preview>
Open this file and scan it with WeChat: C:\Users\jcshi\AppData\Local\OpenCSI\
login-code\opencsi-login-code-5hygag28.png
Scan the image, not the terminal drawing above: this is a WeChat mini-program
code, and its dots are finer than a terminal cell.

Waiting for scan...

error: QR sign-in did not complete (TIMEOUT).
       the QR login did not finish in time
EXIT=13
```

challenge 是真实向线上服务器创建的，图片是真实写出的，超时是一个可区分的退出码——
而不是成功。

**范围上的诚实：** `--qr` 成功意味着*"GitCode 已登录"*，而不是*"openCsiTool 会话已
建立"*。openCsiTool 自己的 `token` 来自 openCsiTool 自己的 OAuth 回调，而那个回调需要
浏览器会话。该命令明确陈述这一点，而不是暗示它替代了浏览器路径。

即使扫码已经可用，`CdpCookieProvider` + `BrowserOAuthRenewer` 这一对仍被保留为主路径
与回退路径。扫码消除了 GitCode 对浏览器的依赖；它没有消除 openCsiTool 对浏览器的依赖。

---

## 8. Windows 托盘

`src/opencsi/tray/`（1,225 行）—— `app.py`（pystray 宿主）、`presenter.py`（纯格式化）、
`icons.py`（Pillow 绘制图标）、`__main__.py`、`__init__.py`。

**托盘是视图，不是客户端。** 它直接 import `MonitorService` 与 `SessionManager`，并调用
`tick()`。它绝不 spawn `opencsi usage --json`，也绝不重新解析 JSON。这一点由一个结构化
测试断言。

**分层。** 一切不需要 Windows 消息循环就能测试的东西都被下沉到 `monitor/` 与
`tray/presenter.py`，它们是纯的。`app.py` 只负责把已经算好的值交给 pystray。这就是为什么
656 项测试可以离线运行，而托盘本身在真机上验证。

**状态**（`MonitorState`）：`STARTING`、`OK`、`REFRESHING`、`RENEWING`、
`LOGIN_REQUIRED`、`OFFLINE`、`SERVER_ERROR`、`AUTH_ERROR` —— 对应中文标签
`启动中 / 正常 / 刷新中 / 续期中 / 需要登录 / 离线 / 服务异常 / 会话失效`。

**刻意为之的精度不对称。** tooltip 做压缩（`36.3亿 tokens`）；菜单显示精确数字
（`3,634,063,175 tokens / 26,566 次请求`）。tooltip 受 Windows shell 限制为 127 个字符，
菜单不受限，而精确数字才是用户真正需要的。两种行为都被测试锁定。

**通知策略——用的是锁存，不是比较。** 在*进入* `LOGIN_REQUIRED` 或 `AUTH_ERROR` 时
恰好弹一次气泡；状态持续期间不再弹；只有在恢复之后又复发时才再弹一次；对 `OFFLINE` 与
`SERVER_ERROR` 永不弹。第一版实现是把新状态与前一状态比较，而由于 `_refresh_once` 会先
发布一个瞬态的 `REFRESHING`，这个比较在*每一次*轮询时都成立——它在 5 次轮询里触发了
5 次通知。修复方案使用显式的 `_attention_latched` 标志与 `_TRANSIENT_STATES` 集合。
对应的失败用例是 `test_repeated_polls_do_not_re_notify`。

通知失败（属性缺失、`notify` 抛异常、图标尚未就绪）被静默吞掉：tooltip 已经表达了状态，
而一个因为气泡失败就崩溃的托盘，比一个保持安静的托盘更糟。

### 实测运行

```
$ opencsi tray --once --no-proxy
state: OK
total tokens: 3,634,063,175
requests:     26,566
pull requests:  249
generated:    3,150 lines
adopted:      120 lines
adoption:     3.8%
server data:  2026-09-21T20:32:04+08:00
EXIT=0

$ opencsi tray --check --no-proxy
tray: ok
state: STARTING
menu items: 6
tooltip: OpenCSI | 启动中 / 等待首次更新
EXIT=0

$ opencsi tray --startup-status --no-proxy
start at sign-in: disabled
would run: ...\pythonw.exe -m opencsi.tray
EXIT=0
```

开机自启是选择加入的（`--install-startup` / `--remove-startup`），写入
`HKCU\Software\Microsoft\Windows\CurrentVersion\Run`。机器被恢复干净：各次运行之间
**无 Run 项、0 个托盘进程**。

---

## 9. 测试

**656 项测试：655 通过，1 跳过，49 个 subtest 通过。**

```
$ pytest
655 passed, 1 skipped, 49 subtests passed in 36.91s

$ python -m unittest discover -s tests -q
Ran 656 tests in 38.734s
OK (skipped=1)
```

两个 runner 在同一棵代码树上都是绿的。整个测试套件完全**离线**运行——没有任何测试
触碰网络；§5–§8 中的实测证据来自探针脚本与手动运行，而不是来自测试套件。

本阶段新增覆盖：

| 区域 | 锁定了什么 |
| --- | --- |
| `tests/test_monitor.py` | `AttentionNotificationTest`（8 项）：每次状态转换一个气泡、重复不弹、恢复后重新武装、`tick()` 是公开的 |
| `tests/test_monitor.py` | `RENEWING` 卡死回归、空操作续期、成功时 `RENEWING` → `OK` |
| `tests/test_tray.py` | `ChineseUnitTest`、`NotificationTest`、`SignInActionTest`、图标颜色/形状语义 |
| `tests/test_packaging.py` | `DeclaredScriptTest` —— 解析 `[project.scripts]` 并解析每个目标（见 §10） |
| `tests/test_client.py` | 基于 AST 的只读守卫：业务客户端只能到达 `GET` |
| `tests/test_cli.py` | 输出流被重新配置为 UTF-8；不可表示的字符不会导致崩溃；**每个 `EXIT_*` 常量唯一** |

---

## 10. 安全与正确性审计

### 只读承诺，以结构化方式证明

对整个 `src/` 树做 AST 扫描，只找到**恰好两个**变更型 HTTP 动词：

```
src\opencsi\auth\gitcode_qr.py:363  POST
src\opencsi\auth\gitcode_qr.py:412  POST
total 2
```

两者都是 GitCode **认证**端点（创建 challenge、换取凭据）。任何地方都不存在业务型
`POST`/`PUT`/`PATCH`/`DELETE`。`OpenCsiToolClient._attempt` 是唯一的业务请求路径，且始终
调用 `get_json`。`sync` 在源码中只作为响应字段名 `syncStatus` 出现。提交 `ded9f93` 加入了
这个结构化守卫，使它无法在一个恰好通过的行为测试背后悄悄回归。

### 敏感信息处理

`register_secret`、`scrub_text`、`Secret` 包装器、`redact_mapping`、`RedactingFilter` 与
`install_logging_redaction` 都已应用在续期/扫码路径上。`scene_id` 与扫码载荷在 `repr` 中
被脱敏。verbose 日志只打印请求**路径**——绝不打印 query string，绝不打印 cookie。

### 本轮发现并修复的七个真实缺陷

1. **图标卡在"续期中"。** `_maybe_renew` 在成功时直接返回，没有离开 `RENEWING`，导致托盘
   在一次已经成功的续期之后仍显示续期状态长达 5 分钟。由一个实测探针发现：在一个已经续期
   完成的会话上显示 `state: RENEWING`。
2. **类 docstring 承诺了一个并不存在的公开 `tick()`**（只有 `_tick_once`）。这正是缺陷 1
   得以存活的原因：不等待真实的 5 分钟定时器就无法驱动自治路径。修复方式是让 `tick()`
   真正公开。
3. **注意力锁存被实现成了比较。** 见 §8 —— 5 次轮询里弹了 5 次通知。
4. **冻结 EXE 把中文弄乱了。** `dist\opencsi.exe` 在代码页 936 下打印
   `OpenCSI | ������ / �ȴ��״θ���`，而 `python -m opencsi` 打印正常。根因：
   `_make_output_robust` 设置了 `errors="replace"`，却从未设置*编码*，而冻结构建不会
   遵循 `PYTHONIOENCODING`。修复方式是钉死 UTF-8。旧测试断言的是较弱的"会出现一个
   `?`"这一性质，这正是它一直通过的原因——那个测试记录了缺陷，而不是抓住缺陷。
5. **`pyproject.toml` 声明了一个并不存在的入口点。**
   `opencsi-monitor = "opencsi.tray.app:main"` 没有对应函数 → `AttributeError`。之所以
   没被发现，是因为机器上已安装的 `opencsi.exe` 早于该声明。修复后，`DeclaredScriptTest`
   现在会解析每一个声明的 script，因此缺失的目标会在测试套件里失败，而不是在用户那里
   失败。

缺陷 4 与缺陷 5 共享一个值得点名的形态：**测试检查的是声明，而不是产物。** 编码测试
检查的是输出*不崩溃*，而不是*正确*；而入口点能否解析则根本没有测试。两者现在都针对
真正运行的那个东西做测试。

6. **`QrProtocolError` 与 `ServerError` 共用了退出码 31。** 一个基于 31 分支的脚本无法
   区分 openCsiTool 的 5xx 与 GitCode 返回的畸形响应体——两个成因不同、修法也不同的失败。
   这正是本项目**已经**拒绝过一次的那种混淆：当时选择新增退出码 32，而不是把一个业务层
   失败塞进网络或服务端桶里。扫码协议错误现在使用退出码 33。

   它为什么能通过评审，才是真正有意思的部分：`ExitCodeTest` *确实*断言了"已记录的退出码
   互不相同"，但它**手工列举了六个常量**，因此在此之后新增的每一个常量在构造上就未被
   检查。现在该测试改为枚举模块中的 `EXIT_*` 整数，所以新增常量在加入的那一刻就被校验，
   而不是等到有人记得去扩展那个列表。验证方式：重新引入 `exit_code = 31`，新测试以
   `AssertionError: 31 == 31` 失败。扫码状态映射中两个裸字面量 `30`/`31` 也被替换为具名
   常量。

缺陷 4、5、6 本质上是同一个错误换了三件外衣：一个只枚举集合中*样本*、或者只检查声明而
不检查产物的测试，会在集合增长到超出它之后永远通过。同一个形态出现三次，那是模式，
不是巧合。

7. **冻结托盘忽略了自己的参数。** `opencsi-tray.exe --once` 什么都不打印，然后永远常驻：
   入口脚本直接丢弃了 `sys.argv`，总是启动阻塞式 GUI，于是"打印一次快照然后退出"静默
   变成了"跑一个托盘直到你杀了我"。这是通过运行**重新构建的二进制**、而不是源码树发现的。

   这是上述模式最尖锐的一个实例。所有既有测试都通过**控制台**二进制驱动
   `opencsi tray --once`，而它走 argparse，是正确的；没有任何测试触碰**窗口化**二进制
   自己的入口脚本。缺陷恰好活在这两者的缝隙里，而对源码树做再多测试也发现不了它——只有
   执行产物才能发现。

   这个回归测试的第一版不是失败，而是**把测试套件挂死了**，因为它只 stub 了 CLI，于是
   回归发生时调用了真正的 GUI 入口点。现在测试同时 stub 托盘入口，因此回归会在毫秒级
   失败。一个复现挂起的测试不是测试。

### 已处理的打包陷阱

* 可选依赖是**在函数内部惰性 import** 的，因此 `opencsi --help` 与 `opencsi doctor` 在
  未安装任何 extra 的机器上也能工作。PyInstaller 的静态分析看不穿这一点，会产出一个
  *构建成功*、然后在用户机器上失败的包。每一个此类 import 都列在 `hiddenimports` 里。
* `pip install -e . --no-deps` 在本环境下会在安装构建依赖时失败；需要
  `--no-build-isolation`。
* `opencsi-monitor` 入口点通过 editable 安装并实际运行来验证（进程 12308，常驻
  4.60 MB）。

---

## 11. 提交

自 `cf34c1b` 起共 **29 个提交**，下表列出其中 **26 个**（最旧在前，覆盖 `cf34c1b`
到 `12d77aa`）。全部以 `opencsi contributors <contributors@opencsi.invalid>` 署名。

被排除的是**三个纯文档提交**：它们撰写、修订本报告，并修正本报告对自身 SHA 的引用。
一个提交无法列出自己的 SHA，所以它们不可能出现在表里；`542fe4f` —— 最后一个修改源码的
提交 —— 是 §2 中命名的锚点。

| SHA | Subject |
| --- | --- |
| `cf34c1b` | refactor: separate credential reload from session renewal |
| `48f05a4` | feat: add silent browser OAuth session renewal |
| `5197a67` | feat: add login --status and login --renew, and teach doctor about renewal |
| `ad18b1d` | research: document the GitCode QR login protocol |
| `5ee2200` | feat: add a pure-HTTP GitCode QR login and terminal QR rendering |
| `faaf8c0` | feat: add the Windows 11 notification-area tray |
| `ad61bfb` | fix: report a cold-start renewal as renewed, not as already-valid or timed out |
| `a052019` | fix: make the tray's sign-in entry point actually work, and accept common flags |
| `36cc305` | fix(qr): stop claiming GitCode's login code is a scannable QR |
| `489b1d2` | build: produce standalone Windows binaries with PyInstaller |
| `7767932` | docs: document the session, QR and tray features; fix a status lie |
| `addcaa0` | fix(tray): make the "Sign in..." menu item actually sign you in |
| `3c9926d` | fix(tray): keep the sign-in poll off the monitor's worker thread |
| `22733e3` | fix(monitor): stop stranding the icon on "Renewing", and notify once |
| `1213836` | docs: document the autonomous renewal path and the notification policy |
| `23c1d10` | test(tray): cover the notification path and split a mis-nested test class |
| `033b5d4` | feat(tray): show usage in Chinese units, and keep the menu exact |
| `6344683` | fix(packaging): stop the frozen EXE mangling Chinese output |
| `a81e7eb` | fix(tray): add the missing opencsi-monitor entry point |
| `80d1f14` | docs: correct the test count in the architecture overview |
| `ded9f93` | test(client): assert the read-only promise structurally, not just behaviourally |
| `73b10f0` | test(tray): lock the icon's colour and shape semantics |
| `5ab3ad3` | docs: refresh the architecture test count to 651 |
| `39967ad` | fix(errors): stop the QR protocol error borrowing the server-error code |
| `542fe4f` | fix(packaging): make the frozen tray honour its own arguments |
| `12d77aa` | docs: record the frozen tray's arguments and the two new exit codes |

---

## 12. 用户使用说明

### 安装

```powershell
git clone https://github.com/jingchangshi/OpenCSIToolMonitor
cd OpenCSIToolMonitor
pip install -e . --no-deps --no-build-isolation
```

核心包有**零**运行时依赖。只有在你需要桌面图标时才安装 tray extra：

```powershell
pip install "opencsi[tray]"
```

### 登录

```powershell
opencsi login              # 打开浏览器（默认）
opencsi login --qr         # 不用浏览器：扫微信码
opencsi login --manual     # 自己粘贴 cookie
opencsi login --status     # 只报告会话状态，不做任何改动
```

### 保持会话存活

只要某条命令或托盘发现会话距离过期不足 5 分钟，续期就会自动发生。无需任何配置。

```powershell
opencsi login --renew                  # 立即强制一次静默续期
opencsi login --renew --renew-timeout 60
opencsi status --no-renew              # 不续期，只报告过期时间
```

`$env:OPENCSI_NO_RENEW=1` 可全局关闭续期。

### 运行托盘

```powershell
opencsi tray                        # 显示图标
opencsi tray --once                 # 取一次快照，打印后退出
opencsi tray --check                # 自检能否启动
opencsi tray --install-startup      # 开机自启
opencsi tray --remove-startup
opencsi tray --startup-status
```

或者运行独立二进制，它完全不需要 Python。它接受与子命令相同的参数，`tray` 一词可省略：

```powershell
dist\opencsi-tray.exe                      # 显示图标（无控制台窗口）
dist\opencsi-tray.exe --once               # 取一次快照，打印后退出
dist\opencsi-tray.exe --check              # 自检能否启动
dist\opencsi-tray.exe --startup-status     # 查看开机自启项
```

裸启动会把冻结 EXE 自己的路径注册进 `--install-startup`，因此这台机器不需要把 Python
放进 `PATH` 就能保持托盘运行。

### 出问题时

```powershell
opencsi doctor --no-proxy
```

`--no-proxy` 很重要：`127.0.0.1:7890` 上的本地代理会**破坏**到 `opencsitool.com` 的 TLS。
`urllib` 会遵循 Windows 注册表里的代理设置，即使 `curl` 忽略它，所以这是开发机上最常见
的一类失败。

**会话大约持续一小时。** 这是站点的性质，不是这里的偷懒——不存在 refresh token。只要
托盘在运行，或者每小时至少跑过一条命令，你就不会察觉到。两者都没有时，你需要重新登录。

**退出码**（请基于这些分支，而不是基于文字描述）：

| 退出码 | 含义 |
| --- | --- |
| 0 | 成功 |
| 1 | 未分类错误 |
| 2 | 参数错误 / 客户端配置错误 / 托盘不可用 |
| 10 | CDP 端点不可用 |
| 11 | 没有可用的浏览器 target |
| 12 | 未登录（无 `token` cookie） |
| 13 | 会话已过期，或某次续期/扫码等待需要人工介入 |
| 20 | 权限不足（403） |
| 30 | 网络错误 |
| 31 | 服务端错误（HTTP 5xx） |
| 32 | 业务错误（HTTP 200，但 `code != 200`） |
| 33 | GitCode 扫码协议错误 |
| 130 | 被中断（Ctrl+C） |

完整表格位于 `src/opencsi/errors.py`，并被测试锁定为互不重复，因此任何两种失败模式都
不可能共用同一个退出码。
