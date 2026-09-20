"""Credential providers: protocol conformance, status shape, no leaks.

The important invariants:

* ``CredentialStatus`` has no field capable of holding a token.
* ``invalidate()`` on a CDP provider drops the *cache* so the next read hits the
  browser again -- it must not permanently poison a re-readable source, because
  the client relies on exactly that for 401 recovery.
* ``invalidate()`` on a manual provider *is* permanent, and that difference is
  documented and asserted here.
* No ``repr``, ``str`` or status dict ever contains the value.
"""

from __future__ import annotations

import json
import time
import unittest

from helpers import FAKE_TOKEN, StubCredentialProvider

from opencsi.auth import CredentialProvider, ManualCookieProvider
from opencsi.auth.base import CredentialStatus, remaining_seconds


class ProtocolConformanceTest(unittest.TestCase):
    def test_manual_provider_satisfies_the_protocol(self) -> None:
        self.assertIsInstance(ManualCookieProvider(FAKE_TOKEN), CredentialProvider)

    def test_stub_provider_satisfies_the_protocol(self) -> None:
        self.assertIsInstance(StubCredentialProvider(), CredentialProvider)

    def test_protocol_requires_the_documented_methods(self) -> None:
        for name in ("get_token", "invalidate", "refresh", "status"):
            self.assertTrue(hasattr(CredentialProvider, name), name)


class StatusShapeTest(unittest.TestCase):
    """``CredentialStatus`` must be structurally incapable of holding a secret."""

    def test_no_token_like_field_exists(self) -> None:
        fields = set(CredentialStatus.__dataclass_fields__)
        for forbidden in ("token", "cookie", "value", "secret", "authorization"):
            self.assertNotIn(forbidden, fields)

    def test_as_dict_omits_absent_optional_fields(self) -> None:
        status = CredentialStatus(available=False, source="test")
        self.assertEqual(status.as_dict(), {"available": False, "source": "test"})

    def test_as_dict_serialises(self) -> None:
        status = CredentialStatus(
            available=True,
            source="cdp",
            expires_at=time.time() + 3600,
            expires_in=3600.0,
            domain="opencsitool.com",
            http_only=True,
            secure=True,
            cookie_count=1,
        )
        self.assertIn("expires_in_seconds", json.loads(json.dumps(status.as_dict())))

    def test_expired_and_expiring_flags(self) -> None:
        self.assertTrue(CredentialStatus(True, "t", expires_in=-1).expired)
        self.assertFalse(CredentialStatus(True, "t", expires_in=3600).expired)
        self.assertTrue(CredentialStatus(True, "t", expires_in=30).expiring_soon)
        self.assertFalse(CredentialStatus(True, "t", expires_in=3600).expiring_soon)
        # Unknown lifetime must not be reported as expired.
        self.assertFalse(CredentialStatus(True, "t").expired)

    def test_remaining_seconds_handles_none(self) -> None:
        self.assertIsNone(remaining_seconds(None))
        self.assertGreater(remaining_seconds(time.time() + 100), 90)


class ManualProviderTest(unittest.TestCase):
    def test_token_is_returned(self) -> None:
        provider = ManualCookieProvider(FAKE_TOKEN)
        self.assertEqual(provider.get_token(), FAKE_TOKEN)

    def test_invalidate_is_permanent(self) -> None:
        """There is no source to re-read, so invalidate really clears it."""
        provider = ManualCookieProvider(FAKE_TOKEN)
        provider.invalidate()
        self.assertIsNone(provider.get_token())
        # refresh() cannot conjure a new value.
        self.assertIsNone(provider.refresh())

    def test_set_token_installs_a_new_value(self) -> None:
        provider = ManualCookieProvider()
        self.assertIsNone(provider.get_token())
        provider.set_token(FAKE_TOKEN)
        self.assertEqual(provider.get_token(), FAKE_TOKEN)

    def test_set_token_rejects_empty(self) -> None:
        provider = ManualCookieProvider()
        with self.assertRaises(ValueError):
            provider.set_token("")

    def test_status_reports_availability(self) -> None:
        self.assertTrue(ManualCookieProvider(FAKE_TOKEN).status().available)
        self.assertFalse(ManualCookieProvider().status().available)

    def test_status_detail_explains_absence(self) -> None:
        status = ManualCookieProvider().status()
        self.assertIsNotNone(status.detail)

    def test_repr_never_contains_the_value(self) -> None:
        provider = ManualCookieProvider(FAKE_TOKEN)
        self.assertNotIn(FAKE_TOKEN, repr(provider))
        self.assertNotIn(FAKE_TOKEN, str(provider))
        self.assertIn("redacted", repr(provider))

    def test_status_never_contains_the_value(self) -> None:
        provider = ManualCookieProvider(FAKE_TOKEN)
        self.assertNotIn(FAKE_TOKEN, json.dumps(provider.status().as_dict()))

    def test_expiry_is_reported(self) -> None:
        provider = ManualCookieProvider(FAKE_TOKEN, expires_at=time.time() + 120)
        status = provider.status()
        self.assertIsNotNone(status.expires_in)
        self.assertGreater(status.expires_in, 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
