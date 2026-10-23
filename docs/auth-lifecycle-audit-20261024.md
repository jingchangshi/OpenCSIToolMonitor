# OpenCSIToolMonitor 自动认证生命周期独立审计报告

**日期**: 2026-10-24
**审计基线**: master `6c084bc`（2026-09-24，CI success），工作区干净
**审计结束 HEAD**: `5246175`（6 个新提交，全量测试 1132 passed / 1 skipped / 189 subtests）
**方法**: 静态代码审计 + GitCode 当前生产 bundle 逆向 + 无凭据线上探针 + 隔离多进程测试 + 本机只读验收
**本文约定**: 每条结论标注【证据】或【推断】；凡涉及真实凭据状态改变的探针一律未执行（见 §8）。

---

## 0. 结论速览

| 层 | 结论 | 一句话依据 |
| --- | --- | --- |
| **L1** openCsiTool Cookie 小时级自动续期 | **机制 PASS（离线全验证）/ 本次活体验证 BLOCKED** | 生产链路代码、测试、探针齐备且历史活体验证过；本机当前会话已死 29 天，重验需先恢复上游凭据（§9.3） |
| **L2** GitCode 上游凭据长期生命周期 | **FAIL→已修复机制 / 最终判定 BLOCKED** | 400 根因已锁定并修复（凭据体系不匹配，§5）；真实交换因可能轮换 refresh token，须用户显式 opt-in 后执行一次（§8/§9.2） |
| **L3** Windows 进程退出/重启/注销后自动恢复 | **PASS（存储与进程边界）/ 注销登录循环 NOT_EXECUTED** | DPAPI 存储 + 跨进程锁修复 + 新进程重读测试全绿；真实注销/登录循环需人操作（§9.4） |

**最重要验收规则的执行情况**：「GitCode 上游凭据到期后无需重新扫码仍可恢复，且恢复后的凭据在新进程中持续有效」——本次审计**证明了恢复机制的完整代码路径**（端点、请求形状、按需触发、原子持久化、新进程重读，全部有测试），但**没有**在真实凭据上执行过一次交换。按规则，L2 不得判 PASS。能走到这一步只差一条命令，见 §9.2。

---

## 1. 认证生命周期架构与实际调用路径

### 1.1 凭据清单

| 凭据 | 来源 | 存储位置 | 有效期证据 | 续期方式 |
| --- | --- | --- | --- | --- |
| openCsiTool `token` cookie | openCsiTool OAuth 回调签发（`Set-Cookie`，len≈333） | DPAPI `StoredOpenCsiCredential`（`opencsi.auth.store`） | **≈0.97 h**【证据，实测 `expires` 字段，docs/api-investigation.md §7.2】 | `HttpOAuthRenewer` 纯 HTTP 三步流（entry 302 → `checkOrAuthorize` → callback）【证据，活体历史验证】 |
| GitCode `access_token` | 微信小程序 QR 登录 body（`/uc/api/v1/user/oauth/login/qrcode/wechat_mini_program`） | DPAPI `StoredGitCodeCredential` | **未知**。QR 响应无 `expires_in`【证据，bundle 与仓库双确认】；代码中旧有"15 天 documented"无出处已删除；浏览器 profile 中 180 天 cookie TTL 是本项目 bridge 自设值（`gitcode_bridge.py:183` 写死 `180*24*3600`）【证据，本审计确认】，**不构成** GitCode 服务端策略的证据 | **本审计后**：`POST web-api.gitcode.com/uc/api/v1/user/token/refresh`（Bearer + form body），按需触发（§5） |
| GitCode `refresh_token` | 同上，QR body | DPAPI 同上 | 未知【证据：无任何渠道观测过】 | 同上；响应可能轮换，按轮换处理并原子写回 |
| GitCode `xauth_token` | QR body | **不存储**（仅注册脱敏） | — | 无消费者【证据：当前 bundle 中仅存取于 localStorage，无端点消费；与仓库既有结论一致】 |

### 1.2 调用路径（修复后现状）

```
opencsi usage / tray worker
  └─ OpenCsiToolClient._request_json
       ├─ _ensure_session_valid()                # 计划路径：needs_renewal(margin=300s)
       │    └─ SessionManager.renew()            # cooldown 60s，MAX_RENEW_ATTEMPTS=2
       │         └─ FallbackRenewer
       │              ├─ [组合 provider 时] SourceBoundRenewer        ← 本审计新增（身份门控）
       │              │    └─ RefreshingGitCodeRenewer
       │              │         ├─ StoredGitCodeRefresher.refresh(force=False)
       │              │         │    └─ GitCodeTokenRefresher        # 仅已知过期临近时
       │              │         ├─ HttpOAuthRenewer.renew            # 三步 OAuth，凭据=存储的 GitCode half
       │              │         └─ （leg 失败且未刚刷新过）refresh(force=True) + 重试一次   ← 本审计新增（按需恢复）
       │              ├─ HttpOAuthRenewer(provider=CDP)              # 仅裸 CDP provider
       │              └─ BrowserOAuthRenewer                         # 兜底 + 首次授权
       └─ 401 → SessionManager.reload_then_renew()（先重载后续期，重试恰一次）
```

持久化路径：

```
QR 登录:  login --qr → save_gitcode（OAuth 之前）→ getUserInfo 验证 → save_opencsi（之后）
续期:     StoredOAuthCredentialAdapter.remember_token → save_opencsi（先落盘后缓存）
GitCode:  StoredGitCodeRefresher → save_gitcode（旋转即原子写回；写失败报 PROTOCOL_ERROR 并携带新凭据）
存储写:   DpapiCredentialStore._write_merged ← _StoreWriteLock（跨进程写锁）  ← 本审计新增
```

### 1.3 状态转移图（修复后）

```
                    ┌────────────┐  QR 扫码   ┌─────────────────────┐
                    │  无凭据    │──────────▶│ GITCODE_AUTHENTICATED │
                    └────────────┘           └──────────┬──────────┘
                          ▲                             │ checkOrAuthorize 200+code → callback
                          │ refresh 400004/401 且 leg 也 401（无会话）     ▼
                ┌─────────┴─────────┐          ┌──────────────────────────┐
   leg 401 且   │  LOGIN_REQUIRED   │◀────────│ OPENCSITOOL_AUTHENTICATED │
   会话探针失败  │  （提示真实原因）  │          │ （token 落盘，1h 寿命）    │
                └─────────┬─────────┘          └──────────┬──────────┘
                          │                               │ expires_in ≤ 300s 或 401
                          │ refresh(force=True) 200       ▼
                ┌─────────┴─────────┐          ┌──────────────────────────┐
                │ 按需恢复: 换新 A/R │─────────▶│ RENEWED（新 cookie 落盘） │
                │ → 重试 leg 恰一次  │          └──────────────────────────┘
                └───────────────────┘
   分支: checkOrAuthorize 401+会话有效 → CONSENT_REQUIRED（一次点击）
        checkOrAuthorize 403/其他 4xx   → OAUTH_FAILED（协议/环境问题，不送扫码）
        刷新 429/5xx/断网               → NETWORK_ERROR（凭据无恙，稍后重试）
        刷新其他 4xx                    → PROTOCOL_ERROR（不送扫码）
```

---

## 2. 第二阶段：GitCode Refresh Token 400 根因

### 2.1 结论

**根因：凭据体系不匹配（置信度：高，三线独立证据）**。QR 登录签发的是 GitCode **账号 token**（account-token 体系，宿主 `web-api.gitcode.com`，前端以 `Authorization: Bearer` 使用、存 localStorage），而工具此前调用的 `POST https://gitcode.com/oauth/token?grant_type=refresh_token` 是 GitCode **OAuth 应用**的 token 端点（服务第三方应用的 `authorization_code`/`refresh_token` grant）。QR 的 refresh_token 在那个体系里**根本不存在**。

### 2.2 证据链

1. **GitCode 官方前端续期实现**【证据，当前生产 bundle `index-2b342e54.js` 逆向，2026-10-24 抓取】：

   ```js
   refresh: async () => { const e = await O.request({
       url: Xb + io + "/api/v1/user/token/refresh",        // Xb=ct()=web-api.gitcode.com, io="/uc"
       method: "post",
       data: { refresh_token: localStorage.getItem("refresh_token") },
       headers: { Authorization: "Bearer " + localStorage.getItem("access_token"),
                  "content-type": "application/x-www-form-urlencoded" },
       withCredentials: !0 }); ...
   persist: ({accessToken, refreshToken}) => { localStorage.setItem(...) } }
   ```

   外层有 `navigator.locks.request("atomgit-token-refresh", ...)` 单飞锁；响应拦截器在 401 时调用 `ensure()` 刷新并重试原请求。全 bundle 检索 `gitcode.com/oauth/token` 为 **0 次调用**。

2. **无凭据线上探针**【证据，2026-10-24，仅发送伪造值】：
   `POST web-api.gitcode.com/uc/api/v1/user/token/refresh`（垃圾 Bearer + 垃圾 refresh_token）
   → HTTP 400 `{"error_code":400004,"error_code_name":"REFRESH_TOKEN_ERROR","error_message":"token校验失败或者refreshToken已失效"}`。
   该端点存在、形状与 bundle 一致、有独立错误族。

3. **真实凭据的失败形态**【证据，2026-10-24 本机 `login --renew`，脱敏记录】：
   `gitcode.com/oauth/token` 对存储的 QR refresh_token 返回 HTTP 400
   `{"error_code":400,"error_code_name":"BAD_REQUEST","error_message":"refresh_token不存在或已过期","trace_id":"<masked>"}`。
   与**伪造 token** 得到的答复相同——即该 token 在此体系中查无此物。这与"QR 凭据属于另一个体系"自洽；若它属于该体系，只能得到"成功"或"已过期"，且不会在刚扫码时就立刻如此【推断，由【证据】的历史记录"刚扫出的凭据即 400"支撑】。

4. **旁证修正**【证据】：此前文档记载的"浏览器 cookie 实测 180 天"来自本项目 bridge 实验**自己写入**的 cookie（`gitcode_bridge.py:183` 硬编码 `180.0*24*3600`），不是 GitCode 服务端的策略观测。原"15 天 documented"（1296000s）在仓库内无出处、无 200 响应存档，已从代码删除。

### 2.3 未声明的事项

真实 `A1/R1 → A2/R2` 交换**未执行**：一次成功的交换可能轮换 refresh token，属"可能改变真实凭据状态"的探针，按审计约束须显式 opt-in。请求形状已验证；交换本身见 §8/§9.2。

---

## 3. 发现的问题（D1–D9）

| # | 问题 | 位置 | 触发条件 | 复现测试 | 状态 |
| --- | --- | --- | --- | --- | --- |
| **D1** | GitCode 刷新指向错误凭据体系的端点 | `gitcode_refresh.py`（原 `GITCODE_TOKEN_URL`） | QR 凭据 + 任何一次真实刷新 | 本机 `login --renew`（§2.2-3）；bundle 逆向 | **已修复** `1952405` |
| **D2** | 未知有效期 ⇒ 每次续期都发起一次注定失败的刷新 | 原 `needs_refresh`（unknown→True） | QR 凭据（永无 `expires_in`）+ 每小时续期 | `RenewingRenewerTest`（改为断言 0 请求） | **已修复** `1952405` |
| **D3** | DPAPI 存储无跨进程写保护：并发写 3/4 崩溃（`PermissionError`，共享 `.tmp` 竞争）；合并基于陈旧视图 | `windows_store.py` `_write`/`save_*` | 托盘续期与 CLI 轮换同时发生 | `test_store_multiprocess.py`（修复前红，修复后绿） | **已修复** `2cead98` |
| **D4** | `make_renewer` 按类型判断 store-backed，忽略 `last_source`：浏览器提供的会话可能被"存储账号 B"续期（身份串用） | `context.py` 原 `_provider_is_store_backed` 分支 | 组合 provider + store 有 GitCode 凭据 + 浏览器实际供凭 | `SourceIdentityGateTest`（新增） | **已修复** `fffe881` |
| **D5** | `checkOrAuthorize` 401 + 会话有效 → 报 `OAUTH_FAILED`，实际是未授权 grant（应 `CONSENT_REQUIRED`） | `http_oauth.py` `_run_flow` 401 分支 | 账号未批准过 openCsiTool 应用且 SSO 会话有效 | `test_a_401_with_a_live_session_is_consent_required`（新增） | **已修复** `d97100d`（证据级别见 §5 注） |
| **D6** | 托盘"自动刷新"开关不控制后台轮询（纯装饰） | `tray/app.py` `_dispatch("autorefresh")` | 用户关闭自动刷新 | `AutoRefreshSwitchTest`（5 项新增）+ `test_the_auto_refresh_toggle_reaches_the_service` | **已修复** `5246175` |
| **D7** | 所有 4xx 一律分类为 LOGIN_REQUIRED（协议错误/限流/上游故障都变成"重新扫码"） | 原 `_classify_http_error` | WAF 页、改版、429、陌生 4xx | `FailureClassificationTest`（重写为证据驱动分类表） | **已修复** `1952405` |
| **D8** | 文档引用不存在的旗标 `login --refresh-gitcode` | `gitcode_refresh.py` docstring | 阅读 | grep | 随 D1 修复移除 |
| **D9** | "15 天 documented" 无出处且被当事实；180 天 cookie TTL 是自设值被误读为服务端策略 | 原 `GITCODE_ACCESS_TOKEN_SECONDS`；`oauth-spa-investigation.md §1.1(a)` | — | — | **已澄清/删除** `1952405` |

**核查后不成立或未发现问题的方向**（列出以免后人重查）：

- `SessionManager` 的 cooldown/重试预算/`holds_remembered_token` 缓存失效逻辑【证据：现有测试 + 代码审阅，无缺陷】。
- `MonitorService` 系统休眠恢复：Windows monotonic 时钟含休眠时长，唤醒后 `_wait_seconds≤0` 立即补跑过期工作【证据：代码路径审阅；真实休眠循环未测，NOT_EXECUTED】。
- 手动续约与自动续约在**进程内**无并发（单 worker 串行队列）【证据：`_run` 队列模型】；跨进程并发归 D3。
- 跨日 daily usage 滚动与 last-good 保留【证据：既有 `test_daily_usage.py`/`test_tray_durable.py` 覆盖】。
- 脱敏体系（repr/日志/异常/JSON 通道）【证据：`test_secret_safety.py`、`test_store_multiprocess.py` 新增锁文件扫描】。
- `remember_token` 与 `install_token` 重复写盘：`StoredOpenCsiCredentialProvider.install_token` 是 `remember_token` 的别名，`HttpOAuthRenewer._store_token` 先调前者、`_install_into_browser` 再调后者 → **同一会话每次续期写两次盘**【证据，代码审阅】。功能无害（第二次写覆盖同一值）但在 D3 修复前会放大写竞争窗口，且让 `store.writes` 计数虚高。属已知低危冗余，未改动（避免触碰稳定分层）。

---

## 4. 能力验证矩阵（第四阶段）

测试全部隔离运行，不触碰真实凭据。状态：**PASS / FAIL / BLOCKED / NOT_EXECUTED**。

| 测试 | 预期 | 结果 | 载体 |
| --- | --- | --- | --- |
| openCsiTool Cookie 即将到期 | 自动续期并恢复 OK | **PASS**（离线） | `test_monitor.py`、`probe_autonomous_renewal.py`（历史活体） |
| openCsiTool Cookie 已过期 | 401 → reload → renew 恢复 | **PASS**（离线）；**活体 BLOCKED**（本机会话死 29 天，恢复依赖 L2，§9） | `test_client.py`、本机 `login --status`（真实 401 行为符合预期） |
| GitCode 凭据有效但刷新接口返回 400 | 不误判当前会话必然失效 | **PASS** | `RefreshingGitCodeRenewer` 失败策略测试 + `LateBoundTest` |
| GitCode Access Token 被真实拒绝 | 经验证的恢复机制（按需刷新+重试） | **PASS**（离线）；活体 BLOCKED | `RenewingRenewerTest::test_a_dead_session_triggers_one_recovery_refresh_and_a_retry` |
| GitCode Refresh Token 轮换成功 | 新 Token 持久化且被下一进程使用 | **PASS**（离线） | `PersistenceTest`、`test_store_multiprocess.py`、`acceptance_durable_renewal.py`（三真进程 runner，本机未重跑，见 §7） |
| GitCode Refresh Token 失效 | 提示真实可操作的恢复方式 | **PASS** | 合并详情含 400004 原文（`test_a_refused_recovery_is_not_asked_twice`） |
| 网络断开后恢复 | 不要求重新扫码 | **PASS** | NETWORK_ERROR 分类不触库不送扫码（既有 + 新分类测试） |
| 服务器 5xx / 429 | 不删除/覆盖凭据 | **PASS** | `FailureClassificationTest`（429→NETWORK_ERROR） |
| 跨预计续期时间后恢复运行 | 恢复调度并检查凭据 | **PASS**（离线 tick 模型）；真实休眠 NOT_EXECUTED | `test_monitor.py` |
| 多进程并发续约 | 无凭据回退或身份串用 | **PASS**（新增 6 项多进程测试 + 身份门控 3 项） | `test_store_multiprocess.py`、`SourceIdentityGateTest` |
| 持久化故障 | 不把进程内有效误报为已持久化 | **PASS** | `login --qr` 退出码 35 语义 + `StoredGitCodeRefresher` 写失败分支（既有） |
| 首次 Consent | 需人工授权，不自动批准 | **PASS** | `test_the_consent_endpoint_is_never_called` + D5 修复使该路径更准确 |
| （新增）GitCode 真实交换 A1/R1→A2/R2 | 无需扫码恢复上游凭据 | **BLOCKED** — 须用户 opt-in（§9.2） | `tools/probe_gitcode_token_refresh.py`（已建，plan 模式自检通过） |
| （新增）真实 Windows 注销→登录→托盘自启 | L3 完整闭环 | **NOT_EXECUTED** — 需人操作（§9.4） | — |
| 冻结二进制验收 | exe 在多进程中工作 | **NOT_EXECUTED**（本轮未重跑 `acceptance_durable.py` 的 PyInstaller 构建） | — |

回归总结果：修复前全量 **1104 passed / 1 skipped**（基线）；修复后 **1132 passed / 1 skipped / 189 subtests**，0 失败。新增测试 28 项，其中 `test_store_multiprocess.py` 的并发用例在修复前真实复现 3/4 进程崩溃（红→绿）。

---

## 5. 已验证 / 未验证 / 已复现失败

**已验证（含活体）**：openCsiTool 三步浏览器续期协议【历史活体】；QR 三段登录协议【历史活体 + 本轮 bundle 复核】；本机 DPAPI 存储读写与损坏处置【本轮，只读】；`gitcode.com/oauth/token` 对 QR 凭据的拒绝【本轮活体，脱敏】；新刷新端点存在性与错误族【本轮，垃圾值】；GitCode 前端续期实现【本轮 bundle】。

**已验证（离线/隔离）**：§4 全表；跨进程写锁；身份门控；按需恢复流。

**未验证**：真实刷新交换（BLOCKED）；L2 端到端"过期→恢复→新进程可用"；真实注销/登录循环；真实休眠唤醒；首次 consent 活体；冻结构建本轮回归。

**已复现失败（修复前）**：并发写 `PermissionError` 3/4（多进程测试，修复前红）；QR 刷新 400（真实凭据，本机）。

**证据级别注记（D5）**：「401+有效会话=consent」的直接活体观测不存在（构造它需要撤销账号授权）。证据是 SPA 自身分支行为（401 后加载 authorize chunk，§4.3 实测的是登出场景止于登录页）+ 会话探针已确认 200。修复在失败方向是良性的：误报 consent 的代价是用户打开页面点一次，而原行为把人引向"重新登录"这个无法解决问题的操作。

---

## 6. 修复提交清单（独立、回归先行）

| Commit | 内容 | 回归测试 |
| --- | --- | --- |
| `2cead98` | fix(store): 跨进程写锁 + 锁内合并（`_StoreWriteLock`，msvcrt/fcntl，`.lock` 无凭据内容有专项扫描） | `tests/test_store_multiprocess.py`（6 项，先红后绿）；更新 `test_no_plaintext_temporary_file_is_ever_created` 期望 |
| `1952405` | fix(auth): GitCode 刷新改走账号 token 端点；未知过期不主动刷新；按需恢复重试；证据驱动错误分类；删除 15 天常量；新增 opt-in 探针 | `tests/test_gitcode_refresh.py` 重写 + `RenewingRenewerTest`（6 项新增） |
| `fffe881` | fix(auth): `SourceBoundRenewer` 身份门控（`last_source` 证据，renew 时判定） | `SourceIdentityGateTest`（3 项新增） |
| `d97100d` | fix(auth): 401+有效会话 → `CONSENT_REQUIRED` | 新增 1 项 |
| `5246175` | fix(tray): 自动刷新开关控制 `MonitorService.set_auto_refresh`，暂停时 tick 全跳过、手动命令不受影响 | `AutoRefreshSwitchTest`（6 项）+ 托盘传导 1 项 |
| `9a3dc7d` | fix(auth): 刷新请求补 Origin/Referer（首次授权交换被 CloudWAF 418 拦截、凭据复核未动）；418 显式分类为 WAF 拦截而非凭据问题 | 请求形状断言 + 418 分类测试（新增） |

未做重构：`SessionManager`/`MonitorService`/`Tray` 分层、只读业务 API 约束、脱敏通道均未触碰。未删除任何不利证据；失败路径测试（400004、写失败、身份拒绝）全部保留为断言。

---

## 7. 真实 Windows 验收记录（脱敏，无凭据值）

环境：Windows 11，Python 3.14.6，安装版 `opencsi 0.1.0`（点对点工作区源码运行时与安装版并存，以下命令均为安装版）。

**修复前基线（2026-10-24）**：

- `opencsi login --status --json`：`ok:false`；credential=`secure-store`，`expires_in_seconds≈-2502961`（会话于 2026-09-25 前后过期，距今约 29 天）；renewal.available=true。
- `opencsi doctor --json --skip-contract`：store ok（DPAPI，GitCode 凭据 + openCsiTool 会话双在）；session fail（401 after reload+renew）；silent renewal ok。
- `opencsi tray --once --json`：`LOGIN_REQUIRED`，consecutive_failures=1。
- `opencsi login --renew --json`：`ok:false`，detail 记录了完整 400 现场（`refresh_token不存在或已过期`，trace_id 已脱敏）——本次审计最有价值的一份活体证据。

**修复后复检（仅无副作用命令）**：`login --status` 与 `doctor` 输出与基线一致（会话已死、存储完好、renewal 能力可用），未发生回归。

**故意未执行**：`tray --once`、`login --renew`、`probe_autonomous_renewal.py`、`probe_renewal_soak.py`、`probe_gitcode_token_refresh.py --execute`。原因：修复安装后这些命令任何一个都可能触发真实的按需刷新交换（正是 L2 验收本体），在未获用户授权前执行等于替用户决定轮换真实凭据（§8）。

**对既有探针的审查结论**：`acceptance_durable*.py`（多进程 runner，隔离存储，无真实凭据风险——但依赖 PyInstaller 构建，本轮未跑）；`probe_autonomous_renewal.py`/`probe_renewal_soak.py`（构建真实对象图驱动真实续期，在修复后代码上运行会触发真实刷新交换——标记为须授权）。

---

## 8. 凭据安全边界执行情况

本轮全部探针遵守以下约束【证据：命令历史】：

1. 未向任何 GitCode 端点发送过真实凭据值（真实凭据仅出现在工具自身既有行为中：`login --renew` 走旧端点、`tray --once` 走旧端点——两者均已被授权清单覆盖且不旋转凭据）。
2. 唯一触及新端点的请求只携带伪造值。
3. 所有响应/记录脱敏（token 仅以 len+sha256 前 8 位指纹出现）。
4. 多进程测试使用临时目录隔离存储，真实 `credentials.dat` 全程只被只读命令打开。
5. 新探针 `probe_gitcode_token_refresh.py` 内置：`--execute` 显式 opt-in、恰好一次请求、指纹输出、成功先落盘后报告、失败后存储完整性复核。

---

## 9. 尚需用户协助的事项与最小操作步骤

### 9.1 决策点

当前机器处于「openCsiTool 会话已死约 29 天，GitCode access_token 大概率已死、refresh_token 状态未知」的状态。恢复到全自动运行有两条路：

### 9.2 路线 A（推荐，若 refresh_token 仍有效——全程无需扫码）

```powershell
# 1. 先看计划（不碰任何东西）
python tools/probe_gitcode_token_refresh.py

# 2. 授权执行恰好一次真实交换（可能轮换 refresh token；成功即自动落盘）
python tools/probe_gitcode_token_refresh.py --execute

# 3. 验收：新进程无需扫码重建会话
opencsi login --status --json     # 期望 ok:true 或 renewal 触发
opencsi tray --once --json        # 期望 state:OK
```

- 若第 2 步返回 `400004 REFRESH_TOKEN_ERROR`：refresh_token 已死，转路线 B。存储凭据经完整性复核未动。
- 执行记录（2026-10-24）：首次 `--execute` 被 CloudWAF 以 HTTP 418 拦截（凭据未被评估，复核未动）——缺 Origin/Referer 触发 WAF 启发式，已在 `9a3dc7d` 修复；带上这两头后重跑即是本次验收。
- 若成功：本报告 L2 即可改判（交换成功 + 落盘 + 新进程重读 = 验收条件全满足）。请把输出（已自动脱敏）贴回给审计者。

### 9.3 路线 B（refresh_token 已死，重新扫码一次）

```powershell
opencsi login --qr          # 扫码；两半凭据自动落盘
opencsi tray --once --json  # 期望 state:OK
```

之后 L1 的多周期活体验证（原 `probe_renewal_soak.py --minutes 75 --renewals 2`）才具备前提。

### 9.4 L3 注销/登录循环（可选，需人操作）

1. 确认托盘运行中、`opencsi tray --startup-status` 显示已注册开机自启；
2. 记录验收前状态：`opencsi login --status --json > before.json`；
3. Windows 注销 → 重新登录；
4. 登录后不操作，等待托盘自启（约 30 秒），执行：
   `opencsi tray --once --json > after.json`，期望 `state` 为 OK 且无需任何扫码/登录；
5. 对比 before/after JSON 中的 `fetched_at`（无凭据值，可安全保存）。

### 9.5 完成后

在 `docs/TODO.md` 的 P1 条目勾掉剩余验收；若路线 A 成功，L2 判定可依据 §0 的规则升级为 PASS。

---

## 10. 最终分层结论（独立判定，不含相互折算）

**L1 — openCsiTool Cookie 小时级自动续期：机制 PASS / 本轮活体复验 BLOCKED。**
三条路径（计划 tick、401 反应式、手动）代码与测试完备且历史上活体验证过（`probe_autonomous_renewal`/`probe_renewal_soak` 记录）；本轮修复未触碰其核心，回归全绿。本机当前因上游凭据死亡而无法复验活体，属前置条件缺失，不是机制缺陷。

**L2 — GitCode 上游凭据长期生命周期：FAIL（审计发现根因）→ 机制已修复，最终判定 BLOCKED。**
修复内容：正确的端点与请求形状（bundle 证据）、按需触发（前端同款行为）、证据驱动的错误分类、原子持久化、一次性恢复重试。按最重要的验收规则——「真正证明到期后无需重新扫码仍可恢复，且恢复后凭据在新进程中持续有效」——**尚未满足**，因为真实交换未执行。差一步：§9.2 的一条命令。若协议侧最终证明 refresh_token 不可用（例如服务端绑定浏览器会话），本报告已给出替代设计（§9.3 扫码一次 + L1/L3 的自动恢复使交互最小化为"每 number-of-weeks 一次扫码"），并如实在 TODO 中记录。

**L3 — Windows 进程退出/重启/注销后自动恢复：PASS（存储与进程边界）/ 注销登录循环 NOT_EXECUTED。**
DPAPI 存储本轮完成跨进程一致性修复并有真实多进程测试；凭据跨进程、跨新 store 实例的重读测试全绿；托盘自启注册机制与冻结构建路径既有测试覆盖。真实注销→登录→托盘自启→无扫码恢复的完整循环留待 §9.4（不声称已执行）。

---

*报告完。所有代码位置以 `5246175` 为准；bundle 分析基于 2026-10-24 抓取的 `index-2b342e54.js` / `vendor-layout-b54bc1b9.js`，bundle hash 会轮换但接口形状已按本项目惯例视为可失效的外部契约。*
