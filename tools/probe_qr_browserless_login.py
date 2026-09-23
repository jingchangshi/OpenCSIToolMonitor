"""LIVE proof that a QR credential alone completes the openCsiTool login.

The claim under test
--------------------
``opencsi login --qr`` obtains a GitCode credential over plain HTTP. The
openCsiTool half is also plain HTTP. So the two join directly and **no browser is
involved at any point** -- which is the thing this probe exists to demonstrate,
because the design it replaces planted the credential into a browser profile and
drove a hidden engine through GitCode's SPA.

What this does, and why it is a fair test
-----------------------------------------
A real QR scan cannot be automated: it needs a phone. What *can* be reproduced
exactly is the part the QR flow hands over -- the credential -- and everything
downstream of it. So this probe:

1. reads a real GitCode session from an already-signed-in browser profile
   (read-only), which is byte-for-byte what a QR scan returns;
2. builds a :class:`~opencsi.auth.http_oauth.GitCodeCookieSource` from it -- the
   same object ``_complete_qr_login`` builds, populated the same way;
3. runs :class:`~opencsi.auth.http_oauth.HttpOAuthRenewer` against it and
   confirms the result with ``getUserInfo``.

Step 3 is the whole openCsiTool leg, and it never opens a browser. If it
succeeds, the only thing a real QR scan adds is the credential's provenance --
and provenance does not change what the server accepts.

What it does not prove
----------------------
That a scanned QR code yields a *usable* credential. That needs a phone, and the
final report says so rather than implying otherwise. What is proven is that a
credential of exactly the shape a QR scan returns is sufficient, with no browser.

Safety
------
**GET only** apart from the ``checkOrAuthorize`` status query, which is never a
consent submit. Reads a browser profile without writing to it. The credential and
the resulting token are **never printed** -- only names, lengths and SHA-256
fingerprints -- and nothing is written to disk.

Usage
-----
    python tools/probe_qr_browserless_login.py --source-port 9222
    python tools/probe_qr_browserless_login.py --source-port 9222 --json

Labels: LIVE, NETWORK, AUTH_SIDE_EFFECT. It never prints a secret.
"""
#: labels: LIVE, NETWORK, AUTH_SIDE_EFFECT

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from typing import Any, Mapping

sys.path.insert(0, "src")

from opencsi.auth.cdp import CdpCookieProvider  # noqa: E402
from opencsi.auth.http_oauth import (  # noqa: E402
    GITCODE_SESSION_COOKIES,
    GitCodeCookieSource,
    HttpOAuthRenewer,
)
from opencsi.client import BASE_URL, OpenCsiToolClient  # noqa: E402
from opencsi.transport import HttpTransport  # noqa: E402


def fingerprint(value: str) -> str:
    return f"len={len(value):<5d} sha256={hashlib.sha256(value.encode()).hexdigest()[:12]}"


class _FixedToken:
    """A provider that serves exactly one token and never re-reads a browser."""

    name = "probe-issued"

    def __init__(self, value: str) -> None:
        self._value = value

    def get_token(self) -> str:
        return self._value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-port", type=int, default=9222)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    def say(text: str = "") -> None:
        if not args.json:
            print(text)

    say("== 1. read a real GitCode session (read-only, as a QR scan would supply) ==")
    reader = CdpCookieProvider(f"http://127.0.0.1:{args.source_port}", discover=False)
    cookies = reader.read_all_cookies()
    found = {
        str(c.get("name")): str(c.get("value"))
        for c in cookies
        if isinstance(c, Mapping)
        and str(c.get("name")) in GITCODE_SESSION_COOKIES
        and "gitcode.com" in str(c.get("domain") or "").lower()
    }
    say(f"  gitcode cookies available: {sorted(found) or '(none)'}")
    if not found:
        say()
        say("SKIP: no GitCode session in that profile, so there is nothing to")
        say("      stand in for a QR scan. This probe proves nothing here.")
        return 2

    say()
    say("== 2. build the credential source the QR path builds ==")
    source = GitCodeCookieSource(
        access_token=found.get("GITCODE_ACCESS_TOKEN"),
        refresh_token=found.get("GITCODE_REFRESH_TOKEN"),
        username=found.get("GitCodeUserName"),
    )
    say(f"  source cookies: {list(source.cookie_names)}")
    if not source.cookie_names:
        say()
        say("VERDICT: QR_CREDENTIAL_INSUFFICIENT -- the session held no cookie the")
        say("         flow can spend, so the QR path could not proceed either.")
        return 1

    say()
    say("== 3. run the openCsiTool leg with no browser anywhere ==")
    renewer = HttpOAuthRenewer(source, base_url=BASE_URL, use_proxy=False)
    renewal = renewer.renew()
    trace = renewer.last_trace
    say(f"  outcome : {renewal.status.value}")
    say(f"  changed : {renewal.token_changed}")
    if renewal.detail:
        say(f"  detail  : {renewal.detail}")
    if trace is not None:
        say(f"  steps   : {json.dumps(trace.as_dict(), sort_keys=True)}")

    token = source.get_token()
    if not token:
        say()
        if renewal.status.value == "CONSENT_REQUIRED":
            say("VERDICT: BROWSERLESS_LOGIN_NEEDS_ONE_CONSENT -- the credential was")
            say("         accepted and the OAuth leg ran, but this account has no")
            say("         existing application grant, so a human must approve the")
            say("         page once. That is the one case the browser route covers.")
            return 3
        say("VERDICT: QR_CREDENTIAL_INSUFFICIENT -- no openCsiTool session cookie")
        say("         was issued from this credential.")
        return 1

    say()
    say("== 4. confirm with getUserInfo (the only accepted proof) ==")
    say(f"  token cookie: {fingerprint(token)}")
    try:
        transport = HttpTransport(base_url=BASE_URL, use_proxy=False)
        client = OpenCsiToolClient(_FixedToken(token), transport=transport)
        identity = client.login_or_restore_session(refresh=True)
    except Exception as exc:  # noqa: BLE001
        say(f"  getUserInfo failed: {type(exc).__name__}: {exc}")
        identity = None

    if identity is None:
        say()
        say("VERDICT: QR_CREDENTIAL_INSUFFICIENT -- a session cookie was issued but")
        say("         the server did not accept it.")
        return 1

    say(f"  getUserInfo : OK (user_name={identity.user_name!r})")
    say()
    say("VERDICT: BROWSERLESS_LOGIN_ACHIEVABLE -- a credential of exactly the shape")
    say("         a QR scan returns established and verified an openCsiTool session")
    say("         with no browser engine at any point.")
    say()
    say("Scope: this proves the credential shape is sufficient and the OAuth leg is")
    say("browserless. It does NOT prove a scanned code yields such a credential --")
    say("that needs a phone, and is reported as not executed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
