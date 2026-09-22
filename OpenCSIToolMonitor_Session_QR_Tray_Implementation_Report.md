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
| 过期会话的静默自动续期 | **完成，实测证明（本轮第三次复核）** | 浸泡探针三次观测到真实余量跨越：`02:13:34`、`16:26:51`（312s→3600s），以及本轮 `17:22:37`（**272s**→3598s，`token_changed=True`，服务端接受 `shijingchang`）。三次都无人触碰、无交互登录 |
| 续期触发策略，且不存在无限循环 | **完成** | `expires_in > margin` → 不动作；`<= margin` → 静默续期；401 → 先 reload，再续期一次 |
| 静默续期不抢占用户焦点 | **完成** | `Target.createTarget` 创建**后台** target，完成后 `Target.closeTarget`；绝不导航用户当前页面 |
| GitCode 纯 CLI / 扫码登录可行性调研 | **完成** | `docs/gitcode-qr-protocol.md` —— 结论 `QR_FLOW_REPRODUCIBLE` |
| 实现 CLI 扫码登录 | **完成，实测验证** | `opencsi login --qr` 真实创建了 challenge、写出图片，未扫码时如实返回 `TIMEOUT`（退出码 13） |
| Windows 11 托盘 v1 | **完成，实测验证** | `opencsi tray --once` 打印真实快照；`--check` 报告 `tray: ok`，7 个菜单项（含「开机自启动」） |
| 浏览器缺失时用户可自救 | **完成，实测验证** | 杀掉 Chrome 后单条命令即恢复：`state: OK`，`EXIT=0`（见 §10 缺陷 8） |
| GitCode 授权页未确认时的正确报告 | **完成，实测（见 §10 缺陷 10 的边界说明）** | 真实站点确认了授权页会让旧代码报 `TIMEOUT`、且点「授权」即签发新 token；新状态的判定逻辑由离线脚本验证（真实站点无法按需复现该页面） |
| 测试 | **完成** | **751 项测试**（750 通过、1 跳过、125 个 subtest），`pytest` 与 `unittest` 双跑全绿 |
| Windows 实机验证 | **完成** | 实测 CLI、扫码、托盘、冻结二进制、入口点 |
| 文档 | **完成** | 5 份文档 + README + 本报告 |
| 规范提交 | **完成** | 49 个触碰源码/测试/探针的提交（§11 完整列出） |
| 实测发现并修复的缺陷 | **完成** | 19 个（§10 完整列出），通过两种方式发现：**运行真实产物**，以及修好一个缺陷后追问**"还有哪里会这样"**。缺陷 9–19 各自带一行显式的「发现方式」；缺陷 1–8 在正文里说明来源 |

一处必须如实声明的**非结论**：**扫码流程的最后一步无法机器验证。** 它需要真人用手
机扫描一个微信小程序码。本报告交付的代码证明了该物理动作之前的每一步，并且把超时
如实报告为可区分的退出码，而不是假装成功。

另一处必须如实声明的**边界**：**扫码登录只覆盖认证流程的前半段，浏览器无法被完全移除。**
本轮用探针实测确认：拿到 GitCode 凭据后，openCsiTool 的 `token` 仍必须由浏览器里的
OAuth 回调签发——`/oauth/authorize` 是客户端渲染的 SPA 外壳，纯 HTTP 客户端（哪怕带上
浏览器里**全部** 29 个 Cookie）只会拿到外壳。因此本轮的成果是**把浏览器从"必需认证源"
降级为"可选认证后端"**，而不是消除它。这与本 Goal 的原始措辞一致，细节见 §7 与
`docs/gitcode-qr-protocol.md` §9.1。

---

## 2. 最终 HEAD

最后一个修改**源码、测试或探针**的提交：

```
8946ebc  fix(tray): stop a second launch failing in total silence
```

最后一个修改 `src/` 的提交也是它：

```
8946ebc  fix(tray): stop a second launch failing in total silence
```

其后都是纯文档提交，包括承载本报告的提交。在这里写出那些提交是循环的——一个提交
无法包含它自己的 SHA——所以锚点取最后一个行为变更，这才是读者真正需要检出的东西。

> **这两个锚点已经修正过两次。** 原文写的是 `a48bce8` / `71e7e54`，之后又写成
> `72562a3` / `8ee38e0`，每次都随新提交过期。这正说明**手工维护"最新提交"这类锚点必然滞后**，
> 所以这里改为给出可复核的命令，而不是要求读者相信这两个 SHA：
>
> ```bash
> git log --oneline -1 -- src tests tools packaging   # 8946ebc
> git log --oneline -1 -- src                          # 8946ebc
> ```

本阶段的基线是 `cf34c1b~1`（`4739898`）。工作区干净；无未跟踪的临时文件；无残留进程；
注册表未留下 `Run` 项。

**二进制产物**（在最后一次源码变更后重新构建并重新验证）：

| 产物 | 大小 | 用途 |
| --- | --- | --- |
| `dist/opencsi.exe` | 16.3 MB | 命令行版 |
| `dist/opencsi-tray.exe` | 16.3 MB | 托盘版（无控制台窗口） |

两个二进制都在本轮改动后**重新构建**，并重新跑过实测：杀掉 Chrome 后，
`dist\opencsi.exe tray --once --no-proxy` 如实报 `BROWSER_UNAVAILABLE` 且退出码 10；
加上 `--auto-recover-browser` 则报 `state: OK` 并退出 0。窗口化版
`dist\opencsi-tray.exe --once` 在有限时间内退出并输出快照——缺陷 7 的回归仍然成立。

冻结产物在本轮还**独立复现了缺陷 14**：源码修好之后、二进制重建之前，用同一个
`dist\opencsi.exe tray --once --json` 取到的仍然是
`credential_expires_in_seconds = '<redacted>'`。这不是多余的步骤——它证明了那个字段
在**真实交付物**里确实是坏的，而不只是在解释器里。重建后同一条命令给出
`2159.6`。

做成两个二进制是刻意的：PyInstaller 的 `--windowed` 是按二进制设置的开关，而托盘
程序在开机自启时闪出一个黑色控制台窗口是不可接受的。

**onefile 引导器的一个副作用值得单独记下**（它直接导致了缺陷 19）：PyInstaller 的 onefile
构建会把真正的程序作为**子进程**运行，因此 `Stop-Process -Id` 或任务管理器"结束任务"
杀掉的是**引导器**，真正的托盘会作为**孤儿进程**留下来，并继续持有单实例互斥量、
继续拥有活的托盘图标窗口。实测：

```text
父进程 29680（引导器）── 子进程 43108（真正的托盘）
Stop-Process -Id 29680  →  43108 存活，仍持有互斥量
再次启动托盘            →  EXIT=2（被正确拒绝）
```

杀掉**子进程**则父进程也随之退出，一切正常。也就是说：单实例保护本身没有问题，
问题在于它拒绝时**一声不响**——见缺陷 19。

---

## 3. 根因

本阶段由三个真实缺陷驱动。三个缺陷都在修复前被复现，且每个修复都有能在旧代码上
失败的测试。

后续在真机运行中又发现并修复了十三个（缺陷 4–16，见 §10）。其中缺陷 8 与本节的三个不同：
它不是"缺少能力"，而是**已有的提示把用户引向了一个不可能完成的动作**。缺陷 10 则更
进一步——它报出的**分类本身就是错的**：把"等一次点击"说成了"超时，可能网络有问题"。

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

### 本轮再次跨越余量线，并暴露了探针自身的一个盲点

本轮重新跑了一次（`--minutes 78 --renewals 2 --interval 20`），结果是
`INCONCLUSIVE: only 0 of 2 renewals observed`。但**探针自己的日志显示寿命跳了两次**：

```
[15:21:06] OK               lifetime   2418s
[15:22:26] OK               lifetime   3557s   <- 跳升，说明签发了新 cookie
[15:31:47] OK               lifetime   3307s
[15:33:07] OK               lifetime   3471s   <- 又一次跳升
```

寿命只可能自行下降，因此**跳升就是新 cookie 已签发的证据**。那两次续期是真的，只是
不经过探针的包装器——本轮我在跑 soak 的同时还跑了 `doctor` 与 `login --renew`，它们
经**另一条路径**续了同一个 cookie，计数器因此始终为 0。

探针现在同时统计"跳升"，并把两个数字分开报告。修正后重跑，干净地跨过了余量线：

```
start lifetime : 613s
renew margin   : 300s (production value)

[16:24:46] OK               lifetime    372s
[16:25:46] OK               lifetime    312s   <- 距余量线 12 秒
[16:26:51] RENEWED  token_changed=True
[16:26:51] OK               lifetime   3600s   <- 回到满值
```

会话在**无人触碰**的情况下从 312 秒回到 3600 秒，服务端随后接受该会话
（`login --status` → `Status : OK`，`Cookie lifetime left : 55m57s`）。

值得记下的是那个 `INCONCLUSIVE` 本身：它**不是**一次失败的 soak，而是一次**成功被
看不见**的 soak。对一个正常工作的系统给出"无法判定"，与之前修掉的那些状态检查属于
同一类错误——只是这次错在探针里，而不是产品里。

### 探针修正后再跨一次余量线，并暴露了修正自身的**反向**缺陷

修好跳升计数后重跑一次 78 分钟 soak（`--minutes 60 --renewals 2 --interval 15`），
干净地跨过了余量线：

```
[17:21:33] OK               lifetime    317s
[17:22:37] RENEWED  token_changed=True
[17:22:37] LIFETIME JUMPED 272s -> 3598s (a new cookie was issued)
final check    : server accepted the session (shijingchang)
```

寿命降到 **272 秒**（余量线 300s），无人触碰地续期并回到 3598 秒，服务端继续接受该会话。
这满足 §52 要求的"至少跨过一次 openCsiTool token 到期"。

但这次输出的汇总行暴露了**修正本身带来的新缺陷**（已记为缺陷 18）：
`renewals + jumps` 把**同一个事件**算了两遍——两行时间戳完全相同，就是一次续期。
它打印 `VERIFIED: 2 silent renewal(s) ... (1 performed by this process, 1 observed
as lifetime jumps)` 并以 0 退出，而真实数字是 1。

**这是本轮唯一一个由探针自身输出暴露、而不是由产品行为暴露的缺陷**，也是本项目反复在
产品代码里找的那种形态出现在探针里：**退出码断言的结论比证据支持的更强**。修法是给每次
跳升归因，并打印三个数字让算术可核对。缺陷 18 记有完整细节与回归测试。

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

**本轮把这个边界从"提示"升级为"实测结论"**（探针 `tools/probe_oauth_pure_http.py`，
GET only）。把浏览器 Cookie 装进 `CookieJar` 后用纯 HTTP 跟随 OAuth 入口，两种场景都
拿不到 `token`：

| 场景 | 装入 Cookie | 最终落点 | 响应 | 拿到 token |
| --- | --- | --- | --- | --- |
| 只装 GitCode SSO | 3 | `gitcode.com/oauth/authorize` | 200 | **否** |
| 装全部浏览器 Cookie（排除 `token`） | 29 | 同上 | 200 | **否** |

两次响应**逐字节相同**：5793 字节、11 个 `<script>`、无重定向、无 `Set-Cookie`。
因此阻断原因**不是缺某个 Cookie**（29 个全带上也一样），也**不是 CAPTCHA**，而是
`/oauth/authorize` 是一个**客户端渲染的 SPA 外壳**——"是否自动批准"的判断发生在
JavaScript 里。结论：`QR_FLOW_REPRODUCIBLE` 只覆盖第一段（GitCode 扫码），**浏览器无法被
完全移除，只能退化为可选认证后端**，这正是本 Goal 的原始措辞所允许的。详见
`docs/gitcode-qr-protocol.md` §9.1。

> 记录一个我差点写错的地方：探针最初只在响应体里 `grep` 关键字，看到 `captcha` 就倾向于
> 把它当成阻断原因。加上"该关键字出现在 `<script>` 内部还是页面标记里"的判定后发现它在
> script bundle **内部**，是某个库的名字。仅凭"关键字出现过"就宣布阻断原因，与本项目此前
> 几次"断言自己没有观测过的事实"是同一类错误；探针现在打印**出处**而非仅仅命中。

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
751 项测试可以离线运行，而托盘本身在真机上验证。

**状态**（`MonitorState`）：`STARTING`、`OK`、`REFRESHING`、`RENEWING`、
`LOGIN_REQUIRED`、`CONSENT_REQUIRED`、`BROWSER_UNAVAILABLE`、`OFFLINE`、
`SERVER_ERROR`、`AUTH_ERROR`
—— 对应中文标签
`启动中 / 正常 / 刷新中 / 续期中 / 需要登录 / 需要授权确认 / 浏览器未运行 / 离线 / 服务异常 / 会话失效`。

**`需要登录` 与 `浏览器未运行` 必须分开。** 前者是 GitCode 登录态没了，后者是持有登录态的
那个浏览器没在运行；修复动作一个是"重新认证"，另一个是"把进程启动起来"。早先两者被合并，
于是产生了一个**没有出口的死循环**（见 §10 缺陷八）：托盘说"需要登录"→ 登录动作调用系统
默认浏览器（没有调试端口）→ Cookie 写进本工具读不到的地方 → 下一次轮询又说"需要登录"。
现在 `浏览器未运行` 的第一项菜单是 `启动浏览器并登录`，`opencsi login` 也走同一条路径。

**`需要授权确认` 也必须独立存在。** 它与"需要登录"的区别是修复动作的大小：GitCode 的
SSO 会话**仍然有效**，页面已经渲染出"授权 OpenCsitool S shijingchang"，只差一次点击。
把一个已登录用户送去"重新登录"，是让他去做一件修不好任何事的工作——与上面那次合并属于
同一类错误。因此该状态的菜单是 `打开页面并批准授权` + `重试静默续期`，而不是 `登录`。
它同时被放进 `_ATTENTION_STATES`：这是全部状态里**最容易修好**的一个，而且它永远不会
自行消失，所以恰恰最不该被托盘静默略过。

**刻意为之的精度不对称。** tooltip 做压缩（`36.3亿 tokens`）；菜单显示精确数字
（`3,634,408,185 tokens / 27,788 次请求`）。tooltip 受 Windows shell 限制为 127 个字符，
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
total tokens: 3,634,408,185
requests:     27,788
pull requests:  249
generated:    3,150 lines
adopted:      120 lines
adoption:     3.8%
server data:  2026-09-21T20:32:04+08:00
EXIT=0

$ opencsi tray --check --no-proxy
tray: ok
state: STARTING
menu items: 7
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

**751 项测试：750 通过，1 跳过，125 个 subtest 通过。**

```
$ pytest
750 passed, 1 skipped, 125 subtests passed in 39.62s

$ python -m unittest discover -s tests -q
Ran 751 tests in 39.910s
OK (skipped=1)
```

两个 runner 在同一棵代码树上都是绿的。整个测试套件完全**离线**运行——没有任何测试
触碰网络；§5–§8 中的实测证据来自探针脚本与手动运行，而不是来自测试套件。

> **两个 runner 都必须跑。** 这不是形式主义。本轮把 `opencsi.tray.__main__.main` 改成会
> 转发参数之后，`pytest` 依然全绿，而 `python -m unittest discover` 报了 2 个失败——因为
> 有两个旧测试调用 `entry.main()` 并依赖"环境里的 `sys.argv` 恰好是空的"。这在 pytest 下
> 成立，在 `unittest discover` 下不成立（argv 里带着 `discover -s tests`）。那是测试对运行
> 环境的假设，不是产品缺陷，但如果只跑 pytest，它会一直藏着。

本阶段新增覆盖：

| 区域 | 锁定了什么 |
| --- | --- |
| `tests/test_monitor.py` | `AttentionNotificationTest`（8 项）：每次状态转换一个气泡、重复不弹、恢复后重新武装、`tick()` 是公开的 |
| `tests/test_monitor.py` | `RENEWING` 卡死回归、空操作续期、成功时 `RENEWING` → `OK` |
| `tests/test_monitor.py` | `BrowserRecoveryTest`（8 项）：默认不启动浏览器、冷却期生效、同一轮只重试一次、网络故障不触发启动 |
| `tests/test_monitor.py` | 授权页未确认 → `CONSENT_REQUIRED`（而非 `LOGIN_REQUIRED`），且**真的**触发 attention 回调 |
| `tests/test_oauth_renewal.py` | 授权页不被误报为 `TIMEOUT`、detail 不含旧有的"slow/unreachable"措辞、检测**提前**结束等待（而不只是在截止时改标签）、探针只返回布尔值不读页面文本、非成功路径同样关闭 target |
| `tests/test_oauth_renewal.py` | 探针的批准词表必须真的包含真实页面上的 `授权`，且**不含** `取消`（否则"用户点了取消"会被当成"正在等待用户"） |
| `tests/test_cli_session.py` | `CONSENT_REQUIRED` 的退出码是会话码而非网络码，且消息不再说"SSO 会话没了"；`_RENEWAL_EXIT` 对 `RenewalStatus` **穷尽** |
| `tests/test_cli_session.py` | `SsoPresenceTest`（4 项）：没有 GitCode Cookie 时不得声称 "SSO available"、读不到时不得断言用户已登出、Cookie 值绝不进入报告 |
| `tests/test_tray.py` | `CONSENT_REQUIRED` 的退出码不是 `EXIT_SERVER_ERROR`（服务器没坏，用户有一个按钮要按） |
| `tests/test_tray.py` | `ChineseUnitTest`、`NotificationTest`、`SignInActionTest`、图标颜色/形状语义 |
| `tests/test_tray.py` | 每个 `_ATTENTION_STATES` 成员都必须**真的有话可说**（见 §10 缺陷八） |
| `tests/test_tray.py` | `CONSENT_REQUIRED` 的菜单与 `LOGIN_REQUIRED` **不同**，且提供批准动作而非纯登录 |
| `tests/test_redaction.py` | 凭据名下的**数字**不得被脱敏成 `"<redacted>"`（托盘凭据剩余寿命字段），而凭据名下的**字符串**仍必须被掩码 |
| `tests/test_tray.py` | `tray --once` 的**文本**输出必须带上会话寿命，且未知时**整行省略**（打印 0 会被读成已过期） |
| `tests/test_tray.py` | `_once_exit_code` 的 catch-all **成员被钉死**：新增状态必须显式决定它是否真该被报成服务端故障 |
| `tests/test_cli_session.py` | `_qr_exit_codes()` 对 `QrLoginStatus` **穷尽**，且任何失败状态都不得映射到 0 |
| `tests/test_browser_launch.py` | 复用而非重复启动、失败不抛异常、`open_or_launch` 的三条分支 |
| `tests/test_packaging.py` | `DeclaredScriptTest` —— 解析 `[project.scripts]` 并解析每个目标（见 §10） |
| `tests/test_packaging.py` | `TrayEntryArgumentsTest`（7 项）—— 三种入口点都必须转发参数；**并且回归时快速失败而不是挂起** |
| `tests/test_packaging.py` | `LiveProbeTest` —— 每个实测探针必须自我说明且声明其安全边界 |
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

### 本轮发现并修复的十九个真实缺陷

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

8. **"浏览器未运行"被当成"需要登录"上报，构成一个没有出口的死循环。** 这是本轮在真机上
   发现的、也是八个缺陷里唯一一个**用户完全无法自救**的：`CDP_UNAVAILABLE` 与
   `NO_BROWSER_TARGET` 都映射到 `LOGIN_REQUIRED`，于是托盘让你去登录；而登录动作调用的是
   `webbrowser.open()`，它启动**系统默认浏览器**、**没有** `--remote-debugging-port`。
   你登录成功，Cookie 却写进了本工具读不到的地方，下一次轮询又是"需要登录"。

   **点多少次都会回到原点。** 这不是"提示不够精确"，而是**给出的建议不可能被遵循**：
   在浏览器没有以调试端口启动时，告诉用户"去登录"是无法完成的操作。

   实测复现（Chrome 完全退出）：

   ```
   $ opencsi login --no-proxy
   opened https://opencsitool.com/myTools
   ... no DevTools endpoint found
   EXIT=10                      ← 与开始时同一个错误
   ```

   修复横跨六个提交（`3ab6cb1`、`8d02ba1`、`7776f4a`、`64f7a97`、`5253d3f`、
   `442d904`），按层次拆开：

   * 新增 `auth/browser_launch.py`，让"启动一个浏览器"这件事第一次真的有人做；
   * `MonitorState.BROWSER_UNAVAILABLE` 把两种成因分开，托盘菜单在该状态下第一项是
     `启动浏览器并登录`；
   * `opencsi login` 与托盘的登录项都改走 `open_or_launch`，启动一个**本工具能读**的
     浏览器（专用 `--user-data-dir` + `--remote-debugging-port`）；
   * 可选 `--auto-recover-browser` 让开机后的常见情形**自行恢复**（默认关闭——它会往
     桌面上弹一个窗口，不该擅自替用户决定）。

   修复后实测（先杀掉 Chrome，再执行一条命令）：

   ```
   $ opencsi tray --once --no-proxy --auto-recover-browser
   state: OK
   total tokens: 3,634,402,985
   requests:     27,758
   EXIT=0
   ```

   **顺带暴露出的第二个同类错误**：`tray --once` 的退出码用 `return 20` 兜底，于是所有
   未被显式列举的状态——包括这个——都被报成"权限不足"。退出码是公开契约，因此现在改成
   具名函数 `_once_exit_code` 并直接测试，`20` 只保留给真正的权限拒绝。

   **第三个**：`BROWSER_UNAVAILABLE` 加进了 `_ATTENTION_STATES`（决定**何时**弹），但
   `_notify_attention`（决定**弹什么**）没有对应文案，于是该状态被静默静音——恰恰是刚开机、
   用户最不会察觉托盘没在工作的时候。现在有一个测试遍历 `_ATTENTION_STATES` 的每个成员，
   要求每个都产生恰好一个气泡，因此"只加一半"会在测试里立刻失败。

   三个修复都先在旧代码上验证过会失败：`AssertionError: LOGIN_REQUIRED is not
   BROWSER_UNAVAILABLE`、`AssertionError: 20 != 10`、`AssertionError: 0 != 1`。

### 缺陷 9：`opencsi-monitor --help` 永久挂起

**发现方式：运行真实产物。** 直接执行 `opencsi-monitor --help`，它什么都不打印然后
永久停在通知区域。`python -m opencsi.tray --help` 同样如此。两个入口点都丢弃
`sys.argv` 并直接启动常驻 GUI。

**这是同一个缺陷的第二次出现。** 它此前被报告并修复过一次（缺陷 7），而修复被写进了
`packaging/tray_entry.py`——错的地方。那里并不是共享逻辑，而是**复制**了一份逻辑，所以
修好一个副本之后，另外两个仍然是坏的，而且没有任何测试覆盖它们：全部测试都在驱动
`opencsi tray`，那条路径走 argparse，从来不受影响。

修复方式是把逻辑收敛到唯一一处：`opencsi.tray.__main__.main` 接受一个可选的显式 argv，
冻结入口脚本只调用它、自己不再做任何判断。有一个测试断言入口脚本**没有**重新实现那个
分支，因此这种复制不会再回来。

**在证明过程中发现了两个测试质量本身的问题**，都值得记录：

其一：把缺陷重新放回去时，新测试**挂起**而不是失败——因为回退路径是一个真实的 pystray
消息循环。一个在 bug 回来时挂起的测试，比没有测试更糟：它什么都不报告，还会卡住整个
套件，而这正是该缺陷能存活两轮的原因。现在测试会把 `TrayApp` 替换成抛异常的桩，于是
回归在 **0.7 秒**内失败，而不是超时。

其二：两个旧测试调用 `entry.main()` 并依赖"环境里的 `sys.argv` 恰好是空的"。这在 pytest
下成立，在 `unittest discover` 下不成立——于是 `python -m unittest discover` 会因为这个
**并不存在的缺陷**而失败，而 pytest 全绿。现在它们显式传入空 argv，并新增一个测试覆盖
真正读取 `sys.argv` 的模块调用路径。

### 缺陷 10：GitCode 授权页未确认时，被误报为"超时"

**发现方式：运行真实产物。** 探针在真实站点上把整个 OAuth 往返打印了出来，看到标签停在 `gitcode.com/oauth/authorize` 且页面已渲染出授权按钮，而结果却是 `TIMEOUT`。

静默续期会以一个**每一条都不成立**的消息失败：

```
the OAuth round-trip did not finish inside the budget; the browser may be
slow or GitCode may be unreachable
```

浏览器并不慢（它处于空闲），GitCode 也没有不可达（它立刻就答复了）——它答复的是一个
OAuth **授权确认页**（`授权 OpenCsitool S shijingchang`），该页面会无限期等待一次点击。
标签停在 `gitcode.com/oauth/authorize`，而这与"仍在跳转中"的标签是**同一个 URL**，所以
续期器无法区分"还在重定向"与"在等人"，只能一直轮询到预算耗尽。

用诊断探针直接测量：手工批准该页面后**立刻**签发了新的 60 分钟 token。整条流程距离成功
只差一次点击，而工具坚持说自己超时了。

这个状态在结果里也与真正的超时无法区分，于是每一个调用方都继承了这个误诊：`doctor`
责怪网络，托盘不提供任何动作，`login --renew` 退出码 30（`EXIT_NETWORK_ERROR`）——
而这个状况与网络毫无关系。

现在改为**询问文档**是否存在批准控件，而不是对 URL 做模式匹配，因为 URL 确实无法区分
这两种情况。探针刻意保持通用（按钮与 submit 输入中标签为批准词的控件），因此不依赖
GitCode 的前端改动；它只返回布尔值，所以任何页面文本都不会进入日志——授权页上显示着
已登录的账号名，这一点很重要。

**它绝不点击。** 代替用户批准一个 OAuth 授权，是用户自己的决定；一个后台监控程序悄悄
扩大自己的权限，恰恰是本项目绝不能有的行为。探针存在的目的是让工具能**说出**卡在哪，
而不是让它自己继续下去。补救动作以菜单项的形式交给用户。

新增的 `CONSENT_REQUIRED` 状态与 `LOGIN_REQUIRED` **刻意分开**，因为补救动作小得多：
SSO 会话仍然有效，没有任何东西需要重新登录。让一个已经通过认证的用户去"重新登录"，
是让他做一件修不好任何事的工作——与前面 `BROWSER_UNAVAILABLE` / `LOGIN_REQUIRED` 那次
合并属于同一类错误。它被放进 `_ATTENTION_STATES`，因为它是全部状态里最容易修好的一个，
而且永远不会自行消失。

检测在**连续第二次**看到该表单时就跳出轮询循环，因此 30 秒的预算大约 1 秒就返回。有一
个测试直接断言这个提前退出，否则"只是在截止时改了标签"的实现也会通过。

**实测证据的边界，必须如实说明。** 本缺陷的实测分成两半，两半的强度不同：

| 观测 | 方式 | 结论 |
| --- | --- | --- |
| 授权页确实会让**旧**代码报 `TIMEOUT` | **真实站点** | 探针打印出标签停在 `gitcode.com/oauth/authorize`、页面文字为 `授权 OpenCsitool S shijingchang`、按钮为 `['取消','授权']`，而结果是 `TIMEOUT` 与"浏览器可能很慢" |
| 手工点「授权」确实能拿到新 token | **真实站点** | 点下之后立刻回到 `opencsitool.com/`，`Storage.getCookies` 出现 `token`，剩余 60.0 分钟 |
| 新代码对该页面报 `CONSENT_REQUIRED` 并提前退出 | **脚本化的假 DevTools 服务器** | `consent` 场景与 `timeout` 场景**停在同一个 URL**，因此只靠路径无法区分；测试断言状态、detail 不含 "slow/unreachable"、以及在 30 秒预算下 15 秒内返回 |

第三行**没有**在真实站点上复现，原因是做不到：GitCode 的 `/setting/oauth` 页面
（`OAuth应用 → 已授权应用`）显示"暂无数据"，没有撤销授权入口，删除 openCsiTool 的
`token` cookie 也不影响授权——授权绑定在 GitCode SSO 上，删除后重跑 OAuth 依然直接
通过（实测：`status: RENEWED`）。因此无法按需把真实站点重新推回授权页。

换句话说：**缺陷的存在与危害是真实站点实测的，修复的判定逻辑是离线脚本验证的。**
两者之间由"同一个 URL 承载两种状态"这一事实连接——这正是必须询问页面而不是匹配 URL
的原因。把检测逻辑改回去会让 3 个新测试失败。

### 缺陷 11：同一条授权页，在**另一条代码路径**上仍然被误报

**发现方式：追问"还有哪里会这样"。** 缺陷 10 的修复全绿之后才被发现的——去问还有哪条代码路径能观察到同一个授权页。

缺陷 10 修好之后，问了一个问题：**还有哪些代码路径能观察到这个授权页？**

答案是"反应式路径"——收到 HTTP 401 之后，客户端会先重载凭据、再重跑 OAuth，而那次
往返同样会停在同一个授权页上。但客户端最终抛出的是一个普通的 `SessionExpiredError`，
`state_for_error` 把它映射为 `AUTH_ERROR`（"会话过期，去续期"）。于是托盘显示的是一个
通用的"会话失效"，而它提供的补救动作**每次都必然失败**——续期会再次停在同一个没人确认
的表单上。

这是与缺陷 10 完全相同的错误，出现在缺陷 10 没有覆盖到的那个位置：反应式路径从来没有
被告知这个新状态。它之所以被发现，不是因为测试跑绿了，而是因为主动去问"还有哪里会看到
它"。

`_classify_failure` 现在在失败属于认证类时，优先采信客户端记录的续期结果。范围刻意收窄：

* 只有 `AUTH_ERROR` 有资格被升级，因此网络抖动仍然报网络问题——即使上一次续期恰好提到
  了授权页；
* 只有补救动作不同于"重试"的两个状态会被采用（`CONSENT_REQUIRED`、`LOGIN_REQUIRED`）；
* `last_renewal` 缺失或不可读时退回原有映射，因此不实现该属性的客户端行为完全不变。

验证方式是把新的分类逻辑绕开：两个测试以 `AUTH_ERROR is not LOGIN_REQUIRED` 失败，第三
个测试钉住了反向情形——授权尝试之后到来的网络故障**不得**被改标签，否则会把用户送去点
一个与问题无关的按钮。

### 缺陷 12：CLI 对一个**仍然登录着**的用户说"你的 SSO 会话没了"

**发现方式：追问"谁会消费这个分类"。** 顺着缺陷 11 的分类改动去查它的调用方，发现 `requires_interaction` 被当成了"已登出"的同义词。

`login --renew` 对任何"需要人介入"的结果都打印同一句话：

```
error: the GitCode SSO session is gone, so silent renewal cannot help.
       -> sign in at https://opencsitool.com/myTools
```

在 `LOGIN_REQUIRED` 是唯一带 `requires_interaction` 的状态时，这句话是对的。对
`CONSENT_REQUIRED` 则是错的：GitCode 刚刚渲染出一个**写着已登录用户名的授权页**，
SSO 会话显然还活着。照着这句提示去做，就是让用户在自己已经登录的情况下再去登录一次；
登录完授权仍未确认，下一次续期又停在同一个表单上。

根因是把 `requires_interaction` 当成了"已登出"的同义词。这个标志的真实含义是"需要人"，
而两个状态需要的是**不同的人做不同的事**，所以消息现在按状态选择，注释里也写明了为什么
不能用这个标志来做这件事。

同一轮还发现退出码映射 `_RENEWAL_EXIT.get(status, 1)` 没有穷尽性守卫：任何被遗漏的状态
会静默变成退出码 1，而那根本不是一个已文档化的续期结果。现在有一个测试断言
`RenewalStatus` 的每个成员都有条目，因此下一个新增状态会立刻失败，而不是被误报。

验证方式是把新分支停用：consent 测试以
`'approval' not found in 'error: the gitcode sso session is gone...'` 失败——正是用户此前
会看到的那句错话。

### 缺陷 13：状态检查声称"GitCode SSO 可用"，却从未去看过

**发现方式：追问"还有哪里在做同类断言"。** 逐个检查产品里所有关于 SSO 状态的断言，发现 `renewal_capability` 从未读取过它所声称的那个 Cookie。

`renewal_capability` 确认了浏览器级 DevTools WebSocket 能连上，然后返回这样一句理由：

```
GitCode SSO available; OAuth can be re-run in a background tab
```

后半句确实由前半句推出。但前半句推不出来：**WebSocket 能连上，并不说明浏览器里还有
GitCode 的登录态。** 这与本轮已经修过的那些错误属于同一类，而且出现在**报告健康状况**
的那个地方——`doctor` 打印 "GitCode SSO available"，`login --status` 打印 "available"，
而本轮新增的排查文档还告诉用户"看到这一行就表示恢复了"。

在**下一次续期就会停在授权页**的机器上，这三个地方会一起说"一切正常"。一个把坏的报成
好的状态检查，比不报更糟，因为用户会照着它行动。

SSO 登录态存在一个长期 Cookie 里，因此可以廉价检查，且不需要真的跑一次 OAuth 交换——
这正是 *capability* 检查被允许做的事。`_has_gitcode_sso` 从浏览器级 Cookie 存储里读取
Cookie **名字**，返回三态：

* `True` —— 存在 GitCode SSO Cookie；
* `False` —— Cookie 存储读到了，但里面没有，于是理由改为指出真正的阻塞点和修复方式；
* `None` —— 存储读不到，此时保留原来偏乐观的措辞。

三态是必要的。"我读不到"绝不能被报成"它不存在"，否则一次短暂的 DevTools 抖动就会变成
一条斩钉截铁的"你已登出"——与"没看就说正常"属于同一类错误。检查只看 Cookie 的名字，
有一个测试断言任何值都不可能进入理由字符串或 `as_dict()`。

实测：真实浏览器下 `doctor` 仍报 "GitCode SSO available"（Cookie 确实在），
`login --status` 报 "available"。把检查绕开则测试以
`'GitCode SSO available' unexpectedly found in ...` 失败。

### 缺陷 14：托盘把凭据剩余寿命脱敏成了 `"<redacted>"`

**发现方式：运行真实产物。** 在真实服务上跑冻结的 `dist\opencsi.exe tray --once --json`，看到文档承诺是数字的字段输出成了字符串 `"<redacted>"`。

托盘 `--json` 的字段表里写着 `credential_expires_in_seconds` 是一个数字，实际输出是：

```json
{"state": "OK", ..., "credential_expires_in_seconds": "<redacted>"}
```

这是**对着真实服务实测出来的**，而且不只是难看：脚本里写
`if snap["credential_expires_in_seconds"] < 300` 会直接抛 `TypeError`，或者更糟——
变成字符串比较而给出错误答案。

根因是 `redact_mapping` 里**两个分支各写各的**。`credentialish` 分支早已放行数字、
布尔和 `None`，并且注释写明了理由：凭据名下的数字是计数或时长，不是凭据。而
`containers` 分支（名字形如 `credential`、`credentials`）把**一切非 dict/list 的值**
都替换掉了，于是任何**名字里含** "credential" 的数值字段都被抹掉。

这与之前修过的过度脱敏是同一个失败，只是当时修在了**看见症状的地方**，没有修到**产生
症状的规则**上。现在两个分支共用同一条规则，按**值的类型**判定：字符串掩码、容器递归、
标量放行。

放宽是按类型放宽，不是一律放行——既有的"嵌套密钥仍被捕获"测试全部继续通过。新增：
`credential_expires_in_seconds` 与 `credential_count` 以数字形态通过、`credential` 下的
裸字符串仍被掩码、托盘自己的快照经真实序列化器往返后仍是数值。

验证方式是把该分支改回无条件掩码：两个新的端到端测试以
`'<redacted>' is not an instance of any of (int, float)` 失败。修复后在真实服务上实测为
`credential_expires_in_seconds = 2344.9`。

### 缺陷 15：`tray --once` 的文本输出漏掉了会话寿命

**发现方式：追问"还有哪里漏了"。** 在核查缺陷 14 的脱敏修复是否留下同类缺口时发现——脱敏没有缺口，但同一份输出的**文本**形式漏掉了会话寿命。

托盘菜单显示 `会话 32m`，`--json` 从一开始就输出 `credential_expires_in_seconds`，
唯独**纯文本**输出没有这一行。于是"我是不是快被要求重新登录了"这个唯一的预测性数字，
脚本能看到、悬停提示能看到，而**人**运行 `tray --once` 时看不到。

这是在检查缺陷 14 的脱敏修复是否留下同类缺口时发现的。脱敏没有缺口——这是**显示遗漏**，
不是掩码问题——但正是那次检查把它翻了出来。

新行复用托盘自己的 `format_duration`，因此两个界面对时长的措辞一致（`30m`，而不是
`1800s` 或 `0.5h`）；寿命未知时整行省略，而不是打印 `0` 或留空——那会被读成"已过期"。

两个测试：该行以托盘的措辞出现；值为 `None` 时不出现。删掉该行验证：
第一个以 `'credential:' not found in 'state: OK'` 失败。

### 缺陷 16：另外两处"静默兜底"没有被同样的守卫覆盖

**发现方式：追问"同一个守卫还有哪里没加"。** 穷尽性守卫只加在了产生症状的那张映射表上，去查还有哪些枚举映射同样以静默兜底结尾。

本轮为 `RenewalStatus` 加了穷尽性守卫（缺陷 12 的副产物）。但**同一个形状还有两处**：

* `login --qr` 以 `{...}.get(result.status, 1)` 结尾。新增一个 `QrLoginStatus` 会被报成
  退出码 1——而 1 并不是这个命令的任何一个已记录结果——且**所有测试仍会通过**。
* `_once_exit_code` 以无条件的 `return EXIT_SERVER_ERROR` 结尾。新增一个 `MonitorState`
  会被默默报成"服务端故障"，正是 `CONSENT_REQUIRED` 差一点踩中的那个坑。

这两处都不是假设。`CONSENT_REQUIRED` 就是本轮新增的状态，它与"被报成服务端故障"只隔
一行；当时能拦住它的，只是一个专门为它写的测试，而不是一条**通用**的守卫。

修法：QR 的映射提取为 `_qr_exit_codes()`，与 `_RENEWAL_EXIT` 一样断言穷尽；托盘的
catch-all 不是映射而是分支，无法用穷尽性断言，因此改为**钉死它的成员集合**——任何新状态
一旦落进去，测试就失败，直到有人公开决定"服务端故障"是否真的是对它最好的解释。

验证：删掉 `QrLoginStatus.TIMEOUT` 的映射 → `no exit code decided for: ['TIMEOUT']`；
把 `CONSENT_REQUIRED` 从显式分支移除 → 两个测试同时失败。

### 缺陷 17：README 里三处描述的是**重载**，而代码做的是**续期**

**发现方式：追问"代码改对了，文档改干净了吗"。** §14 明确要求修正"登录一次后就不用再登录"
这个错误假设。本轮回头**逐条核对** README 是否真的改到位，而不是只看它是否被改过。

三处都成立于初始提交 `4826150`，此后从未随认证层演进更新：

1. **FAQ「会话过期了怎么办」** 写的是"Cookie 有效期约 0.97 小时。过期后重新登录即可。
   本工具会在 Cookie 快过期时主动重新读取一次。"——这正好是本项目要纠正的那个误解：
   重读浏览器 Cookie 救不活已过期的会话，因为浏览器里那份同时也过期了。
2. **专用配置目录的说明** 写的是"之后 Cookie 会保存在这个目录里，后续使用就不用再登录了"。
   真正长期保留的是 **GitCode SSO 登录态**，它签发的 openCsiTool Cookie 仍然每小时过期。
3. **「重试策略」** 写的是"遇到 401 时，**只重新读取一次凭据**然后重试一次"——这是会话层
   出现**之前**的行为。代码现在走 `SessionManager.reload_then_renew()`：先重载，只有重载
   没拿到新 token 时才付出一次 OAuth 往返。

第 1 处尤其值得记录：它使**同一个文件内部自相矛盾**——同文的 `--renew` 一节（约 290 行之前）
正确地写了"续期成功的判据是旧 token ≠ 新 token 且新过期时间更晚"，而 FAQ 却告诉读者
"过期后重新登录即可"。先读 FAQ 的人会拿到错误的模型，而 FAQ 恰恰是遇到问题时最先看的地方。

第 3 处的修法做了**验证而非仅仅重读代码**：`test_reload_alone_can_satisfy_recovery` 断言
重载路径的导航次数为 **0**，`test_reload_falls_through_to_renewal` 断言重载拿到同一个 token
时 OAuth 往返**确实发生**。改后的 README 描述的是这两条断言所钉住的顺序。

### 缺陷 18：soak 探针把**一次**续期报成了**两次**，并以这个数字退出 0

**发现方式：运行真实产物——而且是本轮唯一一个由"探针自身的输出"暴露的缺陷。**
78 分钟的跨余量线 soak 成功跑完后打印：

```
[17:22:37] RENEWED  token_changed=True
[17:22:37] LIFETIME JUMPED 272s -> 3598s (a new cookie was issued)

renewals performed by this process: 1
lifetime jumps seen (any renewer) : 1

VERIFIED: 2 silent renewal(s) across real expiries (1 performed by this process,
1 observed as lifetime jumps), with no user interaction ...
```

两行时间戳**完全相同**，寿命从 272s 跳到 3598s——这是**一个**事件被打印了两次。
根因是两个计数器从来不独立：本进程执行的续期**既**会被 `session.renew` 的包装器计数，
**也**会让寿命随之跳升，所以 `renewals + jumps` 把每一次本地续期都算了两遍。

上一轮引入跳升计数是为了看见**别的进程**做的续期（包装器看不见它们），这个目的仍然正确；
错的是把两个计数器**直接相加**。修法：给每次跳升归因——若本次采样与上次采样之间发生过
本地续期，则该跳升就是那次续期，不计入外部计数；成功判据改为
`renewals + external_jumps`，并同时打印三个数字，让算术可以一眼核对而不必信任。

**这个缺陷值得单独列出，因为它是本项目反复在别人代码里找的那种形态，出现在了我自己的探针里：
退出码断言的结论比证据支持的更强。** 它以 0 退出，而那个数字错了一倍。回归测试
`test_the_soak_does_not_double_count_its_own_renewals` 钉住它——把求和改回
`renewals + jumps` 后该测试失败（`the double-counting sum is back`）。

**该次运行的真实结论不受影响，依然成立**：一次真实的、无人值守的跨余量线续期——寿命降到
272s（生产余量线 300s），随后 `RENEWED token_changed=True`，回到 3598s，服务端继续接受
该会话（`shijingchang`）。

### 缺陷 19：第二次启动托盘时**完全静默地**失败

**发现方式：运行真实产物。** 本轮收尾做机器清洁检查时发现残留了一个 `opencsi-tray.exe`
（PID 49192），追下去才暴露了这个缺陷。

**根因有两层，第二层才是用户真正撞到的那个。**

第一层：PyInstaller 的 **onefile** 引导器会把真正的程序作为**子进程**运行。因此
`Stop-Process -Id`（以及任务管理器的"结束任务"）杀掉的是**引导器**，真正的托盘作为
**孤儿进程**继续运行——实测它仍持有单实例互斥量，仍拥有两个活的
`SystemTrayIcon` 窗口。下一次启动于是被正确地拒绝。**这不是缺陷**，单实例保护工作正常。

第二层才是：**它以什么方式拒绝。** `TrayApp.run` 在
`log.error("another OpenCSI tray is already running")` 之后返回一个裸的 `2`。而在
`--windowed` 构建里既没有控制台，本模块的 docstring 又写着"日志文件是记录这些的地方"
——**但整个项目里根本没有配置任何日志文件**。于是这条消息谁也没收到：用户双击图标，
没有出现托盘，没有任何文字，退出码交给了一个并不存在的父进程。**与"程序坏了"完全无法区分**，
而恢复方法（去通知区域退出那个已经存在的图标）是猜不出来的。

修法：把"已在运行"变成一个**有名字的常量**让入口点能识别它，并在所有"退出前从未显示过图标"
的路径上弹出原生消息框。

**消息框出现的条件被刻意收窄，这里记录我第一次写错的地方。** 我最初按**平台**判断
（`sys.platform == "win32"`），这是错的：它会在任何在 Windows 上运行入口点的**测试**里
弹出一个**真正的模态对话框**，`test_tray.py` 因此挂起直到超时（实测 90 秒未返回，且能在
桌面上枚举到一个标题为 `OpenCSI Monitor` 的 `#32770` 窗口）。正确条件是
**`sys.stderr is None`**——那恰好就是 windowed 构建与 console 构建的区别：有控制台的用户
从他已经盯着的流上读到消息，而非交互式调用者永远不会被阻塞。

同时修掉了那句承诺日志文件的 docstring：**一条描述着并不存在的机制的注释，比没有注释更糟，
因为它让静默看起来像是故意的。**

验证（在**重新构建**的冻结产物上）：让孤儿进程占住互斥量后再次窗口化启动，现在会弹出
标题为 `OpenCSI Monitor` 的对话框并说明恢复步骤，关闭后进程干净退出；`tray --check` 与
`tray --once` 行为不变。

### 缺陷 1–19 的共同形态

缺陷 4、5、6 是"测试检查声明而非产物"；缺陷 7 与 9 是"测试只覆盖了其中一个入口点"；
缺陷 8 是"测试只覆盖了状态映射，没覆盖该状态下**动作能否达成目的**"；缺陷 10、11、12 是
"测试只覆盖了错误**分类**，没覆盖该分类是否**描述事实**，以及是否覆盖了**所有**能产生
该错误的路径与**所有**会消费它的调用方"；缺陷 13 是"状态检查断言了一个**它从未观测过的
事实**"；缺陷 14、15 与 16 是"同一条规则写在两个地方，只修了看见症状的那一处"；
缺陷 17 是"代码改对了，但文档仍描述旧行为"；缺陷 18 是"**度量工具本身算错了**，并以错误的数字宣布成功"；缺陷 19 是"**失败时一声不响**——错误路径写了日志，但那个日志文件并不存在"。

上述形态其实是同一件事：**测试断言的是代码写了什么，而不是用户能否得到他要的东西。**
缺陷 8 的每一个子缺陷都在既有测试的射程之外——`_CODE_STATE` 的映射有测试、退出码的
*唯一性*有测试、`_ATTENTION_STATES` 的*内容*有测试，但没有任何测试问过
"被报成 `LOGIN_REQUIRED` 之后，用户照着做能不能恢复"。缺陷 10 同样：`TIMEOUT` 这个
状态有测试、它的退出码有测试，但没有任何测试问过"这一次真的是超时吗"。

缺陷 11 与 12 额外说明了一件事：**修好一个缺陷之后要问"还有哪里会这样"，而不是"测试绿
了吗"。** 缺陷 10 的修复让所有测试通过，也让实测通过；如果就此收工，反应式路径会继续
误报（缺陷 11），CLI 会继续对已登录用户说"你登出"（缺陷 12）。这两个都比原缺陷更难复现：
一个只在 401 恰好撞上授权页时出现，另一个只在人真的去看错误输出时才被发现。

这也是为什么它们只能靠真机运行发现：整条路径（点菜单 → 启动浏览器 → 读 Cookie → 再轮询）
跨越了进程边界，离线测试套件在构造上就到不了那里。缺陷 9 更极端——它甚至不需要真机，
只需要**换一个入口点**运行一次。

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

自 `cf34c1b` 起，**触碰了"测试能够断言的源码"的提交共 49 个**，下表完整列出（最旧在前，
覆盖 `cf34c1b` 到 `8946ebc`）。全部以
`opencsi contributors <contributors@opencsi.invalid>` 署名。

判定标准是机械的，共四个目录：

```bash
git log --oneline cf34c1b~1..HEAD -- src tests packaging tools   # 49 行，即下表
```

`tools/` 之所以算在内，是因为 `tests/test_packaging.py` 的 `LiveProbeTest` 会断言
**每一个** `tools/probe_*.py` 的 docstring 都声明了自身的安全姿态——它们是被测试断言的
源码，不是随手脚本。此前的标准漏掉了 `tools/`，于是"声明的数量"和"列出的行"对不上：
表里有一个只碰 `tools/` 的提交，却漏掉另外两个。现在两处都由上表统一给出。

表中没有、也不可能有的是**纯文档提交**：它们撰写、修订本报告，修正本报告对自身 SHA 的
引用，并把报告改写为中文。一个提交无法列出自己的 SHA，所以它们不可能出现在表里；
`71e7e54` —— 最后一个触碰这四个目录的提交 —— 是 §2 中命名的锚点。

有两个条目（`ad18b1d`、`1213836`）同时改了 `docs/`，但它们各自还带进了 `tools/` 下的
探针，因此按上面的标准属于本表；这一点写出来，免得读者以为标准被临时放宽过。

这里刻意**不写"总提交数"**：那个数字每写一次文档提交就会失效，而写它的正是文档提交
本身。46 则是稳定的——纯文档提交不碰这四个目录，所以这个数字不会被本节自身的修订改变。

因此上表可以被独立复核，而不必相信这段文字。

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
| `ded9f93` | test(client): assert the read-only promise structurally, not just behaviourally |
| `73b10f0` | test(tray): lock the icon's colour and shape semantics |
| `39967ad` | fix(errors): stop the QR protocol error borrowing the server-error code |
| `542fe4f` | fix(packaging): make the frozen tray honour its own arguments |
| `3ab6cb1` | feat(auth): add a browser launcher, so "start a browser" is actionable |
| `8d02ba1` | fix(monitor): stop reporting a missing browser as "login required" |
| `7776f4a` | fix(cli): make `opencsi login` start a browser the tool can actually read |
| `64f7a97` | fix(tray): make the tray's sign-in open a browser it can read too |
| `5253d3f` | fix(tray): give the browser-unavailable state something to say |
| `442d904` | feat(monitor): opt-in automatic recovery from a missing browser |
| `2730aed` | test(tools): prove the renewal gate stays shut, not just that it opens |
| `a4b6de1` | feat(tray): add the "Start with Windows" menu item (搂28) |
| `9b6c6d7` | fix(tray): stop discarding argv in two of the three tray entry points |
| `9bae2be` | fix(auth): report an unanswered GitCode consent page instead of a timeout |
| `a48bce8` | test: cover the consent state end to end, and the tray menu it produces |
| `27528e8` | fix(monitor): classify a 401 by what renewal said, not just by the HTTP status |
| `9115237` | fix(cli): stop telling a signed-in user that their SSO session is gone |
| `430148e` | test(auth): anchor the consent probe to the labels the real page shows |
| `2d5f2ad` | test(auth): run the consent probe's JavaScript in a real browser |
| `57ced08` | test(tray): pin the consent state's exit code to the session code |
| `848faaa` | fix(auth): stop claiming "GitCode SSO available" without looking for it |
| `cc68bff` | fix(redaction): stop masking the tray's credential lifetime as "<redacted>" |
| `54904d4` | test(tools): audit every JSON command for over-masking, not just the one I hit |
| `cfaf2a7` | feat(tray): show the session lifetime in `tray --once` text output too |
| `8ee38e0` | test(cli): guard the QR exit-code map the way the renewal one is guarded |
| `f400c90` | test(tray): pin which monitor states may reach the exit-code catch-all |
| `71e7e54` | fix(tools): make the soak see renewals it did not perform itself |
| `ea735a4` | research: prove the openCsiTool OAuth leg is browser-bound, not cookie-bound |
| `72562a3` | fix(tools): stop the soak counting one renewal as two |
| `8946ebc` | fix(tray): stop a second launch failing in total silence |

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
