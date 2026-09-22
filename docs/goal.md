# Goal

继续开发仓库：

```text
https://github.com/jingchangshi/OpenCSIToolMonitor
```

基于当前最新 `master` 工作。

本轮不是重新设计整个项目，也不是重新调查 openCsiTool 主业务 API。

当前仓库已经具备：

- `OpenCsiToolClient`
- `SessionManager`
- `CdpCookieProvider`
- `BrowserOAuthRenewer`
- Silent OAuth Renewal
- `GitCodeQrAuthenticator`
- `MonitorService`
- Windows Tray
- Windows Startup
- PyInstaller CLI / Tray EXE
- 大规模离线测试与真实续期探针

这些能力应视为已有基础。

本轮目标是修复当前实现距离最终产品状态还存在的几个关键缺口：

1. 修正 `opencsi login --qr` 的错误成功语义；
2. 尽可能把 GitCode QR 登录真正接入 openCsiTool 登录闭环；
3. 继续验证“openCsiTool OAuth 第二段是否真的必须依赖浏览器”；
4. 如果浏览器确实不可消除，将其降级成隐藏/后台认证引擎，而不是用户需要主动操作的浏览器；
5. 修复 Windows frozen EXE 的 startup registration 边界；
6. 增加 CI，使已有测试成为仓库强制约束；
7. 对最终实现进行真实 Windows 11 验证。

不要大规模重写已经验证有效的 Session / Monitor / Tray 架构。

---

# 0. 先重新读取最新代码

首先：

```bash
git status
git branch --show-current
git log --oneline --decorate -30
```

完整阅读至少：

```text
README.md
pyproject.toml

src/opencsi/client.py

src/opencsi/auth/base.py
src/opencsi/auth/session.py
src/opencsi/auth/cdp.py
src/opencsi/auth/oauth_browser.py
src/opencsi/auth/gitcode_qr.py
src/opencsi/auth/browser_launch.py
src/opencsi/auth/qr_render.py

src/opencsi/cli/login.py
src/opencsi/cli/tray.py
src/opencsi/cli/doctor.py
src/opencsi/cli/context.py

src/opencsi/monitor/service.py

src/opencsi/tray/app.py
src/opencsi/tray/startup.py
src/opencsi/tray/single_instance.py

packaging/opencsi.spec
packaging/cli_entry.py
packaging/tray_entry.py

tools/probe_oauth_pure_http.py
tools/probe_gitcode_qr_live*.py
tools/probe_renewal*.py

docs/authentication.md
docs/gitcode-qr-protocol.md
docs/architecture.md

OpenCSIToolMonitor_Session_QR_Tray_Implementation_Report.md
```

以及相关测试：

```text
tests/test_session.py
tests/test_oauth_renewal.py
tests/test_gitcode_qr.py
tests/test_cli_session.py
tests/test_monitor.py
tests/test_tray.py
tests/test_packaging.py
tests/test_redaction.py
```

以当前 HEAD 为事实来源，不要依赖旧提示词中的行号或测试数字。

---

# 1. 先跑 baseline

修改任何代码前实际执行：

```bash
pytest
python -m unittest discover -s tests -q
```

记录：

```text
passed
failed
skipped
subtests
```

如果当前 baseline 本身失败：

先判断是环境问题还是代码问题。

不要带着未知 baseline 开始重构。

---

# 2. 当前已经确认有效的部分，不要重新实现

以下内容除非发现真实 bug，否则不要大规模修改：

```text
SessionManager
credential reload / session renewal 分离
BrowserOAuthRenewer
5 分钟 renew margin
401 reload -> renew
bounded retry
MonitorService
Tray state model
last-good snapshot
pystray integration
single-instance
Windows Run startup
PyInstaller dual binary
```

特别是：

```text
reload != renewal
```

已经解决。

不要退回旧设计。

---

# 3. P0：修复 `opencsi login --qr` 的成功语义

当前最大的产品语义错误：

```bash
opencsi login --qr
```

GitCode QR 登录成功后可能：

```text
GitCode authentication = success
openCsiTool session = not established
```

但命令仍然：

```text
exit 0
```

这意味着：

```text
opencsi login
```

声称成功，但紧接着：

```bash
opencsi usage
```

仍可能不可用。

这是错误的 CLI 契约。

---

# 4. 定义真正的 login success

对于所有：

```bash
opencsi login ...
```

最终成功判据必须统一为：

```text
GET /opencsitool/rest/v1/user/getUserInfo
→ success
```

只有：

```text
openCsiTool session actually usable
```

才允许：

```text
exit 0
```

不能把：

```text
GitCode login succeeded
```

直接等同于：

```text
openCsiTool login succeeded
```

---

# 5. QR 命令状态模型

如果最终无法自动建立 openCsiTool session：

不要：

```text
exit 0
```

需要有明确结果，例如：

```text
GITCODE_AUTHENTICATED
OPENCSITOOL_SESSION_PENDING
```

考虑增加结构化状态：

```python
class LoginStage(Enum):
    GITCODE_AUTHENTICATED
    OPENCSITOOL_AUTHENTICATED
    OPENCSITOOL_PENDING
```

或者等价设计。

要求：

```text
opencsi login --qr
```

只有：

```text
OPENCSITOOL_AUTHENTICATED
```

才返回 0。

---

# 6. 不要只改 exit code

同时修：

```text
text output
JSON output
README
docs/authentication.md
implementation report
tests
```

避免代码和文档再次漂移。

---

# 7. P1：研究 QR credential 能否桥接到 OAuth runtime

当前 QR 登录成功已经获得：

```text
GitCode access_token
GitCode refresh_token
possibly xauth_token
```

现在这些值注册脱敏后几乎立即被丢弃。

本轮首先回答：

> QR 登录返回的 GitCode token，与 Chromium GitCode profile 中的 SSO token 是什么关系？

---

# 8. Token 对比必须安全

禁止打印原始 token。

使用：

```text
length
SHA-256 fingerprint
expiry metadata
token type
```

例如：

```text
QR access_token:
  len=...
  sha256=<first 12 chars>

browser GITCODE_ACCESS_TOKEN:
  len=...
  sha256=<first 12 chars>
```

只比较：

```text
same / different
```

不要输出完整 hash 也可以。

---

# 9. 调查 browser GitCode session 的真实组成

通过已有 CDP：

```text
Storage.getCookies
```

记录：

```text
cookie names
domains
expiry
httpOnly
secure
sameSite
```

不得记录 value。

重点：

```text
GITCODE_ACCESS_TOKEN
GITCODE_REFRESH_TOKEN
GitCodeUserName
gitcode_oauth_session
```

如果有其它实际关键项，也记录。

---

# 10. 验证 QR token 是否可以构造 Browser SSO

如果 QR 返回：

```text
access_token
refresh_token
```

尝试回答：

```text
是否可以把这些 credential 注入一个干净的 dedicated Chromium profile，
然后访问 GitCode 时被认为已经登录？
```

优先使用：

```text
Storage.setCookies
```

如果 token 实际存储在：

```text
localStorage
sessionStorage
IndexedDB
```

则通过 CDP Runtime / DOMStorage 等受控方式设置。

不要猜。

先观察真实浏览器登录后的 storage。

---

# 11. 创建独立实验 profile

不要污染用户当前 profile。

使用：

```text
%LOCALAPPDATA%\OpenCSI\auth-test-profile
```

或临时 profile。

实验流程：

```text
fresh profile
    ↓
no GitCode login
    ↓
QR login
    ↓
obtain credentials
    ↓
inject only required auth state
    ↓
navigate GitCode
    ↓
check authenticated state
```

如果失败：

逐层记录失败原因。

---

# 12. 成功判定不能靠页面文字

GitCode SSO 注入成功至少使用一种稳定证据：

```text
known authenticated API
```

或者：

```text
OAuth authorize flow does not land on login page
```

不要仅因为：

```text
page contains username
```

就宣布成功。

---

# 13. 如果 QR credential 可以构造浏览器 SSO

实现正式组件，例如：

```text
GitCodeBrowserSessionBridge
```

职责：

```text
GitCodeQrAuthenticator
        │
        ▼
QrLoginResult credentials
        │
        ▼
GitCodeBrowserSessionBridge
        │
        ▼
dedicated Chromium auth context
        │
        ▼
BrowserOAuthRenewer
        │
        ▼
new openCsiTool token
```

---

# 14. QR 最终目标链路

目标：

```text
opencsi login --qr
        │
        ▼
show mini-program code
        │
        ▼
WeChat scan
        │
        ▼
GitCode credentials
        │
        ▼
bridge credentials into auth runtime
        │
        ▼
complete openCsiTool OAuth
        │
        ▼
new token cookie
        │
        ▼
getUserInfo
        │
        ▼
exit 0
```

如果做到这一点：

用户不需要手动操作浏览器。

---

# 15. 即使内部仍用 Chromium，也区分“用户依赖”和“实现依赖”

目标是：

```text
user does not need to:
- manually open Chrome
- enter password
- click around
- manage CDP
```

允许内部：

```text
Chromium / Edge
```

作为认证 engine。

这是比：

```text
完全消灭所有 browser engine
```

更现实、更重要的目标。

---

# 16. P2：继续逆向 `/oauth/authorize` SPA

当前已证明：

```text
urllib + CookieJar
GET /oauth/authorize
→ SPA shell
```

不能完成 OAuth。

这个结论只证明：

```text
plain redirect-following HTTP client
```

不可行。

它没有证明：

```text
SPA 内部调用的后端 API
```

不可复现。

本轮继续研究。

---

# 17. 不要再只分析 HTML shell

目标变成：

```text
/oauth/authorize SPA
       │
       ▼
JS bundles
       │
       ▼
XHR / fetch
       │
       ▼
real authorization API
```

需要找出：

```text
current user endpoint
OAuth app metadata endpoint
consent status endpoint
authorization submit endpoint
code generation endpoint
callback endpoint
```

---

# 18. 两种方法并行

## 静态方法

分析：

```text
<script src=...>
lazy chunks
source maps if available
```

搜索：

```text
authorize
oauth
client_id
redirect_uri
response_type
state
consent
scope
approve
授权
cancel
```

找到真实：

```text
fetch
axios
request()
```

调用。

---

## 动态方法

使用专用 browser/CDP。

捕获：

```text
Network.requestWillBeSent
Network.responseReceived
Network.getResponseBody
```

只观察：

```text
gitcode.com/oauth/authorize
```

页面加载之后产生的认证相关请求。

---

# 19. OAuth consent 的真实行为要拆清

分别测试：

```text
A. existing grant
B. no grant / consent required
C. GitCode signed out
```

识别：

```text
existing grant:
  API sequence?

consent required:
  which endpoint renders metadata?
  which POST is triggered by 授权 button?

signed out:
  which API redirects to login?
```

---

# 20. 不自动批准 OAuth consent

注意：

```text
consent required
```

代表用户授权决定。

即使发现：

```text
POST /oauth/approve
```

也不要让后台自动替用户点击授权。

实现可以：

```text
detect consent
→ CONSENT_REQUIRED
→ show UI
```

用户明确操作后才提交。

这是现有 `CONSENT_REQUIRED` 设计应继续保持的边界。

---

# 21. Browserless OAuth feasibility gate

研究结束后必须输出明确结论之一：

```text
PURE_HTTP_OAUTH_FEASIBLE
```

或者：

```text
PURE_HTTP_OAUTH_NOT_FEASIBLE_WITH_CURRENT_EVIDENCE
```

第二种不要写成：

```text
impossible forever
```

除非发现真正不可复现的浏览器绑定机制。

---

# 22. 什么才算真正证明 browser-bound

至少发现类似：

```text
WebAuthn
hardware-backed key
browser-bound cryptographic proof
unextractable JS-generated credential
mandatory proprietary challenge
server verifies browser attestation
```

才能强判：

```text
browser required
```

仅仅：

```text
page is SPA
```

不够。

---

# 23. 如果 pure HTTP OAuth 可实现

新增例如：

```text
src/opencsi/auth/oauth_http.py
```

实现：

```text
HttpOAuthRenewer
```

并让 SessionManager 支持：

```text
preferred:
HttpOAuthRenewer

fallback:
BrowserOAuthRenewer
```

---

# 24. 如果 pure HTTP OAuth 仍不可行

不要继续无限探索。

转向：

```text
hidden auth browser
```

把浏览器从：

```text
用户可见窗口
```

变成：

```text
内部认证 runtime
```

---

# 25. P3：实现隐藏/后台认证 Browser Host

当前 `browser_launch.py` 会启动：

```text
Chrome/Edge
--remote-debugging-port
--user-data-dir
```

但正常情况下会出现窗口。

Tray 因此默认：

```text
--auto-recover-browser = false
```

导致 Windows 开机后：

```text
Tray starts
Browser not running
→ BROWSER_UNAVAILABLE
```

这不是真正的无人值守。

---

# 26. 研究 Chromium headless auth runtime

优先测试现代 Chromium：

```text
--headless=new
```

组合：

```text
--remote-debugging-port=9222
--user-data-dir=...
--no-first-run
--no-default-browser-check
```

验证：

```text
Cookie persistence
GitCode SSO persistence
OAuth redirect
openCsiTool token Set-Cookie
Storage.getCookies
Target.createTarget
Runtime.evaluate
```

不要假设 headless 与正常 Chrome 完全等价。

真实验证。

---

# 27. Headless 能力 Gate

至少验证：

```text
[ ] GitCode SSO survives restart
[ ] BrowserOAuthRenewer works
[ ] openCsiTool token is minted
[ ] token persists in profile
[ ] Storage.getCookies sees it
[ ] no visible window appears
```

如果成立：

实现：

```text
AuthBrowserHost
```

---

# 28. AuthBrowserHost

建议新层：

```text
AuthBrowserHost
    │
    ├── ensure_running()
    ├── launch_hidden()
    ├── launch_visible()
    ├── status()
    └── shutdown()
```

不要让：

```text
MonitorService
TrayApp
CLI
```

各自维护一份启动 Chrome 的逻辑。

---

# 29. Hidden first，Visible on demand

状态策略：

```text
normal:
  hidden/headless browser

silent renewal:
  hidden

LOGIN_REQUIRED:
  QR login preferred
  or visible browser

CONSENT_REQUIRED:
  visible consent UI required
```

即：

```text
browser exists
≠
browser window visible
```

---

# 30. Windows Tray 最终正常路径

目标：

```text
Windows sign-in
    ↓
OpenCSI Monitor starts
    ↓
AuthBrowserHost.ensure_running(hidden=True)
    ↓
restore GitCode SSO
    ↓
silent openCsiTool renewal
    ↓
fetch usage
    ↓
tray = OK
```

用户不应因为：

```text
Chrome wasn't already open
```

看到：

```text
BROWSER_UNAVAILABLE
```

除非 auth runtime 本身启动失败。

---

# 31. 如果 headless OAuth 不可靠

使用：

```text
visible-but-minimized / offscreen
```

等第二选择。

不要用 fragile hacks 隐藏窗口。

优先支持官方 Chromium flags。

---

# 32. P4：修复 frozen EXE Startup registration

当前：

```python
if sys.frozen:
    return sys.executable
```

存在上下文 bug。

如果用户运行：

```text
opencsi.exe tray --install-startup
```

则 Run key 可能注册：

```text
opencsi.exe
```

而不是：

```text
opencsi-tray.exe
```

下次 Windows 登录时 CLI 会因为没有 subcommand 直接退出。

必须修。

---

# 33. Startup command 必须按运行上下文决定

正确语义：

## Source install

```text
pythonw.exe -m opencsi.tray
```

## Frozen tray binary

```text
opencsi-tray.exe
```

## Frozen CLI binary

优先：

```text
<same-dir>\opencsi-tray.exe
```

如果不存在，再：

```text
opencsi.exe tray
```

但不要注册：

```text
opencsi.exe
```

裸命令。

---

# 34. 不要让 startup.py 猜当前进程目的

建议 API 改成：

```python
startup_command_for_tray()
```

而不是泛化的：

```python
default_command()
```

因为这个 Run entry 永远是：

```text
start tray
```

而不是：

```text
restart whatever executable called me
```

---

# 35. 为三种上下文加测试

必须覆盖：

```text
source python
frozen tray
frozen CLI
```

例如 mock：

```text
sys.frozen
sys.executable
```

断言：

```text
opencsi-tray.exe
```

或：

```text
opencsi.exe tray
```

而不是裸 CLI。

---

# 36. frozen binary 真机验证

构建：

```bash
python tools/build_exe.py
```

真实运行：

```powershell
dist\opencsi.exe tray --install-startup
```

然后读：

```text
HKCU\Software\Microsoft\Windows\CurrentVersion\Run
```

确认：

```text
OpenCSIToolMonitor
```

值确实启动 tray。

测试完恢复注册表原状态。

---

# 37. P5：修复 QR mini-program code UX

当前实证说明：

```text
GitCode qrcode field
```

实际上是：

```text
WeChat mini-program code PNG
```

而不是标准 QR。

因此：

```text
终端字符画
```

不能可靠扫码。

不要继续尝试把它包装成“终端二维码”。

---

# 38. Windows 上提供更合理的扫码体验

当运行：

```text
opencsi login --qr
```

如果有桌面环境：

优先：

```text
打开一个小的 native image window
```

显示原始 430×430 mini-program code。

要求：

```text
no web browser
no browser automation
```

只显示 PNG。

可以考虑：

```text
Tkinter
```

如果 Python 环境可用。

但 frozen build 中当前排除了 tkinter。

更合理方案：

```text
Windows native popup
```

或使用已有 Pillow + 极小 GUI dependency。

不要为了这个目标引入巨大框架。

---

# 39. Tray QR 登录

当：

```text
LOGIN_REQUIRED
```

Tray 的 Login action 最理想应该：

```text
show mini-program code popup
```

而不是首先弹 Chrome。

流程：

```text
Tray
  ↓
Sign in
  ↓
show WeChat code
  ↓
scan
  ↓
GitCode authenticated
  ↓
bridge
  ↓
openCsiTool authenticated
  ↓
tray OK
```

如果 bridge 暂时不可行：

显示：

```text
GitCode authenticated
Finishing openCsiTool sign-in...
```

然后后台启动 auth browser。

---

# 40. P6：增加 CI

当前大量测试仅存在于开发者本地验证。

加入：

```text
.github/workflows/test.yml
```

---

# 41. Linux test matrix

至少：

```text
ubuntu-latest
Python 3.10
Python 3.12
Python 3.13
```

运行：

```bash
pytest
python -m unittest discover -s tests -q
```

---

# 42. Windows CI

增加：

```text
windows-latest
```

运行：

```powershell
pytest
python -m unittest discover -s tests -q
```

以及：

```text
startup helper tests
packaging tests
```

---

# 43. Windows build smoke

安装：

```text
.[build,tray,qr]
```

执行：

```bash
python tools/build_exe.py
```

然后至少：

```powershell
dist\opencsi.exe --help
dist\opencsi.exe --version
dist\opencsi-tray.exe --help
dist\opencsi-tray.exe --check
```

注意：

不要让 CI 真正访问：

```text
opencsitool.com
gitcode.com
```

---

# 44. CI 不跑真实 OAuth

真实：

```text
QR
OAuth
CDP
openCsiTool
```

继续通过：

```text
manual/live probe
```

验证。

CI 只跑：

```text
fake DevTools
fake HTTP
offline fixtures
packaging
```

---

# 45. Live probe 分层

保留并整理：

```text
tools/probe_*.py
```

考虑给 live probe 明确：

```text
LIVE
NETWORK
AUTH_SIDE_EFFECT
GET_ONLY
```

标签。

避免普通测试误运行。

---

# 46. 文档措辞修正

当前一些文字仍然容易让用户理解成：

```text
opencsi login --qr = complete openCsiTool login
```

必须统一。

在最终闭环前：

```text
QR login authenticates GitCode first.
```

若后续 bridge 实现成功，再更新为：

```text
QR login establishes the complete openCsiTool session.
```

文档必须随实际代码能力变化，而不是提前宣布目标状态。

---

# 47. 修正“浏览器无法被完全移除”的结论等级

当前证据只证明：

```text
plain urllib redirect-following
```

失败。

因此文档应写：

```text
The current pure-HTTP redirect-following implementation cannot complete
the GitCode OAuth authorize SPA.
```

如果进一步逆向后仍发现真正 browser-bound 机制：

再升级结论。

不要超出证据。

---

# 48. 对已有报告做批判性更新

`OpenCSIToolMonitor_Session_QR_Tray_Implementation_Report.md`

目前开头：

```text
已完成
```

过强。

在 full QR openCsiTool login 尚未闭环前，应更准确，例如：

```text
核心目标基本完成，QR-to-openCsiTool session bridge 尚未闭环
```

如果本轮完成 bridge：

再恢复：

```text
PASS
```

---

# 49. 安全边界

继续保持：

```text
openCsiTool business API = read-only
```

允许：

```text
authentication POST
OAuth authorization action only when user explicitly approves
QR challenge creation
```

禁止：

```text
sync data
modify grant
delete
approve business request
bind business resources
```

---

# 50. GitCode token 安全

QR 成功后：

```text
access_token
refresh_token
xauth_token
```

属于高敏感信息。

必须：

```text
register_secret immediately
repr=False
no logs
no JSON
no traceback
no temp file
```

---

# 51. 如果需要持久化 GitCode credential

只有在证明：

```text
persistent GitCode credential
```

确实能让 openCsiTool OAuth 自动恢复时才实现。

Windows：

优先：

```text
DPAPI
```

或者：

```text
Windows Credential Manager
```

不要：

```text
credentials.json
.env
registry plaintext
```

---

# 52. 不要持久化短命 openCsiTool token 作为长期方案

它约一小时失效。

持久化：

```text
openCsiTool token
```

没有解决根因。

如果需要 durable state，应保存：

```text
upstream GitCode auth state
```

且仅在它真的可复用后。

---

# 53. Consent 状态保持人工决定

当前：

```text
CONSENT_REQUIRED
```

设计正确。

不要为了“无人值守”自动点击：

```text
授权
```

用户必须明确决定授权第三方应用。

工具可以：

```text
show consent UI
detect completion
continue automatically
```

但不能偷偷批准。

---

# 54. MonitorService 不要继续膨胀

认证新增逻辑应进入：

```text
auth/
```

例如：

```text
GitCodeSessionBridge
AuthBrowserHost
HttpOAuthRenewer
```

Monitor 只处理：

```text
state
schedule
refresh
```

不要把 protocol 逻辑塞进去。

---

# 55. 最终目标架构

理想状态：

```text
                         OpenCSI Client
                              │
                              ▼
                        SessionManager
                              │
             ┌────────────────┼────────────────┐
             │                │                │
             ▼                ▼                ▼
       Credential       SessionRenewer     LoginFlow
         Provider
             │                │                │
             │        ┌───────┴───────┐        │
             │        │               │        │
             ▼        ▼               ▼        ▼
         Cookie    HTTP OAuth    Browser OAuth  QR
                       │               ▲         │
                       │               │         ▼
                       │          AuthBrowser <- GitCode credentials
                       │
                       ▼
                 openCsiTool token
```

Tray：

```text
Tray
 │
 ▼
MonitorService
 │
 ▼
OpenCsiToolClient
```

仍然不包含协议实现。

---

# 56. 最终用户体验目标

正常情况：

```text
Windows sign-in
     ↓
OpenCSI tray starts
     ↓
hidden auth context restored
     ↓
usage shown
     ↓
openCsiTool token expires hourly
     ↓
silent renewal
     ↓
user notices nothing
```

当 GitCode SSO 真失效：

```text
Tray -> Login required
        ↓
Show WeChat mini-program code
        ↓
user scans once
        ↓
GitCode login complete
        ↓
OAuth completion
        ↓
openCsiTool session restored
        ↓
Tray -> OK
```

这才是本轮最终产品状态。

---

# 57. 如果完全 browserless OAuth 最终不可实现

目标退化为：

```text
browser engine required internally
browser interaction not required normally
```

这是可以接受的最终状态。

但必须满足：

```text
用户正常使用时不需要手动打开浏览器。
```

---

# 58. 验收：QR CLI

完成时至少：

```text
[ ] GitCode QR can authenticate
[ ] QR credentials are actually consumed
[ ] openCsiTool session is attempted automatically
[ ] exit 0 only when getUserInfo succeeds
[ ] partial success has a distinct status / exit code
[ ] no secret leak
```

---

# 59. 验收：OAuth

至少：

```text
[ ] SPA authorization API investigated
[ ] browserless feasibility re-evaluated from API evidence
[ ] no conclusion stronger than evidence
```

如果实现 pure HTTP：

```text
[ ] HttpOAuthRenewer
[ ] BrowserOAuthRenewer fallback
```

---

# 60. 验收：Browser Host

如果 browser engine remains necessary：

```text
[ ] hidden/headless mode tested
[ ] cookie persistence tested
[ ] silent OAuth works
[ ] no visible browser for normal renewals
[ ] visible UI only for consent/login where necessary
```

---

# 61. 验收：Tray

```text
[ ] startup from Windows login works
[ ] no BROWSER_UNAVAILABLE merely because Chrome was not manually started
[ ] usage visible after startup
[ ] QR login path reachable from tray
[ ] consent state remains explicit
[ ] last-good snapshot survives auth/network failure
```

---

# 62. 验收：Startup

```text
[ ] source install command correct
[ ] frozen tray command correct
[ ] frozen CLI installs tray startup correctly
[ ] no naked opencsi.exe registered
[ ] registry restored after live test
```

---

# 63. 验收：CI

```text
[ ] GitHub Actions added
[ ] Linux matrix
[ ] Windows tests
[ ] both pytest and unittest
[ ] frozen binaries built
[ ] basic frozen smoke tests
[ ] no credentials/network required
```

---

# 64. 实机测试

完成代码后在 Windows 11 实际测试。

至少：

```powershell
opencsi login --status
opencsi login --renew
opencsi tray --once
opencsi tray --check
```

QR：

```powershell
opencsi login --qr
```

如果用户不实际扫码：

只能报告：

```text
PRE-SCAN VERIFIED
```

不能声称完整成功。

如果可以实际扫码：

必须继续验证到：

```text
getUserInfo 200
```

---

# 65. Headless 实测

如果实现：

```text
AuthBrowserHost
```

实际：

```text
kill existing dedicated Chrome
start headless auth host
renew
usage
restart auth host
renew again
```

确认 profile persistence。

---

# 66. Tray 实机

验证：

```text
icon appears
tooltip updates
menu updates
session lifetime visible
refresh works
renew works
login path works
startup toggle works
second launch reports correctly
exit cleanly terminates the real tray process
```

---

# 67. Windows reboot/login startup evidence

如果环境允许：

至少模拟：

```text
register startup
read registry
invoke exact registered command
verify tray --check / startup path
restore registry
```

如果无法真正注销登录：

明确：

```text
full login-cycle test not executed
```

不要虚构。

---

# 68. Tests

所有修改完成后：

```bash
pytest
python -m unittest discover -s tests -q
```

两者都必须跑。

测试数字以实际结果为准。

---

# 69. Frozen build

重新：

```bash
python tools/build_exe.py
```

然后实际运行产物。

不要只测试源码。

过去已经多次出现：

```text
source correct
frozen product broken
```

所以 frozen binary 是独立测试对象。

---

# 70. Secret scan

最终扫描：

```text
access_token
refresh_token
xauth_token
scene_id
token=
Cookie:
Authorization:
virtualKey
```

区别：

```text
symbol name
test placeholder
```

和：

```text
real secret value
```

仓库中真实值必须为 0。

---

# 71. Commit 策略

不要一个巨大 commit。

建议按真实工作形成类似：

```text
fix(cli): make QR login success mean an openCsiTool session

research: trace the GitCode authorize SPA protocol

feat(auth): bridge QR credentials into the OAuth runtime

feat(auth): add a hidden authentication browser host

fix(startup): register the tray binary from frozen CLI builds

test(auth): cover QR-to-openCsiTool session completion

ci: run tests and frozen Windows smoke builds

docs: align QR and browser-bound claims with measured behavior
```

如果某条路线最终不可实现：

对应 commit 用：

```text
research:
docs:
```

而不是假装实现。

---

# 72. 不要因为某一阶段失败停下

例如：

```text
pure HTTP OAuth still blocked
```

不是整个 Goal 的失败。

继续：

```text
hidden browser
startup fix
CI
QR semantics
```

---

# 73. 最终报告

生成：

```text
OpenCSIToolMonitor_Final_Auth_Closure_Report.md
```

内容：

## 1. Verdict

分别给：

```text
QR GitCode auth
QR -> openCsiTool auth
silent renewal
browserless OAuth
hidden auth runtime
Windows tray
startup
CI
```

不要一个总 PASS 掩盖局部未完成。

---

## 2. Before / After

写：

```text
starting HEAD
ending HEAD
```

---

## 3. QR semantic bug

说明：

```text
why exit 0 was wrong
how fixed
```

---

## 4. QR Credential Bridge

说明：

```text
what tokens QR returns
how they relate to browser SSO
whether they can be consumed
```

不得输出值。

---

## 5. OAuth SPA investigation

列：

```text
bundles
API endpoints
request sequence
browserless verdict
```

---

## 6. Browser requirement

必须精确区分：

```text
browser interaction required?
browser engine required?
```

这两件事不要混在一起。

---

## 7. Final Login Flow

ASCII 时序图。

---

## 8. Tray startup flow

从：

```text
Windows sign-in
```

到：

```text
OK
```

完整说明。

---

## 9. Frozen Startup Fix

给：

```text
source
frozen CLI
frozen tray
```

三个命令。

---

## 10. Tests

真实：

```text
pytest
unittest
Windows
packaging
live probes
```

结果。

---

## 11. CI

列 workflow。

---

## 12. Remaining hard boundaries

只列实证的。

不要把：

```text
尚未解决
```

写成：

```text
理论上不可能
```

---

# 74. 本轮最重要的思维原则

不要为了完成 checklist 把部分成功描述成完整成功。

尤其：

```text
GitCode login success
≠
openCsiTool login success
```

以及：

```text
SPA requires JavaScript
≠
all browserless implementations are impossible
```

以及：

```text
browser engine required
≠
user must interact with a browser window
```

这三个区别决定本轮工作的质量。

---

# 75. 最终目标

把当前状态：

```text
QR
 → GitCode login
 → stop
 → tell user to use browser

Tray startup
 → browser missing
 → user action required
```

推进到：

```text
QR
 → GitCode login
 → automatic OAuth completion
 → verified openCsiTool session

Windows startup
 → hidden auth runtime
 → automatic renewal
 → usage available in tray
```

如果最后仍然必须使用 Chromium engine，也可以接受。

但正常用户体验应该做到：

> 除 GitCode 登录真正失效或 OAuth 首次授权需要用户决定之外，OpenCSIToolMonitor 在 Windows 11 上长期常驻运行时不要求用户手动操作浏览器。

现在开始。

先重新读取最新 `master`、运行 baseline、记录当前 HEAD，然后严格按照：

```text
P0 QR success semantics
→ P1 QR credential bridge
→ P2 OAuth SPA investigation
→ P3 hidden auth runtime
→ P4 frozen startup fix
→ P5 QR/Tray UX
→ P6 CI
→ full verification
```

连续执行。

不要在每个阶段结束后停下来询问用户。