#!/usr/bin/env python3
#: labels: LIVE, NETWORK, AUTH_SIDE_EFFECT
"""The one live measurement the L2 conclusion still needs.

What this measures
------------------
The audit established (bundle + wire evidence, see
``docs/auth-lifecycle-audit-20261024.md``) that the QR login's credentials
belong to GitCode's *account-token* system and are renewed with::

    POST https://web-api.gitcode.com/uc/api/v1/user/token/refresh
    Authorization: Bearer <access_token>
    Content-Type: application/x-www-form-urlencoded
    body: refresh_token=<refresh_token>

Everything up to that request's shape is verified. What is *not* verified is
the one thing only a live exchange can answer: whether a stored QR credential
actually round-trips -- and what the server does to the refresh token when it
does.

Why this probe is opt-in, and why that is not bureaucracy
---------------------------------------------------------
This probe is explicitly **not read-only**: it issues one state-changing POST
against a live credential. ``AUTH_SIDE_EFFECT`` here is not cosmetic. If GitCode
**rotates** the refresh token on every exchange -- the frontend's own client
assumes it might -- then a successful exchange invalidates the token that was
sent. The probe therefore:

* refuses to run unless ``--execute`` is passed (the default run prints the
  plan and touches nothing);
* performs **exactly one** exchange, and reports if more would be needed;
* prints fingerprints only (length + SHA-256 prefix), never a token;
* on success, persists the replacement to the secure store **before** anything
  else can fail, then re-reads it to confirm the round trip;
* on failure, re-reads the store and verifies the stored credential is
  byte-for-byte the one it started with.

Without that last pair of guarantees, a successful exchange followed by a
failed store write would leave the server holding refresh token N+1 and the
disk holding N -- an unrecoverable state that costs the user a QR scan.

Usage:
    python tools/probe_gitcode_token_refresh.py             # plan only
    python tools/probe_gitcode_token_refresh.py --execute   # one real exchange
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
sys.path.insert(0, str(ROOT / "src"))

from opencsi.auth.gitcode_refresh import (  # noqa: E402
    GITCODE_TOKEN_URL,
    RefreshStatus,
)
from opencsi.auth.store import CredentialStoreError  # noqa: E402
from opencsi.auth.windows_store import open_default_store  # noqa: E402

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)


def fingerprint(value: str | None) -> str:
    """A stable, non-reversible identifier for a credential."""
    if not value:
        return "(none)"
    digest = hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:8]
    return f"len={len(value)} sha256:{digest}"


def exchange(access_token: str, refresh_token: str, timeout: float) -> tuple[int, str]:
    """The frontend's request, once. Returns ``(status, body)``."""
    request = urllib.request.Request(
        GITCODE_TOKEN_URL,
        method="POST",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Bearer {access_token}",
        },
        data=urllib.parse.urlencode({"refresh_token": refresh_token}).encode("ascii"),
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return 0, type(exc).__name__


def redact(body: str) -> str:
    """Field names and fingerprints only; never a credential value."""
    try:
        decoded = json.loads(body)
    except json.JSONDecodeError:
        return body[:300]
    if isinstance(decoded, dict) and isinstance(decoded.get("data"), dict):
        decoded = decoded["data"]
    if not isinstance(decoded, dict):
        return body[:300]
    out: dict[str, object] = {}
    for key, value in decoded.items():
        out[key] = fingerprint(value) if isinstance(value, str) and len(value) > 20 else value
    return json.dumps(out, ensure_ascii=False, indent=2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help=(
            "perform exactly one real exchange. This may rotate the refresh "
            "token; the replacement is persisted before the probe reports."
        ),
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()

    store = open_default_store()
    if store is None:
        print("no secure store on this platform; nothing to measure")
        return 2
    try:
        bundle = store.load()
    except CredentialStoreError as exc:
        print(f"the credential store is unreadable: {exc}")
        return 2

    credential = bundle.gitcode
    if credential is None:
        print("no GitCode credential is stored; run 'opencsi login --qr' first")
        return 2

    before_access = fingerprint(credential.access_token)
    before_refresh = fingerprint(credential.refresh_token)

    print(f"endpoint        : POST {GITCODE_TOKEN_URL}")
    print(f"stored access   : {before_access}")
    print(f"stored refresh  : {before_refresh}")
    print()

    if not args.execute:
        print("PLAN ONLY. --execute performs exactly one real exchange, which may")
        print("rotate the refresh token (the replacement is persisted to the")
        print("secure store before this probe reports anything else).")
        return 0

    status, body = exchange(
        credential.access_token, credential.refresh_token or "", timeout=args.timeout
    )
    print(f"[exchange] -> HTTP {status}")
    print(redact(body))

    if status != 200:
        after = store.load().gitcode
        unchanged = (
            after is not None
            and fingerprint(after.access_token) == before_access
            and fingerprint(after.refresh_token) == before_refresh
        )
        print()
        print("RESULT: the exchange was refused.")
        print(
            "  stored credential: "
            + ("UNCHANGED (verified by re-read)" if unchanged else "CHANGED -- investigate")
        )
        return 1

    try:
        decoded = json.loads(body)
    except json.JSONDecodeError:
        print("RESULT: 200 with a non-JSON body; the stored credential is untouched")
        return 1
    if isinstance(decoded, dict) and isinstance(decoded.get("data"), dict):
        decoded = decoded["data"]
    new_access = decoded.get("access_token") if isinstance(decoded, dict) else None
    new_refresh = decoded.get("refresh_token") if isinstance(decoded, dict) else None
    if not isinstance(new_access, str) or not new_access:
        print("RESULT: 200 without an access_token; the stored credential is untouched")
        return 1

    # Persist BEFORE anything else can fail: the server may already have
    # invalidated the pair that was sent.
    from dataclasses import replace as _replace

    from opencsi.auth.store import StoredGitCodeCredential

    replacement = StoredGitCodeCredential(
        access_token=new_access,
        refresh_token=new_refresh if isinstance(new_refresh, str) and new_refresh else (credential.refresh_token or ""),
        username=credential.username,
        access_expires_at=credential.access_expires_at,
        refresh_expires_at=credential.refresh_expires_at,
    )
    try:
        store.save_gitcode(replacement)
    except CredentialStoreError as exc:
        print()
        print(f"RESULT: ROTATION RISK -- the exchange succeeded but the store write")
        print(f"  failed ({exc}). If GitCode rotated the token, the old one is now")
        print(f"  invalid and a QR re-scan may be required. Store: {store.path}")
        return 1

    reread = store.load().gitcode
    persisted = reread is not None and fingerprint(reread.access_token) == fingerprint(new_access)

    print()
    print("RESULT")
    print(f"  access_token   : {fingerprint(new_access)}  (was {before_access})")
    print(f"  refresh_token  : {fingerprint(new_refresh)}  (was {before_refresh})")
    if isinstance(new_refresh, str) and new_refresh and credential.refresh_token:
        print(
            "  ROTATION       : "
            + ("YES -- the stored copy was replaced" if new_refresh != credential.refresh_token else "NO -- the same token was returned")
        )
    print(f"  persisted      : {'yes (verified by re-read)' if persisted else 'NO -- re-read the store'}")
    print(f"  expires_in     : {decoded.get('expires_in', '(absent)')}")
    return 0 if persisted else 1


if __name__ == "__main__":
    raise SystemExit(main())
