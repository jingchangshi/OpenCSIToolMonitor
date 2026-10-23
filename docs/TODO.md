# TODO / Backlog

This file records work that is intentionally **not** solved by the current
implementation. Completed authentication work belongs in the implementation and
the auth closure report; this backlog is for remaining evidence gaps and optional
follow-ups.

## P1 — Close the live GitCode refresh-token lifecycle

**Status:** ROOT CAUSE CLOSED (2026-10-24 audit) — live exchange still NOT
EXECUTED, requires explicit user opt-in.

**Resolved:** the 400 was a credential-family mismatch. The QR login's tokens
belong to GitCode's *account-token* system, renewed with
`POST https://web-api.gitcode.com/uc/api/v1/user/token/refresh`
(`Authorization: Bearer <access_token>`, urlencoded `refresh_token` body) — the
exact request GitCode's own frontend makes (current production bundle). The old
target, `POST https://gitcode.com/oauth/token?grant_type=refresh_token`, serves
the OAuth-application token system and answers a QR refresh token with the same
"refresh_token不存在或已过期" it gives a fabricated one. The production refresher
now targets the account-token endpoint, refreshes demand-driven (on a refused
OAuth leg, once, then retries), and classifies only 401/400004 as a dead
credential. See `docs/auth-lifecycle-audit-20261024.md`.

**Remaining:** one live `A1/R1 -> A2/R2` exchange, because it may rotate the
refresh token. `tools/probe_gitcode_token_refresh.py --execute` performs exactly
one, persists the replacement before reporting, and verifies the store on
failure. Run it only when a real rotation is acceptable.

**Acceptance:** the exchange succeeds, A2/R2 is written to DPAPI, the process
exits, a fresh process renews openCsiTool successfully without another QR scan.

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
