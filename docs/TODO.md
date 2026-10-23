# TODO / Backlog

This file records work that is intentionally **not** solved by the current
implementation. Completed authentication work belongs in the implementation and
the auth closure report; this backlog is for remaining evidence gaps and optional
follow-ups.

## P1 — Close the live GitCode refresh-token lifecycle

**Status:** CLOSED (2026-10-24). Full chain live-verified with the user's
stored credential:

1. `tools/probe_gitcode_token_refresh.py --execute` exchanged once: HTTP 200, a
   NEW access token (fingerprint 7b2d60c8 -> 657b5d01, both len=300), the
   **refresh token was NOT rotated** (1fa842e0 returned unchanged), persisted
   to DPAPI and verified by re-read. The response carries no `expires_in`,
   matching the bundle. (First attempt was blocked by CloudWAF 418 for missing
   Origin/Referer -- fixed in `9a3dc7d`.)
2. The probe process exited; a **fresh process** (`opencsi tray --once --json`)
   loaded the new credential from DPAPI, renewed the openCsiTool session
   without any scan (new cookie ~3597s), fetched real business data, state OK;
   `login --status` ok:true (user confirmed, redacted in the audit report).

Root cause and evidence: `docs/auth-lifecycle-audit-20261024.md` §2/§5.

## P1 — Resolve unknown GitCode access-token expiry

**Status:** CLOSED (2026-10-24 audit) by the evidence-backed third option.

The QR completion response contains no `expires_in` (confirmed again against the
current bundle: the frontend reads none either), so `access_expires_at` stays
unknown for QR credentials. The policy now matches GitCode's own client, which
never refreshes proactively: it refreshes when an API call comes back 401 and
retries the call. Steady-state renewal performs **zero** GitCode refresh
requests; a genuinely dead access token triggers exactly one refresh and one
renewal retry (`RefreshingGitCodeRenewer`), and a known expiry inside the margin
still refreshes proactively. The unverified "15 days" figure was removed from
the code entirely.

## P2 — Identify the role of xauth_token

**Status:** OPEN / LOW-CONFIDENCE RELEVANCE

The QR response also returns `xauth_token`. It is registered for redaction but
is not needed by the currently verified QR -> GitCode SSO -> openCsiTool OAuth
path. Its consumer remains unidentified.

Investigate whether it participates in GitCode credential renewal, Huawei Cloud
IAM, MFA, or another session-lifecycle branch. Do **not** assume it is related to
refresh-token failure without evidence.

## P2 — Live first-consent acceptance

**Status:** NOT EXECUTED

Run the flow with an account that has never approved the openCsiTool GitCode
application:

`QR -> authenticated GitCode account -> CONSENT_REQUIRED -> explicit user approval
-> callback -> durable DPAPI session`.

Consent must remain a human decision. Never automate submission of the approval.

## P3 — Real Windows sign-out/sign-in acceptance

**Status:** NOT EXECUTED

The HKCU Run-key round trip is verified and points to `opencsi-tray.exe`, but a
real Windows `sign out -> sign in -> tray starts -> stored credential loads ->
usage appears` cycle has not been executed.

## P3 — Keep the final report mechanically consistent

The auth closure report has accumulated stale HEAD/test-count sections while the
implementation continued to move. Prefer generated appendices or fewer volatile
numbers instead of manually copying current HEAD, test counts, and CI status into
multiple sections.

## Optional — Browser-to-DPAPI migration

Automatic migration from an existing browser profile into DPAPI remains optional.
The current secure-store-first + CDP fallback design already preserves existing
users, so this is not a blocker.
