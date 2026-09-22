# OpenCSIToolMonitor — Final Auth Closure Report

**Scope**: the eight-phase authentication/startup closure defined in `docs/goal.md`.
**Machine**: Windows 11 (10.0.26200, AMD64), Python 3.14.6, Chrome 153.0.8010.53.
**Date of the live measurements below**: this session.

Every claim in this report is either (a) reproduced by a command named here, or
(b) explicitly marked as **not executed**. There is no third category.

---

## 1. Verdict

Seven independent verdicts. No single overall PASS — several items are genuinely
incomplete, and collapsing them would hide exactly what this report exists to say.

| # | Item | Verdict | Basis |
| --- | --- | --- | --- |
| 1 | **QR GitCode auth** | `PROTOCOL_VERIFIED` / **scan not executed** | Protocol reproduced end-to-end over plain HTTP. No real WeChat scan was performed in this session. |
| 2 | **QR → openCsiTool auth** | `BROWSERLESS_LOGIN_ACHIEVABLE` | Credential of exactly the shape a scan returns established and verified a session. **No browser engine at any point.** |
| 3 | **Silent renewal** | `WORKING` — verified live, both binaries | `RENEWED`, `Server accepted it : yes`, exit 0. Persists across processes. |
| 4 | **Browserless OAuth** | `PURE_HTTP_OAUTH_FEASIBLE` | Independently reproduced three times (twice by me, once by a separate investigation). |
| 5 | **Hidden auth runtime** | `IMPLEMENTED` / **`VISIBLE_FALLBACK` on this machine** | Chrome 153 here rejects `--headless=new`; the host detects that and says so truthfully. |
| 6 | **Windows tray** | `WORKING` | `--once` returns real data; `--check` builds a 7-item menu; QR login wired. |
| 7 | **Startup** | `WORKING` — full round-trip verified | Frozen binary resolves its own sibling. Registry install/remove verified and reverted. |
| 8 | **CI** | `WRITTEN` / **never run on a CI runner** | Workflow is complete and every step was rehearsed locally; no GitHub runner executed it. |

### What is *not* claimed

- **No real QR scan was performed.** Per §64, this can only be reported as
  `PRE-SCAN VERIFIED`. A phone is required, and no phone was involved.
- **A full logout → login cycle was not executed.** Per §67 this is stated plainly
  rather than implied by adjacent successes.
- **CI has never run.** Locally rehearsed, but `ubuntu-latest` and `windows-latest`
  were never actually provisioned.
- **The consent path was not exercised against a never-approved account.** The
  `CONSENT_REQUIRED` branch is unit-tested and its endpoint is deliberately never
  called; the live first-authorization flow is untested.

---

## 2. Before / After

```text
starting HEAD   0637051  docs: make §12 usable, since that is the section a user actually reads
ending HEAD     01ad527  report: final auth closure, with the incomplete items named as incomplete
```

Thirteen commits, each a real work item:

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
```

```text
38 files changed, 9879 insertions(+), 565 deletions(-)   (excluding this report and docs/goal.md)
```

Code and tests only — this report and `docs/goal.md` account for the remaining
~3100 lines of the 40-file total.

| Metric | Before | After |
| --- | --- | --- |
| pytest | 750 passed, 1 skipped, 125 subtests | **824 passed, 1 skipped, 162 subtests** |
| unittest | Ran 751, OK (skipped=1) | **Ran 825, OK (skipped=1)** |

The behavioural change, stated as the user experiences it:

```text
BEFORE
  QR  -> GitCode login -> stop -> "now go use a browser"
  renewal -> only works while a browser is running and readable
  tray startup -> registry pointed at a binary that did not exist

AFTER
  QR  -> GitCode login -> openCsiTool session, established and verified, no browser
  renewal -> plain HTTP; result written back into the browser so the next process sees it
  tray startup -> registry points at the tray binary, derived from the running build
```

---

## 3. QR semantic bug

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
       +-- no token?
       |     -> state BROWSER_UNAVAILABLE (its own label, not "sign in")
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

`StartupStatus` now records where the command came from, and the three shapes are
distinguished explicitly:

```text
SOURCE_FROZEN_TRAY        running as opencsi-tray.exe -> its own sibling
SOURCE_FROZEN_CLI         running as opencsi.exe     -> the tray next to it
SOURCE_SOURCE_INSTALL     running from source        -> the console script
```

`matches_this_build` reports whether the registered command is the binary the user
is currently running. `opencsi tray --startup-status` prints `derived from: <source>`
and warns when it is not.

### The three commands, verified live

```text
$ opencsi.exe tray --startup-status
start at sign-in: disabled
would run: D:\workspace\OpenCSIToolMonitor\dist\opencsi-tray.exe
derived from: frozen-tray

$ opencsi.exe tray --install-startup
The tray will start when you sign in.
command: D:\workspace\OpenCSIToolMonitor\dist\opencsi-tray.exe
derived from: frozen-tray

$ Get-ItemProperty HKCU:\...\Run -Name OpenCSIToolMonitor
D:\workspace\OpenCSIToolMonitor\dist\opencsi-tray.exe      <-- the tray, not the CLI
```

Removal was then verified and the registry returned to its prior (absent) state, so
the machine was left as found.

---

## 10. Tests

| Surface | Command | Result |
| --- | --- | --- |
| pytest | `python -m pytest` | **824 passed, 1 skipped, 162 subtests passed** |
| unittest | `python -m unittest discover -s tests -t tests` | **Ran 825, OK (skipped=1)** |
| Windows (live) | the seven commands in §1 | all as recorded |
| packaging | `python tools/build_exe.py` | both binaries built, **and executed** |
| live probes | `tools/probe_*.py` | verdicts recorded below |

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

### New test files

| File | Tests | Covers |
| --- | --- | --- |
| `tests/test_http_oauth.py` | 36 (+7 subtests) | the three-request flow, refusals, secret safety, fallback chain, **browser persistence** |
| `tests/test_qr_login_semantics.py` | 22 (+8 subtests) | `LoginStage`, exit 34, JSON shape, consent handling |
| `tests/test_proxy_handling.py` | 12 | proxy flags, transport messages, the deliberate default asymmetry |

### Every regression test was verified to bite

A regression test that passes against the broken code proves nothing. Each new test
was run against a temporarily reverted fix and confirmed to **fail**, then the fix
was restored. Two examples, verbatim from that check:

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

The secret scan was checked the same way: synthetic JWT / cookie / token literals
were planted and all three patterns caught them, then the files were removed.

### Live probes

| Probe | Verdict |
| --- | --- |
| `tools/probe_oauth_browserless.py` | `PURE_HTTP_OAUTH_FEASIBLE` |
| `tools/probe_qr_browserless_login.py` | `BROWSERLESS_LOGIN_ACHIEVABLE` |
| `tools/probe_cookie_write.py` | `COOKIE_WRITE_AVAILABLE` |

Each probe declares its safety posture in its docstring, and
`tests/test_packaging.py` enforces that — a probe that mutates state must say so
before anyone runs it.

### Not executed

- **A real WeChat scan.** No phone was involved. `PRE-SCAN VERIFIED` only.
- **A full logout → login cycle.**
- **The live consent flow** against a never-approved account.

---

## 11. CI

`.github/workflows/ci.yml` — six jobs. Each corresponds to a defect this project
actually suffered, not to a best practice.

| Job | Runner | What it catches |
| --- | --- | --- |
| `test` | ubuntu + windows × 3.10/3.12/3.13/3.14 | **both** runners; a test that passes under one and fails under the other |
| `stdlib-only` | ubuntu | the `dependencies = []` promise — one convenient import breaks air-gapped installs, and every dev machine has Pillow installed |
| `secret-scan` | ubuntu | credential-shaped strings in the tree |
| `probe-posture` | ubuntu | a probe that mutates state without saying so |
| `frozen` | windows | a build that produces an EXE which **cannot start** — the class of bug found three times here |
| `hygiene` | ubuntu | generated files tracked; every Python file compiles |

Design notes:

- **No credentials, no network.** Every test needing a live session is stubbed or
  skipped, so the workflow runs on a fork with no secrets configured.
- **The secret scan is a script** (`tools/ci_secret_scan.sh`), not inline YAML, so
  it can be rehearsed locally — and it was.
- **`NO_PROXY=*`** is set: an accidental proxy use in tests should fail loudly.
- **The frozen job runs the binaries**, not just builds them.

**Status: written and locally rehearsed; never executed on a CI runner.** No
`ubuntu-latest` or `windows-latest` machine was provisioned. Reporting this
workflow as "passing" would be exactly the overclaim §74 forbids.

---

## 12. Remaining hard boundaries

Only what was measured. Nothing here is "尚未解决" dressed up as "理论上不可能".

### Genuinely impossible without a phone

- **The WeChat scan itself.** A physical action. This is not a browser-JS
  requirement and does not make the flow `QR_FLOW_BROWSER_BOUND`.

### Genuinely requires a human, but only once

- **First-time consent.** `checkOrAuthorize` answers 401 when no grant exists. The
  consent page needs one click. Automating it would mean approving a third-party
  authorization on the user's behalf, which this tool will not do — the submission
  endpoint is never called, by design.

### Unresolved, with the reason

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
- **CI has never run on a real runner.**

### Environment-specific, measured here

- **Chrome 153 on this machine rejects `--headless=new`.** The auth host detects
  this and falls back to a visible window, reporting `headless=False` truthfully.
  This is a property of this Chrome build, not of the design. The host's
  `describe()` now reports the mode it *got*, not the flag it *asked for* —
  previously it claimed "no user-visible window" while showing one.
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

---

## Appendix: secret scan

Mandated names: `access_token`, `refresh_token`, `xauth_token`, `scene_id`,
`token`, `Cookie`, `Authorization`, `virtualKey`.

```text
scanned 124 tracked files
REVIEW: 19 value-shaped literals next to credential names
```

All 19 inspected individually. Every one is either a **cookie name constant**
(`GITCODE_ACCESS_TOKEN`), a **JSON field path** (`tokens_by_request`), or a
**synthetic test fixture** (`SUPER_SECRET_COOKIE_123`, `TOKENVALUE0123456789`).

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

Secret handling in the implementation: values are never printed, logged, or stored.
Only names, lengths, and SHA-256 fingerprints (first 12 hex) appear in output.
