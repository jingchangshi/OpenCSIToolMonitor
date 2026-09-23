"""The §45 probe-label contract, enforced rather than documented.

§45 asks that the live probes carry ``LIVE`` / ``NETWORK`` / ``AUTH_SIDE_EFFECT``
/ ``GET_ONLY`` labels so a normal test run cannot accidentally execute one. A
label nobody checks is a comment, so these tests assert the labels are present,
well-formed, and consistent with what the probe's code does.

They also assert the §49 safety boundary: no probe *calls* the consent-submit
endpoint. Three probes mention it -- in a printed note, a constant, and prose --
precisely to record that they do not use it, so the check looks for a request
rather than a mention.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from live_probe_labels import (  # noqa: E402
    ALL_LABELS,
    calls_consent_submit,
    declared_labels,
    derives_network,
    derives_side_effect,
    probe_files,
)


class ProbeLabelPresenceTest(unittest.TestCase):
    def test_there_are_probes_to_check(self) -> None:
        """A guard against the glob silently matching nothing."""
        self.assertGreaterEqual(len(probe_files()), 30)

    def test_every_probe_declares_labels(self) -> None:
        missing = [
            p.name
            for p in probe_files()
            if not declared_labels(p.read_text(encoding="utf-8"))
        ]
        self.assertEqual(
            missing,
            [],
            f"these probes have no '#: labels:' declaration: {missing}",
        )

    def test_every_declared_label_is_one_of_the_four(self) -> None:
        for path in probe_files():
            src = path.read_text(encoding="utf-8")
            match = re.search(r"^#: labels: (.+)$", src, re.MULTILINE)
            self.assertIsNotNone(match, f"{path.name} has no declaration")
            raw = [t.strip().upper() for t in match.group(1).split(",")]
            unknown = [t for t in raw if t not in ALL_LABELS]
            self.assertEqual(unknown, [], f"{path.name} declares unknown {unknown}")

    def test_every_probe_is_live(self) -> None:
        """These are probes; none of them is a pure in-process unit check."""
        for path in probe_files():
            labels = declared_labels(path.read_text(encoding="utf-8"))
            self.assertIn("LIVE", labels, f"{path.name} is not labelled LIVE")

    def test_a_probe_is_never_both_side_effecting_and_get_only(self) -> None:
        for path in probe_files():
            labels = declared_labels(path.read_text(encoding="utf-8"))
            self.assertFalse(
                "AUTH_SIDE_EFFECT" in labels and "GET_ONLY" in labels,
                f"{path.name} declares both AUTH_SIDE_EFFECT and GET_ONLY",
            )

    def test_a_probe_always_declares_one_of_the_two_safety_labels(self) -> None:
        """A reader must be able to tell whether running it is safe."""
        for path in probe_files():
            labels = declared_labels(path.read_text(encoding="utf-8"))
            self.assertTrue(
                "AUTH_SIDE_EFFECT" in labels or "GET_ONLY" in labels,
                f"{path.name} says neither AUTH_SIDE_EFFECT nor GET_ONLY",
            )


class ProbeLabelAccuracyTest(unittest.TestCase):
    """The declared label must match the code, in both directions."""

    def test_network_is_declared_when_the_probe_opens_a_connection(self) -> None:
        for path in probe_files():
            src = path.read_text(encoding="utf-8")
            labels = declared_labels(src)
            if derives_network(src):
                self.assertIn(
                    "NETWORK",
                    labels,
                    f"{path.name} opens a connection but is not labelled NETWORK",
                )

    def test_side_effect_is_declared_when_the_probe_changes_auth_state(self) -> None:
        for path in probe_files():
            src = path.read_text(encoding="utf-8")
            labels = declared_labels(src)
            if derives_side_effect(src):
                self.assertIn(
                    "AUTH_SIDE_EFFECT",
                    labels,
                    f"{path.name} changes auth state but is not labelled "
                    f"AUTH_SIDE_EFFECT",
                )

    def test_get_only_probes_declare_no_side_effect_signal(self) -> None:
        """The direction that matters: a mutating probe must not say GET_ONLY.

        This is the under-claim check. A probe labelled GET_ONLY that carries an
        unambiguous mutation signal -- an explicit POST method, a cookie write,
        or a renewal -- would be run casually while changing state.
        """
        offenders = []
        for path in probe_files():
            src = path.read_text(encoding="utf-8")
            labels = declared_labels(src)
            if "GET_ONLY" in labels and derives_side_effect(src):
                offenders.append(path.name)
        self.assertEqual(
            offenders,
            [],
            f"labelled GET_ONLY but changes auth state: {offenders}",
        )

    def test_the_known_renewal_probes_are_side_effecting(self) -> None:
        """A concrete pin on the mistake that started this.

        ``probe_renewal_live.py`` was first labelled read-only while calling
        ``renewer.renew()`` on line 48 -- a real OAuth round trip that persists
        the minted cookie. These three are the ones whose whole purpose is to
        drive a renewal, so they must never be reported as safe.
        """
        for name in (
            "probe_renewal_live.py",
            "probe_renewal_soak.py",
            "probe_renewal_timeout.py",
        ):
            src = (ROOT / "tools" / name).read_text(encoding="utf-8")
            self.assertIn(
                "AUTH_SIDE_EFFECT",
                declared_labels(src),
                f"{name} drives a renewal but is not labelled AUTH_SIDE_EFFECT",
            )


class ProbeSafetyBoundaryTest(unittest.TestCase):
    """§49: approving a third-party grant is the account holder's decision."""

    def test_no_probe_calls_the_consent_submit_endpoint(self) -> None:
        offenders = [
            p.name
            for p in probe_files()
            if calls_consent_submit(p.read_text(encoding="utf-8"))
        ]
        self.assertEqual(
            offenders,
            [],
            f"these probes call the consent-submit endpoint: {offenders}",
        )

    def test_the_consent_endpoint_is_mentioned_somewhere(self) -> None:
        """A guard: the check above must not pass by the path disappearing.

        If the constant were deleted, ``test_no_probe_calls_...`` would still
        pass -- and would be testing nothing. The path is named deliberately in
        three probes to record the finding, so it must still be there.
        """
        mentioning = [
            p.name
            for p in probe_files()
            if "oauth/authorize" in p.read_text(encoding="utf-8")
        ]
        self.assertGreaterEqual(
            len(mentioning),
            1,
            "the consent-submit path is named nowhere, so the check above is vacuous",
        )


class LabelMechanismSelfTest(unittest.TestCase):
    """The checks above are only worth anything if they can fail.

    Each case hands the derivation a snippet whose correct answer is known, so a
    regression that made every probe read as safe would fail here rather than
    silently passing the whole file.
    """

    def test_a_renewal_is_detected_as_a_side_effect(self) -> None:
        src = "def f():\n    renewer.renew()\n"
        self.assertTrue(derives_side_effect(src))

    def test_the_scheduled_tick_is_detected_as_a_side_effect(self) -> None:
        src = "def f():\n    service.tick()\n"
        self.assertTrue(derives_side_effect(src))

    def test_an_explicit_post_is_detected_as_a_side_effect(self) -> None:
        src = 'def f():\n    Request(url, method="POST")\n'
        self.assertTrue(derives_side_effect(src))

    def test_a_cookie_write_is_detected_as_a_side_effect(self) -> None:
        src = 'def f():\n    conn.call("Storage.setCookies", {})\n'
        self.assertTrue(derives_side_effect(src))

    def test_a_plain_get_is_not_a_side_effect(self) -> None:
        """The direction that matters: a reader-only probe must stay GET_ONLY."""
        src = 'def f():\n    urlopen("https://example.com/x")\n'
        self.assertFalse(derives_side_effect(src))

    def test_an_options_preflight_is_not_a_post(self) -> None:
        """Measured case: two QR probes announce POST without issuing one.

        ``probe_gitcode_qr_live2.py`` sends
        ``{"Access-Control-Request-Method": "POST"}`` inside an OPTIONS
        preflight and says in capitals that no POST is issued. Treating the
        header as a POST labelled it AUTH_SIDE_EFFECT on a false signal.
        """
        src = (
            "def f():\n"
            '    call(url, method="OPTIONS", headers={\n'
            '        "Access-Control-Request-Method": "POST",\n'
            "    })\n"
        )
        self.assertFalse(derives_side_effect(src))

    def test_a_request_with_a_none_body_is_not_a_post(self) -> None:
        """Measured case: ``Request(..., data=body)`` where body defaults None."""
        src = "def call(url, body=None):\n    Request(url, data=body, method='GET')\n"
        self.assertFalse(derives_side_effect(src))

    def test_saving_a_downloaded_bundle_is_not_an_auth_side_effect(self) -> None:
        """Measured case: two probes write fetched JavaScript to a scratch dir."""
        src = 'def f():\n    path.write_text(body, encoding="utf-8")\n'
        self.assertFalse(derives_side_effect(src))

    def test_a_remote_url_is_detected_as_network(self) -> None:
        src = 'URL = "https://opencsitool.com/x"\n'
        self.assertTrue(derives_network(src))

    def test_a_localhost_url_is_not_network(self) -> None:
        """CDP talks to a local browser; that is not the network."""
        src = 'URL = "http://127.0.0.1:9222/json/version"\n'
        self.assertFalse(derives_network(src))

    def test_an_http_transport_is_detected_as_network_without_a_url(self) -> None:
        """Measured case: ``base_url=BASE_URL`` imports the host.

        ``probe_qr_browserless_login.py`` performs a real browserless login
        against the live service while containing no remote URL of its own.
        """
        src = "from opencsi.client import BASE_URL\nr = HttpOAuthRenewer(base_url=BASE_URL)\n"
        self.assertTrue(derives_network(src))

    def test_a_cdp_client_alone_is_not_network(self) -> None:
        src = "p = CdpCookieProvider()\n"
        self.assertFalse(derives_network(src))

    def test_a_label_declaration_is_parsed_completely(self) -> None:
        """The capture must not run past the end of the declaration line.

        An earlier pattern used ``[A-Z_,\\s]+`` and swallowed the following
        ``from __future__`` line, silently dropping ``GET_ONLY`` from every
        probe that declared it.
        """
        src = '#: labels: LIVE, NETWORK, GET_ONLY\nfrom __future__ import annotations\n'
        self.assertEqual(
            declared_labels(src), ("LIVE", "NETWORK", "GET_ONLY")
        )

    def test_an_unknown_label_is_ignored(self) -> None:
        src = "#: labels: LIVE, MADE_UP\n"
        self.assertEqual(declared_labels(src), ("LIVE",))


class ProbeNotCollectedTest(unittest.TestCase):
    """§45's actual requirement: a normal test run must not execute a probe."""

    def test_pytest_does_not_collect_the_probes(self) -> None:
        """``testpaths`` keeps the suite inside ``tests/``.

        Checked by reading the configured test paths rather than by trusting it:
        a probe that pytest collected would run on every CI job, with a real
        account and a real browser.
        """
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r"^testpaths\s*=\s*\[(.*?)\]", pyproject, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(match, "no testpaths configured")
        paths = re.findall(r"[\"']([^\"']+)[\"']", match.group(1))
        self.assertEqual(paths, ["tests"], f"unexpected testpaths: {paths}")

    def test_no_probe_is_named_like_a_test(self) -> None:
        """``test_*.py`` under tools/ would be collected by filename."""
        offenders = [p.name for p in probe_files() if p.name.startswith("test_")]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
