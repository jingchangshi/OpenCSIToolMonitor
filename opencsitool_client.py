"""
opencsitool_client.py — openCsiTool「我的工具」只读客户端（设计草案）

调查依据: openCsiTool_API_Investigation_Report.md
目标页面: https://opencsitool.com/myTools

设计原则
--------
1. 只读：仅使用 GET。页面强制的两个只读 POST（menu/list、collectVisitData）
   本客户端不实现——它们是 UI 框架行为，与取数无关。
2. 复用用户自身会话，不绕过任何认证或权限控制。
3. 敏感值（Cookie / virtualKey）永不落盘、永不进日志、永不进异常消息。
4. 三种响应封装（{code,data,message} / 裸 JSON / {success,message}）统一归一化。
5. 客户端聚合逻辑与页面严格一致（见报告 §5 的 39 项映射证明）。

关键事实（来自实测，务必遵守）
------------------------------
* 认证 = HttpOnly Cookie `token`（GitCode OAuth 签发，TTL ≈ 0.97 小时）。
* 服务端【只认 Cookie】。发送 Authorization 头会得到 401 Invalid Authorization。
* 401 "empty Authorization" 的真实含义是【缺少 Cookie】，不是缺少 HTTP 头。
* 无 CSRF Token；无速率限制头。
* personalQueueStatus 的 startDate/endDate 只影响 tokenTrend 长度，
  requestList（工具列表）恒为全量，与日期无关。
* /myTools 无搜索、无排序、无有效分页、无详情接口 → 全部本地过滤。

用法
----
    from opencsitool_client import OpenCsiToolClient, CdpCookieProvider

    with OpenCsiToolClient(CdpCookieProvider()) as client:
        client.login_or_restore_session()
        snap = client.get_my_tools()
        print(snap.total_tokens, snap.pr_count, f"{snap.adoption_rate:.1%}")
        for g in snap.active_grants:
            print(g.request_type, g.account_name, g.status_text, g.virtual_key_masked)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://opencsitool.com"
APP_PREFIX = "/opencsitool"
REST = f"{APP_PREFIX}/rest/v1"
LLM_GATEWAY = f"{APP_PREFIX}/llmgateway/rest/v1/users"

DEFAULT_TIMEOUT = 15.0
SLOW_TIMEOUT = 30.0          # call-logs 前端亦为 30s
CACHE_TTL = 300.0            # 5 分钟；页面自身行为是"进入拉一次"


# ────────────────────────────── 异常体系 ──────────────────────────────
class OpenCsiToolError(Exception):
    """所有客户端异常的基类。异常消息只含端点路径与状态码，不含凭证。"""


class SessionExpiredError(OpenCsiToolError):
    """401 empty Authorization —— 实际含义是 Cookie 缺失/过期，需重新登录。"""


class BadAuthHeaderError(OpenCsiToolError):
    """401 Invalid Authorization —— 说明误发了 Authorization 头，应移除。"""


class PermissionDeniedError(OpenCsiToolError):
    """403 —— 用户权限不足（如 VISITOR 访问管理员接口）。禁止重试。"""


class MissingParamError(OpenCsiToolError):
    """400 缺少请求参数。"""


class ServerError(OpenCsiToolError):
    """500 系统内部错误，可退避重试。"""


# ────────────────────────────── 凭证提供者 ──────────────────────────────
class CredentialProvider(Protocol):
    """凭证提供者契约。实现方负责拿到 token Cookie 值。"""

    def get_token(self) -> str | None:
        """返回 token 值，或 None 表示当前不可用。"""

    def invalidate(self) -> None:
        """标记当前 token 失效，下次 get_token 应重新获取。"""


class CdpCookieProvider:
    """方案 A（首选）：从本机已登录 Chrome 通过 CDP 抽取 Cookie。

    实现要点：
      - 连接 localhost CDP 端点
      - 调用 Network.getCookies(urls=["https://opencsitool.com/"])
      - 只取 name == "token" 且 domain == "opencsitool.com" 的那一条
      - 值仅存于内存；__repr__ 必须脱敏

    这是唯一可持续且合规的会话维持方式：零额外认证、用户无感。
    """

    def __init__(self, cdp_url: str = "http://127.0.0.1:9222") -> None:
        self._cdp_url = cdp_url
        self._token: str | None = None

    def get_token(self) -> str | None:
        if self._token:
            return self._token
        self._token = self._fetch_from_cdp()
        return self._token

    def _fetch_from_cdp(self) -> str | None:
        """通过 CDP 读取 opencsitool.com 的 token Cookie。

        参考实现（需按实际 CDP 客户端调整）::

            import httpx
            r = httpx.get(f"{self._cdp_url}/json/list", timeout=5)
            ws = next(t["webSocketDebuggerUrl"] for t in r.json()
                      if t["type"] == "page")
            # ... 通过 WebSocket 发送 Network.getCookies ...
            # cookies = resp["result"]["cookies"]
            # for c in cookies:
            #     if c["name"] == "token" and c["domain"] == "opencsitool.com":
            #         return c["value"]
        """
        return None

    def invalidate(self) -> None:
        self._token = None

    def __repr__(self) -> str:                    # 防泄露
        return f"CdpCookieProvider(cdp_url={self._cdp_url!r}, token=<redacted>)"


class ManualCookieProvider:
    """方案 C（兜底）：由用户提供 token 字符串（仅存内存，约 1 小时后需重来）。"""

    def __init__(self, token: str) -> None:
        self._token = token

    def get_token(self) -> str | None:
        return self._token

    def invalidate(self) -> None:
        self._token = None

    def __repr__(self) -> str:
        return "ManualCookieProvider(token=<redacted>)"


# ────────────────────────────── 数据模型 ──────────────────────────────
@dataclass(frozen=True)
class SyncStatus:
    data_fresh_time: str | None = None
    etl_time: str | None = None
    replication_time: str | None = None


@dataclass(frozen=True)
class TokenBudget:
    exists: bool
    max_budget: float | None = None
    budget_duration: str | None = None
    spend: float | None = None


@dataclass(frozen=True)
class ToolGrant:
    """对应 personalQueueStatus.data.requestList[]（接口共 22 个字段）。"""

    id: int
    application_number: str
    request_type: str
    status: int
    account_name: str
    issue_date: str | None
    create_time: str | None
    last_used_date: str | None
    token_usage: int
    request_count: int
    pr_count: int
    added_lines_count: int
    generated_code_lines: int
    adopted_code_lines: int
    remark: str | None = None
    wait_days: int = 0
    queue_position: int = 0
    estimated_wait_time: str | None = None
    issue_count: int = 0
    ai_tool_name: str | None = None
    employee_id: str | None = None
    _virtual_key: str | None = field(default=None, repr=False)   # 永不 repr

    @property
    def status_text(self) -> str:
        """页面映射规则：status==1 → 使用中；否则 → 已失效。"""
        return "使用中" if self.status == 1 else "已失效"

    @property
    def virtual_key_masked(self) -> str:
        """页面映射规则：virtualKey[:10] + '****'；为空显示 '-'。"""
        return f"{self._virtual_key[:10]}****" if self._virtual_key else "-"

    @property
    def is_active(self) -> bool:
        return self.status == 1


@dataclass(frozen=True)
class TokenTrendPoint:
    date: str
    request_type: str
    tokens: int
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True)
class MyToolsSnapshot:
    """「我的工具」页面完整快照 —— 与页面渲染内容一一对应。

    接口直接提供：user_id / bound_employee_id / grants / token_trend /
                  total_tokens / total_request_count / sync_status / token_budget
    客户端聚合：  pr_count / added_lines_count / generated_code_lines /
                  adopted_code_lines / adoption_rate / tokens_by_request_type
    """

    user_id: str
    bound_employee_id: str
    grants: list[ToolGrant]
    token_trend: list[TokenTrendPoint]
    total_tokens: int
    total_request_count: int
    sync_status: SyncStatus
    token_budget: TokenBudget | None
    fetched_at: float

    # ── 以下均为客户端聚合（复刻页面逻辑，接口不直接提供）──
    @property
    def pr_count(self) -> int:
        return sum(g.pr_count for g in self.grants)

    @property
    def added_lines_count(self) -> int:
        return sum(g.added_lines_count for g in self.grants)

    @property
    def generated_code_lines(self) -> int:
        return sum(g.generated_code_lines for g in self.grants)

    @property
    def adopted_code_lines(self) -> int:
        return sum(g.adopted_code_lines for g in self.grants)

    @property
    def adoption_rate(self) -> float:
        gen = self.generated_code_lines
        return (self.adopted_code_lines / gen) if gen else 0.0

    @property
    def tokens_by_request_type(self) -> dict[str, int]:
        """概览卡片 1 的子项来源：API套餐 / Trae 等分组求和。"""
        out: dict[str, int] = {}
        for g in self.grants:
            out[g.request_type] = out.get(g.request_type, 0) + g.token_usage
        return out

    @property
    def active_grants(self) -> list[ToolGrant]:
        return [g for g in self.grants if g.is_active]


@dataclass(frozen=True)
class ModelPrice:
    request_type: str
    display_name: str
    bill_type: str
    enabled: int
    price_mode: str | None = None
    blended_price: float | None = None
    input_price: float | None = None
    output_price: float | None = None
    monthly_fee: float | None = None


# ────────────────────────────── 客户端 ──────────────────────────────
class OpenCsiToolClient:
    """openCsiTool「我的工具」只读客户端。

    用法::

        with OpenCsiToolClient(CdpCookieProvider()) as client:
            snap = client.get_my_tools("2026-08-20", "2026-09-19")
            for g in snap.active_grants:
                print(g.request_type, g.account_name, g.status_text)
            print(snap.total_tokens, snap.pr_count, f"{snap.adoption_rate:.1%}")
    """

    def __init__(
        self,
        credentials: CredentialProvider,
        *,
        base_url: str = BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        cache_ttl: float = CACHE_TTL,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._creds = credentials
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._cache_ttl = cache_ttl
        self._client = httpx.Client(
            base_url=self._base,
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN",
                # 刻意不设置 Authorization：服务端只认 Cookie，
                # 发送 Bearer 反而会得到 401 Invalid Authorization
            },
        )
        self._cache: dict[str, tuple[float, Any]] = {}
        self._identity: dict[str, Any] | None = None

    # ── 生命周期 ──────────────────────────────────────────────
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OpenCsiToolClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── 会话 ──────────────────────────────────────────────────
    def login_or_restore_session(self) -> dict[str, Any]:
        """恢复/验证会话，返回用户身份信息。

        返回::

            {"user_id": ..., "employee_id": "653124", "user_name": "shijingchang",
             "account_id": ..., "organization_id": ..., "role_view": "Normal",
             "role_view_name": "普通用户", "organization_name": "体验项目",
             "roles": ["VISITOR"]}

        抛出:
            SessionExpiredError —— Cookie 缺失/过期，需用户重新完成 GitCode 登录。
        """
        token = self._creds.get_token()
        if not token:
            raise SessionExpiredError(
                "未获取到 openCsiTool 会话 Cookie。请在浏览器中打开 "
                "https://opencsitool.com 并完成 GitCode 登录后重试。"
            )
        self._client.cookies.set("token", token, domain="opencsitool.com", path="/")

        info = self._get_json(f"{REST}/user/getUserInfo")     # 裸 JSON
        user = (info or {}).get("user") or {}
        org_id = user.get("currentOrganizationId") or ""

        roles: list[str] = []
        if org_id:
            raw = self._get_json(
                f"{REST}/user/getUserRolesByOrganizationId",
                params={"organization_id": org_id},
            )
            roles = [r.get("name") for r in (raw or []) if r.get("name")]

        self._identity = {
            "user_id": user.get("id"),
            "employee_id": user.get("employeeId"),
            "user_name": user.get("userName"),
            "account_id": ((info or {}).get("threePartyUserInfo") or {}).get("accountId"),
            "organization_id": org_id,
            "organization_name": user.get("currentOrganizationName"),
            "role_view": user.get("currentRoleView"),
            "role_view_name": user.get("currentRoleViewName"),
            "roles": roles,
        }
        return self._identity

    def session_status(self) -> dict[str, Any]:
        """会话健康检查：是否可用、身份信息。用于前置检查。"""
        try:
            ident = self.login_or_restore_session()
            return {"ok": True, "identity": ident}
        except OpenCsiToolError as e:
            return {"ok": False, "error": type(e).__name__, "message": str(e)}

    # ── 主接口 ────────────────────────────────────────────────
    def get_my_tools(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        *,
        refresh: bool = False,
    ) -> MyToolsSnapshot:
        """拉取「我的工具」完整快照（页面主数据源）。

        Args:
            start_date: `YYYY-MM-DD`；省略则用接口默认区间。
            end_date:   `YYYY-MM-DD`。
            refresh:    忽略缓存强制刷新。

        说明:
            `startDate`/`endDate` **只影响 tokenTrend 的长度**，
            `requestList`（工具列表）恒为全量，与日期无关。
            实测响应体积：1d 2070B < 7d 3918B < 本月 5603B < 30d 6887B ≈ 全量 7056B。
        """
        params: dict[str, str] = {}
        if start_date:
            params["startDate"] = start_date
        if end_date:
            params["endDate"] = end_date

        key = f"queue:{start_date}:{end_date}"
        payload = self._cached(key, refresh, lambda: self._get_json(
            f"{REST}/ai/operations/personalQueueStatus", params=params or None
        ))
        data = (payload or {}).get("data") or {}

        grants = [self._to_grant(x) for x in (data.get("requestList") or [])]
        trend = [
            TokenTrendPoint(
                date=p.get("date", ""),
                request_type=p.get("requestType", ""),
                tokens=int(p.get("tokens") or 0),
                prompt_tokens=int(p.get("promptTokens") or 0),
                completion_tokens=int(p.get("completionTokens") or 0),
            )
            for p in (data.get("tokenTrend") or [])
        ]
        summary = data.get("tokenSummary") or {}
        sync = data.get("syncStatus") or {}
        budget = data.get("tokenBudget")

        return MyToolsSnapshot(
            user_id=data.get("userId", ""),
            bound_employee_id=str(data.get("boundEmployeeId") or ""),
            grants=grants,
            token_trend=trend,
            total_tokens=int(summary.get("totalTokens") or 0),
            total_request_count=int(summary.get("totalRequestCount") or 0),
            sync_status=SyncStatus(
                data_fresh_time=sync.get("dataFreshTime"),
                etl_time=sync.get("etlTime"),
                replication_time=sync.get("replicationTime"),
            ),
            token_budget=(
                TokenBudget(
                    exists=bool(budget.get("exists")),
                    max_budget=budget.get("maxBudget"),
                    budget_duration=budget.get("budgetDuration"),
                    spend=budget.get("spend"),
                ) if budget else None
            ),
            fetched_at=time.time(),
        )

    # ── 本地检索（无对应接口，纯客户端过滤）──────────────────
    def list_my_tools(self, *, active_only: bool = False) -> list[ToolGrant]:
        """列出工具。页面无搜索接口，全部本地过滤。"""
        snap = self.get_my_tools()
        return snap.active_grants if active_only else snap.grants

    def get_tool(self, identifier: str | int) -> ToolGrant | None:
        """按 id 或 applicationNumber 取单个工具（本地查找，无详情接口）。"""
        for g in self.get_my_tools().grants:
            if str(g.id) == str(identifier) or g.application_number == str(identifier):
                return g
        return None

    def search_tools(
        self,
        keyword: str = "",
        *,
        status: int | None = None,
        request_type: str | None = None,
    ) -> list[ToolGrant]:
        """本地关键字 + 条件过滤。

        搜索域：account_name / request_type / application_number / remark。
        """
        kw = (keyword or "").strip().lower()
        out: list[ToolGrant] = []
        for g in self.get_my_tools().grants:
            if status is not None and g.status != status:
                continue
            if request_type and g.request_type != request_type:
                continue
            if kw:
                hay = " ".join(filter(None, [
                    g.account_name, g.request_type, g.application_number, g.remark or "",
                ])).lower()
                if kw not in hay:
                    continue
            out.append(g)
        return out

    # ── 辅助接口 ──────────────────────────────────────────────
    def get_token_trend(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        *,
        with_display_name: bool = True,
    ) -> list[dict[str, Any]]:
        """趋势序列；可选把 requestType 关联为 displayName（图表图例显示名）。"""
        snap = self.get_my_tools(start_date, end_date)
        names = (
            {p.request_type: p.display_name for p in self.get_model_prices()}
            if with_display_name else {}
        )
        return [
            {
                "date": p.date,
                "request_type": p.request_type,
                "display_name": names.get(p.request_type, p.request_type),
                "tokens": p.tokens,
                "prompt_tokens": p.prompt_tokens,
                "completion_tokens": p.completion_tokens,
            }
            for p in snap.token_trend
        ]

    def get_model_prices(self, *, enabled_only: bool = False) -> list[ModelPrice]:
        """模型费用单价表（裸数组响应，20 行）。变化频率低，可长缓存。

        也是图表"消费金额"口径与图例显示名的换算依据。
        """
        raw = self._cached("cost", False, lambda: self._get_json(f"{REST}/ai/config/cost"))
        out = [
            ModelPrice(
                request_type=x.get("requestType", ""),
                display_name=x.get("displayName", ""),
                bill_type=x.get("billType", ""),
                enabled=int(x.get("enabled") or 0),
                price_mode=x.get("priceMode"),
                blended_price=x.get("blendedPrice"),
                input_price=x.get("inputPrice"),
                output_price=x.get("outputPrice"),
                monthly_fee=x.get("monthlyFee"),
            )
            for x in (raw or [])
        ]
        return [p for p in out if p.enabled] if enabled_only else out

    def get_call_logs(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """调用日志（llmgateway 服务，前端超时 30s）。

        当前调查账号返回 {"list":[],"total":0,"page":1,"pageSize":20}。
        """
        emp = self._require_employee_id()
        params: dict[str, Any] = {"page": page, "pageSize": page_size}
        if start_date:
            params["startDate"] = start_date
        if end_date:
            params["endDate"] = end_date
        return self._get_json(
            f"{LLM_GATEWAY}/{emp}/call-logs", params=params, timeout=SLOW_TIMEOUT
        )

    def get_key_budget(self) -> TokenBudget:
        """员工 Token 预算（llmgateway 侧，会覆盖 personalQueueStatus.tokenBudget）。"""
        emp = self._require_employee_id()
        raw = self._get_json(f"{LLM_GATEWAY}/{emp}/key-budget") or {}
        return TokenBudget(
            exists=bool(raw.get("exists")),
            max_budget=raw.get("maxBudget"),
            budget_duration=raw.get("budgetDuration"),
            spend=raw.get("spend"),
        )

    # ── 内部：传输与错误归一化 ────────────────────────────────
    def _get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        _retried: bool = False,
    ) -> Any:
        try:
            r = self._client.get(path, params=params, timeout=timeout or self._timeout)
        except httpx.HTTPError as e:
            raise OpenCsiToolError(f"网络错误: {type(e).__name__}") from e

        if r.status_code == 200:
            try:
                return r.json()
            except ValueError as e:
                raise OpenCsiToolError("响应不是合法 JSON") from e

        body = (r.text or "").strip()

        if r.status_code == 401:
            self._creds.invalidate()
            if "Invalid Authorization" in body:
                # 说明调用方误发了 Authorization 头
                raise BadAuthHeaderError(
                    "服务端拒绝了 Authorization 头。openCsiTool 只认 Cookie，"
                    "请移除请求中的 Authorization。"
                )
            # "empty Authorization" 的实际含义是 Cookie 缺失/过期
            if not _retried:
                token = self._creds.get_token()
                if token:
                    self._client.cookies.set(
                        "token", token, domain="opencsitool.com", path="/"
                    )
                    return self._get_json(
                        path, params=params, timeout=timeout, _retried=True
                    )
            raise SessionExpiredError(
                "openCsiTool 会话已过期（Cookie 有效期约 1 小时）。"
                "请在浏览器中重新完成 GitCode 登录。"
            )

        if r.status_code == 403:
            raise PermissionDeniedError(self._message(body) or "权限不足")

        if r.status_code == 400:
            raise MissingParamError(self._message(body) or "缺少请求参数")

        if 500 <= r.status_code < 600:
            raise ServerError(self._message(body) or "服务端内部错误")

        raise OpenCsiToolError(
            f"HTTP {r.status_code}: {self._message(body) or body[:120]}"
        )

    @staticmethod
    def _message(body: str) -> str:
        """从 `{"message": "..."}` 中提取中文业务提示。"""
        try:
            return str(json.loads(body).get("message") or "")
        except Exception:
            return ""

    def _require_employee_id(self) -> str:
        if not self._identity:
            self.login_or_restore_session()
        emp = (self._identity or {}).get("employee_id")
        if not emp:
            raise OpenCsiToolError(
                "未能获取 employeeId，请先调用 login_or_restore_session()"
            )
        return str(emp)

    # ── 内部：缓存 ────────────────────────────────────────────
    def _cached(self, key: str, refresh: bool, factory) -> Any:
        now = time.time()
        if not refresh:
            hit = self._cache.get(key)
            if hit and (now - hit[0]) < self._cache_ttl:
                return hit[1]
        val = factory()
        self._cache[key] = (now, val)
        return val

    # ── 内部：模型转换 ────────────────────────────────────────
    @staticmethod
    def _to_grant(x: dict[str, Any]) -> ToolGrant:
        return ToolGrant(
            id=int(x.get("id") or 0),
            application_number=x.get("applicationNumber") or "",
            request_type=x.get("requestType") or "",
            status=int(x.get("status") or 0),
            account_name=x.get("accountName") or "",
            issue_date=x.get("issueDate"),
            create_time=x.get("createTime"),
            last_used_date=x.get("lastUsedDate"),
            token_usage=int(x.get("tokenUsage") or 0),
            request_count=int(x.get("requestCount") or 0),
            pr_count=int(x.get("prCount") or 0),
            added_lines_count=int(x.get("addedLinesCount") or 0),
            generated_code_lines=int(x.get("generatedCodeLines") or 0),
            adopted_code_lines=int(x.get("adoptedCodeLines") or 0),
            remark=x.get("remark"),
            wait_days=int(x.get("waitDays") or 0),
            queue_position=int(x.get("queuePosition") or 0),
            estimated_wait_time=x.get("estimatedWaitTime"),
            issue_count=int(x.get("issueCount") or 0),
            ai_tool_name=x.get("aiToolName"),
            employee_id=x.get("employeeId"),
            _virtual_key=x.get("virtualKey"),      # repr=False，永不输出
        )


# ────────────────────────────── 契约测试基线 ──────────────────────────────
def test_mapping_regression(client: OpenCsiToolClient) -> None:
    """用真实会话验证客户端聚合逻辑与页面渲染一致。

    断言数值取自调查报告 §5 的 39 项映射证明
    （同一次请求: startDate=2026-08-20&endDate=2026-09-19）。
    注意：这些是 2026-09-19 的快照值，站点数据变化后需同步更新基线。
    """
    snap = client.get_my_tools("2026-08-20", "2026-09-19")

    # ── 概览卡片（8 项）──
    assert snap.total_tokens == sum(g.token_usage for g in snap.grants)
    assert snap.pr_count == 246
    assert snap.added_lines_count == 31167
    assert snap.generated_code_lines == 3150
    assert snap.adopted_code_lines == 120
    assert round(snap.adoption_rate * 100, 1) == 3.8

    by_type = snap.tokens_by_request_type
    assert round(by_type["API_BUNDLE"] / 1e8, 1) == 28.0
    assert round(by_type["TRAE"] / 1e8, 1) == 2.6

    # ── 费用单价表格（7 列 × 3 行）──
    g0 = next(g for g in snap.grants if g.id == 5593)
    assert g0.application_number == "REQ202608170007"
    assert g0.request_type == "API_BUNDLE"
    assert g0.status_text == "使用中"
    assert g0.account_name == "AI编程助手-002"
    assert g0.issue_date == "2026-08-17"
    assert g0.create_time == "2026-08-17 16:28:00"
    assert g0.virtual_key_masked.startswith("sk-bM4LUSm")
    assert g0.virtual_key_masked.endswith("****")
    assert g0.last_used_date == "2026-09-19 00:00:00"

    g2 = next(g for g in snap.grants if g.id == 1094)
    assert g2.status_text == "已失效"
    assert g2.virtual_key_masked == "-"

    # ── 趋势图 ──
    assert all(p.tokens == p.prompt_tokens + p.completion_tokens
               for p in snap.token_trend)
    assert len({p.date for p in snap.token_trend}) == 30
    assert {p.request_type for p in snap.token_trend} == {
        "DEEPSEEK_V4_FLASH_0731", "GLM_5_3_FLASH", "GLM_5_3",
        "DEEPSEEK_V4_PRO", "QWEN3_8_FLASH",
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    with OpenCsiToolClient(CdpCookieProvider()) as _client:
        status = _client.session_status()
        if not status["ok"]:
            raise SystemExit(f"会话不可用: {status['message']}")
        print("身份:", json.dumps(status["identity"], ensure_ascii=False, indent=2))
        _snap = _client.get_my_tools()
        print(f"Token 总量: {_snap.total_tokens:,}")
        print(f"请求次数:   {_snap.total_request_count:,}")
        print(f"PR 数量:    {_snap.pr_count}")
        print(f"采纳率:     {_snap.adoption_rate:.1%}")
        print(f"数据更新至: {_snap.sync_status.data_fresh_time}")
        for _g in _snap.grants:
            print(f"  [{_g.status_text}] {_g.request_type:12s} "
                  f"{_g.account_name}  {_g.virtual_key_masked}  "
                  f"{_g.token_usage:,} tokens")
