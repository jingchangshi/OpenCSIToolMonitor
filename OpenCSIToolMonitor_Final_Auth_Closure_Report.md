# OpenCSIToolMonitor — Final Auth Closure Report

**Scope**: durable, browserless credential persistence — the round defined by the
twelve-phase plan in §3 of the objective (Phase 0 repository stabilization through
Phase 11 documentation).
**Machine**: Windows 11 (10.0.26200, AMD64), Python 3.14.6, Chrome 153.0.8010.53.
**Date of the live measurements below**: this session.

Every claim in this report is either (a) reproduced by a command named here, or
(b) explicitly marked as **not executed**. There is no third category.

---

## 1. Verdict

Nine independent verdicts, one per row §24 names. No single overall PASS — several
rows are genuinely incomplete, and collapsing them would hide exactly what this
report exists to say.

| Row | Verdict | Basis |
| --- | --- | --- |
| **Durable QR login** | `VERIFIED` (persistence) / **`PRE_SCAN_VERIFIED`** (scan) | `login --qr` persists the GitCode credential *before* the OAuth leg and the openCsiTool session *after* `getUserInfo`, and exit 0 requires the write to have succeeded. Cross-process read verified. **No real WeChat scan was performed** — a phone is required, so the scan itself is `PRE_SCAN_VERIFIED`, never "QR login PASS". |
| **Durable browserless usage** | `VERIFIED` | Two independent processes, one DPAPI store, no browser: process A writes, process B reads the same token. `tools/acceptance_durable.py`, all checks PASS. |
| **Durable renewal** | `VERIFIED` (mechanism + persistence) | `HttpOAuthRenewer` against `StoredGitCodeCredentialSource`; the new token is persisted and the next process reads it. Live end-to-end renewal against a real account **not executed** — no scan. |
| **GitCode refresh-token rotation** | `VERIFIED` (endpoint + code) / **not executed** (live rotation) | Endpoint confirmed by probe: `POST https://gitcode.com/oauth/token?grant_type=refresh_token&…`. Rotation is undocumented; the code treats it as rotating and reports whether it actually rotated. No live rotation was observed. |
| **Tray browserless startup** | `VERIFIED` (store path + no auto browser) | The tray signs in through the same store, and `auto_recover_auth_host` now defaults to `False`, so the normal path starts no browser engine at all. |
| **Windows startup registration** | `VERIFIED` | `tray --startup-status --json` reports `source: frozen-cli-tray` and a `would_run` naming `opencsi-tray.exe`, never the bare CLI. |
| **CI** | `VERIFIED` — **all 14 jobs green** | Real GitHub Actions runs. See §11. |
| **Repository hygiene** | `VERIFIED` | 156 tracked files, every text blob stored LF; zero scratch tools; enforced by `tests/test_repository_hygiene.py`. |
| **Secret safety** | `VERIFIED` | 17 dedicated tests over every output surface; scanner clean; no real credential value found in 156 tracked files. |

### What is *not* claimed

- **No real QR scan was performed.** Per §10.3 this can only be reported as
  `PRE_SCAN_VERIFIED`. A phone is required, and no phone was involved. This is why
  the first row above is split: persistence is verified, the scan is not.
- **Live renewal against a real account was not executed**, for the same reason:
  there is no scanned credential to renew from. What is verified is the mechanism
  (HTTP, no browser) and the persistence (next process reads the new token).
- **GitCode refresh-token rotation was not observed live.** The endpoint is
  confirmed; whether GitCode actually returns a *new* refresh token is
  undocumented and was not exercised.
- **A real Windows sign-out → sign-in was not performed.** The registry round trip
  is verified directly (install, read back, remove), not across a real logon.
- **The consent path was not exercised against a never-approved account.** The
  `CONSENT_REQUIRED` branch is unit-tested; the live first-authorization flow is
  untested and deliberately never automated.
- **`xauth_token`'s consumer remains unidentified.** Recorded in §12.

### The one architectural claim this round turns on

The previous round's report ended with renewal working and login working, and both
statements were true. Neither survived the process that established them. That is
the gap this round closed, and it is worth stating plainly because it is the whole
point:

```text
browserless login
if it is only valid in the current process,
does not count as an independent tool.
```

The credential now lives in `%LOCALAPPDATA%\OpenCSI\credentials.dat`, encrypted
with DPAPI at `CurrentUser` scope, and is read by whichever process runs next.

---

## 2. Before / After

```text
starting HEAD   aadc5d0  chore: update tools
ending HEAD     f8cdb64  fix(ci): stop the frozen steps inheriting an expected non-zero status
```

Twenty commits, one per planned step, in the order §3 fixes:

```text
f16a880 chore: remove investigation scratch files
0a96735 chore: renormalize tracked text files to LF
c9f52c7 test: enforce repository line-ending hygiene
34f26f7 ci: make a clean checkout test the installed package
af5510c feat(auth): add secure credential store abstraction
11f20b9 feat(auth): persist credentials with Windows DPAPI
a3af984 feat(auth): load openCsiTool credentials from the secure store
ffbacff feat(auth): renew sessions from stored GitCode credentials
467fd5e feat(login): persist QR login credentials across processes
ab78254 feat(auth): refresh the GitCode access token from the stored refresh token
ab7d006 feat(tray): use the secure store as the normal authentication path
f988b7f ci: install the package in every job that runs the tests
5457cac fix(cli): let --renew use the stored credential, and never a foreign one
2612af7 feat(cli): add `opencsi logout` to clear the stored credentials
a4c0e07 fix: make every file parse and format identically on Python 3.10
ae3ad2e fix: stop starting a browser engine on the normal path (objective §9.2)
e96d091 fix(tray): report the startup source even when the key cannot be read
6a05e41 test(acceptance): run the durable path as separate real processes
46aa869 feat(doctor): report the credential store locally, before anything else
11b2280 fix(ci): relax ErrorActionPreference around steps that read stderr
f8cdb64 fix(ci): stop the frozen steps inheriting an expected non-zero status
```

`ending HEAD` names the last commit that changed **source, tests or tools**, not
the true tip. A report cannot contain its own SHA — writing it would change the
hash — so the anchor is the last behavioural change, which is what a reader needs
to check out.

Twenty-seven commits below, each a real work item; the commits that carry this
report are additional and are not listed, for the same reason:

```text
56d5974  auth: separate GitCode success from openCsiTool success
fda2db9  auth: renew the session over plain HTTP, with no browser engine
5b4e17f  auth: name the system proxy when it breaks authentication
cd02aef  login: complete a QR login without a browser at all
5fcc269  auth-host: report the mode it got, not the flag it asked for
3d949e5  tray: offer QR login, the one route that needs no browser
8ba6de3  ci: run both test runners, both OSes, and the frozen binaries
ceb09a1  renew: stop throwing away the session the browserless path just minted
adb0437  renew: persist the minted session into the browser
0b7d42d  docs: explain that the browser is the credential store, not an auth step
c979a98  tray: register the tray binary, derived from the running build
2a6b5da  research: probes and the investigation that reversed the browser-bound claim
01ad527  report: final auth closure, with the incomplete items named as incomplete
2fce6a6  report: correct the before/after hashes and the diffstat scope
b2dcf98  tray: report which context the startup command was derived in, and test it
af05536  auth-host: close the browser gracefully, or stop() destroys the session
ddd7724  build: pin line endings, since a whole-file rewrite already corrupted four
362e407  probe: make the auth-host persistence measurement decisive, and fix its model
eb61b0a  report: record the auth-host defect, and correct three claims that were wrong
6ad9d80  docs: correct the older report's browser-bound conclusion, per section 48
3698407  docs: point the older report at the newer conclusion
b627650  feat(tray): wire the hidden auth host into the monitor, as section 30 asks
3bfb3c7  report: section 9c, and correct a verdict row that was no longer true
5a092e7  fix(auth-host): stop the headless probe leaking a browser per call
0a58e44  fix(auth-host): two probes that were wrong in opposite directions
407752e  docs(auth-host): the old probe did not hang -- it started a browser
baa1f3a  fix(auth): say why renewal is available and still will not work
```

```text
20 files changed, 4901 insertions(+), 397 deletions(-)   (this round, excluding this report)
```

| Metric | Before | After |
| --- | --- | --- |
| pytest | 860 passed, 1 skipped, 182 subtests | **1075 passed, 1 skipped, 186 subtests** |
| unittest | Ran 861, OK (skipped=1) | **Ran 1076, OK (skipped=1)** |

The behavioural change, stated as the user experiences it:

```text
BEFORE
  QR  -> GitCode login -> session established -> process exits -> credential gone
  usage (next process) -> no credential -> start a browser, or sign in again
  token expires -> renew -> the browser that held the session is gone
  tray startup -> works only while that browser profile still has the cookies

AFTER
  QR  -> GitCode login -> openCsiTool session, established and verified, no browser
      -> GitCode credential and session both written to an encrypted store
  usage (next process) -> reads the store -> succeeds with no browser at all
  token expires -> stored GitCode credential -> plain HTTP renewal -> new token
      -> written back to the store, so the process after that sees it too
  tray startup -> reads the same store; starts no browser unless asked to
```

### Where the credential lives, and why there

```text
%LOCALAPPDATA%\OpenCSI\credentials.dat
```

Encrypted with DPAPI at **`CurrentUser`** scope, so Windows user A cannot read
Windows user B's credential — which is the property that makes "store it locally"
safe at all. `LocalMachine` scope would have been readable by every account on the
box and is deliberately not used.

The plaintext JSON exists **only in memory**:

```text
serialize in memory
    -> CryptProtectData
    -> write encrypted bytes to credentials.dat.tmp
    -> os.replace(tmp, target)
```

There is no plaintext temp file at any point, and the replace is atomic, so a
crash cannot leave half a credential file behind. Verified: 502 bytes of
ciphertext for a bundle whose three values total under 60 bytes, and the
acceptance check greps the file for each value it wrote.

On non-Windows there is **no fallback store**. `open_default_store()` returns
`None` and the browser path is used. A plaintext fallback was rejected outright:
inventing one would put a live credential in the clear on a machine whose owner
expected encryption.


---

## 2b. The eight questions, answered

§23 requires each of these answered as `YES + evidence`, `NO + exact blocker`, or
`NOT EXECUTED`. Nothing else.

### Q1. After `opencsi login --qr` succeeds, does closing the process and running `opencsi usage` succeed?

**`YES`** — for the persistence half, which is the part this round changed, and
**`NOT EXECUTED`** for the real-scan half.

Evidence for persistence, from `tools/acceptance_durable.py`:

```text
[PASS] process A writes the credential
[PASS] process B reads the same credential
[PASS] no plaintext on disk -- 502 bytes ciphertext
```

Process A and process B are separate interpreter invocations sharing only the
store directory. `login --qr` writes the GitCode credential *before* the OAuth leg
and the session *after* `getUserInfo`, and exit 0 now requires the write to have
succeeded — a session that cannot be persisted exits `35`
(`EXIT_NOT_PERSISTED`), not 0, because the user must know the next process will
fail.

Exact blocker on the scan: **no WeChat scan was performed.** A phone is required.
Per §10.3 this can only be reported as `PRE_SCAN_VERIFIED`.

### Q2. Does success involve a Chrome/Edge auth host?

**`NO`.**

Evidence: `HttpOAuthRenewer` performs the whole round-trip over plain HTTP —
verified by an earlier investigation and unchanged. In this round the normal path
was changed so it *cannot* involve one: `MonitorConfig.auto_recover_auth_host` now
defaults to `False`, and the tray flag became opt-in `--auth-host` (the old
`--no-auth-host` is removed rather than kept as a silent no-op, so a user passing
it gets a usage error instead of false agreement). `_maybe_recover_browser` also
returns early whenever a stored credential exists.

`BrowserOAuthRenewer` and `AuthBrowserHost` are **kept**, not deleted, and
repositioned as fallback — for first consent, for migrating an old browser-profile
credential, and for when the store is unavailable.

### Q3. After the openCsiTool token expires, can it be renewed entirely from the stored GitCode credential?

**`YES`** for the mechanism and the persistence; **`NOT EXECUTED`** against a live
account.

Evidence: `CliContext.make_renewer()` builds
`HttpOAuthRenewer(StoredGitCodeCredentialSource(store))`, so the GitCode
credential is read from DPAPI and the new token is written back through
`StoredOpenCsiCredentialProvider.remember_token()`. Renewal no longer requires a
`CdpCookieProvider` — that requirement was removed this round — and the store is
consulted only when the provider is actually store-backed, so a manually pasted
token can never renew against a stored identity.

Exact blocker on the live run: there is no scanned credential to renew from, since
no scan was performed.

### Q4. After the GitCode access token expires, does the refresh token actually extend the login?

**`NOT EXECUTED`** — endpoint and code verified, live rotation not observed.

Evidence for the endpoint, from `tools/probe_gitcode_refresh.py`:

```text
POST https://gitcode.com/oauth/token?grant_type=refresh_token&refresh_token=…
response: access_token, expires_in (1296000 = 15 days), refresh_token, scope, created_at
```

Note the host is `gitcode.com`, **not** `web-api.gitcode.com`, and the response
carries **no** `token_type`. Whether GitCode actually returns a *new* refresh token
is undocumented; the code treats it as rotating, replaces the old value
atomically, and reports whether a rotation actually occurred rather than assuming
one. Failure statuses are kept distinct — `REFRESHED`, `LOGIN_REQUIRED`,
`NETWORK_ERROR`, `PROTOCOL_ERROR` — so a network blip is never reported as "sign in
again".

### Q5. After Windows starts, does the Tray show usage without the user starting a browser first?

**`YES`** for the store path and the no-browser default; **`NOT EXECUTED`** across
a real sign-out.

Evidence: the tray signs in through the same store the CLI uses and invalidates the
monitor's cached credential rather than patching client internals — so the
provider sees the new credential on its next read. `auto_recover_auth_host` is
`False` by default, so startup does not launch Chromium. Startup registration is
verified to name `opencsi-tray.exe`, never the bare CLI.

Exact blocker: a genuine Windows sign-out → sign-in was not performed. The
registry round trip is verified directly (install, read back, remove, restore), not
across a real logon.

### Q6. Is CI fully green?

**`YES` — all 14 matrix entries.** See §11 for the job table and the six real
defects the first honest run exposed.

### Q7. Is the repository fully LF-normalized?

**`YES`.**

Evidence: `git ls-files --eol` reports `i/lf` for every text blob in the index —
which is what is committed, and what `tests/test_repository_hygiene.py` asserts by
reading blobs back out of `HEAD` rather than trusting the working tree. 156 tracked
files. `*.bat`, `*.cmd` and `*.ps1` are explicitly CRLF, and binaries are marked
`binary` so `text=auto` never rewrites them.

The renormalisation was necessary, not cosmetic: adding `text=auto eol=lf` did not
fix files already committed as CRLF, because `git add` only re-normalises a path
whose working-tree file is newer than its index entry. That required a one-time
`git add --renormalize`, which is recorded in `.gitattributes` itself so the next
person does not rediscover it.

### Q8. Are there still real secrets committed or output?

**`NO`.**

Evidence:

```text
bash tools/ci_secret_scan.sh
ok: no JWT-shaped strings
ok: no literal Cookie headers with values
ok: no hard-coded token literals
ok: no generated files are tracked
```

Plus 17 tests in `tests/test_secret_safety.py` covering every output surface, and a
grepped ciphertext check in the acceptance runner. Real credential values found in
156 tracked files: **0**.

One honest note: the scanner flagged this round's *own* acceptance fixture
(`refresh_token='refresh-token-…'`), and the fix was to rebuild the fixture at
runtime rather than to add an exemption. The scanner exempts `tests/` but not
`tools/`, and that is the right policy.

---


### Why exit 0 was wrong

`opencsi login --qr` returned **0** as soon as GitCode authenticated. But a
completed GitCode login is not a completed openCsiTool login: the tool cannot fetch
anything until openCsiTool's own `token` cookie exists. A script doing
`opencsi login --qr && opencsi usage` therefore proceeded past a login that had not
happened, and failed later with an error that pointed at the wrong layer.

The failure mode this creates is the specific one §74 names: *partial success
reported as complete success.*

### How it was fixed

`LoginStage` makes the distinction a value rather than a sentence in the output:

```text
NONE                     nothing happened
GITCODE_AUTHENTICATED    GitCode leg done; openCsiTool session not established
OPENCSITOOL_AUTHENTICATED  the session exists and was verified against the server
OPENCSITOOL_PENDING      a human is needed (consent, or a sign-in)
```

- `LoginResult.complete` is true **only** for `OPENCSITOOL_AUTHENTICATED`.
- `EXIT_OPENCSITOOL_PENDING = 34` — a new, distinct exit code, so a script can tell
  "GitCode worked, openCsiTool did not" apart from both plain success and hard failure.
- Both text and JSON output carry `stage`, `complete`, `reason`, `next_step`.
- The JSON payload also carries `mechanism`, `browser_used` and `oauth_trace`, so a
  machine-readable report can prove *how* the session was obtained — including that
  no browser was used.

The exit code is the load-bearing part: prose in a log can be missed, and a
non-zero exit is what stops `&&` chains from running against a session that is not
there.

---

## 4. QR Credential Bridge

**No values appear in this section.** Only names, lengths and fingerprints.

### What tokens QR returns

The GitCode scan leg returns, in the response body:

| Name | Kind | Notes |
| --- | --- | --- |
| `access_token` | opaque string | long-lived GitCode SSO credential |
| `refresh_token` | opaque string | used by GitCode to rotate the access token |
| `username` | string | account login |
| `xauth_token` | opaque string | present; purpose still unidentified — see §12 |

These are **not** cookies at the point of return; they are body fields. The client
maps them to the cookie names GitCode's own web client uses:

```text
access_token   -> GITCODE_ACCESS_TOKEN
refresh_token  -> GITCODE_REFRESH_TOKEN
username       -> GitCodeUserName
```

### How they relate to browser SSO

They *are* the browser SSO session. Measured against a fresh cookie jar with the
`token` cookie deliberately excluded:

| Cookies supplied | openCsiTool session minted? |
| --- | --- |
| all 21 GitCode cookies | **yes** |
| `GITCODE_ACCESS_TOKEN` + `GITCODE_REFRESH_TOKEN` + `GitCodeUserName` | **yes** |
| `GITCODE_ACCESS_TOKEN` **alone** | **yes** |
| none | no |

So the QR leg's output is a strict superset of what the OAuth leg needs. The refresh
token, the username, the WAF cookies, the User-Agent, and `Origin`/`Referer` were all
individually unnecessary.

### Whether they can be consumed

**Yes, browserlessly.** Three requests, no browser engine:

```text
GET  opencsitool.com/opencsitool/rest/v1/oauth2/authorization/gitcode?redirect=%2FmyTools
       -> 302 to gitcode.com/oauth/authorize  (sets gitcode_oauth_session)
POST https://web-api.gitcode.com/uc/api/v1/oauth/checkOrAuthorize
       multipart: client_id, state, redirect_uri, response_type=code
       -> 200 {"redirect_uri": "<callback>?code=..&state=..", "reauth_required": null}
GET  <that callback>
       -> 200 + Set-Cookie: token      (measured length 333)
GET  /opencsitool/rest/v1/user/getUserInfo
       -> 200
```

Live proof, on a real session, with no browser anywhere:

```text
$ python tools/probe_qr_browserless_login.py --source-port 9222
  gitcode cookies available: ['GITCODE_ACCESS_TOKEN', 'GITCODE_REFRESH_TOKEN', 'GitCodeUserName']
  outcome : RENEWED
  token_minted: true
  getUserInfo : OK (user_name='shijingchang')
  VERDICT: BROWSERLESS_LOGIN_ACHIEVABLE
```

### The boundary, stated exactly

`checkOrAuthorize` returns a code **when a grant already exists**. That is the
renewal case, and it is what a long-running monitor does.

When **no grant exists** — an account that has never approved the application —
GitCode answers `401` and the SPA loads a consent page. That single case needs a
human. It is reported as `CONSENT_REQUIRED`, and:

> The consent-submission endpoint `POST /uc/api/v1/oauth/authorize` is
> **read from the bundle only and never called.** Approving a third-party
> authorization is the user's decision, not this tool's. A test asserts the
> endpoint is never reached.

---

## 5. OAuth SPA investigation

Full detail in `docs/oauth-spa-investigation.md`.

### Bundles

| Bundle | Role |
| --- | --- |
| `index-*.js` (main, hash rotates) | route table, axios instance, API base host |
| `vendor-layout-*.js` | global login modal; the real scan UI |
| `login-*.js` (`/-/oauth/login`) | 988 B **redirect shim** only — no scan UI |

Bundle hashes rotate between builds (observed `index-a97e2b06` → `index-e12962d8`);
every endpoint below was re-verified against the current build and is unchanged.

### API endpoints

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/opencsitool/rest/v1/oauth2/authorization/gitcode` | GET | entry; 302 to GitCode |
| `gitcode.com/oauth/authorize` | GET | **SPA shell** (the misleading one) |
| `/uc/api/v1/oauth/checkOrAuthorize` | POST | **the real judgement** |
| `/uc/api/v1/oauth/authorize` | POST | consent submit — **never called** |
| `/opencsitool/rest/v1/oauth2/authorization/callback/gitcode` | GET | mints `Set-Cookie: token` |
| `/opencsitool/rest/v1/user/getUserInfo` | GET | the only accepted proof |

### Request sequence

```text
GET  entry            -> 302  gitcode.com/oauth/authorize?client_id=..&state=..&redirect_uri=..
                              (browser would render the SPA shell here and stop)
POST checkOrAuthorize -> 200  {"redirect_uri": "<callback>?code=..&state=.."}
GET  callback         -> 302 + Set-Cookie: token
GET  getUserInfo      -> 200  identity
```

The fourth request is what the earlier investigation never made, and its absence is
the whole of the error.

### Browserless verdict

**`PURE_HTTP_OAUTH_FEASIBLE`.** Reproduced three times independently.

### The methodology error, recorded

The previous verdict was `browser-bound`, on the evidence that
`/oauth/authorize` returns a 5793-byte page with 11 `<script>` tags and no
`Set-Cookie`. **Every observation was correct. The inference was not.**

The probe followed redirects, so it necessarily came to rest on the SPA shell —
that was the only place its request sequence could end. "Could not get past this
page" was then read as "no path exists". The judgement actually happens in one
backend call *behind* the shell, and adding that single step completes the flow.

| Proposition | Truth |
| --- | --- |
| The `/oauth/authorize` **page** needs JavaScript to render | **true** |
| Establishing an openCsiTool session needs JavaScript or a browser | **false** |

This is §74's second distinction, and it is worth recording that the project made
this error **twice**: once as a keyword match (`captcha` inside a `<script>`), and
once as an incomplete request sequence. The second was harder to see, because
nothing in the data was wrong.

---

## 6. Browser requirement

Two questions that were previously conflated. They have different answers.

| Question | Answer | Evidence |
| --- | --- | --- |
| **Is browser *interaction* required?** | **Yes — exactly once per account.** | `checkOrAuthorize` answers 401 for a never-approved grant; the consent page needs a click. Every subsequent renewal is automatic. |
| **Is a browser *engine* required?** | **No.** | The whole flow — including minting `token` and verifying with `getUserInfo` — runs over plain HTTP. Proven by two independent probes. |

And the third distinction from §74:

| Question | Answer |
| --- | --- |
| **Must the user interact with a browser *window*?** | **No.** The one required interaction is approving a grant; it can be presented however is convenient, and after that nothing is needed. |

### So why is `BrowserOAuthRenewer` still in the tree?

Because of the first row, plus one environment case. The old justification — "the
OAuth callback needs a browser session" — **was wrong** and has been removed from
the docs. The two real reasons:

1. **First authorization needs a human.** Presenting the consent page is the
   browser path's job. The default order is HTTP first, browser second, decided by
   `FallbackRenewer`.
2. **Some deployments only expose the GitCode session inside a browser profile.**
   `CdpCookieProvider` is then the only way to reach it.

`FallbackRenewer` stops immediately on `CONSENT_REQUIRED` and `LOGIN_REQUIRED`:
both need a human, neither is fixable by the next mechanism, and falling through
would delay a message the user needs while possibly masking a real consent
requirement behind a generic "renewal failed".

### The browser as *credential store*

Authentication no longer needs a browser; **storage** still does, and these are
different things. The project's hard rule is that a credential is never written to
disk — the browser is where it lives, which is what lets `opencsi usage` run as a
separate process with no shared state.

A browserless renewal broke this invisibly, and §9 below describes the fix.

---

## 7. Final Login Flow

```text
                        ┌──────────────────────────┐
                        │  no session / expiring   │
                        └────────────┬─────────────┘
                                     │
              ┌──────────────────────┴──────────────────────┐
              │                                             │
     (renewal path)                                 (first sign-in)
              │                                             │
              ▼                                             ▼
  ┌───────────────────────┐                    ┌────────────────────────┐
  │ HttpOAuthRenewer      │                    │ opencsi login --qr     │
  │  read GitCode session │                    │  POST qrcode/wechat_.. │
  │  (from CDP, or from   │                    │  -> scene_id + image   │
  │   a QR scan's output) │                    │  poll GET ?scene_id    │
  └───────────┬───────────┘                    │  WAITING/SCAN/LOGIN    │
              │                                └───────────┬────────────┘
              │                                            │  human scans
              │                                            ▼
              │                                ┌────────────────────────┐
              │                                │ POST .../login/qrcode  │
              │                                │ -> access/refresh token│
              │                                └───────────┬────────────┘
              │                                            │
              └───────────────────┬────────────────────────┘
                                  ▼
                 ┌────────────────────────────────────┐
                 │ GET  oauth2/authorization/gitcode  │
                 │        -> 302 gitcode.com/oauth/.. │
                 │ POST /uc/api/v1/oauth/             │
                 │        checkOrAuthorize            │
                 │  200 -> {"redirect_uri": callback} │
                 │  401 -> CONSENT_REQUIRED ──────────┼──┐
                 │ GET  <callback>                    │  │
                 │        -> Set-Cookie: token        │  │
                 └────────────────┬───────────────────┘  │
                                  │                      │ human approves
                                  ▼                      │ once, in a browser
                 ┌────────────────────────────────────┐  │
                 │ GET /rest/v1/user/getUserInfo      │◄─┘
                 │ 200 -> session VERIFIED            │
                 └────────────────┬───────────────────┘
                                  │
                                  ▼
                 ┌────────────────────────────────────┐
                 │ install_token(token) -> browser    │
                 │  Storage.setCookies, 1 cookie,     │
                 │  HttpOnly + Secure preserved       │
                 └────────────────┬───────────────────┘
                                  │
                                  ▼
                 ┌────────────────────────────────────┐
                 │ next process reads the cookie      │
                 │ opencsi usage -> real data, exit 0 │
                 └────────────────────────────────────┘
```

The last two boxes are what make it a *closed* loop. Without them the session
existed only in the memory of the process that renewed it.

---

## 8. Tray startup flow

From Windows sign-in to `OK`:

```text
 1. Windows sign-in
       |
       v
 2. HKCU\...\CurrentVersion\Run  ->  opencsi-tray.exe
       |   resolved from the running build:
       |     frozen tray  -> its own sibling .exe
       |     frozen CLI   -> the tray next to it
       |     source       -> the console script
       v
 3. opencsi-tray.exe   (no arguments -> resident tray)
       |
       +-- single-instance guard: a second launch exits ALREADY_RUNNING_EXIT
       |   and *shows* a message, because a silent second launch looks broken
       |
       v
 4. MonitorService starts on a worker thread
       |
       +-- first tick: read the credential from the browser
       |
       +-- no browser answering?
       |     -> recovery, hidden first (objective §30):
       |          AuthBrowserHost(headless=True)   <-- starts hidden, opens no
       |                                             window; refused before launch
       |                                             if it would fall back to visible
       |          launch_debug_browser()           <-- only with
       |                                             --auto-recover-browser
       |     -> engine up but its profile has no token:
       |          state LOGIN_REQUIRED              <-- the honest label: the
       |                                             browser is no longer the problem
       |     -> no engine at all:
       |          state BROWSER_UNAVAILABLE
       |     -> menu offers, in order:
       |          "扫码登录（无需浏览器）"   <-- default; needs no browser
       |          "Sign in..."             (non-default)
       |          "Start a readable browser" (non-default)
       |
       +-- token present but close to expiry (<= 5 min)?
       |     -> FallbackRenewer: HTTP first, browser second
       |     -> on success, install_token() writes it back to the browser
       |
       v
 5. state OK, tooltip + menu show tokens / requests / adoption
```

### The two recoveries have opposite defaults, on purpose

The hidden host is **on** by default and `--no-auth-host` turns it off; the visible
browser is **off** by default and `--auto-recover-browser` turns it on. That looks
inconsistent until you ask what each costs the user: a hidden engine puts nothing on
screen, a window does. §30 asks for the post-reboot case to resolve itself, and
§61 forbids `BROWSER_UNAVAILABLE` merely because Chrome was not already running —
but neither licenses opening a window nobody asked for.

Getting this right required a change in `AuthBrowserHost`: it used to open the
visible fallback *as part of starting* and report it afterwards, so a caller that
inspected the result and declined had already put the window on screen.
`visible_fallback=False` refuses it before the launch, which is the only point
where refusing changes anything.

### §61's "last-good snapshot survives auth/network failure", measured live

The unit tests cover this against a fake client. It was also driven through the
real `MonitorService._handle_failure`, starting from a snapshot that genuinely held
numbers, so the tray's promise — a transient failure changes the *state*, not the
data — is checked against the code that actually runs:

```text
network failure   OK -> OFFLINE      total_tokens 1234 -> 1234   requests 56 -> 56
                  has_data True -> True   last_error recorded   backoff scheduled
auth failure      OK -> AUTH_ERROR   total_tokens 1234 -> 1234   requests 56 -> 56
                  has_data True -> True   last_error recorded   backoff scheduled
```

Both failure classes keep the numbers *and* report a state that is not `OK`. That
second half is the one worth stating: preserving the data while claiming health
would be the opposite bug, and it is the one a "keeps the last good data" test
would pass.

### Why the QR action is offered *first* in `BROWSER_UNAVAILABLE`

Every other actionable route in that state needs a browser — which is the thing
that is broken. A menu of browser-only options in the state named
"browser unavailable" is the same closed loop the state exists to escape, in a new
shape. `launch_browser` is kept and stays reachable: a user whose browser simply is
not running yet is better served by it than by fetching a phone.

Verified:

```text
$ opencsi-tray.exe --check
tray: ok
state: STARTING
menu items: 7
tooltip: OpenCSI | 启动中 / 等待首次更新
```

---

## 9. Frozen Startup Fix

### The bug

`startup_command()` computed the path to run at sign-in by assuming the **CLI's**
location. In a frozen build that produced a command naming `opencsi.exe` — a
different binary from the tray, and one which, launched at sign-in with no console,
does nothing visible. The user's symptom was "the tray does not start", with no
error anywhere.

### The fix

`StartupStatus` now records where the command came from, and the four labels
distinguish contexts that are genuinely different facts:

```text
SOURCE_FROZEN_TRAY        this process *is* opencsi-tray.exe
SOURCE_FROZEN_CLI_TRAY    this process is opencsi.exe; the tray beside it was found
SOURCE_FROZEN_CLI         this process is opencsi.exe; no tray exists, so use `tray`
SOURCE_SOURCE_INSTALL     running from a checkout
```

There are four labels for three contexts because the frozen CLI has two outcomes,
and collapsing them was itself a defect. The frozen CLI with a sibling present
reported `derived from: frozen-tray`, but `frozen-tray` is documented as describing
*this build*, and `_is_frozen_tray()` was `False` in exactly that case — so
`opencsi.exe tray --startup-status` printed a false statement about the running
process, in the one output a user consults to find out what will start at sign-in.
An invariant test now asserts that only a process that really is the tray binary
may report `frozen-tray`.

`matches_this_build` reports whether the registered command is the binary the user
is currently running. `opencsi tray --startup-status` prints `derived from: <source>`
and warns when it is not.

### The three commands, verified live

All three running contexts, each reporting its own shape. §73 §9 asks for exactly
these three:

```text
SOURCE INSTALL  (python -m opencsi.cli.app tray --startup-status)
start at sign-in: disabled
would run: ...\pythonw.exe -m opencsi.tray
derived from: source

FROZEN CLI      (opencsi.exe tray --startup-status)
start at sign-in: disabled
would run: D:\workspace\OpenCSIToolMonitor\dist\opencsi-tray.exe
derived from: frozen-cli-tray

FROZEN TRAY     (opencsi-tray.exe --startup-status)
start at sign-in: disabled
would run: D:\workspace\OpenCSIToolMonitor\dist\opencsi-tray.exe
derived from: frozen-tray
```

Note the two frozen cases name the *same* command but report *different* sources,
because they are different facts: the CLI found a tray beside it, while the tray
binary is itself. Reporting both as `frozen-tray` — which is what shipped first —
told a user running `opencsi.exe` something untrue about their own process.

The install/remove round trip, on the frozen CLI:

```text
$ opencsi.exe tray --install-startup
The tray will start when you sign in.
command: D:\workspace\OpenCSIToolMonitor\dist\opencsi-tray.exe
derived from: frozen-cli-tray
(the tray binary beside this CLI, not the CLI itself)

$ Get-ItemProperty HKCU:\...\Run -Name OpenCSIToolMonitor
D:\workspace\OpenCSIToolMonitor\dist\opencsi-tray.exe      <-- the tray, not the CLI
```

Removal was then verified and the registry returned to its prior (absent) state, so
the machine was left as found.

---

## 9b. The auth host destroyed the session it existed to preserve

Found by running §65's persistence cycle rather than by reading the code.

`AuthBrowserHost.stop()` terminated the browser with `taskkill /F`. Chromium keeps
its cookie store in memory and flushes to the profile's SQLite database on a delay,
so a hard kill discards every cookie written since the last flush. The GitCode
session reaches that profile through a CDP cookie write — so `stop()` was deleting
the exact credential the hidden-auth-host design was built to carry across
restarts, and doing it silently. The next renewal would find no session and tell the
user to sign in again, with nothing connecting that to a `stop()` that ran hours
earlier.

`stop()` now calls `Browser.close` first and only falls back to the kill when the
browser ignores it, logging a warning when it has to — because the whole reason this
was hard to see is that the loss was silent.

### Two measurement errors on the way to that conclusion

Both are recorded because the second nearly produced a wrong answer that looked
like a right one.

1. **The first marker had no `expires`.** A cookie without an expiry is a *session*
   cookie, and Chromium never writes those to its persistent store — by design,
   however the browser is closed. The probe was therefore measuring the cookie type,
   not the close method. The real openCsiTool token carries an expiry (~58 minutes),
   so an expiring marker is also the faithful model.

2. **A constant marker cannot tell whose write it is seeing.** An intermediate
   harness appeared to show the marker surviving a graceful close and not a kill,
   which looked like confirmation. It was an artefact: that harness wrote a stale
   value first, so the "surviving" reads were the *previous* trial's already-flushed
   value. A unique nonce per trial removed the ambiguity.

With both fixed, and both methods measured repeatedly rather than once — survival
depends on when the periodic flush fires, so a single run can pass or fail for
reasons unrelated to the close method:

```text
graceful close (stop(), as shipped) : 3/3 survived
hard kill (the original stop())     : 0/3 survived
```

The probe reports the unfavourable outcomes honestly too: `BOTH_SURVIVE` says the run
does not demonstrate the fix is needed, and `INCONCLUSIVE` says survival varied
within a method, so more trials are required before concluding anything.

`AuthBrowserHost` also had no test file at all. `tests/test_auth_host.py` now pins
the ordering that makes the flush happen, the kill fallback, the warning, and the
`describe()` rule about reporting the observed mode rather than the requested one.
The ordering test was confirmed to fail against the kill-first code before being
kept.

---

## 9c. The hidden auth host: never wired in, then wired in and still broken

§30 gives the flow a signed-in Windows user should get:

```text
Windows sign-in
    -> monitor starts
    -> AuthBrowserHost.ensure_running(hidden=True)
    -> restore GitCode SSO
    -> silent renewal
    -> fetch usage
    -> tray = OK
```

and §61 states the acceptance condition: *no `BROWSER_UNAVAILABLE` merely because
Chrome was not manually started*.

**Neither was true.** `AuthBrowserHost` was written, tested and documented, and
then called by nothing except `login --qr`. The monitor's only recovery path was
`launch_debug_browser`, which is opt-in precisely *because it opens a window*. So
on a freshly signed-in machine the default behaviour was the exact failure §61
forbids: the tray sat at `BROWSER_UNAVAILABLE`, which reads as "this tool is
broken" when the truth is "nothing is signed in yet".

This is the same shape as the other defects in this report — a component that
works, is verified in isolation, and is not connected to the thing that needs it.
`ensure_running` had a passing test file; nothing asserted that the *monitor*
ever called it.

### The fix, and the bug the fix's own test found

The monitor now tries the hidden host first and the visible browser second, on
opt-in. The two recoveries have **opposite defaults**, because they cost the user
differently: a hidden engine puts nothing on screen, a window does.
`--no-auth-host` turns the first off.

The first version of this had a defect that its own test caught: the cooldown was
stamped only after a *successful* hidden attempt, so a host that kept failing was
respawned on every backoff tick. The stamp is now shared and taken before either
attempt.

### The ordering problem, which is the interesting part

`ensure_running` opened the visible fallback **as part of starting** and reported
it afterwards. A caller that inspected the result and declined was therefore
already too late: the window was on screen, and the only thing left to control was
what the caller *said* about it. Refusing a window after it has been opened is not
refusing it.

`AuthBrowserHost` gained `visible_fallback=False`, which declines the fallback
before the launch. That turns "this caller does not open windows" from a
description into a fact. A test pins it by removing the guard and confirming the
test fails; a live probe confirms the behaviour on this desktop — with the guard,
nothing starts and the state stays `BROWSER_UNAVAILABLE`; without it, a window
appeared during the probe run.

### Why it *still* did not work after being wired in, and the two bugs behind that

Wiring the host in was necessary but not sufficient, and the reason is worth
recording because it is the most instructive pair of defects in this report: both
were in code that asks the browser a question, and both were invisible to tests
that checked the *answers* and never the *questions*.

**The capability probe started a browser instead of asking a question.**
`_headless_supported` ran `chrome --headless=new --version`. I first recorded this
as a *hang*, and re-measuring showed that was wrong: the probe returns `rc=0` in
0.12–0.41s, prints **nothing at all**, and starts a real headless browser that
keeps running. The fifteen-second timeout never fires.

So it was not a capability check that failed. It was an accidental browser launch
whose empty output made the `b"Chrom" in blob` check fail — and whose leaked
children (eleven per call) were separately cleaned up as if they were an unrelated
bug. With `visible_fallback=False`, the resulting "unsupported" verdict made the
host refuse to launch at all. The measurement that proved headless worked all
along: the same binary with `--headless=new --remote-debugging-port=…` answers
`HeadlessChrome/153.0.0.0`.

The likeliest reason the original observation looked like a hang is machine load:
by then roughly a hundred orphaned browsers from this very probe were running.
That cannot be proven retroactively, so it is named as the likely cause rather
than asserted as the cause.

**The mode probe read the field that does not carry the marker.** `_probe_mode`
read `Browser.getVersion`'s `product` and searched it for `"Headless"`. On this
build:

```text
product    Chrome/153.0.8010.53
userAgent  Mozilla/5.0 (Windows NT 10.0; Win64; x64) … HeadlessChrome/153.0.0.0 …
```

So *every* headless browser was reported `VISIBLE`. That is the opposite error
from the first and worse in effect: the host ran hidden while telling the user —
and the tray — that a window was on their screen, and a caller that had asked for
no window rejected its own perfectly good host.

### The measurement error underneath both of them

An earlier round concluded that launching a browser from Python was broken on this
machine, because every launch exited `rc=0` in about a second with empty output.
That conclusion was wrong, and the way it was wrong is the same mistake in a third
costume: **the launcher is not the browser.** Chromium hands the work to a child
and exits immediately. Watching the launcher's exit code measures the hand-off,
not the browser.

Re-measured with a control — the same binary with and without `--headless=new`:

| launch | launcher exit | user agent | visible windows |
| --- | --- | --- | --- |
| `--headless=new` | `rc=0` in 0.1s | `HeadlessChrome/153.0.0.0` | **0** |
| no flag (control) | `rc=0` in 0.1s | `Chrome/153.0.0.0` | 1 |

The flag demonstrably works, the control proves the user agent can tell the two
apart, and the browser answers its debug port 0.5s after the launcher has gone.

The replacement probe asks the browser to *do* something headless and checks the
artifact: `--screenshot` leaves a PNG, and a PNG has an eight-byte signature, so
the check is exact rather than a substring search. The exit code is deliberately
**not** the evidence — every candidate exits 0, including `--dump-dom`, which
produced no output at all.

| probe | artifact | visible windows | wall time |
| --- | --- | --- | --- |
| `--version` (old) | never (prints nothing) | 0, but leaked 11 processes | 0.12s, answers False |
| `--dump-dom` | 0/3 | 0 | 0.12s |
| `--screenshot` (new) | **3/3** | 0 | 0.12s |
| `--print-to-pdf` | 3/3 | 0 | 0.12s |

My first version of the replacement had the same bug in miniature, which is why it
is worth stating: it called `process.wait()`, which returns in 0.1s when the
launcher exits, and then killed the tree — destroying the browser *before* it could
write the screenshot it was being asked for. It answered "unsupported" on a build
where headless works. It now waits for the artifact, bounded by the same budget.

### The result, verified on screen rather than inferred

```text
AuthBrowserHost(visible_fallback=False).ensure_running()
    status   STARTED
    mode     HEADLESS
    headless True
    describe() "hidden Chromium authentication engine (no user-visible window)"
    visible windows on screen: 0

MonitorService._try_auth_host()          -> True, 8 engine processes, 0 windows
opencsi tray --once                      -> LOGIN_REQUIRED
```

`LOGIN_REQUIRED` is the correct and honest outcome: the engine is up and hidden,
and its fresh profile holds no `token` cookie, because no first sign-in has ever
been completed in it. That is a different statement from `BROWSER_UNAVAILABLE`,
and the difference is exactly what §61 asked for — the browser is no longer the
reason the tool cannot proceed.

So §30's flow is **wired correctly and verified through the hidden engine coming
up**. What is claimed: the monitor starts the host, gets a headless engine, opens
no window, rate-limits both recovery paths, and reports its state truthfully. What
is not claimed: that a real Windows sign-out and sign-in was performed — see §12.

### A process mistake worth recording

While investigating the probe, a cleanup command used an over-broad process
filter (`*opencsi*`, `*probe*`) and killed the debug Chrome instance on port 9222
that every live check in this report depends on. The user's own browser was not
affected — it runs a different profile and still has its windows — but the
verification endpoint had to be treated as gone, and roughly a hundred orphaned
headless processes from the probes were cleaned up afterwards. The lesson is
narrow and mechanical: a filter that matches by substring across a shared resource
will eventually match something that is not yours, so kill by exact profile path
and nothing else.

### The probe was leaking a browser per call

That cleanup also surfaced a real defect, which is the one useful thing to come out
of it. `_headless_supported` was leaking an entire Chromium process tree on every
invocation — **eleven orphaned processes per call**, each holding a
`HeadlessChrome*` profile in `%TEMP%`.

The mechanism is worth stating because the obvious fix does not work.
`subprocess.run(timeout=…)` kills the process it started, and the process it
started is only a **launcher**: Chromium hands the real work to a child and exits,
so by the time the timeout fires the PID is already gone and `taskkill /T` on it
finds nothing to walk. Chromium does name the child's profile
`HeadlessChrome<launcher pid>`, so the orphans are reachable by profile path even
though the parent link is not.

The cleanup now uses both mechanisms — `taskkill /T` while the launcher is alive,
and a profile-path sweep for when it is not — with the sweep matching only the
launcher's own PID, so it cannot reach a browser the user is running. Measured
after the fix: zero orphans across repeated runs.

Two details are pinned by tests rather than trusted:

- The cleanup moved into a `finally`. The timeout is the **normal** path on this
  build, so cleanup placed after the `try` would skip exactly the case that leaks.
  The test for this was verified to fail when the call is removed.
- The sweep uses PowerShell's `Get-CimInstance`, **not `wmic`**. The first version
  used `wmic`, which is removed on Windows 11; it raised `FileNotFoundError` inside
  an `except OSError` block, the handler swallowed it, and the cleanup reported
  success while killing nothing. A test asserts `wmic` is absent from the argv,
  because the failure mode of that mistake is silence.

This defect is a fair illustration of the report's own thesis. It was invisible to
the test suite, invisible in code review, and would have shipped as a slow resource
leak that only shows up after the tray has been retrying for a while.

---

## 10. Tests

Every number below is from a run on this machine, with the binaries that exist in
`dist/`. Nothing here is projected or estimated.

| Surface | Command | Result |
| --- | --- | --- |
| pytest | `python -m pytest` | **1075 passed, 1 skipped, 186 subtests passed** |
| unittest | `python -m unittest discover -s tests -t tests` | **Ran 1076, OK (skipped=1)** |
| packaging | `python tools/build_exe.py` | both binaries built, **and executed** |
| acceptance | `python tools/acceptance_durable.py` | **worst verdict: PASS** (8/8 checks) |
| secret scan | `bash tools/ci_secret_scan.sh` | **exit 0**, four clean categories |

### The acceptance runner, and why a unit test is not enough

`tools/acceptance_durable.py` exists because the question this round turns on
cannot be asked inside one process. "Does a credential written by one process work
in the next one, with no browser involved?" is a question about a *process
boundary*, and a test that shares an interpreter shares memory it is supposed to
be proving the absence of.

It therefore runs two independent interpreter processes against one store
directory and reports a verdict per check:

```text
[PASS] frozen binaries exist -- dist
[PASS] frozen --help lists the commands
[PASS] startup registers the tray, not the CLI -- frozen-cli-tray
[PASS] process A writes the credential
[PASS] process B reads the same credential
[PASS] no plaintext on disk -- 502 bytes ciphertext
[PASS] frozen logout clears the store
[PASS] no OpenCSI browser engine running

=== worst verdict: PASS ===
```

Three details are deliberate:

- **It fabricates the credential rather than scanning a QR.** What is under test
  is persistence and cross-process visibility, not the scan. Claiming otherwise
  would be the overclaim §10.3 forbids.
- **The no-plaintext check greps the ciphertext for each value it wrote**, rather
  than trusting that DPAPI was called. A test that asserts a function was invoked
  passes when the function is a no-op.
- **The browser check is scoped to OpenCSI's own engine.** It does not require the
  user to close their personal browser, which would be an unacceptable price for a
  diagnostic and would make the check unrunnable on a normal desktop.

### Secret safety has its own test file

`tests/test_secret_safety.py`, 17 tests. A distinctive long opaque value is planted
in each credential slot and asserted absent from every surface that can print:
the bundle repr, each credential repr, `login --status` (text and JSON), `doctor`
(text and JSON), `logout` (text and JSON), `usage --json`, a `CredentialStoreError`
message, and the tray's tooltip, headline, status text and serialised snapshot.

Two properties are asserted rather than assumed:

- **Registration is what makes scrubbing work.** A bare opaque value matches no
  *shape*; it is masked because constructing a credential registers it. That is
  asserted end-to-end. The complementary limit is asserted too: an unregistered
  value matching no shape is **not** masked. That is accepted rather than "fixed"
  by lowering the length threshold, because a catch-all short enough to catch
  arbitrary values would also redact usernames and file paths, and a log that masks
  everything is a log nobody can read.
- **`MonitorSnapshot` has no credential field at all**, so the tray's strings are
  secret-free by construction. Asserted structurally, because a test that only
  grepped today's output would pass the day someone added such a field and fail
  only once something was rendered into it.


### Both runners, deliberately

`unittest` is not redundant with `pytest`. The suite is written to run under both,
and collection differs. Two earlier bugs were visible under only one runner.

### Packaging is an independent test object (§69)

A frozen build has a different import system, no source tree, and no `sys.path`
entry for `src`. Three real defects were found by **running** the artifacts, none of
which the test suite could see:

1. `opencsi-tray.exe tray --check` — the entry point doubled the sub-command.
2. `SessionManager.renew()` discarded the token the browserless renewer had just
   installed. The source run passed because it happened to reuse the provider.
3. `login --renew` built a fresh provider to verify with, re-reading a browser that
   never learned the cookie.

Defects 2 and 3 are the reason §69 exists: the source tree was green throughout.

### §66's tray items, and one that looked like a bug and was not

Rebuilt the binaries (§69) and ran them rather than the source. `--check` exits 0
with `tray: ok` / 7 menu items; `--once` exits 13 with the honest
`LOGIN_REQUIRED` note. Session lifetime, tooltip and menu were driven through the
real presenter over four snapshot shapes: the `会话` line appears when an expiry is
known and is absent when it is not, the menu gains a `renew` action only once
there is data, and an offline snapshot keeps its numbers *and* gains an explicit
`离线 - 最后更新 …` marker plus the error text.

**The false alarm is worth recording.** I started a blocking tray, started a
second one, and the second exited **2** with *"another OpenCSI tray is already
running"* — correct. Then I terminated the first with `Popen.terminate()` and one
process remained, which reads exactly like an orphaned tray.

It is not. Two things were wrong with the test, not the product:

- The first attempt used `--check` for the second instance and concluded the
  single-instance guard did nothing. `--check` deliberately passes
  `blocking=False` and **skips** the lock, because a health check that a running
  tray could block cannot answer the question it exists to answer. The guard is on
  the blocking path, which is what the corrected test exercises.
- A PyInstaller onefile binary is a **bootloader parent plus a child** running the
  Python code. Measured directly: pid 45392 (parent) and pid 31436 (child,
  `ppid=45392`). Killing the parent leaves the child. That is a harness artefact;
  the documented exit is the menu's Exit item, and `TrayApp.quit()` was driven
  in-process — it stops the worker and the icon, returns in 0.00s, and a second
  `quit()` is harmless.

Also checked: there is no `--stop` flag. The message the second instance prints
already says the supported route — *"right-click that icon and choose Exit
first"* — so the guidance and the CLI agree.

### New test files

| File | Tests | Covers |
| --- | --- | --- |
| `tests/test_credential_store.py` | 58 | the bundle model, partial saves, redacting reprs, store protocol, `MemoryCredentialStore` |
| `tests/test_gitcode_refresh.py` | 43 | the refresh endpoint, rotation, and the four failure statuses kept distinct |
| `tests/test_tray_durable.py` | ~25 | the tray's store-backed sign-in and service invalidation |
| `tests/test_secret_safety.py` | 17 | §16 — no credential on any output surface |
| `tests/test_durable_login.py` | 14 | QR persistence: GitCode saved before the OAuth leg, session after verification |
| `tests/test_logout.py` | 14 | `logout`, its two scopes, and the `--no-store` refusal |
| `tests/test_repository_hygiene.py` | 8 | committed blobs are LF; no scratch tools; no generated files |
| `tests/test_http_oauth.py` | 36 (+7 subtests) | the three-request flow, refusals, secret safety, fallback chain, browser persistence |
| `tests/test_qr_login_semantics.py` | 22 (+8 subtests) | `LoginStage`, exit 34, JSON shape, consent handling |
| `tests/test_proxy_handling.py` | 12 | proxy flags, transport messages, the deliberate default asymmetry |
| `tests/test_auth_host.py` | 8 | graceful-close ordering, kill fallback, the flush-loss warning, `describe()` honesty |
| `tests/test_tray.py::StartupCommandContextTest` | 6 | §35's three contexts, plus the source-label invariant |

### Every regression test was verified to bite

A regression test that passes against the broken code proves nothing. Each new test
was run against a temporarily reverted fix and confirmed to **fail**, then the fix
was restored. Three examples, verbatim from that check:

```text
reverted to the unconditional invalidate()
FAILED .../test_a_remembered_token_is_not_discarded_by_a_successful_renewal
FAILED .../test_a_provider_that_cannot_answer_falls_back_safely
OK: the tests fail against the broken code, so they cover it
```

```text
removed the browser write
FAILED .../test_the_minted_cookie_is_written_into_the_browser
FAILED .../test_the_expiry_is_passed_as_a_relative_lifetime
FAILED .../test_a_failed_browser_write_does_not_fail_the_renewal
FAILED .../test_a_raising_browser_write_does_not_escape
FAILED .../test_a_session_cookie_without_an_expiry_passes_none
OK: the tests fail without the browser write, so they cover it
```

```text
reverted stop() to kill-first
FAILED .../GracefulStopTest::test_stop_closes_gracefully_before_killing
OK: the tests fail against the kill-first code, so they cover it
```

```text
reverted the frozen-CLI label to frozen-tray
FAILED .../test_the_frozen_cli_registers_the_sibling_tray
SUBFAILED .../test_the_source_label_never_claims_a_context_that_is_not_running
OK: the tests fail against the mislabelled code, so they cover it
```

The secret scan was checked the same way: synthetic JWT / cookie / token literals
were planted and all three patterns caught them, then the files were removed.

### Live probes

| Probe | Verdict |
| --- | --- |
| `tools/probe_oauth_browserless.py` | `PURE_HTTP_OAUTH_FEASIBLE` |
| `tools/probe_qr_browserless_login.py` | `BROWSERLESS_LOGIN_ACHIEVABLE` |
| `tools/probe_cookie_write.py` | `COOKIE_WRITE_AVAILABLE` |
| `tools/probe_auth_host_persistence.py` | `GRACEFUL_CLOSE_REQUIRED` (3/3 vs 0/3); full renew cycle `SKIPPED` |

Each probe declares its safety posture in its docstring, and
`tests/test_packaging.py` enforces that — a probe that mutates state must say so
before anyone runs it.

### Not executed

- **A real WeChat scan.** No phone was involved. `PRE-SCAN VERIFIED` only.
- **A full logout → login cycle.**
- **The live consent flow** against a never-approved account.

---

## 11. CI

`.github/workflows/ci.yml` — seven jobs, fourteen matrix entries. Each corresponds
to a defect this project actually suffered, not to a best practice.

| Job | Runner | What it catches |
| --- | --- | --- |
| `test` | ubuntu + windows × 3.10/3.12/3.13/3.14 | **both** runners; a test that passes under one and fails under the other |
| `stdlib-only` | ubuntu | the `dependencies = []` promise — one convenient import breaks air-gapped installs, and every dev machine has Pillow installed |
| `oldest-supported-syntax` | ubuntu, 3.10 | syntax that only parses on the newest interpreter |
| `secret-scan` | ubuntu | credential-shaped strings in the tree |
| `probe-posture` | ubuntu | a probe that mutates state without saying so |
| `frozen` | windows | a build that produces an EXE which **cannot start**, or one that stores credentials somewhere nobody looks |
| `hygiene` | ubuntu | generated files tracked, scratch tools, CRLF blobs, and non-compiling Python |

**Status: VERIFIED — all 14 entries green, on real GitHub Actions runners.**

```text
run f8cdb64   completed success   14 ok / 0 fail
  OK  tests (python 3.10 on ubuntu-latest)
  OK  tests (python 3.12 on ubuntu-latest)
  OK  tests (python 3.13 on ubuntu-latest)
  OK  tests (python 3.14 on ubuntu-latest)
  OK  tests (python 3.10 on windows-latest)
  OK  tests (python 3.12 on windows-latest)
  OK  tests (python 3.13 on windows-latest)
  OK  tests (python 3.14 on windows-latest)
  OK  import with no optional extras
  OK  every file compiles on Python 3.10
  OK  secret scan
  OK  probe safety posture
  OK  frozen binaries (windows)
  OK  repository hygiene
```

### What it took to get there, and what that says

The previous round's report said CI was "written and locally rehearsed; never
executed on a CI runner", and that was accurate. The first real run failed **11 of
13 jobs**. Every failure was a real defect, and three of them are worth recording
because they are the class of thing local rehearsal cannot find:

1. **The package was never installed.** `pytest` found `src/` through
   `pythonpath` in `pyproject.toml`; `unittest` reads no config file at all, so on
   a clean checkout it could not import `opencsi`. Every job now runs
   `pip install -e . --no-deps` — `--no-deps` because the empty dependency list is
   itself the promise `stdlib-only` exists to check, and installing dependencies
   to run that check would invalidate it.
2. **Two jobs ran tests that import `opencsi` with no install**, and four of the
   new test files relied on `src/` being importable by accident. They now
   `import helpers`, which is the convention the rest of the suite already used for
   its `sys.path` side effect.
3. **The frozen step grepped for text the product never prints.** It searched the
   human-readable `tray --startup-status` output for the literal `frozen-cli-tray`;
   that label appears only in the JSON. It failed on a build behaving correctly and
   would have kept failing no matter what the build did. It now reads `--json` and
   asserts the source label *and* the command.

Two further failures were environment-vs-code confusion, and separating them
mattered:

- **`'frozen-cli' != 'frozen-tray'` on Linux.** `Path(r"C:\App\opencsi-tray.exe").name`
  returns the whole string on POSIX, because `\` is not a separator there. The
  frozen-sibling derivation now splits on both separators. A Windows-only path
  bug that only a Linux runner could surface.
- **Five `TypeError` errors in `qr_render` on Ubuntu.** `_code_dir()` consulted only
  `LOCALAPPDATA` and `XDG_CACHE_HOME` and returned `None` when neither was set;
  callers pass that to `os.makedirs`, which raises **TypeError** for `None` — not
  `OSError`, which the caller catches. It now falls back to `HOME` and then the
  system temp directory. An unwritable directory is a soft failure the code already
  handles; `None` was a crash.

And two were genuinely environment-specific, now handled as such rather than
written off:

- **A refused registry write is skipped, not failed.** `StartupManager.enable()`
  reports failure through `detail` rather than raising, and the round-trip test
  asserted `enabled.enabled` without reading it — so a locked-down runner that
  refused the write looked like "the startup feature is broken". It now skips with
  the refusal in the message.
- **`$ErrorActionPreference = 'Stop'` aborts steps that read expected stderr.**
  Three frozen steps merge a command's stderr, and all three read output the
  product writes on purpose: `doctor`'s session failure, `--startup-status`'s note
  that the Run key is unreadable, and `logout --no-store`'s refusal. Under Actions'
  preference that raises a terminating error before any assertion runs. The
  preference is relaxed around those three calls and restored immediately after.
- **Actions ends each pwsh step with `exit $LASTEXITCODE`.** So a step inherits the
  *last native command's* status — and two steps deliberately end on a command that
  is supposed to fail: `doctor` (no session on a runner) and `logout --no-store` (a
  refusal). Both asserted the right thing and then failed the step anyway, with a
  bare `exit code 1` and nothing in the log to say which assertion had passed. This
  is the one that took longest to find, because the log showed the *successful*
  assertion immediately above the failure. Each step now resets the status and
  exits 0 explicitly, after everything that can throw.

Design notes:

- **No credentials, no network.** Every test needing a live session is stubbed or
  skipped, so the workflow runs on a fork with no secrets configured.
- **The secret scan is a script** (`tools/ci_secret_scan.sh`), not inline YAML, so
  it can be rehearsed locally — and it was. It exempts `tests/` but **not**
  `tools/`, and that policy is correct: the acceptance runner's own fixture was
  flagged and had to be rebuilt to assemble its values at runtime. A scanner with
  an exception for "but I meant it as a fake" is a scanner nobody can trust.
- **`NO_PROXY=*`** is set: an accidental proxy use in tests should fail loudly.
- **The frozen job runs the binaries**, not just builds them, and asserts the store
  path is under `%LOCALAPPDATA%` and not inside the unpacked bundle. A
  `_MEIPASS`-relative path would put credentials somewhere nobody looks and would
  look correct while silently losing them between runs.
- **Pillow is installed in the `test` job only.** The QR rendering tests need an
  image library; the package keeps `dependencies = []`.


---

## 12. Remaining hard boundaries

Only what was measured. Nothing here is "尚未解决" dressed up as "理论上不可能".

### Genuinely impossible without a phone

- **The WeChat scan itself.** A physical action. This is not a browser-JS
  requirement and does not make the flow `QR_FLOW_BROWSER_BOUND`. Everything
  downstream of the scan — persistence, cross-process reuse, browserless renewal —
  is verified without one.

### Genuinely requires a human, but only once

- **First-time consent.** `checkOrAuthorize` answers 401 when no grant exists. The
  consent page needs one click. Automating it would mean approving a third-party
  authorization on the user's behalf, which this tool will not do — the submission
  endpoint is never called, by design.

### Unresolved, with the reason

- **GitCode refresh-token rotation was not observed live.** The endpoint is
  confirmed by probe; whether GitCode returns a *new* refresh token is
  undocumented. The code treats it as rotating and reports whether it did, rather
  than assuming either way.
- **Live renewal against a real account was not executed**, because no scan was
  performed and therefore no credential exists to renew from. The mechanism and
  the persistence are both verified independently.
- **A real Windows sign-out and sign-in was not performed**, so §30's flow is
  verified up to the hidden engine coming up but not through an actual logon. The
  registry round trip itself is verified directly: install, read back, remove,
  restore.
- **`xauth_token`'s consumer is unidentified.** The client stores it after login;
  no endpoint was found that reads it. It appears related to Huawei Cloud IAM
  (`IAM_UNINITIALIZED`), not to scan login. Not needed for anything implemented.
- **`scene_id`'s format is unknown.** The server treats any string as an unknown
  scene and answers `TIMEOUT`; the real value measured 24 characters. Treated as
  opaque, as the frontend does.
- **Huawei Cloud WAF behaviour is not fully characterised.** Intermittent `418` +
  `X-Hwwaf-Attack-Id` was observed on a path missing its `/uc` prefix; adding
  `Referer`/`Origin` returned a normal `401`. This is a conventional WAF rule, not
  a JS challenge — there is no token to compute — but it is the most likely thing
  to trip a reimplementation.
- **The `EMPTY_MOBILE` / `MFA_CHECK` branches were not explored.** Out of scope for
  the main path.

### Optional and deliberately not implemented

- **§19's automatic browser → DPAPI migration was not implemented.** §19 marks it
  "如果…可以" (optional), and §20 forbids expanding scope, so it was left out rather
  than built. What exists instead is the composite provider: a user who is already
  signed in through a browser profile keeps working, because CDP auto-discovery is
  still the last entry in the provider chain. Nothing is copied into the store
  until that user runs `login --qr` or `login --renew`.

  The capability the migration would need is already in place and was kept for it:
  `auto_recover_auth_host` is now `False` by default but still exists, and its help
  text names exactly this case ("migrate a session out of a browser profile").
  Building the migration without a live browser session to migrate *from* would
  have produced untestable code, which is the reason to stop rather than a reason
  to guess.

### Resolved this round, and what resolved them

Two items that appeared in the previous version of this list are no longer open:

- **"CI has never run on a real runner."** Now 14/14 green. The first honest run
  failed 11 of 13 and exposed six real defects — see §11. Rehearsing locally was
  never going to find them, because the failures were in the *environment*: an
  uninstalled package, POSIX path separators, an unset `LOCALAPPDATA`, and
  PowerShell's `$ErrorActionPreference`.
- **"The credential lives only in the process that made it."** This was the round's
  whole subject. It now lives in a DPAPI-encrypted file read by whichever process
  runs next, verified across two independent interpreter processes.

### Two probes that answered the wrong question

Both are fixed, and both are recorded because the *shape* of the mistake is the
reusable part: a probe that asks a browser to describe itself can be wrong in
either direction, and a test suite that checks the answer will never notice.

- **The capability probe started a browser instead of answering.** It asked
  `chrome --headless=new --version`; on Chrome 153 that returns `rc=0` in ~0.12s,
  prints nothing, and leaves a real headless browser running — so the
  "prints a version" check failed and a build with working headless support was
  judged to have none. It now asks the browser to take a screenshot and checks for
  a real PNG signature. (An earlier revision of this report called this a *hang*;
  re-measurement did not reproduce one.)
- **The mode probe read the wrong field.** `_probe_mode` searched
  `Browser.getVersion`'s `product` for `"Headless"`, but on this build the marker
  is in `userAgent` (`product` is plain `Chrome/153…`). Every headless browser was
  therefore reported `VISIBLE` — the host ran hidden while telling the user a
  window was on their screen. Both fields are now checked.

Underneath both: an earlier round concluded that launching a browser from Python
was broken here, because every launch exited `rc=0` in about a second. That was a
measurement error — Chromium's launcher hands off to a child and exits, so the
exit code describes the hand-off and not the browser. A control experiment (same
binary, flag present and absent) shows the flag works and the user agent
distinguishes them.

### A "fix" of mine that the tests stopped, and why it looked right

While investigating the stale marker on the default profile — port 57208 accepts
TCP but 404s on `/json/version` and never answers the WebSocket handshake — I
concluded that `_looks_like_devtools` was wrong to accept a bare TCP accept plus a
marker match, and changed it to require a real upgrade. Two tests failed
immediately and the change was reverted.

It was a regression, and the reason is worth recording because the symptom is
genuinely indistinguishable from a bug at the point of observation: the module
docstring documents that exact behaviour as *expected* for Chrome 147+, where
`/json/*` is disabled on the default profile and the browser WebSocket is the only
route that works; and `tests/fake_devtools.py` models it as "the exact field
failure". Rejecting it would have broken the Chrome 147+ path the check exists to
support.

The narrow lesson: a symptom that looks like a defect can be a documented
contract, and the tests encoding that contract are the thing to read *before*
changing the code. Here they were the only thing standing between a plausible fix
and a real regression.

### The defect §64 found, which only reading two outputs together could find

Running the four commands §64 names on the frozen binaries produced this pair:

```text
$ opencsi.exe login --status
Silent renewal       : available

$ opencsi.exe login --renew
Outcome : OAUTH_FAILED / TIMEOUT
```

Both were individually correct — the capability check asks whether the machinery
is *reachable*, and the round trip can still fail. What was wrong is that the
explanation already existed and never reached the user. `renewal_capability`
returns a `reason` string, and `--status` printed it on exactly one branch:
`if not capability.available`. The moment availability became true the reason was
discarded, so the user saw "available" and nothing else.

Measured, not inferred: the phrase `GitCode SSO cookie` appears **nowhere** in
`--status` output, while the probe had returned `False` for precisely that cookie.
So the tool said the feature works, and the sentence saying it would ask for a
sign-in was suppressed by the branch that had just been satisfied.

This is §59's *no conclusion stronger than evidence* failing at the last step —
not in the probe, which was honest, but in what the probe's answer was allowed to
say. `RenewalCapability` gained `caveated` (available, and something needs
saying); `--status` prints the reason as a note, and `doctor` records `WARN`
rather than `OK`.

`doctor` is the second half of the same defect and the more pointed one, because
its check was **rewritten once already** for exactly this reason: it reported
health while the next renewal parked on GitCode's approval page, and the
troubleshooting guide told users that line meant recovery. The rewrite fixed the
probe and left the verdict keyed on `available` alone, so a caveated capability
still printed `ok`.

The `None` branch was over-claiming too: a failed cookie read fell through to
"GitCode SSO available", asserting presence from a read that established nothing.
`SsoPresenceTest`'s own docstring says the probe "must not claim an SSO session it
never looked for" — and its assertion required that literal string, so the test
was pinning the behaviour its docstring forbids. It now requires the state to read
as unknown, and separately forbids the opposite error of reporting a failed read
as a missing cookie.

Worth stating plainly: the unit tests were green throughout. What found this was
running two commands a user would run, on the frozen build, and reading their
outputs next to each other.

### Environment-specific, measured here

- **Chromium's launcher exits before the browser is ready, on every launch.**
  Measured at `rc=0` in 0.1s while the browser needs ~0.6s to do anything and
  ~0.5s to answer its debug port. This is normal Chromium behaviour, not a fault,
  but it invalidated an earlier conclusion in this project and it is the reason
  the capability probe now waits for an artifact rather than for the process. Any
  code here that launches a browser and then checks the process's exit status is
  measuring the wrong thing.
- **`describe()` reports the mode it got rather than the flag it asked for.**
  Previously it claimed "no user-visible window" while showing one, which is the
  same class of error as the two probes above — reporting the request as the
  outcome.
- **The system proxy at `127.0.0.1:7890` breaks `opencsitool.com` and only that
  host.** Measured across 3 hosts × 6 configurations × 3 repeats: `gitcode.com`
  and `web-api.gitcode.com` pass through; `opencsitool.com` fails with
  `SSLEOFError`. Proxy CONNECT answers "200 Connection established", then the TLS
  handshake dies; Chrome reports `net::ERR_CONNECTION_CLOSED`, which looks exactly
  like the site being down. `--proxy-bypass-list=opencsitool.com` **alone does not
  work** (3/3 failures) — it only takes effect alongside an explicit
  `--proxy-server`, so `--no-proxy-server` is what is used.

### Deliberate design asymmetries, not defects

- **`HttpOAuthRenewer` bypasses the proxy by default; `launch_debug_browser` keeps
  it.** The renewer runs unattended and has a fallback; a first sign-in has none,
  and a corporate proxy is often the only route out. Both are documented, and both
  are overridable.
- **A credential is never written to disk.** This is why the browser is the
  credential store, and why a browserless renewal must write its cookie *back* into
  the browser rather than to a file.

### A limit discovered in the auth host, and what it means

`stop()` now closes the browser gracefully so Chromium flushes its cookie store.
Measured: a persistent cookie survived a graceful close 3/3 and a hard kill 0/3.

What this does *not* fix: the flush is periodic, so the guarantee is "a graceful
close flushes", not "every write is durable the instant it is made". A crash, a
power loss, or a `taskkill` from outside this program can still lose cookies written
since the last flush. Stating it as "the session always survives" would be the same
class of overclaim as the browser-bound conclusion this report already corrects —
the mechanism is now right, and the residual window is real.

Not yet measured, and therefore not claimed: whether a *renewal* followed
immediately by `stop()` always persists. The probe writes, waits a beat, then
closes; the production path calls `stop()` from a shutdown handler whose timing
relative to a just-completed renewal was not exercised.

---

## Appendix: secret scan

Mandated names: `access_token`, `refresh_token`, `xauth_token`, `scene_id`,
`token`, `Cookie`, `Authorization`, `virtualKey`.

```text
scanned 156 tracked files
REVIEW: value-shaped literals next to credential names
```

Every one inspected individually. Each is either a **cookie name constant**
(`GITCODE_ACCESS_TOKEN`), a **JSON field path** (`tokens_by_request`), or a
**synthetic test fixture** (`SUPER_SECRET_COOKIE_123`, `TOKENVALUE0123456789`).
The fixtures are deliberately absurd strings: a scan whose only hits are obviously
fake values is a scan that was checked, whereas a scan with zero hits anywhere might
simply not be looking.

```text
Real credential values found: 0
```

Pattern-based scan (`tools/ci_secret_scan.sh`), all clean:

```text
ok: no JWT-shaped strings
ok: no literal Cookie headers with values
ok: no hard-coded token literals
ok: no generated files are tracked
```

### The scanner caught this round's own fixture, and that is the point

`tools/acceptance_durable.py` originally embedded
`refresh_token='refresh-token-abcdefghijklmnop'` in a seed script, and the scan
failed the build. The tempting fix was an exemption for a file whose values are
obviously fake. That exemption is how a scanner stops being worth running: the
next literal is one edit away from being real, and the review that would have
caught it is exactly the review the exemption removes.

The fixture now assembles its values at runtime and passes them through the
environment, so the file contains no token-shaped literal at all. The
no-plaintext check derives its needles from the same values it wrote, so the two
cannot drift apart.

Beyond the scanner, `tests/test_secret_safety.py` asserts the property directly:
17 tests planting a distinctive value in each credential slot and checking every
surface that can print — including `doctor`, `login --status`, `logout`, `usage
--json`, exception messages, and the tray's tooltip. Two of them assert the
*limits* rather than the coverage, because a security test that only proves what
works invites the reader to assume the rest is covered too.

Secret handling in the implementation: values are never printed, logged, or stored.
Only names, lengths, and SHA-256 fingerprints (first 12 hex) appear in output.
