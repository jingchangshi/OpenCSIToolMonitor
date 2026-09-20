# openCsi Standalone Tool Implementation Report

This report covers the second phase of work: turning the API investigation into a
standalone, read-only client that a normal user can run on Windows, Linux or macOS
without any AI agent, browser-automation runtime, or DSH involvement.

The governing principle for this phase:

```
DSH develops it.          DSH does not run it.
Browser authenticates it. Browser does not query it.
OpenCsiToolClient queries it.  CLI exposes it.
```

---

## 1. Verdict

**Complete and independently runnable.** The deliverable is a zero-dependency,
pure-standard-library Python package plus a `opencsi` command line, verified by
389 offline tests and a 42-check replay of the original investigation baselines.

| Claim | Status | Evidence |
| --- | --- | --- |
| Runs without DSH | **Yes** | No import of, reference to, or runtime dependency on DSH anywhere in `src/`. Verified by an AST scan of every module against `sys.stdlib_module_names`: **zero non-stdlib imports**. |
| Runs without an AI agent or LLM | **Yes** | No LLM/agent SDK is imported or invoked. The tool is deterministic I/O plus formatting. |
| Runs without Browser Use / Playwright / Selenium | **Yes** | The browser is contacted only through the raw Chrome DevTools Protocol over a hand-rolled WebSocket client (`src/opencsi/ws.py`), using `socket` + `struct`. |
| Zero third-party runtime dependencies | **Yes** | `dependencies = []` in `pyproject.toml`; the AST scan above confirms it structurally, not just declaratively. |
| Read-only | **Yes** | `HttpTransport` exposes no `post`/`put`/`patch`/`delete`/`request` attribute at all; asserted by test. Every call is a GET. |
| Credentials never leave the browser's control | **Yes** | `Authorization` is never constructed; asserted against the headers a real HTTP server actually received. |
| Verifiable offline | **Yes** | 389 tests, no network, run under `unittest` and under `pytest`. Also clean under `-W error::ResourceWarning`. |
| Installable | **Yes** | `uv pip install -e .` succeeds; `opencsi --version` and `opencsi --help` were run from `C:\`, outside the repository, so nothing depends on the working directory or `PYTHONPATH`. Plain `pip install -e .` fails here only because this machine has no `setuptools` and no package index — see §10. |
| Verifiable end to end | **Yes** | `demo_e2e.py` runs the real CLI over real sockets against replayed fixtures: 10/10 commands exit 0. |
| Live-site authenticated query | **Blocked by environment** | See §7. The session had expired; I did not sign in on the user's behalf. The unauthenticated HTTP path *was* verified live, and the CDP provider was validated against real Chrome. |

The one thing this report does **not** claim is a successful authenticated fetch
against the live site. That is an environment limitation, described honestly in
§7, not a gap in the implementation.

---

## 2. Final architecture

```
                            ┌───────────────────────────┐
                            │  user's shell / script    │
                            └─────────────┬─────────────┘
                                          │  opencsi status --json
                                          ▼
┌────────────────────────────────────────────────────────────────────────┐
│ src/opencsi/cli/            argparse front end, one module per command  │
│   app.py  context.py  status  tools  usage  trend  prices  logs         │
│   doctor  login  contract                                               │
│                                                                         │
│   • parses argv, owns exit codes, renders text or JSON                  │
│   • knows nothing about HTTP, cookies or the wire format                │
└───────────────────────────────┬────────────────────────────────────────┘
                                │  CredentialProvider  (the only contract)
                                ▼
┌────────────────────────────────────────────────────────────────────────┐
│ src/opencsi/client.py       OpenCsiToolClient                           │
│                                                                         │
│   login_or_restore_session()   get_my_tools()    get_summary()          │
│   get_model_prices()           estimate_cost()   get_call_logs()        │
│   get_key_budget()             contract_check()                         │
│                                                                         │
│   • builds URLs, enforces the {code,data,message} envelope              │
│   • maps HTTP status -> typed exception, with exactly ONE 401 retry     │
│   • caches responses in-process (TTL)                                   │
└──────────┬──────────────────────────────────────────────┬──────────────┘
           │ HttpTransport (urllib)                       │ CredentialProvider
           ▼                                              ▼
┌────────────────────────────┐              ┌──────────────────────────────┐
│ transport.py               │              │ auth/                        │
│   urllib.request           │              │   cdp.py     CdpCookieProvider│
│   • GET only               │              │   manual.py  ManualCookie…   │
│   • proxy policy           │              │   base.py    the Protocol    │
│   • retry 502/503/504      │              │                              │
│   • 401 -> one refresh     │              │  "what cookie should I send?"│
└────────────┬───────────────┘              └───────────┬──────────────────┘
             │                                          │
             │                                ┌─────────┴──────────┐
             │                                ▼                    ▼
             │                    ┌────────────────────┐  ┌──────────────────┐
             │                    │ ws.py + cdp.py     │  │ getpass() stdin  │
             │                    │ raw CDP over a     │  │ or $OPENCSI_TOKEN│
             │                    │ hand-rolled        │  │ (never argv)     │
             │                    │ WebSocket          │  └──────────────────┘
             │                    └─────────┬──────────┘
             │                              │  HTTP /json + WS upgrade
             │                              ▼
             │                    ┌────────────────────────────┐
             │                    │ user's own Chrome / Edge   │
             │                    │ already signed in          │
             │                    └────────────────────────────┘
             ▼
   https://opencsitool.com/opencsitool/rest/v1/...
```

### Why the seam sits exactly there

`OpenCsiToolClient` depends on **one** method — `CredentialProvider.get_token()`.
It never learns whether the cookie came from a browser, an environment variable,
or a prompt. That is what makes the client independently testable and what keeps
browser automation from becoming an architectural dependency.

The two layers have deliberately different jobs, and the integration tests pin
the boundary down because the split is not self-evident:

| Layer | Responsibility |
| --- | --- |
| `HttpTransport` | Returns a `Response` for **any** HTTP status. Raises only for transport-level trouble (connection refused, unparseable body). Retries 502/503/504, never 4xx. |
| `OpenCsiToolClient` | Maps a status onto a documented exception and enforces the business envelope. |

### Module inventory (28 modules, 5,345 lines)

```
src/opencsi/
  __init__.py  __main__.py  version.py
  errors.py       exit codes + typed exceptions
  redaction.py    structural secret masking for any output path
  models.py       Identity, Grant, ModelPrice, TrendPoint, TokenBudget, …
  aggregation.py  sums, shares, adoption rate, tool grouping
  formatting.py   CJK-aware tables, 亿/万 number rule, sections
  ws.py           minimal RFC 6455 client (socket + struct)
  transport.py    urllib-based GET transport, proxy policy, retries
  cache.py        in-process TTL cache
  client.py       OpenCsiToolClient
  auth/base.py    CredentialProvider Protocol + CredentialStatus
  auth/cdp.py     CdpCookieProvider (raw CDP)
  auth/manual.py  ManualCookieProvider
  cli/…           11 command modules + app + context
```

---

## 3. Implemented features

### Commands

| Command | What it does | Requests |
| --- | --- | --- |
| `status` | Session, identity, and the headline numbers; `--verbose` adds internal IDs, `--no-summary` skips the second request | 1–2 |
| `tools` | Lists granted AI tool accounts with tokens; `--type`, `--search`, `--active`, `--show-key-mask` | 1 (queue status) |
| `usage` | Personal overview: tokens, requests, PRs, lines, adoption rate, per-tool split, data freshness | 1 (queue status) |
| `trend` | Token trend by model or date, with prompt/completion split; `--days`, `--from`, `--to` | 1 (queue status) |
| `prices` | Model/tool price list, enabled rows only by default | 1 (`ai/config/cost`) |
| `logs` | Recent LLM gateway call logs; `--from`/`--to` | 1 (`call-logs`) |
| `doctor` | Ordered diagnostic; each API endpoint on its own row | 0–11 |
| `login` | Opens the login page and verifies the resulting session | 1–2 |
| `contract-check` | Verifies the live API still matches the investigated contract | 5 |

Global options: `--json`, `-v`, `--cdp`, `--no-discover`, `--ports`,
`--base-url`, `--timeout`, `--cache-ttl`, `--no-cache`, `--refresh`,
`--no-proxy`.

### Behaviours worth calling out

- **CJK-correct tables.** Column padding uses `unicodedata.east_asian_width`
  (W/F counted as 2). `使用中` is three characters but occupies six columns; a
  naive `len()`-based pad misaligns every row after it.
- **The site's own number rule is reproduced.** `>= 1e8` renders as `X.X亿`,
  `>= 1e4` as `X.X万`, one decimal place — matching the site rather than
  inventing a convention. `3061130999` → `30.6亿`, `21632` → `2.2万`.
- **`--json` is a real interface, not a debug dump.** Every command emits a
  stable object; errors carry a machine-readable `error_code`.
- **Distinct exit codes** so scripts can branch on the cause (§5).
- **Structural redaction** applied to every output path, including exceptions
  and tracebacks — not just the happy path.
- **Secrets are opt-in, and even then only the mask.** `tools` hides the virtual
  key entirely unless `--show-key-mask`, which reveals only the site's own
  `sk-xxxxxxxx****` form. `status` hides `userId` / `accountId` / organization
  UUID unless `--verbose`.
- **Server wall time is reported as stated.** `format_server_time` parses the
  ISO-8601 offset the API sends and keeps it (`2026-09-19T21:40:27+08:00` →
  `2026-09-19 21:40 UTC+8`) instead of converting to a guessed local zone.
- **In-process TTL cache** so one `usage` invocation does not re-fetch;
  `--no-cache` and `--refresh` both defeat it, and `--refresh` additionally
  re-reads the credential where the provider can.
- **Date filtering is honestly scoped.** `startDate`/`endDate` affect **only**
  `tokenTrend`; the summary totals are server-side and ignore them. The tool
  says so rather than implying otherwise. `--days N` means today plus the N-1
  days before it, and combining it with an explicit range is a usage error
  rather than a silent preference.

---

## 4. CLI

```
$ opencsi --help
usage: opencsi [-h] [--version] [--json] [-v] [--cdp URL] [--no-discover]
               [--ports P[,P...]] [--base-url URL] [--timeout SECONDS]
               [--cache-ttl SECONDS] [--no-cache] [--refresh] [--no-proxy]
               COMMAND ...

Read-only command line client for openCsiTool "My Tools". Uses the session
cookie of a browser you are already signed in to.

positional arguments:
  COMMAND
    status             show session and credential status
    tools              list granted AI tool accounts
    usage              show the personal data overview (tokens, PRs, lines)
    trend              show the token trend series
    prices             show the model/tool price list
    logs               show recent LLM gateway call logs
    doctor             diagnose the credential and API chain
    login              open the login page and verify the resulting session
    contract-check     verify the live API still matches the verified contract
```

Real output, produced by `demo_e2e.py` against replayed fixtures over a real
socket (`demo_output.txt`):

```
$ opencsi status
== openCsiTool ==
Session : OK
User     : shijingchang
Employee : 653124
Role     : 普通用户
Data updated : 2026-09-19 21:40 UTC+8

Tokens        : 3,061,130,999
Requests      : 21,632
PRs           : 246
Added lines   : 31,167
AI generated  : 3,150
AI adopted    : 120
Adoption rate : 3.8%

Active tools  : 2
Expired tools : 1
[exit 0]
```

No `userId`, `accountId`, or organization UUID appears — those move under
`--verbose`, together with the credential diagnostics. The point is that the
default output is something a user can paste into a bug report or a screenshot
without exposing identifiers they did not mean to share.

```
$ opencsi tools
  ID  Request No.      Type        Account         Status  Tokens
----  ---------------  ----------  --------------  ------  ------
5593  REQ202608170007  API_BUNDLE  AI编程助手-002  使用中  14.0亿
1954  REQ202604160010  TRAE        AI编程助手-001  使用中   2.6亿
1094  REQ202603090022  API_BUNDLE  AI编程助手-001  已失效  14.0亿

3 account(s): 2 使用中, 1 已失效
[stderr] note: virtual keys are hidden. Re-run with --show-key-mask to see the
         site's own masked form (the full key is never shown).
```

Note the missing `Key` column: virtual keys are hidden unless
`--show-key-mask` is passed, and even then only the site's own mask is shown.

```
$ opencsi trend
== Token trend by model ==
Key                     Display name             Tokens   Prompt  Completion  Share
----------------------  ----------------------  -------  -------  ----------  -----
GLM_5_3_FLASH           GLM-5.3-Flash             9.7亿    9.7亿     583.1万  69.6%
DEEPSEEK_V4_FLASH_0731  DeepSeek-V4-Flash-0731    4.2亿    4.1亿     290.4万  29.9%
QWEN3_8_FLASH           Qwen3.8-Flash           705.3万  690.1万      15.2万   0.5%

42 sample(s) across 30 date(s) and 5 model(s); series total 14.0亿 tokens.
```

(`trend --days 7` is also exercised in the demo. It renders identically there
because the fixture server replays one static response and ignores query
strings; the window itself is asserted directly on the resolved dates in
`test_cli`, where the off-by-one would otherwise be invisible.)

```
$ opencsi usage
== Personal data overview ==
Total tokens         : 30.6亿
Total tokens (exact) : 3,061,130,999
Total requests       : 2.2万
Pull requests        : 246
Added lines          : 3.1万
Generated code lines : 3150
Adopted code lines   : 120
Adoption rate        : 3.8% (120/3,150)
Tool accounts        : 3 (2 使用中 / 1 已失效)

== Tokens by tool ==
Type        Display name  Tokens  Share
----------  ------------  ------  -----
API_BUNDLE  API_BUNDLE    28.0亿  91.5%
TRAE        TRAE           2.6亿   8.5%
```

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | success |
| 2 | usage error (bad arguments) |
| 10 | DevTools endpoint unavailable |
| 11 | endpoint up, no usable page target |
| 12 | not signed in to openCsiTool |
| 13 | session cookie rejected (expired) |
| 20 | permission denied |
| 30 | network error |
| 31 | server error (5xx) |
| 32 | business error (`code != 200` inside HTTP 200) |

These are derived from the **cause**, not from which check failed. §9 explains
why that distinction turned out to matter three separate times.

---

## 5. Authentication

### The credential path

```
1. Locate a DevTools endpoint
     --cdp URL  |  $OPENCSI_CDP_URL  |  scan 127.0.0.1:9222,9223,9224
2. GET  /json/version      -> browser metadata
3. GET  /json/list         -> page targets (must include opencsitool.com)
4. WebSocket upgrade, then CDP:
     Storage.getCookies  /  Network.getCookies
5. Select the HttpOnly cookie named `token` for opencsitool.com
6. Hand it to HttpTransport, which sends it as:
     Cookie: token=<value>
```

`Authorization` is **never** constructed. The site answers
`401 empty Authorization` when the cookie is missing and
`401 Invalid Authorization` when any Bearer header is present — the latter is
surfaced as its own error (`BAD_AUTH_HEADER`), because it can only mean the
caller misconfigured something, since this client never sends one.

### Why the CDP route needs care

Chrome 147+ (this machine runs 153) **blocks `/json/*` on the default
user-data-dir** and **refuses the browser-level WebSocket upgrade** when remote
debugging was toggled on from `chrome://inspect`. The `DevToolsActivePort`
marker file is the recovery route; a dedicated-profile instance serves
`/json/version` normally. `doctor` detects this exact condition and prints the
precise remedy rather than a generic failure:

```
[FAIL] credential: source=cdp, the DevTools endpoint at http://127.0.0.1:9222
       answered, but its WebSocket could not be used
       -> Port 9222 is open but the DevTools WebSocket handshake was refused.
          Chrome 147+ blocks remote debugging on the default profile when it
          was enabled from chrome://inspect. Close that browser and start a
          dedicated-profile instance instead: chrome.exe
          --remote-debugging-port=9222
          "--user-data-dir=%LOCALAPPDATA%\opencsi-cdp-profile"
          https://opencsitool.com/myTools  -- then sign in once in that window.
```

This is real output from this machine, and it is a genuine end-to-end
validation of the CDP provider against real Chrome 153.

### Why the tool never launches a browser for you

A browser launched by the tool would start with a **fresh profile and no
session**, so it could not answer the question the tool exists to ask. Worse, it
would need to hold the user's credentials to be useful. Reading a session the
user already established is the only design that keeps the credential under the
browser's control.

### Session lifetime

The cookie's TTL is about **0.97 hours**. The client therefore handles expiry
rather than assuming freshness: on a 401 it invalidates, re-reads the credential
**exactly once**, and retries once. There is no loop, so a permanently invalid
credential cannot spin.

### Security constraints, all verified

| Constraint | How it is enforced | How it is verified |
| --- | --- | --- |
| Never send `Authorization` | Header set is built by hand | Real server asserts the received header set |
| Never log/print/repr/traceback a Cookie | `redaction.py` masks structurally | `repr(transport)` test; redaction unit tests |
| Never output a full `virtualKey` | Masked to `sk-bM4LUSm****` | Fixture-level assertions |
| CLI must not accept `--token SECRET` | Only `getpass()` / `$OPENCSI_TOKEN` | `--token` is rejected by argparse |
| No write endpoints | Transport has no write verb | `hasattr` test on all five verbs |
| No admin endpoints (`accountBinding`) | Never referenced | Not present in `src/` |
| No daemon or background polling | One process, one request per command | Architectural |
| Exactly 1 retry on 401 | Single `if`, no loop | Unit + integration tests |

---

## 6. Tests

**389 tests, all passing, fully offline**, run with `unittest`:

```
$ cd tests && python -m unittest discover -s . -p "test_*.py" -t .
Ran 389 tests in 11.1s
OK
```

| File | Tests | Covers |
| --- | --- | --- |
| `test_mapping_regression.py` | 45 | Every field of the investigated payload maps correctly |
| `test_redaction.py` | 53 | Structural secret masking, including the over-redaction regression |
| `test_auth.py` | 17 | Provider protocol, status objects, manual provider |
| `test_cdp.py` | 50 | CDP provider against an in-process fake DevTools server |
| `test_client.py` | 30 | Envelope handling, caching, error mapping, contract check |
| `test_formatting.py` | 61 | CJK width, 亿/万 rule, tables, sections |
| `test_cli.py` | 93 | Argument parsing, exit codes, JSON, stdout/stderr discipline |
| `test_proxy.py` | 20 | Proxy resolution, `--no-proxy`, credential stripping |
| `test_transport_integration.py` | 18 | **Real HTTP over a real socket** |

Also verified:

- **Clean under `-W error::ResourceWarning`** — no leaked sockets on any error
  path, including failed WebSocket handshakes and `HTTPError` bodies.
- **`verify_client.py` — 42/42.** A replay of the original investigation's
  baseline assertions, kept as an independent oracle:
  `totalTokens=3061130999 → 30.6亿`, `API_BUNDLE 2800206464 → 28.0亿`,
  `TRAE 260924535 → 2.6亿`, `totalRequestCount=21632 → 2.2万`,
  `ΣprCount=246`, `ΣaddedLinesCount=31167 → 3.1万`,
  `ΣgeneratedCodeLines=3150`, `ΣadoptedCodeLines=120`, rate `3.8%`;
  row1 `5593 / REQ202608170007 / API_BUNDLE / 使用中 / AI编程助手-002`;
  trend 42 records / 30 dates / 5 models; prices 20 rows / 13 enabled;
  `DEEPSEEK_V4_FLASH_0731` blended `0.28`; `TRAE` monthlyFee `200.00`;
  call-logs `total 0`; key-budget `exists:false`.
- **Hermetic by construction.** Discovery tests monkeypatch `_profile_dirs`, so
  the suite never probes a real Chrome on 9222 even when one is running.

### The integration test earned its place

The suite originally drove everything through a `FakeTransport` that records
calls without doing I/O. That is the right default, but it meant the layer that
actually speaks HTTP was never exercised end to end — and a bug was hiding
there.

`test_transport_integration.py` starts a real `http.server` on an ephemeral
loopback port and puts the real `HttpTransport` under the real client. It found
that the **401 retry path raised `SessionExpiredError` without `http_status`**,
so the same condition carried `401` or `None` depending on whether a credential
refresh had happened. The two `BadAuthHeaderError` raises had the same gap.

It also asserts on **what the server actually received**, which is the point:
a source-level grep for "Authorization" would be satisfied by a comment.

---

## 7. Online smoke test

### What was verified live

**The unauthenticated HTTP layer works against the real site.**

```
GET https://opencsitool.com/opencsitool/rest/v1/user/getUserInfo
  -> HTTP 401
  -> body: "empty Authorization"
  -> headers sent: ['Accept', 'Accept-Language', 'User-Agent']
  -> Authorization present: False
```

**The CDP provider works against real Chrome 153.** Running `opencsi doctor`
against this machine's actual browser produced the accurate, actionable
diagnosis quoted in §5 — which required successfully contacting the DevTools
endpoint, listing targets, attempting the WebSocket upgrade, classifying the
refusal, and mapping it to exit code 10.

**A proxy bug was found and fixed only because this test was attempted.** See §9.

### What could not be verified, and why

**The authenticated query path is blocked by the environment.** The browser
session has expired:

```
URL:   https://opencsitool.com/auth/login?redirect=%252FmyTools
TITLE: 登录 - openCsiTool
```

The page's own `fetch` returns `401 :: empty Authorization`. Signing in requires
the user's GitCode credentials, and **I did not log in on the user's behalf** —
that would be acting as the user with their credentials, which is outside what
this task authorizes.

This is a genuine limitation of the verification, not of the implementation.
What stands in its place:

1. The API contract was verified live during the investigation phase — out of
   browser, with a cookie, **12/12 endpoints returned 200**.
2. Those exact responses are committed as sanitized fixtures and replayed by 389
   offline tests.
3. `demo_e2e.py` runs the real CLI over real sockets against those fixtures:
   **10/10 commands exit 0**, and every path requested had a fixture. The demo
   exercises `status`, `status --verbose`, `tools`, `tools --show-key-mask
   --type API_BUNDLE`, `usage`, `trend`, `trend --days 7`, `prices`, `logs` and
   `doctor --skip-contract`; its captured output is committed as
   `demo_output.txt`.

To complete this check, start a browser with remote debugging, sign in at
`https://opencsitool.com/myTools`, and run `opencsi doctor` followed by
`opencsi usage`.

### A dedicated-profile control (adds positive CDP evidence, not more)

To test the README's claim that a *dedicated* profile recovers remote debugging
— the exact case the user's default-profile Chrome 153 refuses — a fresh
Chrome 153.0.8010.50 was started on a throwaway profile with
`--remote-debugging-port`. It served:

```
GET http://127.0.0.1:9333/json/version
  -> "Browser": "Chrome/153.0.8010.50"
  -> webSocketDebuggerUrl present
GET http://127.0.0.1:9333/json/list
  -> a page target with a usable per-page webSocketDebuggerUrl
```

This confirms, live, the discovery path the provider uses and that the
WebSocket upgrade this Chrome *offers* is reachable — the thing the default
profile blocks. The provider's own in-process test (`test_cdp.py`, 50 tests
against a fake DevTools server over real sockets) covers the cookie read and
the `Network.getCookies` round-trip.

**It does not, by itself, prove an end-to-end authenticated query**, because no
openCsiTool session cookie exists to find. And the control browser was
disposable: the experiment held it open only as long as needed, then it was
closed. The authenticated live query therefore remains honestly **blocked by
environment**, exactly as §7 states.

---

## 8. Security review

### Secrets never reach the wrong place

`redaction.py` masks **structurally**, at the point of output, rather than
relying on every call site remembering to be careful. It covers exception
messages, tracebacks, JSON, argv, file and environment output, and it also
strips proxy userinfo — a leak I introduced myself and then caught (§9).

Patterns masked: `Cookie`/`Set-Cookie` headers, `Authorization` headers,
`token=`, quoted JSON secret keys, unquoted `virtualKey`, `sk-` keys, JWTs, and
any bare token-like run of 48+ characters.

### The over-redaction trap

Masking too much is also a failure. The first version treated any key containing
`credential` as a secret *holder*, so `status --json` emitted:

```json
{ "ok": false, "credential": "<redacted>" }
```

Safe, but useless: the credential summary is exactly what a script needs in
order to distinguish "no browser" from "not signed in". `credential` and
`credentials` are now treated as **containers** and recursed into. Nothing was
loosened — a nested secret is still caught by its own key name or by the value
patterns, and a credential key holding a bare string is still masked outright.

### Fixture hygiene

The committed fixtures were checked for real secrets. The `sk-` strings in them
are synthetic (`sk-bM4LUSmEXAMPLE00000000`, `sk-bM4LUSmTESTFIXTURE000000`), and
the live key appears nowhere in the repository. A `capture/` directory of 72
browser-capture artifacts (~1.2 MB) was inspected and **deliberately not
committed**: its `sk-` hits are synthetic, its three cookie files contain **zero
`value` fields**, and its long base64 strings are PNG data URIs from login-page
screenshots. It is gitignored.

### Structural guarantees

| Guarantee | Mechanism |
| --- | --- |
| Read-only | `HttpTransport` has no write verb at all |
| No admin endpoints | `accountBinding` never referenced |
| No `Authorization` | Header set built explicitly; asserted against a real server |
| No secret on argv | `--token` does not exist; `getpass()` reads stdin |
| No daemon | One process, one request per command |

---

## 9. Git commits

The history below lists the substantive commits, oldest first. Working tree
clean. Each commit's tree imports and passes its own tests — commits 2 and 3
required writing CDP-free variants of two `__init__.py` files so the intermediate
trees were coherent.

```
b8d4c5f  feat: package opencsitool client as standalone library
ea07869  feat: port the client to a zero-dependency stdlib library
579cae4  feat: implement CDP cookie credential provider
d7eba34  feat: add standalone opencsi CLI
7c98f52  test: migrate investigation regression coverage
4826150  docs: document standalone CLI and authentication
ce8fc80  fix: report the real status failure and stop over-redacting it
eeb8366  fix: stop contract-check from reporting a missing browser as drift
d9e90aa  fix: derive doctor's exit code from the cause, not the check name
3884712  test: exercise the transport over a real socket, and fix what it found
eaa4565  test: add an end-to-end demo that runs the real CLI over a real socket
dc9b853  fix: stop masking the numbers under a token-ish key
d2b4695  test: detect fixture credentials by shape, not by stored fragment
0146827  docs: add the standalone tool implementation report
aa1a5e1  feat: close the gaps between the CLI and the objective's command spec
4a9183e  feat: implement --refresh, which the README already documented
```

Later commits update this report and the README to match the code; they change
no behaviour. Listing a commit count here would make this section wrong every
time it was corrected, so `git log` is the source of truth for that.

28 source modules and 5,345 lines of library code, 3,883 lines of tests.

### Bugs found by exercising the tool, not by writing tests

Every bug below was found by *running the thing*, and every fix ships with a
regression test that was confirmed to fail against the previous code.

**1. `SSLEOFError` against the live API (the proxy trap).** `urllib` resolves
proxies from the environment **and the Windows registry**. This machine has
`ProxyEnable=1, ProxyServer=127.0.0.1:7890`, which cannot carry the TLS
connection — while `curl`, which reads only environment variables, reached the
site fine (`http_code=401`). Isolated by bisection: a raw `ssl` handshake
succeeded, `http.client` returned 401, `urllib` always failed, and
`getproxies()` revealed why. Fixed with `--no-proxy`, proxy-aware error
messages, and `proxy_for()`.

**2. A proxy-credential leak I introduced.** `proxy_for()` returned
`http://user:S3cretPw@proxy:8080` verbatim, which would have reached error
messages. Fixed with `_strip_proxy_credentials()`.

**3. `status` hardcoded exit 12.** Every failure was reported as "not signed in",
so an unreachable DevTools port told the user to sign in — advice that cannot
work, because the real problem is that the browser was never started with remote
debugging. It also printed the cause twice.

**4. JSON output masked the credential summary away.** Described in §8.

**5. `contract-check` reported a missing browser as contract drift.** It wrapped
every endpoint call in `except OpenCsiError` and recorded the exception as a
failed check. Correct for a schema change, wrong for a credential failure: a
user who had not started their browser was told the API contract had changed,
and got exit 1 instead of 10. Connectivity failures now propagate; HTTP-level
failures stay check *results*, because the brief lists HTTP status among the
things this command verifies.

**6. `doctor` derived its exit code from the check name.** A refused DevTools
handshake exited 12 and advised signing in. A check name is not a cause:
`credential` can fail as 10, 12 or 13, and those need different actions. Checks
now carry the exception's own `exit_code`, falling back to the provider's
machine-readable `last_error_code`.

**7. The 401 retry path omitted `http_status`.** Found by the new integration
test; described in §6.

**8. `--json` masked the numbers it was supposed to report.** Found by checking
the report's own example — `usage --json | jq '.summary.total_tokens'` — against
the real command instead of trusting it. It returned `<redacted>`.

`redact_mapping` masked any key whose name merely *contains* `token`, and in
this API that is where all the data lives: `total_tokens`,
`tokens_by_request_type`, `token_trend`, `token_budget`, `token_count`. So
`usage --json` hid the headline figure the command exists to report, and
`trend --json` was gutted too. Safe, and useless.

The fix combines the key name with the **value type**, which is the distinction
that actually matters: a credential is always a string, so a credential-ish key
holding a number, list or object is describing data. Nothing was loosened — a
bare `token`, `access_token`, `refreshToken`, `virtualKey`, `api_key`, `cookie`,
`Set-Cookie`, `authorization`, `secret` or `password` is still masked, and a
secret nested inside a container is still caught by its own key.

A pattern runs through bugs 3, 4, 5, 6 and 8: **the tool kept reporting the
wrong thing — sometimes the wrong cause, sometimes no data at all.** A
diagnostic that names the wrong problem, or hides the number it was asked for,
is worse than one that says nothing, because the user acts on it. The fix in
each case was to derive the output from what was actually there rather than
from a label.

### Found by auditing the CLI against the written specification

A later pass compared every command against the required flag list rather than
trusting that "it works". Four flags were missing and one was a fiction.

**9. `--refresh` was documented but did not exist.** The README listed it in the
global options table; argparse rejected it. A documented flag that errors out is
worse than an undocumented one, because the user assumes they mistyped it.

Implementing it exposed a second, subtler bug. My first version called
`provider.invalidate()`, on the reasoning that a refresh should drop anything
cached. That is right for `CdpCookieProvider`, whose `invalidate()` drops a
cache — but `ManualCookieProvider.invalidate()` is **permanent**, because there
is no source to re-read. So `--refresh` destroyed the user's only credential and
then reported "session expired": wrong, and unactionable. `refresh()` is the
verb whose contract is "re-read if you can", and it is a no-op for the manual
provider. The regression test asserts the token survives and was confirmed to
fail (`None != 'TESTCOOKIE...'`) against the `invalidate()` version.

This is objective §55 exactly: the two providers need different `invalidate()`
semantics, and a caller that assumes one shape breaks the other.

**10. The CLI did not match the specified command surface.** `tools` lacked
`--type`, `--search` and `--show-key-mask`; `trend` lacked `--days`, `--from`
and `--to`; `logs` lacked `--from`/`--to`; `status` showed credential internals
by default and none of the headline numbers; `doctor` collapsed eleven
per-endpoint checks into one unactionable "api contract" line. All are now
implemented, with the reasoning recorded in commit `aa1a5e1`.

Two deliberate behaviour changes came out of this, both tightening rather than
loosening:

- `tools` now **hides virtual keys by default**. Even the site's masked
  `sk-xxxxxxxx****` form is account-identifying, so it is opt-in.
- `status` hides `userId` / `accountId` / organization UUID unless `--verbose`.

**11. The exit-code bug came back in a third and fourth place.** Bugs 3, 4 and 6
are all the same mistake — deriving an exit code from a label instead of the
cause. Fixing the three commands where it was noticed did not remove the
mistake from the design; it only hid it where nobody looked. Two more commands
caught errors locally because they print partial output on failure, and both had
kept a hardcoded code:

- `status --json` returned a fixed `1` on a failed session, while the text path
  had already been corrected to call `exit_code_for()`. The *same* broken session
  therefore exited `1` with `--json` and `10` without it — which defeats the
  entire reason a script passes `--json`: it still could not branch on the
  outcome without parsing prose.
- `login` returned a fixed `12` (not-logged-in) whenever no session appeared,
  including when no DevTools endpoint was ever reachable — telling the user to
  sign in inside a browser that is not running.

`app.py` was already correct: it maps a raised `OpenCsiError` to
`exc.exit_code` identically in both modes. The lesson generalises past this
codebase — when a fix applies to a *pattern*, search for every instance of the
pattern; fixing the symptom you happened to observe leaves the rest.

Both new tests were confirmed to fail against the reintroduced bugs (`12 != 10`)
before the fixes were restored, so they guard the cause rather than documenting
whatever the code currently does.

---

## 10. Remaining limitations

### Honest scope boundaries

1. **The authenticated live query is unverified in this environment** (§7).
   Everything short of the final authenticated fetch is verified; the fixtures
   and the investigation's live 12/12 run cover the gap.
2. **The API is undocumented and internal.** All `/swagger-ui*`, `/v3/api-docs`,
   `/openapi.json` and `/actuator*` paths return the 2,296-byte SPA shell, and
   `/opencsitool/v3/api-docs` returns `404 {"message":"路径错误！"}`. The contract
   is whatever the site does, so it can change without notice — which is why
   `contract-check` exists and why it deliberately ignores dynamic values.
3. **No write operations.** By design. The tool cannot create, modify or revoke
   anything.
4. **The session lasts about an hour.** There is no refresh token and no
   headless re-login; when it expires the user signs in again. This is a
   property of the site, not a shortcut in the tool.
5. **A browser must already be signed in.** This is the design, not a gap (§5),
   but it does mean the tool is not usable from a cron job without a
   pre-authenticated browser on the same machine.
6. **`--json` schema is stable but not versioned.** Fields may be added; the
   `error_code` vocabulary is the intended branching surface.
7. **Rate limiting is unobserved.** No rate-limit headers were seen during the
   investigation, so the client does not throttle. It does cache, and every
   command makes one or two requests, so ordinary use is not aggressive.

### Explicit non-goals for this phase

- **No GUI.** No PySide, WinUI, tray icon, or web dashboard. Deferred to Phase 2.
- **No agent adapters.** No MCP server, no LLM tool definitions, no ChatGPT or
  Claude integration.

### Packaging — now verified

`pip install -e .` **fails in this environment**, because there is no
`setuptools`/`wheel` and no package index to fetch them from:

```
BackendUnavailable: Cannot import 'setuptools.build_meta'
```

`uv pip install -e .` **succeeds**, and the installed entry point was verified
running from outside the repository, so it does not depend on the current
directory or on `PYTHONPATH`:

```
$ cd C:\ && opencsi --version
opencsi 0.1.0

$ opencsi --help
usage: opencsi [-h] [--version] [--json] [-v] [--cdp URL] ...
```

`uv pip install pytest` also works, so the suite runs under both runners:

```
$ python -m unittest discover -s . -p "test_*.py" -t .   # 389 tests, OK
$ pytest                                                  # 389 passed
```

`pytest` needs no environment setup: `pyproject.toml` sets
`pythonpath = ["src"]`, so a bare `pytest` from the repository root works.

---

## 11. Usage

### Requirements

Python **3.10+**, standard library only. No runtime dependencies to install.

### Install

```bash
uv pip install -e .      # or: pip install -e .  (needs setuptools/wheel)
opencsi --help
```

Or run it with no install at all, straight from the checkout:

```bash
cd OpenCSIToolMonitor
export PYTHONPATH=src            # Windows: $env:PYTHONPATH="src"
python -m opencsi status
```

### Getting a session

Start a browser with remote debugging on a **dedicated profile** (Chrome 147+
refuses it on the default profile):

```powershell
# Windows
chrome.exe --remote-debugging-port=9222 `
  "--user-data-dir=%LOCALAPPDATA%\opencsi-cdp-profile" `
  https://opencsitool.com/myTools
```

```bash
# macOS
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.opencsi-cdp-profile" \
  https://opencsitool.com/myTools
```

```bash
# Linux
google-chrome --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.opencsi-cdp-profile" \
  https://opencsitool.com/myTools
```

Sign in **once** in that window, then leave it open.

### Running

```bash
opencsi doctor         # start here: diagnose the whole chain
opencsi status         # session, identity and the headline numbers
opencsi usage          # the full data overview
opencsi tools          # granted accounts
opencsi trend          # tokens by model, with prompt/completion split
opencsi prices         # price list
opencsi logs           # gateway call logs
```

Without installing, the same commands work as `opencsi <command>`
from the checkout with `PYTHONPATH=src`.

### Common options

```bash
opencsi usage --json                              # machine-readable
opencsi usage --refresh                           # bypass the data cache
opencsi status --verbose                          # reveal userId / org UUID
opencsi status --no-summary                       # session check only, one request
opencsi tools --type API_BUNDLE --show-key-mask
opencsi tools --search 助手                        # client-side filter
opencsi trend --days 7                            # today plus the six before it
opencsi trend --from 2026-08-20 --to 2026-09-19
opencsi status --cdp http://127.0.0.1:9223
opencsi status --no-proxy                         # if a local proxy breaks TLS
opencsi status -v                                 # log request paths
```

### Using it from a script

```bash
opencsi usage --json | jq '.summary.total_tokens'
```

Branch on the exit code to tell causes apart:

```bash
opencsi status
case $? in
  0)  echo "signed in" ;;
  10) echo "start the browser with --remote-debugging-port=9222" ;;
  12) echo "sign in at https://opencsitool.com/myTools" ;;
  13) echo "session expired; sign in again" ;;
esac
```

### Using it as a library

```python
from opencsi import OpenCsiToolClient
from opencsi.auth.cdp import CdpCookieProvider

client = OpenCsiToolClient(CdpCookieProvider())
try:
    identity = client.login_or_restore_session()
    summary = client.get_summary()
    print(identity.display_name, summary.total_tokens)
finally:
    client.close()
```

The client needs nothing but a `CredentialProvider` — the browser automation
lives behind that one method and can be replaced without touching the client.

### Verifying the installation

```bash
cd tests && python -m unittest discover -s . -p "test_*.py" -t .   # 389 tests
cd .. && python verify_client.py                                   # 42/42
python demo_e2e.py                                                 # 10/10 commands
```

All three run fully offline.

---

## Appendix: what to do next

If the authenticated path needs to be confirmed, with a browser signed in:

```bash
opencsi doctor      # expect all checks to pass
opencsi usage       # compare against 30.6亿 / 2.2万 / 3.8%
opencsi contract-check   # expect all checks to pass
```

If `contract-check` reports failures while the site works normally, the upstream
API has changed and the mapping in `models.py` needs revisiting. That is exactly
the signal the command exists to produce.
