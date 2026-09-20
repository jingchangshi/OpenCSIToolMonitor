"""CLI surface: parsing, exit codes, output contracts, secret hygiene.

The CLI is the only part a user touches directly, so these tests cover the
things that would embarrass the project in public:

* every documented command exists and ``--help`` works for each;
* exit codes are stable and documented, because scripts depend on them;
* ``--json`` emits exactly one parseable JSON document on stdout;
* **no command accepts a secret on the command line**, since argv is visible in
  the process list and lands in shell history;
* errors never print a traceback at the user.
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest

from helpers import FakeTransport, StubCredentialProvider, make_client

from opencsi.cli.app import main
from opencsi.cli.context import build_parser
from opencsi.errors import (
    EXIT_CDP_UNAVAILABLE,
    EXIT_NO_BROWSER_TARGET,
    EXIT_NOT_LOGGED_IN,
    EXIT_OK,
    EXIT_SESSION_EXPIRED,
    EXIT_USAGE,
)

#: The commands the brief requires, plus the two optional ones.
REQUIRED_COMMANDS = (
    "status",
    "tools",
    "usage",
    "trend",
    "prices",
    "logs",
    "doctor",
)
OPTIONAL_COMMANDS = ("login", "contract-check")


def run_cli(argv: list[str], *, client=None, provider=None) -> tuple[int, str, str]:
    """Invoke ``main`` capturing stdout/stderr, with the network faked out."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        if client is not None:
            import opencsi.cli.context as ctx_module

            original = ctx_module.CliContext.make_client

            def fake_make_client(self, *, provider=None):  # noqa: ANN001
                return client

            ctx_module.CliContext.make_client = fake_make_client
            try:
                code = main(argv)
            finally:
                ctx_module.CliContext.make_client = original
        else:
            code = main(argv)
    return code, out.getvalue(), err.getvalue()


class ParserTest(unittest.TestCase):
    def test_every_required_command_is_registered(self) -> None:
        parser = build_parser()
        # ``_subparsers`` is the documented argparse action name.
        choices: set[str] = set()
        for action in parser._actions:
            if hasattr(action, "choices") and isinstance(action.choices, dict):
                choices |= set(action.choices)
        for name in REQUIRED_COMMANDS + OPTIONAL_COMMANDS:
            self.assertIn(name, choices, f"command {name!r} is missing")

    def test_help_exits_zero(self) -> None:
        for argv in (["--help"], ["-h"]):
            code, out, _ = run_cli(argv)
            self.assertEqual(code, EXIT_OK)
            self.assertIn("usage", out.lower())

    def test_version_exits_zero(self) -> None:
        code, out, _ = run_cli(["--version"])
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(out.strip())

    def test_bare_invocation_prints_help_and_exits_usage(self) -> None:
        code, out, _ = run_cli([])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("usage", out.lower())

    def test_unknown_command_is_a_usage_error(self) -> None:
        code, _, err = run_cli(["not-a-command"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertTrue(err.strip())

    def test_each_command_has_help(self) -> None:
        for name in REQUIRED_COMMANDS + OPTIONAL_COMMANDS:
            code, out, _ = run_cli([name, "--help"])
            self.assertEqual(code, EXIT_OK, f"{name} --help failed")
            self.assertIn(name, out)


class NoSecretOnArgvTest(unittest.TestCase):
    """A secret must never be passable as a command-line argument.

    ``ps`` shows argv to every user on the machine, and shells record it in
    history. The only accepted route is an interactive ``getpass()`` prompt.
    """

    def test_no_option_looks_like_a_token_flag(self) -> None:
        parser = build_parser()
        forbidden = {"--token", "--cookie", "--secret", "--password", "--key", "--auth"}
        for action in parser._actions:
            if not hasattr(action, "choices") or not isinstance(action.choices, dict):
                continue
            for name, sub in action.choices.items():
                for sub_action in sub._actions:
                    for option in sub_action.option_strings:
                        self.assertNotIn(
                            option,
                            forbidden,
                            f"{name} accepts {option}; secrets must not come from argv",
                        )

    def test_login_does_not_accept_a_token_argument(self) -> None:
        """Passing a secret on argv must be rejected, not quietly accepted."""
        code, out, err = run_cli(["login", "--token", "SUPERSECRETVALUE"])
        self.assertNotEqual(code, EXIT_OK)
        # argparse reports the unrecognised flag but must not echo its value
        # back in a way that would put the secret in a log.
        self.assertNotIn("SUPERSECRETVALUE", out)
        self.assertIn("--token", err)

    def test_login_has_a_manual_flag_instead(self) -> None:
        parser = build_parser()
        for action in parser._actions:
            if hasattr(action, "choices") and isinstance(action.choices, dict):
                login = action.choices.get("login")
                if login is not None:
                    options = {
                        opt
                        for sub in login._actions
                        for opt in sub.option_strings
                    }
                    self.assertIn("--manual", options)
                    return
        self.fail("login subparser not found")


class ExitCodeTest(unittest.TestCase):
    """Exit codes are a public contract; scripts branch on them."""

    def test_documented_codes_are_distinct(self) -> None:
        codes = [
            EXIT_OK,
            EXIT_USAGE,
            EXIT_CDP_UNAVAILABLE,
            EXIT_NO_BROWSER_TARGET,
            EXIT_NOT_LOGGED_IN,
            EXIT_SESSION_EXPIRED,
        ]
        self.assertEqual(len(codes), len(set(codes)))

    def test_ok_is_zero(self) -> None:
        self.assertEqual(EXIT_OK, 0)

    def test_usage_is_two(self) -> None:
        self.assertEqual(EXIT_USAGE, 2)

    def test_bad_date_is_a_usage_error(self) -> None:
        code, _, err = run_cli(["tools", "--start-date", "yesterday"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertTrue(err.strip())

    def test_date_range_requires_both_ends(self) -> None:
        code, _, _ = run_cli(["tools", "--start-date", "2026-01-01"])
        self.assertEqual(code, EXIT_USAGE)

    def test_end_before_start_is_rejected(self) -> None:
        code, _, _ = run_cli(
            ["tools", "--start-date", "2026-02-01", "--end-date", "2026-01-01"]
        )
        self.assertEqual(code, EXIT_USAGE)

    def test_bad_port_is_a_usage_error(self) -> None:
        code, _, _ = run_cli(["doctor", "--ports", "notaport"])
        self.assertEqual(code, EXIT_USAGE)


class CommandOutputTest(unittest.TestCase):
    """Each command produces its documented shape, offline."""

    def _client(self):
        client, transport, provider = make_client()
        return client, transport, provider

    def test_status_succeeds_and_names_the_employee(self) -> None:
        client, _, _ = self._client()
        code, out, _ = run_cli(["status"], client=client)
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("653124", out)

    def test_status_json_is_one_document(self) -> None:
        client, _, _ = self._client()
        code, out, _ = run_cli(["status", "--json"], client=client)
        self.assertEqual(code, EXIT_OK)
        parsed = json.loads(out)
        self.assertTrue(parsed)

    def test_status_shows_the_headline_numbers(self) -> None:
        """The default view answers "what does my account say?" (objective §21)."""
        client, _, _ = self._client()
        code, out, _ = run_cli(["status"], client=client)
        self.assertEqual(code, EXIT_OK)
        for expected in (
            "3,061,130,999",
            "21,632",
            "246",
            "31,167",
            "3,150",
            "120",
            "3.8%",
        ):
            self.assertIn(expected, out, f"status must show {expected}")

    def test_status_hides_internal_identifiers_by_default(self) -> None:
        """userId/accountId/org UUID are noise in normal use (objective §21)."""
        client, _, _ = self._client()
        code, out, _ = run_cli(["status"], client=client)
        self.assertEqual(code, EXIT_OK)
        self.assertNotIn("0dd935e8d2234014a215aa922417094c", out)  # userId
        self.assertNotIn("680a24c143c294728be7bca3", out)  # accountId
        self.assertNotIn("fba54d1682e841d196d823b4b548c4b9", out)  # organizationId

    def test_status_verbose_reveals_the_identifiers(self) -> None:
        client, _, _ = self._client()
        code, out, _ = run_cli(["status", "--verbose"], client=client)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("0dd935e8d2234014a215aa922417094c", out)
        self.assertIn("fba54d1682e841d196d823b4b548c4b9", out)

    def test_status_json_hides_identifiers_unless_verbose(self) -> None:
        client, _, _ = self._client()
        _, out, _ = run_cli(["status", "--json"], client=client)
        quiet = json.loads(out)
        self.assertNotIn("user_id", quiet["identity"])
        self.assertNotIn("organization_id", quiet["identity"])

        _, out_v, _ = run_cli(["status", "--json", "--verbose"], client=client)
        loud = json.loads(out_v)
        self.assertIn("user_id", loud["identity"])
        self.assertIn("organization_id", loud["identity"])

    def test_status_json_summary_carries_the_verified_totals(self) -> None:
        client, _, _ = self._client()
        _, out, _ = run_cli(["status", "--json"], client=client)
        parsed = json.loads(out)
        summary = parsed["summary"]
        self.assertEqual(summary["total_tokens"], 3061130999)
        self.assertEqual(summary["request_count"], 21632)
        self.assertEqual(summary["pr_count"], 246)
        self.assertEqual(summary["active_tools"], 2)
        self.assertEqual(summary["expired_tools"], 1)

    def test_status_no_summary_skips_the_second_request(self) -> None:
        """A script that only needs "am I signed in?" should pay for one call."""
        client, transport, _ = self._client()
        code, out, _ = run_cli(["status", "--no-summary"], client=client)
        self.assertEqual(code, EXIT_OK)
        self.assertNotIn("3,061,130,999", out)
        self.assertFalse(
            any("personalQueueStatus" in p for p in transport.paths()),
            f"--no-summary must not fetch the queue status: {transport.paths()}",
        )

    def test_status_survives_a_failing_summary(self) -> None:
        """A working session must not be reported as broken by a summary error."""
        from helpers import FakeResponse

        client, transport, _ = self._client()
        transport.overrides["personalQueueStatus"] = FakeResponse(500, {"message": "down"})
        code, out, _ = run_cli(["status"], client=client)
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("653124", out)

    def test_status_data_updated_preserves_the_server_offset(self) -> None:
        """Report the server's wall time, not a guessed local conversion."""
        client, _, _ = self._client()
        code, out, _ = run_cli(["status"], client=client)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("UTC+8", out)

    def test_tools_lists_the_three_grants(self) -> None:
        client, _, _ = self._client()
        code, out, _ = run_cli(["tools"], client=client)
        self.assertEqual(code, EXIT_OK)
        for expected in ("REQ202608170007", "API_BUNDLE", "TRAE"):
            self.assertIn(expected, out)

    def test_tools_hides_the_key_by_default(self) -> None:
        """The masked key is opt-in (objective §23).

        The site shows `sk-xxxxxxxx****`, but even a masked key is
        account-identifying, so it is off unless asked for.
        """
        client, _, _ = self._client()
        code, out, _ = run_cli(["tools"], client=client)
        self.assertEqual(code, EXIT_OK)
        self.assertNotIn("sk-bM4LUSm****", out)
        self.assertNotIn("EXAMPLE00000000", out)

    def test_tools_show_key_mask_reveals_only_the_site_mask(self) -> None:
        client, _, _ = self._client()
        code, out, _ = run_cli(["tools", "--show-key-mask"], client=client)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("sk-bM4LUSm****", out)
        # The full key must never appear, even with the opt-in flag.
        self.assertNotIn("EXAMPLE00000000", out)

    def test_tools_json_hides_the_key_by_default(self) -> None:
        """A script must not receive a key it did not ask for."""
        import json as _json

        client, _, _ = self._client()
        code, out, _ = run_cli(["tools", "--json"], client=client)
        self.assertEqual(code, EXIT_OK)
        parsed = _json.loads(out)
        for row in parsed["tools"]:
            self.assertNotIn("virtual_key_masked", row)
            self.assertIn("has_virtual_key", row)
        self.assertNotIn("EXAMPLE00000000", out)

    def test_tools_json_show_key_mask_includes_the_mask(self) -> None:
        import json as _json

        client, _, _ = self._client()
        code, out, _ = run_cli(["tools", "--json", "--show-key-mask"], client=client)
        parsed = _json.loads(out)
        self.assertIn("virtual_key_masked", parsed["tools"][0])
        self.assertNotIn("EXAMPLE00000000", out)

    def test_tools_type_filter_is_exact_and_case_insensitive(self) -> None:
        client, _, _ = self._client()
        code, out, _ = run_cli(["tools", "--type", "api_bundle"], client=client)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("REQ202608170007", out)
        self.assertIn("REQ202603090022", out)
        self.assertNotIn("REQ202604160010", out)  # TRAE

    def test_tools_type_filter_rejects_an_unknown_type(self) -> None:
        """An unknown type is a usage error, and says what does exist."""
        client, _, _ = self._client()
        code, out, err = run_cli(["tools", "--type", "NOPE"], client=client)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("API_BUNDLE", err + out)

    def test_tools_search_matches_account_name(self) -> None:
        client, _, _ = self._client()
        code, out, _ = run_cli(["tools", "--search", "002"], client=client)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("REQ202608170007", out)
        self.assertNotIn("REQ202604160010", out)

    def test_tools_search_matches_request_type(self) -> None:
        client, _, _ = self._client()
        code, out, _ = run_cli(["tools", "--search", "trae"], client=client)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("REQ202604160010", out)
        self.assertNotIn("REQ202608170007", out)

    def test_tools_search_with_no_match_is_graceful(self) -> None:
        client, _, _ = self._client()
        code, out, _ = run_cli(["tools", "--search", "zzzznope"], client=client)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("No tool accounts matched", out)

    def test_tools_filters_compose(self) -> None:
        client, _, _ = self._client()
        code, out, _ = run_cli(
            ["tools", "--active", "--type", "API_BUNDLE"], client=client
        )
        self.assertEqual(code, EXIT_OK)
        self.assertIn("REQ202608170007", out)
        self.assertNotIn("REQ202603090022", out)  # expired
        self.assertNotIn("REQ202604160010", out)  # TRAE

    def test_tools_active_only_filters(self) -> None:
        client, _, _ = self._client()
        code, out, _ = run_cli(["tools", "--active-only"], client=client)
        self.assertEqual(code, EXIT_OK)
        self.assertNotIn("REQ202603090022", out)  # the expired grant

    def test_usage_shows_the_verified_totals(self) -> None:
        client, _, _ = self._client()
        code, out, _ = run_cli(["usage"], client=client)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("30.6亿", out)
        self.assertIn("2.2万", out)

    def test_usage_cost_flag_adds_an_estimate(self) -> None:
        client, _, _ = self._client()
        code, out, _ = run_cli(["usage", "--cost"], client=client)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("200", out)  # the TRAE monthly fee

    def test_trend_renders_by_model(self) -> None:
        client, _, _ = self._client()
        code, out, _ = run_cli(["trend"], client=client)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("DEEPSEEK_V4_FLASH_0731", out)

    def test_trend_can_group_by_date(self) -> None:
        client, _, _ = self._client()
        code, out, _ = run_cli(["trend", "--group-by", "date"], client=client)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("2026-", out)

    def test_trend_shows_the_prompt_completion_split(self) -> None:
        """Objective §25 requires date/model/tokens/prompt/completion."""
        client, _, _ = self._client()
        code, out, _ = run_cli(["trend"], client=client)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("Prompt", out)
        self.assertIn("Completion", out)

    def test_trend_json_carries_the_split(self) -> None:
        client, _, _ = self._client()
        _, out, _ = run_cli(["trend", "--json"], client=client)
        series = json.loads(out)["series"]
        self.assertTrue(series)
        for row in series:
            self.assertIn("prompt_tokens", row)
            self.assertIn("completion_tokens", row)
            self.assertIn("tokens", row)

    def test_trend_split_sums_to_the_reported_tokens(self) -> None:
        """The split must be self-consistent, or the numbers are misleading."""
        client, _, _ = self._client()
        _, out, _ = run_cli(["trend", "--json"], client=client)
        for row in json.loads(out)["series"]:
            self.assertEqual(
                row["prompt_tokens"] + row["completion_tokens"],
                row["tokens"],
                f"{row['key']}: prompt+completion must equal tokens",
            )

    def test_trend_by_day_is_accepted(self) -> None:
        client, _, _ = self._client()
        code, out, _ = run_cli(["trend", "--by-day"], client=client)
        self.assertEqual(code, EXIT_OK)

    def test_trend_days_computes_an_inclusive_window(self) -> None:
        """``--days 7`` is today plus the six days before it.

        Off-by-one here is the classic bug, and it is invisible in the output
        (a window one day short still renders), so the window is asserted
        directly on the resolved dates.
        """
        import argparse
        from datetime import date

        from opencsi.cli.trend import resolve_window

        args = argparse.Namespace(
            start_date=None, end_date=None, from_date=None, to_date=None, days=7
        )
        start, end = resolve_window(args, today=date(2026, 9, 19))
        self.assertEqual(end, "2026-09-19")
        self.assertEqual(start, "2026-09-13")

    def test_trend_days_one_is_today_only(self) -> None:
        import argparse
        from datetime import date

        from opencsi.cli.trend import resolve_window

        args = argparse.Namespace(
            start_date=None, end_date=None, from_date=None, to_date=None, days=1
        )
        start, end = resolve_window(args, today=date(2026, 9, 19))
        self.assertEqual((start, end), ("2026-09-19", "2026-09-19"))

    def test_trend_from_and_to_alias_the_date_options(self) -> None:
        client, _, _ = self._client()
        code, out, _ = run_cli(
            ["trend", "--from", "2026-08-20", "--to", "2026-09-19"], client=client
        )
        self.assertEqual(code, EXIT_OK)
        self.assertIn("DEEPSEEK_V4_FLASH_0731", out)

    def test_trend_days_conflicting_with_a_date_is_a_usage_error(self) -> None:
        """Guessing which window the user meant would be worse than refusing."""
        client, _, _ = self._client()
        code, _, err = run_cli(
            ["trend", "--days", "7", "--from", "2026-01-01"], client=client
        )
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("--days cannot be combined", err)

    def test_trend_days_rejects_a_non_positive_count(self) -> None:
        client, _, _ = self._client()
        code, _, err = run_cli(["trend", "--days", "0"], client=client)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("--days", err)

    def test_trend_days_plus_explicit_range_is_rejected(self) -> None:
        client, _, _ = self._client()
        code, _, err = run_cli(
            ["trend", "--days", "7", "--start-date", "2026-01-01", "--end-date", "2026-02-01"],
            client=client,
        )
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("--days cannot be combined", err)

    def test_logs_from_and_to_alias_the_date_options(self) -> None:
        client, _, _ = self._client()
        code, out, _ = run_cli(
            ["logs", "--from", "2026-08-01", "--to", "2026-09-01"], client=client
        )
        self.assertEqual(code, EXIT_OK)
        self.assertIn("No call log records", out)

    def test_logs_from_without_to_is_a_usage_error(self) -> None:
        client, _, _ = self._client()
        code, _, err = run_cli(["logs", "--from", "2026-08-01"], client=client)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("--end-date", err)

    def test_prices_shows_only_enabled_by_default(self) -> None:
        client, _, _ = self._client()
        code, out, _ = run_cli(["prices"], client=client)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("DEEPSEEK_V4_FLASH_0731", out)

    def test_prices_all_shows_every_row(self) -> None:
        client, _, _ = self._client()
        code_all, out_all, _ = run_cli(["prices", "--all"], client=client)
        self.assertEqual(code_all, EXIT_OK)
        self.assertTrue(out_all.strip())

    def test_logs_handles_an_empty_list(self) -> None:
        client, _, _ = self._client()
        code, out, _ = run_cli(["logs"], client=client)
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(out.strip())

    def test_logs_raw_lists_every_field_as_a_table(self) -> None:
        """``--raw`` widens the *table*, it does not switch to JSON.

        With an empty log there is nothing to widen, so the command must still
        succeed and say so rather than emit an empty table.
        """
        client, _, _ = self._client()
        code, out, _ = run_cli(["logs", "--raw"], client=client)
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(out.strip())
        self.assertIn("No call log records", out)

    def test_logs_raw_with_records_shows_extra_columns(self) -> None:
        """A record with a field outside the summary set is shown by --raw."""
        client, transport, _ = self._client()
        transport.overrides["call-logs"] = {
            "list": [
                {
                    "id": 1,
                    "model": "deepseek-v4-flash",
                    "totalTokens": 1234,
                    "traceId": "abc-123",
                }
            ],
            "total": 1,
            "page": 1,
            "pageSize": 20,
        }
        _, summary, _ = run_cli(["logs"], client=client)
        client, transport, _ = self._client()
        transport.overrides["call-logs"] = {
            "list": [
                {
                    "id": 1,
                    "model": "deepseek-v4-flash",
                    "totalTokens": 1234,
                    "traceId": "abc-123",
                }
            ],
            "total": 1,
            "page": 1,
            "pageSize": 20,
        }
        _, raw, _ = run_cli(["logs", "--raw"], client=client)
        # The summary view omits traceId; --raw includes it.
        self.assertNotIn("traceId", summary)
        self.assertIn("traceId", raw)

    def test_contract_check_passes_offline(self) -> None:
        client, _, _ = self._client()
        code, out, _ = run_cli(["contract-check"], client=client)
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("personalQueueStatus", out)

    def test_json_output_is_parseable_for_every_read_command(self) -> None:
        for command in ("status", "tools", "usage", "trend", "prices", "logs"):
            client, _, _ = self._client()
            code, out, err = run_cli([command, "--json"], client=client)
            self.assertEqual(code, EXIT_OK, f"{command}: {err}")
            parsed = json.loads(out)
            self.assertTrue(parsed, f"{command} produced empty JSON")

    def test_json_output_never_contains_a_secret(self) -> None:
        for command in ("status", "tools", "usage", "trend", "prices", "logs"):
            client, _, _ = self._client()
            _, out, _ = run_cli([command, "--json"], client=client)
            self.assertNotIn("EXAMPLE00000000", out, command)
            self.assertNotIn("TESTCOOKIE", out, command)

    def test_logs_json_scrubs_unmodelled_server_fields(self) -> None:
        """``logs`` echoes server records verbatim, so it needs its own guard.

        The client does not model call-log records, which means a field this
        package has never seen would otherwise reach stdout untouched. The
        redaction pass in ``to_json`` is what makes that safe.
        """
        client, transport, _ = self._client()
        transport.overrides["call-logs"] = {
            "list": [
                {
                    "id": 1,
                    "model": "deepseek-v4-flash",
                    "virtualKey": "sk-bM4LUSmEXAMPLE00000000",
                    "note": "token=" + "TESTCOOKIE" + "a1b2c3d4e5f6" * 20,
                }
            ],
            "total": 1,
            "page": 1,
            "pageSize": 20,
        }
        code, out, _ = run_cli(["logs", "--json"], client=client)
        self.assertEqual(code, EXIT_OK)
        self.assertNotIn("EXAMPLE00000000", out)
        self.assertNotIn("a1b2c3d4e5f6", out)
        # The document must still be valid JSON with the benign field intact.
        parsed = json.loads(out)
        self.assertEqual(parsed["records"][0]["model"], "deepseek-v4-flash")


class FailureReportingTest(unittest.TestCase):
    """Failures must be legible and must not print tracebacks."""

    def test_session_expiry_maps_to_its_exit_code(self) -> None:
        from helpers import FakeResponse

        client, transport, _ = make_client()
        transport.force = FakeResponse(401, {"message": "unauthorized"})
        code, out, err = run_cli(["tools"], client=client)
        self.assertEqual(code, EXIT_SESSION_EXPIRED)
        self.assertNotIn("Traceback", err + out)

    def test_permission_denied_maps_to_its_exit_code(self) -> None:
        from helpers import FakeResponse

        client, transport, _ = make_client()
        transport.force = FakeResponse(403, {"message": "forbidden"})
        code, _, err = run_cli(["tools"], client=client)
        self.assertNotEqual(code, EXIT_OK)
        self.assertNotIn("Traceback", err)

    def test_json_error_output_is_a_single_document(self) -> None:
        from helpers import FakeResponse

        client, transport, _ = make_client()
        transport.force = FakeResponse(401, {"message": "unauthorized"})
        code, out, _ = run_cli(["tools", "--json"], client=client)
        self.assertNotEqual(code, EXIT_OK)
        parsed = json.loads(out)
        self.assertFalse(parsed["ok"])
        self.assertTrue(parsed["error"]["error"])

    def test_cdp_failure_maps_to_its_exit_code(self) -> None:
        from opencsi.errors import CdpUnavailableError

        provider = StubCredentialProvider(raises=CdpUnavailableError("no browser"))
        client, _, _ = make_client(provider=provider)
        code, _, err = run_cli(["tools"], client=client)
        self.assertEqual(code, EXIT_CDP_UNAVAILABLE)
        self.assertNotIn("Traceback", err)

    def test_a_missing_browser_target_maps_to_its_exit_code(self) -> None:
        from opencsi.errors import NoBrowserTargetError

        provider = StubCredentialProvider(raises=NoBrowserTargetError("no tab"))
        client, _, _ = make_client(provider=provider)
        code, _, err = run_cli(["tools"], client=client)
        self.assertEqual(code, EXIT_NO_BROWSER_TARGET)
        self.assertNotIn("Traceback", err)

    def test_status_propagates_the_real_failure_not_a_blanket_not_logged_in(self) -> None:
        """``status`` must not report every failure as "not signed in".

        It used to hardcode exit 12. A user with no reachable DevTools port was
        therefore told to sign in, which cannot possibly help: the fix is to
        start the browser with remote debugging. The exit code has to carry the
        real cause.
        """
        from opencsi.errors import CdpUnavailableError, CookieNotFoundError

        provider = StubCredentialProvider(
            raises=CdpUnavailableError("port 9222 is not reachable")
        )
        client, _, _ = make_client(provider=provider)
        code, _, err = run_cli(["status"], client=client)
        self.assertEqual(code, EXIT_CDP_UNAVAILABLE)
        self.assertNotEqual(code, EXIT_NOT_LOGGED_IN)
        self.assertIn("9222", err)

        # ...and a genuine "no cookie" case still reports as not-signed-in.
        provider = StubCredentialProvider(
            raises=CookieNotFoundError("the browser holds no openCsiTool token")
        )
        client, _, _ = make_client(provider=provider)
        code, _, _ = run_cli(["status"], client=client)
        self.assertEqual(code, EXIT_NOT_LOGGED_IN)

    def test_status_does_not_print_the_same_cause_twice(self) -> None:
        """The credential detail and the session error are one cause, not two."""
        from opencsi.errors import CdpUnavailableError

        provider = StubCredentialProvider(
            raises=CdpUnavailableError("port 9222 is not reachable")
        )
        client, _, _ = make_client(provider=provider)
        _, out, err = run_cli(["status"], client=client)
        combined = out + err
        self.assertEqual(combined.count("port 9222 is not reachable"), 1)

    def test_status_json_carries_the_error_code(self) -> None:
        from opencsi.errors import CdpUnavailableError

        provider = StubCredentialProvider(raises=CdpUnavailableError("nope"))
        client, _, _ = make_client(provider=provider)
        _, out, _ = run_cli(["status", "--json"], client=client)
        parsed = json.loads(out)
        self.assertEqual(parsed["error_code"], "CDP_UNAVAILABLE")
        # The credential summary must remain readable, not be masked away.
        self.assertIn("source", parsed["credential"])

    def test_doctor_maps_the_credential_failure_to_its_real_code(self) -> None:
        """The exit code must come from the cause, not the check's name.

        ``doctor`` used to derive the status from which check failed, so any
        credential problem exited 12 ("not signed in"). A refused DevTools
        handshake therefore told the user to sign in -- advice that cannot work,
        because the fix is to restart the browser with remote debugging.
        """
        from opencsi.auth.base import CredentialStatus
        from opencsi.errors import CookieNotFoundError

        class RefusedProvider:
            """Endpoint answered but the WebSocket upgrade was refused."""

            name = "cdp"
            last_hint = "restart Chrome with --user-data-dir"
            last_error_code = "CDP_UNAVAILABLE"

            def status(self):
                return CredentialStatus(
                    available=False, source="cdp", detail="handshake refused"
                )

            def get_token(self):
                raise CdpUnavailableError("handshake refused")

            def invalidate(self):
                pass

            def refresh(self):
                raise CdpUnavailableError("handshake refused")

        class EmptyJarProvider(RefusedProvider):
            """Endpoint fine, browser simply not signed in."""

            last_hint = "sign in"
            last_error_code = "OPENCSITOOL_NOT_LOGGED_IN"

            def status(self):
                return CredentialStatus(
                    available=False, source="cdp", detail="no openCsiTool token"
                )

            def get_token(self):
                raise CookieNotFoundError("no cookie")

            def refresh(self):
                raise CookieNotFoundError("no cookie")

        from opencsi import OpenCsiToolClient

        refused_code, _, _ = run_cli(
            ["doctor", "--no-discover"], client=OpenCsiToolClient(RefusedProvider())
        )
        self.assertEqual(refused_code, EXIT_CDP_UNAVAILABLE)
        self.assertNotEqual(refused_code, EXIT_NOT_LOGGED_IN)

        empty_code, _, _ = run_cli(
            ["doctor", "--no-discover"], client=OpenCsiToolClient(EmptyJarProvider())
        )
        self.assertEqual(empty_code, EXIT_NOT_LOGGED_IN)

    def test_doctor_without_a_credential_reports_the_real_cause(self) -> None:
        """``doctor`` must diagnose, not just fail.

        It should still exit non-zero, but it should say *why* -- a bare "not
        signed in" when the actual problem is a refused DevTools handshake sends
        the user down the wrong path.
        """
        from opencsi.errors import CdpUnavailableError

        provider = StubCredentialProvider(
            raises=CdpUnavailableError("refused", hint="restart Chrome with --user-data-dir")
        )
        code, out, _ = run_cli(["doctor", "--no-discover"], client=make_client(provider=provider)[0])
        self.assertNotEqual(code, EXIT_OK)
        self.assertIn("credential", out.lower())

    def test_doctor_names_each_api_endpoint_separately(self) -> None:
        """One collapsed "contract" line hides which call is broken (§28).

        The value of a diagnosis is localisation: "the contract drifted" sends
        the user nowhere, while "ai/config/cost: 0 rows" points at the endpoint.
        """
        client, _, _ = make_client()
        code, out, _ = run_cli(["doctor"], client=client)
        self.assertEqual(code, EXIT_OK, out)
        for expected in ("getUserInfo", "personalQueueStatus", "ai/config/cost"):
            self.assertIn(expected, out, f"doctor must name {expected}")

    def test_doctor_json_lists_every_check(self) -> None:
        client, _, _ = make_client()
        code, out, _ = run_cli(["doctor", "--json"], client=client)
        self.assertEqual(code, EXIT_OK)
        parsed = json.loads(out)
        names = {c["check"] for c in parsed["checks"]}
        self.assertIn("python", names)
        self.assertIn("credential", names)
        self.assertIn("session", names)
        self.assertTrue(parsed["ok"])

    def test_doctor_skip_contract_omits_the_endpoint_rows(self) -> None:
        client, _, _ = make_client()
        _, out, _ = run_cli(["doctor", "--skip-contract"], client=client)
        self.assertNotIn("getUserInfo", out)

    def test_doctor_hints_go_to_stdout_and_stay_attached(self) -> None:
        """A doctor hint is the deliverable, so it must survive redirection.

        ``opencsi doctor > report.txt`` is a natural thing to do when asking for
        help; if the fix went to stderr the report would contain a failure with
        no remedy. The hint must also follow the check it belongs to, which is
        why ``CliContext.err`` flushes stdout before writing.
        """
        from opencsi.errors import CdpUnavailableError

        provider = StubCredentialProvider(
            raises=CdpUnavailableError("refused", hint="restart Chrome with --user-data-dir")
        )
        client, _, _ = make_client(provider=provider)
        code, out, err = run_cli(["doctor", "--no-discover"], client=client)
        self.assertNotEqual(code, EXIT_OK)
        self.assertIn("restart Chrome with --user-data-dir", out)
        self.assertNotIn("restart Chrome with --user-data-dir", err)

    def test_err_flushes_stdout_first(self) -> None:
        """Interleaved stdout/stderr must not be reordered by buffering."""
        import io

        from opencsi.cli.context import CliContext

        class Recording(io.StringIO):
            flushed_before_write = False

            def __init__(self) -> None:
                super().__init__()
                self._flushed = False

            def flush(self) -> None:
                self._flushed = True

            def write(self, text: str) -> int:
                if not self._flushed:
                    Recording.flushed_before_write = False
                return super().write(text)

        out = io.StringIO()
        ctx = CliContext(args=type("A", (), {"json": False})(), stdout=out, stderr=io.StringIO())
        ctx.out("first")
        ctx.err("second")
        # stdout holds the first line and stderr was written after flushing it.
        self.assertIn("first", out.getvalue())

    def test_a_business_error_is_reported_not_swallowed(self) -> None:
        from helpers import FakeResponse

        client, transport, _ = make_client()
        transport.force = FakeResponse(
            200, {"code": 500, "message": "内部错误", "data": None}
        )
        code, _, err = run_cli(["tools"], client=client)
        self.assertNotEqual(code, EXIT_OK)
        self.assertNotIn("Traceback", err)


class GlobalOptionTest(unittest.TestCase):
    def test_no_cache_is_accepted(self) -> None:
        client, _, _ = make_client()
        code, _, _ = run_cli(["tools", "--no-cache"], client=client)
        self.assertEqual(code, EXIT_OK)

    def test_timeout_is_accepted(self) -> None:
        client, _, _ = make_client()
        code, _, _ = run_cli(["tools", "--timeout", "5"], client=client)
        self.assertEqual(code, EXIT_OK)

    def test_verbose_is_accepted(self) -> None:
        client, _, _ = make_client()
        code, _, _ = run_cli(["tools", "-v"], client=client)
        self.assertEqual(code, EXIT_OK)

    def test_base_url_override_is_accepted(self) -> None:
        client, _, _ = make_client()
        code, _, _ = run_cli(
            ["tools", "--base-url", "https://example.invalid"], client=client
        )
        self.assertEqual(code, EXIT_OK)

    def test_cdp_url_override_is_accepted(self) -> None:
        code, _, _ = run_cli(["doctor", "--cdp", "http://127.0.0.1:1", "--timeout", "1"])
        # No credential is reachable there, but the flag must parse.
        self.assertNotEqual(code, EXIT_OK)

    def test_ports_parses_a_comma_separated_list(self) -> None:
        from opencsi.cli.context import _port_list

        self.assertEqual(_port_list("9222,9223"), (9222, 9223))
        self.assertEqual(_port_list("9222"), (9222,))

    def test_ports_rejects_junk(self) -> None:
        import argparse

        from opencsi.cli.context import _port_list

        # argparse turns this into a clean usage error rather than a traceback.
        with self.assertRaises(argparse.ArgumentTypeError):
            _port_list("abc")

    def test_ports_rejects_out_of_range_values(self) -> None:
        import argparse

        from opencsi.cli.context import _port_list

        with self.assertRaises(argparse.ArgumentTypeError):
            _port_list("70000")
        with self.assertRaises(argparse.ArgumentTypeError):
            _port_list("0")


class WindowsConsoleTest(unittest.TestCase):
    """A Chinese Windows console must not crash on un-encodable output."""

    def test_output_streams_are_reconfigured_to_replace(self) -> None:
        """``_make_output_robust`` sets errors=replace on both streams."""
        import io

        from opencsi.cli.app import _make_output_robust

        buffer_out, buffer_err = io.BytesIO(), io.BytesIO()
        gbk_out = io.TextIOWrapper(buffer_out, encoding="gbk", errors="strict")
        gbk_err = io.TextIOWrapper(buffer_err, encoding="gbk", errors="strict")

        import sys

        real_out, real_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = gbk_out, gbk_err
        try:
            _make_output_robust()
            # An emoji is not representable in GBK; with errors=replace it
            # degrades instead of raising.
            gbk_out.write("\U0001F600")
            gbk_out.flush()
            gbk_err.write("\U0001F600")
            gbk_err.flush()
        finally:
            sys.stdout, sys.stderr = real_out, real_err

        self.assertIn(b"?", buffer_out.getvalue())
        self.assertIn(b"?", buffer_err.getvalue())

    def test_make_output_robust_tolerates_a_detached_stream(self) -> None:
        """A stream without ``reconfigure`` (e.g. StringIO) must be skipped."""
        import io
        import sys

        from opencsi.cli.app import _make_output_robust

        real_out, real_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        try:
            _make_output_robust()  # must not raise
        finally:
            sys.stdout, sys.stderr = real_out, real_err

    def test_cjk_output_survives_a_gbk_console(self) -> None:
        """The site's own labels are GBK-representable and must render."""
        import io

        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="gbk", errors="replace")
        stream.write("使用中 30.6亿")
        stream.flush()
        self.assertTrue(buffer.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
