# TODO / Backlog

This file records work that is intentionally **not** solved by the current
implementation. Completed authentication work belongs in the implementation and
the auth closure report; this backlog is for remaining evidence gaps and optional
follow-ups.

## P1 — Close the live GitCode refresh-token lifecycle

**Status:** OPEN

**Problem:** QR login returns an `access_token` and `refresh_token`, and the
production lifecycle is wired to `POST https://gitcode.com/oauth/token` with
`grant_type=refresh_token`. Offline rotation tests pass, but the real credential
obtained from the QR flow is reproducibly refused by the live endpoint with HTTP
400 `BAD_REQUEST`.

**Current evidence:**
- openCsiTool session renewal is live-verified and browserless.
- the GitCode refresh endpoint exists and the implementation is production-wired.
- no successful live `R1 -> R2` rotation has been observed.
- the failure cannot be attributed to a rotation performed by this tool because
  no live rotation has ever succeeded.

**Why it matters:** until this is closed, the tool has proven hour-scale automatic
openCsiTool renewal but has not proven unattended operation after the upstream
GitCode access credential itself expires.

**Next investigation:**
1. Determine whether the QR API's `refresh_token` is the same credential lineage
   accepted by the OAuth `refresh_token` grant.
2. Capture the real GitCode web client's credential-renewal request, if any.
3. Check whether the request needs client context, cookies, Authorization,
   Origin/Referer, `xauth_token`, or another endpoint.
4. Do not infer protocol requirements from field names alone.

**Acceptance:** a real `A1/R1 -> A2/R2` exchange succeeds, A2/R2 is written to
DPAPI, the process exits, a fresh process renews openCsiTool successfully without
another QR scan.

## P1 — Resolve unknown GitCode access-token expiry

**Status:** OPEN

The QR completion response contains no verified `expires_in`, so the persisted
`access_expires_at` is unknown. The current conservative policy attempts an
upstream refresh before openCsiTool renewal; on the live account that means an
unnecessary HTTP 400 before the still-valid GitCode access credential is used
successfully.

Do not assume the QR credential has the 15-day lifetime returned by a different
OAuth-token response unless protocol evidence proves they are the same token
type. Prefer one of:
- a verified QR-token lifetime;
- a server-side expiry source; or
- a policy that refreshes only after the access credential is actually rejected.

**Acceptance:** steady-state openCsiTool renewal no longer performs a known-useless
GitCode refresh, while a genuinely expiring/rejected GitCode credential still has
a proven recovery path.

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
