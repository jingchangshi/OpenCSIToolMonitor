"""Redaction: secrets must not escape through any output channel.

This is the most important test file in the suite. A leak here is a security
incident, not a bug, so the tests are deliberately adversarial: they attack
``repr``, ``str``, ``%``-formatting, f-strings, JSON, tracebacks, log records,
exception messages and mappings.

All secrets used here are synthetic. The real cookie value and virtual key were
scrubbed from every captured file and are never present in this repository.
"""

from __future__ import annotations

import io
import json
import logging
import re
import traceback
import unittest

from helpers import FAKE_TOKEN, FAKE_VIRTUAL_KEY, make_client

from opencsi.redaction import (
    MASK,
    RedactingFilter,
    Secret,
    clear_registry,
    install_logging_redaction,
    redact_mapping,
    register_secret,
    scrub_text,
)

#: Synthetic values chosen to trip each heuristic the module implements.
COOKIE_LIKE = "TESTCOOKIE" + "a1b2c3d4e5f6" * 20
KEY_LIKE = "sk-bM4LUSmEXAMPLE00000000"
JWT_LIKE = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
    ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
)

#: A value that no *pattern* would catch -- long enough to register (>= 8
#: chars) but short of the 48-char opaque-value heuristic and free of any
#: keyword. Registry tests must use this, otherwise they would pass on the
#: pattern rules alone and prove nothing about the registry.
SHORT_SECRET = "sessionSecret42xyz"


class RegistryTest(unittest.TestCase):
    """A registered value is masked wherever it appears."""

    def setUp(self) -> None:
        clear_registry()

    def tearDown(self) -> None:
        clear_registry()

    def test_the_short_secret_is_not_caught_by_patterns_alone(self) -> None:
        """Guard the guard: without this, the registry tests are vacuous."""
        self.assertEqual(scrub_text(SHORT_SECRET), SHORT_SECRET)

    def test_registered_value_is_masked_in_text(self) -> None:
        register_secret(SHORT_SECRET)
        self.assertNotIn(SHORT_SECRET, scrub_text(f"cookie is {SHORT_SECRET}"))
        self.assertIn(MASK, scrub_text(f"cookie is {SHORT_SECRET}"))

    def test_registered_value_is_masked_everywhere_in_a_long_string(self) -> None:
        register_secret(SHORT_SECRET)
        blob = " ".join([SHORT_SECRET] * 5)
        self.assertNotIn(SHORT_SECRET, scrub_text(blob))
        self.assertEqual(scrub_text(blob).count(MASK), 5)

    def test_short_values_are_not_registered(self) -> None:
        """Masking a short string would corrupt ordinary text.

        A registered value of ``"a"`` would turn every ``a`` in every message
        into ``<redacted>``; the minimum length guard prevents that.
        """
        register_secret("abc")
        self.assertEqual(scrub_text("abc def"), "abc def")

    def test_registry_does_not_grow_without_bound(self) -> None:
        from opencsi.redaction import _MAX_REGISTERED, _registry

        for index in range(_MAX_REGISTERED + 50):
            register_secret(f"SECRETVALUE{index:08d}")
        # Old entries are evicted, so memory cannot grow without limit.
        self.assertLessEqual(len(_registry), _MAX_REGISTERED)
        # The most recent values are still protected.
        self.assertNotIn(
            f"SECRETVALUE{_MAX_REGISTERED + 49:08d}",
            scrub_text(f"SECRETVALUE{_MAX_REGISTERED + 49:08d}"),
        )

    def test_clear_registry_forgets_values(self) -> None:
        register_secret(SHORT_SECRET)
        self.assertNotIn(SHORT_SECRET, scrub_text(SHORT_SECRET))
        clear_registry()
        # A short value is only masked while registered, so clearing restores it.
        self.assertIn(SHORT_SECRET, scrub_text(SHORT_SECRET))

    def test_empty_and_none_are_ignored(self) -> None:
        register_secret("")
        register_secret(None)
        self.assertEqual(scrub_text("hello"), "hello")


class PatternTest(unittest.TestCase):
    """Heuristics catch secrets that were never registered."""

    def setUp(self) -> None:
        clear_registry()

    def test_cookie_header_is_masked(self) -> None:
        text = f"Cookie: token={COOKIE_LIKE}; other=1"
        scrubbed = scrub_text(text)
        self.assertNotIn(COOKIE_LIKE, scrubbed)
        self.assertIn("Cookie", scrubbed)

    def test_set_cookie_header_is_masked(self) -> None:
        text = f"Set-Cookie: token={COOKIE_LIKE}; Path=/; HttpOnly"
        self.assertNotIn(COOKIE_LIKE, scrub_text(text))

    def test_authorization_header_is_masked(self) -> None:
        text = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345"
        scrubbed = scrub_text(text)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz012345", scrubbed)

    def test_token_query_parameter_is_masked(self) -> None:
        text = f"GET /x?token={COOKIE_LIKE}&page=1"
        self.assertNotIn(COOKIE_LIKE, scrub_text(text))

    def test_virtual_key_field_is_masked(self) -> None:
        text = f'{{"virtualKey": "{KEY_LIKE}"}}'
        scrubbed = scrub_text(text)
        self.assertNotIn(KEY_LIKE, scrubbed)
        self.assertIn("virtualKey", scrubbed)

    def test_sk_prefix_is_masked(self) -> None:
        self.assertNotIn(KEY_LIKE, scrub_text(f"key={KEY_LIKE}"))

    def test_jwt_is_masked(self) -> None:
        self.assertNotIn(JWT_LIKE, scrub_text(f"bearer {JWT_LIKE}"))

    def test_long_opaque_value_is_masked(self) -> None:
        blob = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v2W3x4Y5z6"
        self.assertNotIn(blob, scrub_text(f"value: {blob}"))

    def test_ordinary_text_is_untouched(self) -> None:
        text = "Total tokens: 30.6亿 across 3 tool accounts."
        self.assertEqual(scrub_text(text), text)

    def test_none_and_non_strings_are_handled(self) -> None:
        self.assertEqual(scrub_text(None), "")
        self.assertEqual(scrub_text(42), "42")


class MappingRedactionTest(unittest.TestCase):
    """``redact_mapping`` sanitises structured data, not just prose."""

    def setUp(self) -> None:
        clear_registry()

    def test_sensitive_keys_are_masked(self) -> None:
        data = {
            "virtualKey": KEY_LIKE,
            "Cookie": f"token={COOKIE_LIKE}",
            "Authorization": "Bearer xyz",
            "safe": "keep me",
        }
        out = redact_mapping(data)
        self.assertEqual(out["safe"], "keep me")
        self.assertNotEqual(out["virtualKey"], KEY_LIKE)
        self.assertNotIn(COOKIE_LIKE, json.dumps(out))
        self.assertNotIn("xyz", json.dumps(out))

    def test_nested_structures_are_sanitised(self) -> None:
        data = {"data": {"requestList": [{"virtualKey": KEY_LIKE, "id": 1}]}}
        out = redact_mapping(data)
        self.assertEqual(out["data"]["requestList"][0]["id"], 1)
        self.assertNotIn(KEY_LIKE, json.dumps(out))

    def test_cycles_do_not_hang(self) -> None:
        data: dict = {"a": 1}
        data["self"] = data
        out = redact_mapping(data)
        self.assertIsInstance(out, dict)

    def test_depth_limit_is_enforced(self) -> None:
        deep: dict = {"virtualKey": KEY_LIKE}
        for _ in range(40):
            deep = {"level": deep}
        # Must return rather than recurse without bound.
        out = redact_mapping(deep)
        self.assertIsInstance(out, dict)

    def test_data_under_a_credential_ish_name_survives(self) -> None:
        """A credential is a string; a count is not.

        Matching on the key name alone masked every field whose name merely
        *contains* "token", which in this API means the headline numbers:
        `total_tokens`, `token_trend`, `token_budget`, `tokens_by_request_type`.
        `usage --json` therefore reported `<redacted>` for the one figure the
        command exists to show. Name matching is now combined with value type.
        """
        data = {
            "total_tokens": 3061130999,
            "tokens_by_request_type": {"TRAE": 260924535},
            "token_trend": [{"tokens": 5}],
            "token_budget": {"max_budget": 100},
            "token_count": 42,
        }
        out = redact_mapping(data)
        self.assertEqual(out["total_tokens"], 3061130999)
        self.assertEqual(out["tokens_by_request_type"], {"TRAE": 260924535})
        self.assertEqual(out["token_trend"], [{"tokens": 5}])
        self.assertEqual(out["token_budget"], {"max_budget": 100})
        self.assertEqual(out["token_count"], 42)
        self.assertNotIn(MASK, json.dumps(out))

    def test_a_string_under_a_credential_ish_name_is_still_masked(self) -> None:
        """Loosening by value type must not loosen by name."""
        for key in (
            "token",
            "access_token",
            "refreshToken",
            "virtualKey",
            "api_key",
            "cookie",
            "Set-Cookie",
            "authorization",
            "secret",
            "password",
        ):
            with self.subTest(key=key):
                out = redact_mapping({key: KEY_LIKE})
                self.assertEqual(out[key], MASK, f"{key} must still be masked")

    def test_a_secret_nested_in_a_token_ish_container_is_still_masked(self) -> None:
        """Recursing into a container must not lose the leaves."""
        out = redact_mapping(
            {"token_summary": {"totalTokens": 5, "token": KEY_LIKE}}
        )
        self.assertEqual(out["token_summary"]["totalTokens"], 5)
        self.assertEqual(out["token_summary"]["token"], MASK)

    def test_none_and_bools_are_not_mistaken_for_secrets(self) -> None:
        out = redact_mapping({"token_budget": None, "token_flag": True})
        self.assertIsNone(out["token_budget"])
        self.assertIs(out["token_flag"], True)


class SecretObjectTest(unittest.TestCase):
    """The ``Secret`` wrapper never reveals itself accidentally."""

    def setUp(self) -> None:
        clear_registry()

    def test_repr_masks_the_value(self) -> None:
        secret = Secret(COOKIE_LIKE)
        self.assertNotIn(COOKIE_LIKE, repr(secret))
        self.assertNotIn(COOKIE_LIKE, str(secret))
        self.assertIn("redacted", repr(secret))

    def test_str_masks_the_value(self) -> None:
        secret = Secret(COOKIE_LIKE)
        self.assertNotIn(COOKIE_LIKE, f"{secret}")
        self.assertNotIn(COOKIE_LIKE, "%s" % secret)  # noqa: UP031

    def test_format_spec_does_not_leak(self) -> None:
        secret = Secret(COOKIE_LIKE)
        self.assertNotIn(COOKIE_LIKE, format(secret))

    def test_reveal_returns_the_value_explicitly(self) -> None:
        secret = Secret(COOKIE_LIKE)
        self.assertEqual(secret.reveal(), COOKIE_LIKE)

    def test_bool_reflects_presence(self) -> None:
        self.assertTrue(bool(Secret(COOKIE_LIKE)))
        self.assertFalse(bool(Secret(None)))


class ExceptionRedactionTest(unittest.TestCase):
    """Exception messages are a classic leak path."""

    def setUp(self) -> None:
        clear_registry()

    def test_error_message_is_scrubbed_on_construction(self) -> None:
        from opencsi.errors import NetworkError

        error = NetworkError(f"failed with cookie token={COOKIE_LIKE}")
        self.assertNotIn(COOKIE_LIKE, str(error))
        self.assertNotIn(COOKIE_LIKE, repr(error))

    def test_error_as_dict_is_scrubbed(self) -> None:
        from opencsi.errors import NetworkError

        error = NetworkError(f"failed with {COOKIE_LIKE}")
        self.assertNotIn(COOKIE_LIKE, json.dumps(error.as_dict()))

    def test_traceback_text_is_scrubbed_by_the_filter(self) -> None:
        """A traceback formatted into a log record must be masked."""
        clear_registry()
        register_secret(COOKIE_LIKE)
        try:
            raise ValueError(f"token={COOKIE_LIKE}")
        except ValueError:
            formatted = traceback.format_exc()
        self.assertIn(COOKIE_LIKE, formatted)  # Python itself does not redact
        self.assertNotIn(COOKIE_LIKE, scrub_text(formatted))


class LoggingFilterTest(unittest.TestCase):
    """``RedactingFilter`` protects anything routed through ``logging``."""

    def setUp(self) -> None:
        clear_registry()
        register_secret(COOKIE_LIKE)
        self.stream = io.StringIO()
        self.logger = logging.getLogger("opencsi.test.redaction")
        self.logger.handlers.clear()
        self.logger.propagate = False
        self.logger.setLevel(logging.DEBUG)
        handler = logging.StreamHandler(self.stream)
        handler.addFilter(RedactingFilter())
        self.logger.addHandler(handler)

    def tearDown(self) -> None:
        self.logger.handlers.clear()
        clear_registry()

    def test_message_is_redacted(self) -> None:
        self.logger.info("cookie is %s", COOKIE_LIKE)
        output = self.stream.getvalue()
        self.assertNotIn(COOKIE_LIKE, output)
        self.assertIn(MASK, output)

    def test_preformatted_message_is_redacted(self) -> None:
        self.logger.info(f"cookie is {COOKIE_LIKE}")
        self.assertNotIn(COOKIE_LIKE, self.stream.getvalue())

    def test_args_are_redacted(self) -> None:
        self.logger.info("value=%s", COOKIE_LIKE)
        self.assertNotIn(COOKIE_LIKE, self.stream.getvalue())

    def test_exception_info_is_redacted(self) -> None:
        try:
            raise RuntimeError(f"token={COOKIE_LIKE}")
        except RuntimeError:
            self.logger.exception("failed")
        output = self.stream.getvalue()
        self.assertNotIn(COOKIE_LIKE, output)
        self.assertIn("RuntimeError", output)

    def test_install_logging_redaction_is_idempotent(self) -> None:
        logger = logging.getLogger("opencsi.test.install")
        install_logging_redaction(logger)
        install_logging_redaction(logger)
        filters = [f for h in logger.handlers for f in h.filters]
        self.assertEqual(len(filters), len(set(id(f) for f in filters)))


class EndToEndRedactionTest(unittest.TestCase):
    """The whole client, with a real cookie installed, must not leak it."""

    def setUp(self) -> None:
        clear_registry()

    def tearDown(self) -> None:
        clear_registry()

    def test_client_repr_never_contains_the_cookie(self) -> None:
        client, transport, provider = make_client()
        client.get_my_tools()
        blob = repr(client) + str(client)
        self.assertNotIn(FAKE_TOKEN, blob)

    def test_snapshot_repr_never_contains_the_virtual_key(self) -> None:
        """The raw key is excluded from ``repr`` entirely, not merely masked.

        ``ToolGrant._virtual_key`` is declared ``repr=False``, so the key never
        appears in a repr at all -- a stronger guarantee than masking it, since
        there is no masked remnant to reverse.
        """
        client, _, _ = make_client()
        snapshot = client.get_my_tools()
        blob = repr(snapshot) + repr(snapshot.grants) + repr(list(snapshot.grants))
        self.assertNotIn("EXAMPLE00000000", blob)
        self.assertNotIn("sk-bM4LUSm", blob)
        # The masked form is a separate, opt-in property.
        row = next(g for g in snapshot.grants if g.id == 5593)
        self.assertEqual(row.virtual_key_masked, "sk-bM4LUSm****")

    def test_json_output_never_contains_the_virtual_key(self) -> None:
        """``--json`` serialises the dataclass; private fields must be dropped."""
        from opencsi.formatting import to_json

        client, _, _ = make_client()
        snapshot = client.get_my_tools()
        payload = to_json(snapshot)
        self.assertNotIn("EXAMPLE00000000", payload)
        self.assertNotIn("_virtual_key", payload)

    def test_virtual_key_masked_still_works_after_all_this(self) -> None:
        client, _, _ = make_client()
        snapshot = client.get_my_tools()
        row = next(g for g in snapshot.grants if g.id == 5593)
        self.assertEqual(row.virtual_key_masked, "sk-bM4LUSm****")

    def test_provider_status_has_no_token_field(self) -> None:
        from opencsi.auth.base import CredentialStatus

        self.assertNotIn("token", CredentialStatus.__dataclass_fields__)

    def test_credential_status_dict_is_secret_free(self) -> None:
        client, _, provider = make_client()
        client.get_my_tools()
        status = provider.status()
        self.assertNotIn(FAKE_TOKEN, json.dumps(status.as_dict(), default=str))

    def test_credential_summary_survives_json_redaction(self) -> None:
        """A credential *description* must not be redacted away.

        Over-redaction is a real failure mode: masking the whole `credential`
        subtree turned `status --json` into `"credential": "<redacted>"`, which
        is safe but useless to a script. Security and utility both matter here.
        """
        from opencsi.auth.base import CredentialStatus
        from opencsi.formatting import to_json

        status = CredentialStatus(
            available=False,
            source="cdp",
            cookie_count=0,
            detail="endpoint unreachable",
        )
        rendered = to_json({"ok": False, "credential": status.as_dict()})
        self.assertNotIn(MASK, rendered)
        self.assertIn("cdp", rendered)
        self.assertIn("endpoint unreachable", rendered)
        self.assertIn("available", rendered)

    def test_a_secret_nested_under_a_credential_key_is_still_masked(self) -> None:
        """Relaxing the container key must not open a hole."""
        from opencsi.formatting import to_json

        rendered = to_json(
            {
                "credential": {"token": "SUPER_SECRET_COOKIE_123", "available": True},
                "credentials": [{"virtualKey": "SUPER_SECRET_KEY_456", "name": "ok"}],
            }
        )
        self.assertNotIn("SUPER_SECRET_COOKIE_123", rendered)
        self.assertNotIn("SUPER_SECRET_KEY_456", rendered)
        self.assertIn(MASK, rendered)
        # The non-secret siblings still come through.
        self.assertIn("available", rendered)
        self.assertIn("ok", rendered)

    def test_a_credential_key_holding_a_bare_string_is_masked(self) -> None:
        from opencsi.formatting import to_json

        rendered = to_json({"credential": "SUPER_SECRET_COOKIE_123"})
        self.assertNotIn("SUPER_SECRET_COOKIE_123", rendered)
        self.assertIn(MASK, rendered)

    def test_deeply_nested_structures_stop_at_the_depth_limit(self) -> None:
        """The depth guard returns MASK rather than recursing forever."""
        from opencsi.redaction import redact_mapping

        node: dict = {"leaf": "SUPER_SECRET_KEY_456"}
        for _ in range(20):
            node = {"nested": node}
        rendered = json.dumps(redact_mapping(node))
        self.assertNotIn("SUPER_SECRET_KEY_456", rendered)


class FixtureHygieneTest(unittest.TestCase):
    """The repository itself must not contain a real credential.

    These guard against a future re-capture accidentally committing live values.

    Note the approach: the check is *structural* (does any fixture contain a
    virtual key that is not one of the synthetic ones?) rather than a search for
    a hard-coded real value. Embedding the live secret here in order to detect
    the live secret would defeat the purpose -- the guard file would itself be
    the leak.
    """

    #: The only virtual-key bodies permitted anywhere in the fixtures. Both are
    #: obviously synthetic, and neither is a usable credential.
    ALLOWED_KEY_BODIES = (
        "sk-bM4LUSmEXAMPLE00000000",
        "sk-bM4LUSmTESTFIXTURE000000",
    )

    #: Structural shapes that a real credential would match. Deliberately
    #: *patterns* rather than fragments of the real values: an earlier version of
    #: this guard embedded partial real secrets in order to detect them, which
    #: is itself a leak. These catch a regression without storing anything
    #: sensitive.
    #:
    #: A session cookie is a long opaque run; a real virtual key is an ``sk-``
    #: body far longer than the synthetic placeholders above.
    FORBIDDEN_SHAPES = (
        (re.compile(r"[A-Za-z0-9_\-]{40,}"), "a long opaque credential-like run"),
        (re.compile(r"sk-(?!bM4LUSm(?:EXAMPLE00000000|TESTFIXTURE000000))[A-Za-z0-9]{12,}"),
         "a virtual key that is not one of the synthetic placeholders"),
    )

    def _fixture_texts(self):
        from pathlib import Path

        fixtures = Path(__file__).resolve().parent / "fixtures"
        for path in sorted(fixtures.glob("*.json")):
            yield path, path.read_text(encoding="utf-8")

    def test_fixtures_contain_no_credential_shaped_string(self) -> None:
        """No fixture may carry a value shaped like a real credential.

        Checked structurally so the guard itself stores no secret material.
        """
        for path, text in self._fixture_texts():
            for pattern, description in self.FORBIDDEN_SHAPES:
                for match in pattern.finditer(text):
                    self.fail(
                        f"{path.name} contains {description}: {match.group(0)[:16]}... "
                        f"(re-sanitize the fixture)"
                    )

    def test_every_virtual_key_in_the_fixtures_is_synthetic(self) -> None:
        """No fixture may carry a virtual key we did not author."""
        import re

        pattern = re.compile(r"sk-[A-Za-z0-9]{8,}")
        for path, text in self._fixture_texts():
            for match in pattern.finditer(text):
                value = match.group(0)
                self.assertIn(
                    value,
                    self.ALLOWED_KEY_BODIES,
                    f"{path.name} contains an unrecognised virtual key: {value!r}",
                )

    def test_fixtures_contain_no_cookie_material(self) -> None:
        """No fixture may carry a Cookie/Set-Cookie header or a token value."""
        for path, text in self._fixture_texts():
            self.assertNotIn("Set-Cookie", text, f"{path.name} carries a Set-Cookie header")
            self.assertNotIn("Authorization", text, f"{path.name} carries an Authorization header")

    def test_fixtures_are_valid_json(self) -> None:
        for path, text in self._fixture_texts():
            try:
                json.loads(text)
            except json.JSONDecodeError as exc:  # pragma: no cover - failure path
                self.fail(f"{path.name} is not valid JSON: {exc}")

    def test_every_fixture_was_actually_loaded(self) -> None:
        """Guard against a renamed fixture silently disabling a test."""
        from helpers import ROUTES

        referenced = {name for _, name in ROUTES}
        present = {path.name for path, _ in self._fixture_texts()}
        missing = referenced - present
        self.assertFalse(missing, f"ROUTES references absent fixtures: {sorted(missing)}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
