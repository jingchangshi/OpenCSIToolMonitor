# 架构

本文说明 opencsi 的分层设计，以及**为什么**要这样分层。

---

## 一句话概括

> **DSH 开发它。DSH 不运行它。**
> **浏览器 认证它。浏览器 不查询它。**
> **OpenCsiToolClient 查询它。CLI 暴露它。**

---

## 分层图

```
┌──────────────────────────────────────────────────────────────────────┐
│  用户                                                                 │
└──────┬───────────────────────────────────────────┬───────────────────┘
       │  opencsi usage --json                     │  托盘图标 / 菜单
┌──────▼───────────────────────────────┐  ┌────────▼───────────────────┐
│  CLI 层            src/opencsi/cli/  │  │  托盘层  src/opencsi/tray/ │
│                                      │  │                            │
│   app.py        入口、退出码映射      │  │   app.py     TrayApp       │
│   context.py    参数解析、输出        │  │   presenter.py 纯函数：    │
│   status.py …   每个命令一个模块      │  │              快照 → 文案/菜单│
│   login.py      认证生命周期入口      │  │   icons.py  状态图标       │
│   tray.py       托盘命令入口          │  │   startup.py 开机自启      │
│                                      │  │   single_instance.py 互斥  │
│   职责：参数 → 调用 → 渲染。          │  │                            │
│   不含任何业务逻辑或 HTTP 细节。      │  │   职责：**纯 UI**。        │
└──────┬───────────────────────────────┘  └────────┬───────────────────┘
       │                                            │
       │              ┌─────────────────────────────┘
       │              │  直接 import，不是 subprocess
┌──────▼──────────────▼────────────────────────────────────────────────┐
│  监控层            src/opencsi/monitor/                              │
│                                                                      │
│   service.py   MonitorService —— 刷新循环、退避、状态机              │
│                MonitorSnapshot —— 冻结快照，不含任何可承载密钥的字段  │
│                                                                      │
│   职责：决定"什么时候去取数据、失败了怎么退避"。不认识终端，也不认识 │
│   托盘控件。因为这一层不依赖 UI，它才能在任意平台上被测。            │
└───────────────────────────────┬──────────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────────┐
│  领域层                                                                 │
│                                                                      │
│   client.py       OpenCsiToolClient —— 端点编排、401 重试、契约校验   │
│   models.py       冻结 dataclass：ToolGrant / TokenTrendPoint / …     │
│   aggregation.py  纯函数聚合：summarise() / estimate_usage_cost()     │
│   cache.py        线程安全 TTL 缓存                                   │
│                                                                      │
│   职责：知道"有哪些端点、返回什么形状"。不知道浏览器，不知道终端。     │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  只依赖 CredentialProvider 协议
┌───────────────────────────────▼──────────────────────────────────────┐
│  认证层            src/opencsi/auth/                                 │
│                                                                      │
│   base.py         CredentialProvider 协议 + CredentialStatus          │
│   cdp.py          CdpCookieProvider    —— 从浏览器读 Cookie           │
│   manual.py       ManualCookieProvider —— 手工/测试用                 │
│   session.py      SessionManager       —— 三种语义的协调者            │
│   oauth_browser.py BrowserOAuthRenewer —— 后台标签页静默续期          │
│   gitcode_qr.py   GitCodeQrAuthenticator —— 无浏览器扫码登录          │
│   qr_render.py    把登录码画到终端 / 存成文件                         │
│                                                                      │
│   职责：回答三个不同的问题 ——                                        │
│     "Cookie 的值是什么？"（重载）                                    │
│     "能换一个新的吗？"（续期）                                       │
│     "需要用户本人操作吗？"（交互登录）                               │
└───────────────┬──────────────────────────────┬───────────────────────┘
                │                              │
┌───────────────▼──────────────┐  ┌────────────▼──────────────────────┐
│  传输层                       │  │  浏览器（外部进程，不由本工具管理）│
│   transport.py  HttpTransport │  │                                   │
│   ws.py         WebSocket     │  │  Chrome / Edge / Brave            │
│   errors.py     退出码与异常   │  │  --remote-debugging-port=9222     │
│   redaction.py  脱敏           │  │                                   │
│   formatting.py CJK 宽度/数字  │  │  只被读取 Cookie 和跑后台标签页    │
└───────────────┬──────────────┘  └───────────────────────────────────┘
                │
┌───────────────▼──────────────────────────────────────────────────────┐
│  opencsitool.com  内部 Web API（仅 GET）                             │
│  web-api.gitcode.com  扫码登录协议（纯 HTTP 轮询）                   │
└──────────────────────────────────────────────────────────────────────┘
```

**一条必须守住的边界**：托盘层 import 监控层，**不是** subprocess 调
`opencsi usage --json`。少了子进程、少了 JSON 二次解析、也少了第二份认证逻辑。
托盘里没有任何业务逻辑 —— 它只把快照渲染成图标和菜单。

---

## 关键设计决定

### 1. 核心客户端不认识浏览器

这是整个设计里**最重要**的一条。

`OpenCsiToolClient` 的构造函数签名是：

```python
OpenCsiToolClient(credentials: CredentialProvider, ...)
```

它只认识 `CredentialProvider` 这个协议（`get_token` / `invalidate` /
`refresh` / `status`）。**没有任何地方** import `chrome`、`cdp`、
`websocket`、`target` 或 `selenium`。

**为什么重要**：浏览器自动化是脆弱的 —— 选择器会变、窗口会抢焦点、
无头模式会被反爬。把认证和查询彻底分开，意味着：

- 查询路径 100% 可离线测试（本仓库的 731 个测试**不碰网络**）
- 换认证方式（浏览器 Cookie → 环境变量 → 手工输入）**不需要改客户端**
- 这个工具**永远不会**因为页面改版而无法查询

### 2. 零第三方依赖

`pyproject.toml` 里 `dependencies = []`。

**为什么**：一个"独立工具"如果装不上就没有意义。目标用户可能在一个
受限的企业网络里，`pip install` 未必可用。纯标准库意味着：

```bash
PYTHONPATH=src python -m opencsi usage
```

就已经能跑了 —— 不需要 `pip`，不需要虚拟环境，不需要编译。

代价是自己实现了 WebSocket（RFC 6455，`ws.py`，约 380 行）。
这个代价是值得的：`websockets` 库有 20+ 个传递依赖。

### 3. 只实现 GET

`HttpTransport` 只有 `get_json()`。没有 `post`、`put`、`patch`、`delete`。

**为什么**：这不是"约定不写"，而是**让写入在结构上不可能发生**。
一个只读工具不应该有能力误删数据，哪怕代码写错了。

有一条测试直接断言这些方法**不存在**。

### 4. 401 只重试一次

```python
response = self._attempt(...)          # 第一次
if response.status == 401:
    self._refresh_credentials()        # 重新读一次 Cookie
    response = self._attempt(...)      # 第二次（唯一的一次）
    if response.status == 401:
        raise SessionExpiredError(...) # 放弃
```

**为什么**：Cookie 可能在浏览器里已经刷新了，所以重试一次是有价值的。
但如果是真的过期了，无限重试只会变成对服务器的软性 DoS，
而且会让用户以为"卡住了"。一次是收益/风险的平衡点。

### 5. 区分"端点坏了"和"没登录"

这是从实际调试中得到的教训。CDP 读取失败有两种**完全不同**的原因：

| 情况 | 真实原因 | 用户该做什么 |
| --- | --- | --- |
| WebSocket 握手被拒 | Chrome 147+ 默认配置限制 | **重启浏览器**加 `--user-data-dir` |
| 连上了但没 Cookie | 没登录 | **去登录** |

如果两者都报"未登录"，用户会去登录 —— 但登录**解决不了**第一种问题。
所以 `CdpCookieProvider` 会记录失败的具体原因，`doctor` 打印真实提示。

代码里的实现是 `_read_cookies()` 里的 `connected` 标志：
只要有一条策略成功跑完 CDP 调用，就说明"连接是好的"，
此时返回空列表（→ `CookieNotFoundError`，提示登录），
而不是报"端点不可用"。

### 6. 数字格式复刻站点规则

站点把 `3061130999` 显示成 `30.6亿`。本工具**完全复刻**这个规则：

```python
if value >= 100_000_000:   # 亿
    return f"{value / 100_000_000:.1f}亿"
if value >= 10_000:        # 万
    return f"{value / 10_000:.1f}万"
return str(value)
```

**为什么**：用户是拿着网页和终端对照的。如果终端显示 `3061130999`
而网页显示 `30.6亿`，用户会怀疑数据不对。

### 7. CJK 宽度必须单独计算

```python
display_width("使用中")  # → 6，不是 3
```

`使用中` 是 3 个字符，但占 **6 个终端列**。用 `len()` 补空格会让所有
含中文的表格错位 —— 而本工具的输出里到处都是中文。

`formatting.py` 用 `unicodedata.east_asian_width()` 判断宽字符（`W`/`F` 算 2 列），
并有专门的测试防止回归。

### 8. 脱敏是结构性的，不是"记得别打印"

三层防护：

1. **类型层**：`CredentialStatus` 这个结构体**没有**能装 token 的字段
2. **对象层**：`ToolGrant._virtual_key` 是 `repr=False` 的私有字段，
   `virtual_key_masked` 才是唯一出口
3. **输出层**：`RedactingFilter` 过滤日志，`to_json()` 丢弃 `_` 前缀字段
   并对值做二次清洗

`logs` 命令比较特殊：它原样输出服务端记录，而本工具**没有建模**这些记录，
所以无法保证字段安全。`to_json()` 里的 `redact_mapping()` 就是为它准备的。

代理 URL 是另一个容易忽略的泄漏点：`http://user:password@proxy:8080`
是合法写法，而这个字符串会进入错误消息和 `-v` 输出。
`transport._strip_proxy_credentials()` 会剥掉 userinfo，
只保留"走了哪个代理"这个有用的信息。

### 9. 代理：一个真实的坑

`urllib` 解析代理的来源有两处：

- 环境变量 `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY`
- **Windows 注册表** `HKCU\...\Internet Settings`（`curl` 不读这个）

本项目的开发机上因此出现过：

| 方式 | 结果 |
| --- | --- |
| `curl https://opencsitool.com/...` | `401`（正确） |
| `python -c "urllib.request.urlopen(...)"` | `SSLEOFError` |

本地代理 `127.0.0.1:7890` 无法转发该域名，而错误信息**完全没提代理**，
看起来像服务端 TLS 故障。

处理方式：

1. `--no-proxy` / `use_proxy=False` 走 `build_opener(ProxyHandler({}))`。
   注意 `build_opener` 的语义很微妙：传入 `ProxyHandler` 会让它**移除**
   默认的 ProxyHandler，所以结果是"完全没有代理处理" —— 这正是想要的。
2. 连接失败时，`proxy_for()` 报出实际会用的代理，错误里带上它
3. 提示里直接给出 `--no-proxy` 这个动作
4. `NO_PROXY` 命中的主机不会被误报为"走了代理"
5. 代理凭据被剥离

---

## 数据流：一次 `opencsi usage`

```
1. cli/app.py          main(["usage"]) → 解析参数
2. cli/context.py      make_client() → 构造 CdpCookieProvider + OpenCsiToolClient
3. cli/usage.py        run(ctx)
4. client.py           login_or_restore_session()
                          └─ provider.get_token()
                               └─ CDP: /json/list → WebSocket → Network.getCookies
                                  （失败则退回浏览器级 + Target.attachToTarget）
5. client.py           get_my_tools()
                          └─ transport.get_json("/opencsitool/rest/v1/ai/operations/personalQueueStatus")
6. models.py           coerce_grants() → tuple[ToolGrant, ...]
7. aggregation.py      summarise() → Summary
8. formatting.py       Table / format_count → 文本
9. cli/context.py      ctx.emit() → stdout 或 JSON
```

---

## 模块清单

| 文件 | 行数级别 | 职责 |
| --- | --- | --- |
| `version.py` | 小 | 版本号、User-Agent |
| `errors.py` | 中 | 退出码常量、异常层次、`exit_code_for()` |
| `redaction.py` | 中 | 脱敏：注册表、正则、日志过滤器、`Secret` |
| `ws.py` | 大 | 纯标准库 RFC 6455 WebSocket + `CdpConnection` |
| `transport.py` | 中 | `HttpTransport`（仅 GET）、`Response` |
| `cache.py` | 小 | 线程安全 TTL 缓存 |
| `models.py` | 大 | 冻结 dataclass + 宽容的类型转换 |
| `aggregation.py` | 中 | 纯函数聚合与费用估算 |
| `formatting.py` | 大 | CJK 宽度、亿/万、表格、JSON |
| `client.py` | 大 | `OpenCsiToolClient`：端点编排 |
| `auth/base.py` | 小 | `CredentialProvider` 协议 |
| `auth/cdp.py` | 大 | 从浏览器读 Cookie |
| `auth/browser_launch.py` | 中 | 启动一个**本工具能读**的浏览器（专用配置 + 调试端口） |
| `auth/manual.py` | 小 | 手工凭据 |
| `auth/session.py` | 大 | `SessionManager`：重载 / 续期 / 登录三种语义 |
| `auth/oauth_browser.py` | 大 | `BrowserOAuthRenewer`：后台标签页静默续期 |
| `auth/gitcode_qr.py` | 大 | `GitCodeQrAuthenticator`：无浏览器扫码登录 |
| `auth/qr_render.py` | 中 | 登录码的终端预览与文件落盘 |
| `monitor/service.py` | 大 | `MonitorService`：刷新循环、退避、状态机 |
| `tray/app.py` | 大 | `TrayApp`：pystray 图标与菜单（纯 UI） |
| `tray/presenter.py` | 中 | 快照 → 文案 / 菜单的纯函数 |
| `tray/icons.py` | 中 | 状态图标绘制 |
| `tray/startup.py` | 中 | HKCU Run 键读写 |
| `tray/single_instance.py` | 小 | 命名互斥量单实例守卫 |
| `cli/*.py` | 中 | 10 个命令 + 入口 + 上下文 |

---

## 为什么这样分层是"可验证"的

每一层都能被独立测试：

| 层 | 测试方式 | 需要网络？ |
| --- | --- | --- |
| `formatting` | 纯函数断言 | 否 |
| `aggregation` | 喂 fixture | 否 |
| `models` | 喂 fixture | 否 |
| `client` | `FakeTransport` 重放 fixture | 否 |
| `transport` | 构造 header 后断言 | 否 |
| `auth/cdp` | 进程内假 DevTools 服务器 | 否（仅 127.0.0.1） |
| `auth/session` | 假 provider + 假 renewer | 否 |
| `auth/gitcode_qr` | 进程内假 HTTP 服务器 | 否（仅 127.0.0.1） |
| `monitor` | 假 client，可控时钟 | 否 |
| `tray/presenter` | 纯函数断言 | 否 |
| `cli` | 替换 `make_client` | 否 |

**托盘层是唯一无法在 CI 里完整验证的层** —— pystray 需要一个真实的
Windows 消息循环。所以业务逻辑全部被推到 `monitor/` 和 `tray/presenter.py`，
它们都是纯的、可测的；`tray/app.py` 只剩下"把已经算好的东西交给 pystray"，
这部分用真机验证（见 README 的托盘一节）。

这就是为什么整个测试套件能在**离线环境**下跑完 731 个测试。
