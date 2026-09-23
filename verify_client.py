"""Offline verification of opencsitool_client.py against the real captured API response.

Stubs httpx so the client can be exercised without network access, replays the
actual captured personalQueueStatus / ai-config-cost / call-logs / key-budget
payloads through the transport layer, and asserts the 39 mapping proofs from the
investigation report. Prints only masked/aggregate values.
"""

import importlib.util
import json
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
CAP = os.path.join(HERE, "capture")
RESP = os.path.join(CAP, "responses")

# ── stub httpx (not installed in this environment) ────────────────────────
httpx = types.ModuleType("httpx")


class HTTPError(Exception):
    pass


class BaseTransport:
    pass


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def json(self):
        if isinstance(self._payload, str):
            return json.loads(self._payload)
        return self._payload


class _Cookies:
    def __init__(self):
        self.store = {}

    def set(self, name, value, **kw):
        self.store[name] = value


class Client:
    def __init__(self, base_url=None, timeout=None, transport=None, follow_redirects=None,
                 headers=None, **kw):
        self.base_url = base_url
        self.cookies = _Cookies()
        self.requests = []

    def get(self, path, params=None, timeout=None):
        self.requests.append((path, params))
        return ROUTER(path, params)

    def close(self):
        pass


httpx.HTTPError = HTTPError
httpx.BaseTransport = BaseTransport
httpx.Client = Client
sys.modules["httpx"] = httpx

# ── replay the captured payloads ──────────────────────────────────────────
with open(os.path.join(RESP, "07_opencsitool_rest_v1_ai_operations_personalQueueStatus.json"),
          encoding="utf-8") as f:
    QUEUE = json.load(f)
with open(os.path.join(RESP, "05_opencsitool_rest_v1_ai_config_cost.json"), encoding="utf-8") as f:
    COST = json.load(f)
with open(os.path.join(RESP, "10_opencsitool_llmgateway_rest_v1_users_653124_call-logs.json"),
          encoding="utf-8") as f:
    LOGS = json.load(f)
with open(os.path.join(RESP, "11_opencsitool_llmgateway_rest_v1_users_653124_key-budget.json"),
          encoding="utf-8") as f:
    BUDGET = json.load(f)
with open(os.path.join(RESP, "00_opencsitool_rest_v1_user_getUserInfo.json"), encoding="utf-8") as f:
    USERINFO = json.load(f)
with open(os.path.join(RESP, "01_opencsitool_rest_v1_user_getUserRolesByOrganizationId.json"),
          encoding="utf-8") as f:
    ROLES = json.load(f)


def default_router(path, params):
    if path.endswith("/user/getUserInfo"):
        return _Resp(200, USERINFO)
    if "getUserRolesByOrganizationId" in path:
        return _Resp(200, ROLES)
    if path.endswith("/ai/config/cost"):
        return _Resp(200, COST)
    if path.endswith("/personalQueueStatus"):
        return _Resp(200, QUEUE)
    if path.endswith("/call-logs"):
        return _Resp(200, LOGS)
    if path.endswith("/key-budget"):
        return _Resp(200, BUDGET)
    return _Resp(404, {"message": "path error"})


ROUTER = default_router


# ── load the client ───────────────────────────────────────────────────────
spec = importlib.util.spec_from_file_location("oc", os.path.join(HERE, "opencsitool_client.py"))
oc = importlib.util.module_from_spec(spec)
sys.modules["oc"] = oc          # dataclasses needs the module registered
spec.loader.exec_module(oc)

TOKEN = "x" * 333


class StubCreds:
    def __init__(self, token=TOKEN):
        self._t = token
        self.calls = 0

    def get_token(self):
        self.calls += 1
        return self._t

    def invalidate(self):
        self._t = None


print("=" * 72)
print("A. 会话恢复 + 身份解析")
print("=" * 72)
client = oc.OpenCsiToolClient(StubCreds(), transport=BaseTransport())
ident = client.login_or_restore_session()
for k in ("user_id", "employee_id", "user_name", "account_id", "organization_id",
          "organization_name", "role_view", "role_view_name", "roles"):
    print(f"  {k:20s} = {ident.get(k)}")
assert ident["employee_id"] == "653124"
assert ident["role_view_name"] == "普通用户"
assert ident["roles"] == ["VISITOR"]
print("  -> OK")

print()
print("=" * 72)
print("B. 快照解析 + 客户端聚合（对照报告 §5 的 39 项映射）")
print("=" * 72)
snap = client.get_my_tools("2026-08-20", "2026-09-19")

checks = []


def chk(label, got, want):
    ok = got == want
    checks.append(ok)
    print(f"  [{'OK' if ok else '!!'}] {label:42s} got={got!r} want={want!r}")


chk("total_tokens (卡片1 30.6亿)", round(snap.total_tokens / 1e8, 1), 30.6)
chk("total_tokens == Σ token_usage", snap.total_tokens,
    sum(g.token_usage for g in snap.grants))
by = snap.tokens_by_request_type
chk("API套餐 (卡片1子项 28.0亿)", round(by["API_BUNDLE"] / 1e8, 1), 28.0)
chk("Trae (卡片1子项 2.6亿)", round(by["TRAE"] / 1e8, 1), 2.6)
chk("total_request_count (卡片2 2.2万)", round(snap.total_request_count / 1e4, 1), 2.2)
chk("pr_count (卡片3 246)", snap.pr_count, 246)
chk("added_lines_count (卡片4 3.1万)", round(snap.added_lines_count / 1e4, 1), 3.1)
chk("generated_code_lines (卡片5 3,150)", snap.generated_code_lines, 3150)
chk("adopted_code_lines (卡片6 120)", snap.adopted_code_lines, 120)
chk("adoption_rate (卡片6 3.8%)", round(snap.adoption_rate * 100, 1), 3.8)

print()
print("  -- 费用单价表格 7 列 x 3 行 --")
g0 = next(g for g in snap.grants if g.id == 5593)
chk("row1 申请类型", g0.request_type, "API_BUNDLE")
chk("row1 状态", g0.status_text, "使用中")
chk("row1 账号名称", g0.account_name, "AI编程助手-002")
chk("row1 发放日期", g0.issue_date, "2026-08-17")
chk("row1 申请日期", g0.create_time, "2026-08-17 16:28:00")
chk("row1 API Key 掩码", g0.virtual_key_masked, "sk-bM4LUSm****")
chk("row1 最后使用时间", g0.last_used_date, "2026-09-19 00:00:00")

g1 = next(g for g in snap.grants if g.id == 1954)
chk("row2 申请类型", g1.request_type, "TRAE")
chk("row2 状态", g1.status_text, "使用中")
chk("row2 账号名称", g1.account_name, "AI编程助手-001")
chk("row2 API Key (null -> '-')", g1.virtual_key_masked, "-")

g2 = next(g for g in snap.grants if g.id == 1094)
chk("row3 状态 (status=2)", g2.status_text, "已失效")
chk("row3 申请编号", g2.application_number, "REQ202603090022")

print()
print("  -- 趋势图 --")
chk("tokenTrend 记录数", len(snap.token_trend), 42)
chk("tokenTrend 去重日期数", len({p.date for p in snap.token_trend}), 30)
chk("tokens == prompt + completion", all(
    p.tokens == p.prompt_tokens + p.completion_tokens for p in snap.token_trend), True)
chk("模型集合 (5 个)", {p.request_type for p in snap.token_trend},
    {"DEEPSEEK_V4_FLASH_0731", "GLM_5_3_FLASH", "GLM_5_3",
     "DEEPSEEK_V4_PRO", "QWEN3_8_FLASH"})
chk("同步状态已解析", bool(snap.sync_status.data_fresh_time), True)

print()
print("=" * 72)
print("C. 本地检索（无服务端搜索接口）")
print("=" * 72)
print(f"  list_my_tools()             -> {len(client.list_my_tools())} 条")
print(f"  list_my_tools(active_only)  -> {len(client.list_my_tools(active_only=True))} 条")
print(f"  get_tool(5593)              -> {client.get_tool(5593).request_type}")
print(f"  get_tool(REQ202604160010)   -> {client.get_tool('REQ202604160010').request_type}")
print(f"  get_tool(999999)            -> {client.get_tool(999999)}")
print(f"  search_tools('Triton')      -> {[g.id for g in client.search_tools('Triton')]}")
print(f"  search_tools(type=API_BUNDLE) -> {[g.id for g in client.search_tools(request_type='API_BUNDLE')]}")
print(f"  search_tools(status=2)      -> {[g.id for g in client.search_tools(status=2)]}")
assert client.get_tool(999999) is None
assert [g.id for g in client.search_tools("Triton")] == [1954, 1094]
assert [g.id for g in client.search_tools(request_type="API_BUNDLE")] == [5593, 1094]

print()
print("=" * 72)
print("D. 辅助接口")
print("=" * 72)
prices = client.get_model_prices()
enabled = client.get_model_prices(enabled_only=True)
print(f"  get_model_prices()             -> {len(prices)} 行")
print(f"  get_model_prices(enabled_only) -> {len(enabled)} 行")
chk("费用表总行数", len(prices), 20)
chk("已启用行数", len(enabled), 13)
chk("DeepSeek-V4-Flash-0731 单价", next(p.blended_price for p in prices
                                        if p.request_type == "DEEPSEEK_V4_FLASH_0731"), 0.28)
chk("Trae 月费", next(p.monthly_fee for p in prices if p.request_type == "TRAE"), 200.00)

trend = client.get_token_trend("2026-08-20", "2026-09-19")
chk("get_token_trend 关联 displayName",
    trend[0]["display_name"], "DeepSeek-V4-Flash-0731")

logs = client.get_call_logs()
chk("call-logs 分页结构", (logs["total"], logs["page"], logs["pageSize"]), (0, 1, 20))
budget = client.get_key_budget()
chk("key-budget exists", budget.exists, False)

print()
print("=" * 72)
print("E. 错误分类（stub 401/403/400/500）")
print("=" * 72)


def err_case(status, body):
    def r(path, params):
        return _Resp(status, body)
    return r


cases = [
    (401, "empty Authorization", oc.SessionExpiredError, "Cookie 缺失/过期"),
    (401, "Invalid Authorization", oc.BadAuthHeaderError, "误发 Authorization 头"),
    (403, '{"message":"权限不足: 无权限操作，仅 【管理员】 可执行此操作"}',
     oc.PermissionDeniedError, "权限不足"),
    (400, '{"message":"缺少请求参数：employeeId"}', oc.MissingParamError, "缺少参数"),
    (500, '{"message":"系统内部错误！"}', oc.ServerError, "服务端错误"),
]
for status, body, exc, label in cases:
    saved = ROUTER
    ROUTER = err_case(status, body)
    try:
        c = oc.OpenCsiToolClient(StubCreds(), transport=BaseTransport())
        c._get_json("/x")
        print(f"  [!!] {status} 未抛出异常")
        checks.append(False)
    except exc as e:
        print(f"  [OK] {status:3d} -> {type(e).__name__:22s} ({label})")
        checks.append(True)
    except Exception as e:
        print(f"  [!!] {status} -> 错误类型 {type(e).__name__}: {e}")
        checks.append(False)
    finally:
        ROUTER = saved

print()
print("=" * 72)
print("F. 脱敏验证（敏感值不得出现在 repr / 异常消息中）")
print("=" * 72)
blob = repr(snap) + repr(g0) + repr(snap.grants) + repr(client)
leak = [s for s in ("sk-bM4LUSmEXAMPLE00000000", TOKEN) if s in blob]
print(f"  repr 中泄露的敏感值: {leak or '无'}")
checks.append(not leak)
print(f"  virtual_key_masked 可用: {g0.virtual_key_masked}")
checks.append(g0.virtual_key_masked == "sk-bM4LUSm****")
print(f"  Cookie 已注入会话: {list(client._client.cookies.store.keys())}")
print(f"  Authorization 头未被设置: {'Authorization' not in (client._client.__dict__.get('headers') or {})}")

print()
print("=" * 72)
total = len(checks)
passed = sum(1 for c in checks if c)
print(f"结果: {passed}/{total} 项通过" + ("  ✅ 全部通过" if passed == total else "  ❌ 存在失败"))
print("=" * 72)
sys.exit(0 if passed == total else 1)
