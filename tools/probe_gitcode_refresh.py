#!/usr/bin/env python3
#: labels: LIVE, NETWORK, AUTH_SIDE_EFFECT
"""Does GitCode's refresh_token grant actually renew an access token?

What this measures
------------------
The openCsiTool session expires hourly and is renewed over plain HTTP from a
stored GitCode credential. That credential's *own* access token also expires --
the documented lifetime is 1296000 seconds (15 days) -- and when it does, either
the refresh token extends the login or the user has to scan a QR code again. The
difference decides whether this tool needs a human once a fortnight or once
forever, so it is worth measuring rather than assuming.

The endpoint was established by probing, not by guessing
--------------------------------------------------------
``POST https://gitcode.com/oauth/token`` with ``grant_type=refresh_token``. The
decisive evidence was that a bogus ``grant_type`` makes the server enumerate its
own supported values:

    grant_type必须为以下值：'authorization_code','refresh_token'

Note the host: the token endpoint is on ``gitcode.com``, **not** on
``web-api.gitcode.com`` where ``checkOrAuthorize`` lives. Those are different
services, and using the API host here would answer 404 rather than explain itself.

What this prints
----------------
Status codes, lengths, and fingerprints. **Never a token.** A fingerprint is the
first 8 characters of a SHA-256 of the value, which is enough to tell "the token
changed" from "the server handed back the same one" without being usable.

The rotation question
---------------------
Whether GitCode rotates the refresh token is the one thing the documentation does
not state either way. This probe answers it directly: it prints whether the
returned ``refresh_token`` differs from the one sent. The implementation treats
it as rotating regardless -- always overwriting the stored value is correct under
both behaviours -- but an unmeasured assumption in a credential lifetime is worth
resolving when the probe exists to be run.

Safety
------
``AUTH_SIDE_EFFECT``: a successful refresh may rotate the refresh token, which
invalidates the copy that was sent. It reads the credential from the secure store
and prints only metadata, but running it can change the stored credential's
validity, so it is not a read-only probe.

Usage:
    python tools/probe_gitcode_refresh.py            # report and stop
    python tools/probe_gitcode_refresh.py --refresh  # perform the refresh
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TOKEN_URL = "https://gitcode.com/oauth/token"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)


def fingerprint(value: str | None) -> str:
    """A stable, non-reversible identifier for a credential.

    Length plus the first eight hex characters of a SHA-256. Enough to answer
    "did this change?" and useless for authenticating as anyone.
    """
    if not value:
        return "(none)"
    digest = hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:8]
    return f"len={len(value)} sha256:{digest}"


def load_refresh_token() -> str | None:
    """Read the refresh token from the secure store, if there is one."""
    sys.path.insert(0, str(ROOT / "src"))
    from opencsi.auth.windows_store import open_default_store  # noqa: PLC0415

    store = open_default_store()
    if store is None:
        return None
    bundle = store.load()
    if bundle.gitcode is None:
        return None
    return bundle.gitcode.refresh_token


def post(url: str, params: dict[str, str], *, timeout: float = 20.0) -> tuple[int, str]:
    """POST and return ``(status, body)``, including for an HTTP error."""
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{url}?{query}",
        method="POST",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        data=b"",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def redact_body(body: str) -> str:
    """The response with any credential value replaced.

    The endpoint returns tokens in the body, so a raw dump would print live
    credentials. Only the *field names* and the error text are informative here,
    so values are replaced wholesale rather than selectively.
    """
    try:
        decoded = json.loads(body)
    except json.JSONDecodeError:
        return body[:200]
    if not isinstance(decoded, dict):
        return body[:200]
    out: dict[str, object] = {}
    for key, value in decoded.items():
        if isinstance(value, str) and len(value) > 20:
            out[key] = fingerprint(value)
        else:
            out[key] = value
    return json.dumps(out, ensure_ascii=False, indent=2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="perform the refresh (it may rotate the stored refresh token)",
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()

    token = load_refresh_token()
    print(f"endpoint        : POST {TOKEN_URL}")
    print(f"stored refresh  : {fingerprint(token)}")

    if token is None:
        print()
        print("No refresh token is stored, so the grant cannot be measured.")
        print("Run 'opencsi login --qr' first.")
        return 2

    # ── the control: a bogus grant type makes the server list its own ─────
    status, body = post(
        TOKEN_URL, {"grant_type": "bogus_probe_value", "refresh_token": "x"}, timeout=args.timeout
    )
    print()
    print(f"[control] grant_type=bogus_probe_value -> HTTP {status}")
    print(redact_body(body))

    if not args.refresh:
        print()
        print("Dry run: pass --refresh to perform the real exchange.")
        return 0

    before = fingerprint(token)
    status, body = post(
        TOKEN_URL, {"grant_type": "refresh_token", "refresh_token": token}, timeout=args.timeout
    )
    print()
    print(f"[refresh] grant_type=refresh_token -> HTTP {status}")
    print(redact_body(body))

    if status != 200:
        print()
        print("RESULT: the refresh did not succeed. See the error above.")
        return 1

    try:
        decoded = json.loads(body)
    except json.JSONDecodeError:
        print("RESULT: the response was not JSON.")
        return 1

    new_access = decoded.get("access_token")
    new_refresh = decoded.get("refresh_token")
    expires_in = decoded.get("expires_in")

    print()
    print("RESULT")
    print(f"  access_token   : {fingerprint(new_access)}")
    print(f"  refresh_token  : {fingerprint(new_refresh)}")
    print(f"  expires_in     : {expires_in}")
    print(f"  scope          : {decoded.get('scope')}")
    print(f"  token_type     : {decoded.get('token_type', '(absent)')}")
    print()
    if new_refresh and new_refresh != token:
        print("  ROTATION: YES -- the refresh token changed; the stored copy must be replaced")
    elif new_refresh:
        print("  ROTATION: NO -- the same refresh token was returned")
    else:
        print("  ROTATION: no refresh_token in the response; the stored one remains valid")
    print(f"  previous token fingerprint was {before}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
