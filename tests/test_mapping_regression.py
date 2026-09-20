"""Mapping regression: the 42 verified facts from the API investigation.

Every assertion here corresponds to a value confirmed against the live
openCsiTool page during the investigation (report §5, §18). They are the
contract this package must not break: if a refactor changes how a field is
coerced, one of these fails.

The expected values are **frozen**. They come from the captured response, so the
suite stays meaningful offline and a change in a fixture cannot silently change
the expectation.
"""

from __future__ import annotations

import unittest

from helpers import FakeTransport, StubCredentialProvider, make_client

# ── frozen expectations from the investigation ────────────────────────────
EMPLOYEE_ID = "653124"
USER_ID = "0dd935e8d2234014a215aa922417094c"
ACCOUNT_ID = "680a24c143c294728be7bca3"
ORG_ID = "fba54d1682e841d196d823b4b548c4b9"
LOGIN = "shijingchang"

TOTAL_TOKENS = 3_061_130_999
TOTAL_REQUESTS = 21_632
API_BUNDLE_TOKENS = 2_800_206_464
TRAE_TOKENS = 260_924_535
PR_COUNT = 246
ADDED_LINES = 31_167
GENERATED_LINES = 3_150
ADOPTED_LINES = 120
ADOPTION_RATE_PCT = 3.8

TREND_RECORDS = 42
TREND_DATES = 30
TREND_MODELS = {
    "DEEPSEEK_V4_FLASH_0731",
    "GLM_5_3_FLASH",
    "GLM_5_3",
    "DEEPSEEK_V4_PRO",
    "QWEN3_8_FLASH",
}

PRICE_ROWS = 20
PRICE_ENABLED = 13
DEEPSEEK_FLASH_BLENDED = 0.28
TRAE_MONTHLY_FEE = 200.00

MASKED_KEY = "sk-bM4LUSm****"


class IdentityTest(unittest.TestCase):
    """A. Session restore and identity parsing."""

    def setUp(self) -> None:
        self.client, self.transport, self.provider = make_client()

    def test_identity_fields(self) -> None:
        identity = self.client.login_or_restore_session()
        self.assertEqual(identity.employee_id, EMPLOYEE_ID)
        self.assertEqual(identity.user_id, USER_ID)
        self.assertEqual(identity.account_id, ACCOUNT_ID)
        self.assertEqual(identity.account_login, LOGIN)
        self.assertEqual(identity.organization_id, ORG_ID)
        self.assertEqual(identity.role_view_name, "普通用户")
        self.assertEqual(identity.roles, ("VISITOR",))
        self.assertTrue(identity.display_name)

    def test_roles_lookup_is_scoped_to_the_organization(self) -> None:
        self.client.login_or_restore_session()
        params = self.transport.params_for("getUserRolesByOrganizationId")
        self.assertEqual(params, {"organization_id": ORG_ID})

    def test_identity_is_cached_within_a_session(self) -> None:
        self.client.login_or_restore_session()
        before = len(self.transport.calls)
        self.client.login_or_restore_session()
        self.assertEqual(len(self.transport.calls), before)

    def test_refresh_bypasses_the_identity_cache(self) -> None:
        self.client.login_or_restore_session()
        before = len(self.transport.calls)
        self.client.login_or_restore_session(refresh=True)
        self.assertGreater(len(self.transport.calls), before)

    def test_role_lookup_failure_does_not_break_the_session(self) -> None:
        from opencsi.errors import ServerError

        # A 500 on the advisory roles endpoint must not prevent sign-in.
        self.transport.overrides["getUserRolesByOrganizationId"] = ServerError("boom")
        identity = self.client.login_or_restore_session()
        self.assertEqual(identity.employee_id, EMPLOYEE_ID)
        self.assertEqual(identity.roles, ())

    def test_unexpected_role_lookup_errors_still_propagate(self) -> None:
        """Only OpenCsiError is swallowed; a programming error must surface.

        Swallowing everything would turn a genuine bug into a silently missing
        role list, which is worse than a loud failure.
        """
        self.transport.overrides["getUserRolesByOrganizationId"] = RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            self.client.login_or_restore_session()


class SnapshotMappingTest(unittest.TestCase):
    """B. Snapshot parsing and client-side aggregation (the 42 facts)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.client, cls.transport, _ = make_client()
        cls.snapshot = cls.client.get_my_tools("2026-08-20", "2026-09-19")

    # -- headline card ----------------------------------------------------
    def test_total_tokens_renders_as_30_6_yi(self) -> None:
        self.assertEqual(round(self.snapshot.total_tokens / 1e8, 1), 30.6)

    def test_total_tokens_equals_sum_of_grants(self) -> None:
        self.assertEqual(
            self.snapshot.total_tokens,
            sum(g.token_usage for g in self.snapshot.grants),
        )

    def test_tokens_by_request_type_matches_the_card_subitems(self) -> None:
        by_type = self.snapshot.tokens_by_request_type
        self.assertEqual(round(by_type["API_BUNDLE"] / 1e8, 1), 28.0)
        self.assertEqual(round(by_type["TRAE"] / 1e8, 1), 2.6)
        self.assertEqual(by_type["API_BUNDLE"], API_BUNDLE_TOKENS)
        self.assertEqual(by_type["TRAE"], TRAE_TOKENS)

    def test_total_request_count(self) -> None:
        self.assertEqual(self.snapshot.total_request_count, TOTAL_REQUESTS)
        self.assertEqual(round(self.snapshot.total_request_count / 1e4, 1), 2.2)

    def test_pr_count_is_summed_client_side(self) -> None:
        self.assertEqual(self.snapshot.pr_count, PR_COUNT)

    def test_added_lines_is_summed_client_side(self) -> None:
        self.assertEqual(self.snapshot.added_lines_count, ADDED_LINES)
        self.assertEqual(round(self.snapshot.added_lines_count / 1e4, 1), 3.1)

    def test_generated_and_adopted_lines(self) -> None:
        self.assertEqual(self.snapshot.generated_code_lines, GENERATED_LINES)
        self.assertEqual(self.snapshot.adopted_code_lines, ADOPTED_LINES)

    def test_adoption_rate(self) -> None:
        self.assertEqual(round(self.snapshot.adoption_rate * 100, 1), ADOPTION_RATE_PCT)

    # -- fee table rows ---------------------------------------------------
    def test_row1_fields(self) -> None:
        row = next(g for g in self.snapshot.grants if g.id == 5593)
        self.assertEqual(row.request_type, "API_BUNDLE")
        self.assertEqual(row.application_number, "REQ202608170007")
        self.assertEqual(row.status, 1)
        self.assertEqual(row.status_text, "使用中")
        self.assertTrue(row.is_active)
        self.assertEqual(row.account_name, "AI编程助手-002")
        self.assertEqual(row.issue_date, "2026-08-17")
        self.assertEqual(row.create_time, "2026-08-17 16:28:00")
        self.assertEqual(row.last_used_date, "2026-09-19 00:00:00")

    def test_row1_virtual_key_is_masked_by_the_site_rule(self) -> None:
        row = next(g for g in self.snapshot.grants if g.id == 5593)
        self.assertEqual(row.virtual_key_masked, MASKED_KEY)
        self.assertTrue(row.has_virtual_key)

    def test_row2_has_no_virtual_key(self) -> None:
        row = next(g for g in self.snapshot.grants if g.id == 1954)
        self.assertEqual(row.request_type, "TRAE")
        self.assertEqual(row.status_text, "使用中")
        self.assertEqual(row.account_name, "AI编程助手-001")
        self.assertEqual(row.virtual_key_masked, "-")
        self.assertFalse(row.has_virtual_key)

    def test_row3_is_expired(self) -> None:
        row = next(g for g in self.snapshot.grants if g.id == 1094)
        self.assertEqual(row.status, 2)
        self.assertEqual(row.status_text, "已失效")
        self.assertFalse(row.is_active)
        self.assertEqual(row.application_number, "REQ202603090022")

    def test_grant_counts(self) -> None:
        self.assertEqual(len(self.snapshot.grants), 3)
        self.assertEqual(len(self.snapshot.active_grants), 2)
        self.assertEqual(len(self.snapshot.expired_grants), 1)

    # -- trend ------------------------------------------------------------
    def test_trend_shape(self) -> None:
        self.assertEqual(len(self.snapshot.token_trend), TREND_RECORDS)
        self.assertEqual(len(self.snapshot.trend_dates), TREND_DATES)
        self.assertEqual(set(self.snapshot.trend_models), TREND_MODELS)

    def test_trend_tokens_are_additive(self) -> None:
        for point in self.snapshot.token_trend:
            self.assertEqual(
                point.tokens,
                point.prompt_tokens + point.completion_tokens,
                f"row {point.date}/{point.request_type} is not additive",
            )

    # -- metadata ---------------------------------------------------------
    def test_sync_status_parsed(self) -> None:
        self.assertTrue(self.snapshot.sync_status.data_fresh_time)
        self.assertIsNotNone(self.snapshot.sync_status.data_fresh_dt)

    def test_fetched_at_is_separate_from_data_fresh_time(self) -> None:
        # The two timestamps mean different things and must not be conflated.
        self.assertIsNotNone(self.snapshot.fetched_at)
        self.assertNotEqual(
            self.snapshot.fetched_at.isoformat(),
            self.snapshot.sync_status.data_fresh_time,
        )

    def test_bound_employee_id(self) -> None:
        self.assertEqual(self.snapshot.bound_employee_id, EMPLOYEE_ID)


class DateWindowTest(unittest.TestCase):
    """Dates filter ``tokenTrend`` only; ``requestList`` is always full."""

    def test_dates_are_forwarded_as_query_parameters(self) -> None:
        client, transport, _ = make_client()
        client.get_my_tools("2026-08-20", "2026-09-19")
        params = transport.params_for("personalQueueStatus")
        self.assertEqual(params, {"startDate": "2026-08-20", "endDate": "2026-09-19"})

    def test_omitting_dates_sends_no_parameters(self) -> None:
        client, transport, _ = make_client()
        client.get_my_tools()
        self.assertIsNone(transport.params_for("personalQueueStatus"))

    def test_request_list_is_unaffected_by_the_window(self) -> None:
        narrow, _, _ = make_client()
        wide, _, _ = make_client()
        a = narrow.get_my_tools("2026-09-01", "2026-09-02")
        b = wide.get_my_tools("2020-01-01", "2030-01-01")
        self.assertEqual(len(a.grants), len(b.grants))
        self.assertEqual(
            [g.id for g in a.grants],
            [g.id for g in b.grants],
        )


class AuxiliaryEndpointTest(unittest.TestCase):
    """D. Price list, call logs, key budget."""

    def setUp(self) -> None:
        self.client, self.transport, _ = make_client()

    def test_price_list(self) -> None:
        prices = self.client.get_model_prices()
        self.assertEqual(len(prices), PRICE_ROWS)
        self.assertEqual(sum(1 for p in prices if p.is_enabled), PRICE_ENABLED)

    def test_deepseek_flash_blended_price(self) -> None:
        prices = self.client.get_model_prices()
        row = next(p for p in prices if p.request_type == "DEEPSEEK_V4_FLASH_0731")
        self.assertEqual(row.blended_price, DEEPSEEK_FLASH_BLENDED)
        self.assertTrue(row.is_token_billed)
        self.assertFalse(row.is_flat_billed)

    def test_trae_is_flat_billed(self) -> None:
        prices = self.client.get_model_prices()
        row = next(p for p in prices if p.request_type == "TRAE")
        self.assertEqual(row.monthly_fee, TRAE_MONTHLY_FEE)
        self.assertTrue(row.is_flat_billed)
        # A subscription must never be read as a per-token price.
        self.assertIsNone(row.blended_price)

    def test_price_list_returns_a_bare_array(self) -> None:
        # ai/config/cost has no {code,data} envelope; the client must accept it.
        prices = self.client.get_model_prices()
        self.assertTrue(prices)
        self.assertTrue(all(p.request_type for p in prices))

    def test_call_logs_shape(self) -> None:
        logs = self.client.get_call_logs()
        self.assertEqual(logs["total"], 0)
        self.assertEqual(logs["page"], 1)
        self.assertEqual(logs["pageSize"], 20)
        self.assertEqual(logs["list"], [])

    def test_call_logs_uses_the_bound_employee_id(self) -> None:
        self.client.get_call_logs()
        path = next(p for p in self.transport.paths() if p.endswith("/call-logs"))
        self.assertIn(f"/users/{EMPLOYEE_ID}/call-logs", path)

    def test_call_logs_pagination_is_forwarded(self) -> None:
        self.client.get_call_logs(page=3, page_size=50)
        params = self.transport.params_for("call-logs")
        self.assertEqual(params, {"page": 3, "pageSize": 50})

    def test_key_budget(self) -> None:
        budget = self.client.get_key_budget()
        self.assertFalse(budget.exists)

    def test_call_logs_rejects_a_bad_page(self) -> None:
        from opencsi.errors import UsageError

        with self.assertRaises(UsageError):
            self.client.get_call_logs(page=0)
        with self.assertRaises(UsageError):
            self.client.get_call_logs(page_size=0)


class SummaryAggregationTest(unittest.TestCase):
    """The summary object the CLI renders must match the page exactly."""

    def test_summary_matches_snapshot(self) -> None:
        from opencsi.aggregation import summarise

        client, _, _ = make_client()
        snapshot = client.get_my_tools()
        summary = summarise(snapshot)
        self.assertEqual(summary.total_tokens, TOTAL_TOKENS)
        self.assertEqual(summary.total_request_count, TOTAL_REQUESTS)
        self.assertEqual(summary.pr_count, PR_COUNT)
        self.assertEqual(summary.added_lines_count, ADDED_LINES)
        self.assertEqual(summary.generated_code_lines, GENERATED_LINES)
        self.assertEqual(summary.adopted_code_lines, ADOPTED_LINES)
        self.assertEqual(round(summary.adoption_rate * 100, 1), ADOPTION_RATE_PCT)
        self.assertEqual(summary.active_tools, 2)
        self.assertEqual(summary.expired_tools, 1)
        self.assertEqual(summary.total_tools, 3)

    def test_cost_estimate_separates_token_and_flat_billing(self) -> None:
        client, _, _ = make_client()
        snapshot = client.get_my_tools()
        estimate = client.estimate_cost(snapshot.tokens_by_request_type)
        by_type = {line.request_type: line for line in estimate.lines}

        # TRAE is a subscription: a monthly fee, and no token charge.
        trae = by_type["TRAE"]
        self.assertEqual(trae.bill_type, "FLAT")
        self.assertIsNone(trae.estimated_cost)
        self.assertEqual(trae.monthly_fee, TRAE_MONTHLY_FEE)
        self.assertAlmostEqual(estimate.flat_cost, TRAE_MONTHLY_FEE, places=6)

        # API_BUNDLE is a *bundle*, not a priced model: it appears in
        # requestList but has no row in ai/config/cost. The estimator must say
        # "unknown" rather than invent a price or quietly report zero.
        bundle = by_type["API_BUNDLE"]
        self.assertEqual(bundle.bill_type, "UNKNOWN")
        self.assertIsNone(bundle.estimated_cost)
        self.assertIsNone(bundle.unit_price)
        self.assertIsNotNone(bundle.note)

        # With only a flat fee priced, there is no token-based spend at all.
        self.assertEqual(estimate.token_cost, 0.0)
        self.assertAlmostEqual(estimate.total_cost, TRAE_MONTHLY_FEE, places=6)

    def test_token_billed_estimate_uses_the_blended_price(self) -> None:
        """A priced TOKEN model is charged at tokens / 1e6 * blendedPrice."""
        from opencsi.aggregation import estimate_usage_cost

        client, _, _ = make_client()
        prices = client.get_model_prices()
        estimate = estimate_usage_cost({"DEEPSEEK_V4_FLASH_0731": 1_000_000}, prices)
        line = estimate.lines[0]
        self.assertEqual(line.bill_type, "TOKEN")
        self.assertEqual(line.unit_price, DEEPSEEK_FLASH_BLENDED)
        self.assertAlmostEqual(line.estimated_cost, DEEPSEEK_FLASH_BLENDED, places=6)
        self.assertAlmostEqual(estimate.token_cost, DEEPSEEK_FLASH_BLENDED, places=6)
        self.assertEqual(estimate.flat_cost, 0.0)

    def test_unknown_request_type_is_not_silently_zero(self) -> None:
        from opencsi.aggregation import estimate_usage_cost

        estimate = estimate_usage_cost({"NOT_A_REAL_TYPE": 1000}, ())
        line = estimate.lines[0]
        self.assertIsNone(line.estimated_cost)
        self.assertIsNotNone(line.note)
        self.assertEqual(line.bill_type, "UNKNOWN")


class OfflineContractTest(unittest.TestCase):
    """The verified endpoint inventory must not silently grow."""

    def test_client_only_calls_get(self) -> None:
        transport = FakeTransport()
        self.assertTrue(hasattr(transport, "get_json"))
        # Mutating verbs must not exist anywhere in the transport surface.
        from opencsi.transport import HttpTransport

        for verb in ("post", "put", "patch", "delete", "request"):
            self.assertFalse(
                hasattr(HttpTransport, verb),
                f"HttpTransport must not expose {verb}()",
            )

    def test_client_never_sends_authorization(self) -> None:
        """Assert on the headers actually built, not on the source text.

        A source scan would be defeated by a comment (and indeed the transport
        documents the omission in one). What matters is the header dict handed
        to urllib, so that is what this checks -- with a cookie installed, since
        that is the case where a bug would most likely add one.
        """
        from opencsi.transport import HttpTransport

        transport = HttpTransport("https://opencsitool.com")
        transport.set_cookie("TESTCOOKIE" + "0" * 40)
        headers = transport._headers()

        lowered = {key.lower() for key in headers}
        self.assertNotIn("authorization", lowered)
        self.assertIn("cookie", lowered)
        self.assertEqual(headers["Cookie"], "token=TESTCOOKIE" + "0" * 40)
        # No value anywhere may look like a bearer token.
        for key, value in headers.items():
            self.assertNotIn("bearer", str(value).lower(), f"{key} carries a bearer value")

    def test_transport_headers_never_leak_the_cookie_into_other_fields(self) -> None:
        from opencsi.transport import HttpTransport

        secret = "TESTCOOKIE" + "z" * 40
        transport = HttpTransport("https://opencsitool.com")
        transport.set_cookie(secret)
        headers = transport._headers()
        for key, value in headers.items():
            if key == "Cookie":
                continue
            self.assertNotIn(secret, str(value), f"{key} leaks the cookie")

    def test_repr_never_contains_the_cookie(self) -> None:
        from opencsi.transport import HttpTransport

        secret = "TESTCOOKIE" + "q" * 40
        transport = HttpTransport("https://opencsitool.com")
        transport.set_cookie(secret)
        self.assertNotIn(secret, repr(transport))
        self.assertNotIn(secret, str(transport))

    def test_no_admin_endpoints_are_referenced(self) -> None:
        """accountBinding and friends are out of scope and must stay untouched."""
        from pathlib import Path

        package = Path(__file__).resolve().parent.parent / "src" / "opencsi"
        forbidden = ("accountBinding", "account-binding", "/admin", "deleteUser")
        for path in package.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for needle in forbidden:
                self.assertNotIn(
                    needle,
                    text,
                    f"{path.name} references the forbidden endpoint {needle!r}",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
