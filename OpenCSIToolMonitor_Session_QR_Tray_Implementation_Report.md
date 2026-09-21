# OpenCSIToolMonitor Session + QR + Tray Implementation Report

This report covers the third phase of work on `jingchangshi/OpenCSIToolMonitor`
(branch `master`): turning a read-only CLI that *can* be signed in from a browser
into a tool that **keeps itself signed in**, **can sign in without a browser at
all**, and **stays visible on the Windows 11 desktop**.

The governing principle, unchanged from the previous phase but now extended:

```
DSH develops it.              DSH does not run it.
Browser may authenticate it.  Browser does not query it.
OpenCsiToolClient queries it. CLI and tray expose it.
The tray is a view.           It never shells out to the CLI.
```

---

## 1. Verdict

**Complete, and verified live against the real site.**

| Objective | Status | Evidence |
| --- | --- | --- |
| Separate *credential reload* from *session renewal* | **Done** | `SessionManager` + three protocols; commit `cf34c1b` |
| Silent automatic renewal of an expiring openCsiTool session | **Done, proven live** | Soak probe observed a real expiry at `02:13:34`, `token_changed=True`, new session accepted by the server as `shijingchang` |
| Renewal trigger policy with no infinite loops | **Done** | `expires_in > margin` → no-op; `<= margin` → silent renew; 401 → reload, then renew once |
| Silent renewal does not steal focus | **Done** | `Target.createTarget` on a **background** target, then `Target.closeTarget`; the user's current page is never navigated |
| GitCode pure-CLI / QR login feasibility research | **Done** | `docs/gitcode-qr-protocol.md` — verdict `QR_FLOW_REPRODUCIBLE` |
| Implement CLI QR login | **Done, verified live** | `opencsi login --qr` created a real challenge, saved the image, and reported an honest `TIMEOUT` (exit 13) when not scanned |
| Windows 11 tray v1 | **Done, verified live** | `opencsi tray --once` printed a real snapshot; `--check` reported `tray: ok`, 6 menu items |
| Tests | **Done** | **656 tests** (655 passed, 1 skipped) green under `pytest` **and** `unittest` |
| Windows real-machine verification | **Done** | Live CLI, QR, tray, frozen binaries, entry points |
| Documentation | **Done** | 5 docs + README + this report |
| Normative commits | **Done** | 27 commits, `cf34c1b` onward; 3 fixes for defects found by running the real artifacts |

One honest non-claim: **the QR flow's final step is not machine-verifiable.** It
requires a human to scan a WeChat mini-program code with a phone. The code
delivered proves every step up to that physical action, and reports the timeout
as a distinguishable exit code rather than pretending to succeed.

---

## 2. Final HEAD

The last commit that changes **source or tests**:

```
542fe4f  fix(packaging): make the frozen tray honour its own arguments
```

Documentation-only commits follow it, including the ones that carry this report.
Naming those here would be circular — a commit cannot contain its own SHA — so
the anchor is the last behavioural change, which is the thing a reader actually
needs to check out.

Baseline for this phase was `cf34c1b~1` (`4739898`). Working tree is clean; no
untracked scratch files; no stray processes; no `Run` registry entry left behind.

**Binaries** (rebuilt and re-verified after the last source change):

| Artifact | Size | Purpose |
| --- | --- | --- |
| `dist/opencsi.exe` | 16.3 MB | Console CLI |
| `dist/opencsi-tray.exe` | 16.3 MB | Windowed tray (no console window) |

Two binaries are deliberate: PyInstaller's `--windowed` is a per-binary flag, and
a black console window flashing up at sign-in is not acceptable for a tray app.

---

## 3. Root causes

Three real defects motivated this phase. All three were reproduced before being
fixed, and each fix has a test that fails against the old code.

### 3.1 The session died after ~58 minutes and nothing renewed it

The openCsiTool `token` cookie has a TTL of ≈ 0.97 h (3,472 s measured live). The
existing `CdpCookieProvider.refresh()` **re-read the cookie from the browser** —
it did not cause a *new* cookie to be issued. So a user who left the tool running
found it silently degraded to unauthenticated after an hour, and the only
recovery was a manual browser login.

The word "refresh" was doing double duty for two different operations, and that
conflation is the root cause:

| Operation | What it does | Effect on lifetime |
| --- | --- | --- |
| **credential reload** | re-reads the cookie value from the browser profile | none — same token, same expiry |
| **session renewal** | re-runs GitCode OAuth so the server issues a *new* cookie | extends it by a full TTL |
| **interactive login** | opens the login page and waits for a human | extends it, but needs a human |

### 3.2 `opencsi login` hard-depended on opening a browser

There was no path to authentication that did not involve launching a browser
window and driving a human through a web page. On a headless or locked-down
machine — or simply when the user does not want their browser hijacked — the tool
was unusable.

### 3.3 There was only a CLI

No persistent presence. To know whether the session was alive you had to run a
command, and there was nothing on the desktop to tell you the tool was degrading.

---

## 4. SessionManager architecture

The three operations from §3.1 are now three separate protocols, coordinated by
one object. Nothing else in the codebase decides when to renew.

```
                    ┌──────────────────────────────────────────────┐
                    │              SessionManager                  │
                    │  renew_margin    renew_cooldown  last_renewal│
                    │                                              │
                    │  needs_renewal()   -> bool                   │
                    │  ensure_valid()    -> RenewalResult          │
                    │  renew(force)      -> RenewalResult          │
                    │  reload_then_renew()-> RenewalResult          │
                    │  login()           -> LoginResult            │
                    │  describe()        -> dict                   │
                    └───┬──────────────┬───────────────┬───────────┘
                        │              │               │
        ┌───────────────▼──┐  ┌────────▼────────┐  ┌───▼──────────────────┐
        │ CredentialProvider│  │ SessionRenewer  │  │InteractiveAuthenticator│
        │  (pre-existing)   │  │   (Protocol)    │  │      (Protocol)      │
        │                   │  │                 │  │                      │
        │  get_credentials()│  │  renew()        │  │  login()             │
        │  refresh()        │  │  can_renew()    │  │  describe()          │
        │  invalidate()     │  │  describe()     │  │                      │
        └───────────────────┘  └────────┬────────┘  └──────────┬───────────┘
                                        │                      │
                    ┌───────────────────▼───┐      ┌───────────▼────────────┐
                    │ BrowserOAuthRenewer   │      │ GitCodeQrAuthenticator │
                    │  (silent, CDP)        │      │  (no browser at all)   │
                    └───────────────────────┘      └────────────────────────┘
```

`RenewalStatus` is a `str` enum compared **by identity**, never parsed from a
string: `RENEWED`, `ALREADY_VALID`, `LOGIN_REQUIRED`, `CDP_UNAVAILABLE`,
`OAUTH_FAILED`, `TIMEOUT`, `UNSUPPORTED`.

### Renewal trigger policy

```
expires_in  >  renew_margin (300 s)   ->  ALREADY_VALID, do nothing
expires_in  <= renew_margin           ->  silent renew (one attempt)
API returns 401                       ->  reload_then_renew(): reload the
                                          cookie once, then renew once
renew fails                           ->  LOGIN_REQUIRED; never loop
```

A cooldown (`renew_cooldown`) prevents a failing renewal from being retried on
every tick. There is no unbounded retry anywhere in the path.

---

## 5. Silent OAuth renewal

`BrowserOAuthRenewer` re-runs the GitCode OAuth authorization-code flow over the
Chrome DevTools Protocol, using the SSO session that already lives in the
dedicated browser profile. That SSO session outlives the openCsiTool cookie,
which is precisely why this works without user interaction.

**Focus is never stolen.** The renewer creates a *background* target and closes
it when done:

```
Target.createTarget  (background: true)   <- does not activate the tab
   -> drive the OAuth redirect chain
   -> read the resulting cookie via Storage.getCookies (browser-scoped)
Target.closeTarget
```

The user's current page is never navigated, never focused, and never closed.
`Network.getCookies` / `Network.deleteCookies` are page-scoped and would have
required touching the user's page; `Storage.getCookies` is browser-scoped and is
the correct domain for this.

**Success is proven, not assumed.** A renewal counts only when *all* of:

1. `old token != new token`, **and**
2. `new expiry > old expiry`, **and**
3. the server accepts the new session (`getUserInfo` → `200`).

Reaching `Page.loadEventFired` proves only that a page loaded. That is not
authentication, and the implementation does not treat it as such.

### Live evidence — a real expiry, renewed with no user action

`tools/probe_renewal_soak.py --minutes 75 --renewals 1 --interval 20`, started
against a session with 3,472 s of life left and using the **production** renewal
margin (300 s):

```
start lifetime : 3472s
renew margin   : 300s (production value)
observing for  : 75 min, every 20s

[01:20:23] OK               lifetime   3472s
[02:12:28] OK               lifetime    347s      <- margin about to be crossed
[02:13:34] RENEWED  token_changed=True

renewals observed: 1
final check    : server accepted the session (shijingchang)

VERIFIED: 1 silent renewals across real expiries, with no
user interaction and no interactive login required.
```

This is the single most important piece of evidence in this report: the session
was allowed to run down from a full hour to under five minutes, and it renewed
itself. No human touched the machine.

A second probe, `tools/probe_autonomous_renewal.py`, drives the *scheduled* path
through the production `MonitorService.tick()` with a widened margin and asserts
the same four properties, so the timer-driven path is covered independently of
the direct `renew()` call.

---

## 6. GitCode QR investigation

Full report: `docs/gitcode-qr-protocol.md` (905 lines).

**Verdict: `QR_FLOW_REPRODUCIBLE`.** GitCode's WeChat mini-program login is a pure
HTTP + JSON polling flow that needs no browser JS:

| Step | Method | Path |
| --- | --- | --- |
| Create code | `POST` | `/uc/api/v1/qrcode/wechat_mini_program` |
| Poll status | `GET` | `/uc/api/v1/qrcode/wechat_mini_program?scene_id=…` |
| Exchange for credentials | `POST` | `/uc/api/v1/user/oauth/login/qrcode/wechat_mini_program?scene_id=…` |

Method: static bundle analysis of the lazily-loaded chunks on
`cdn-static.gitcode.com`, plus read-only `GET`/`OPTIONS` probes. No `POST` was
issued during the investigation itself.

Three findings that changed the implementation:

1. **`X-Source` is not a signature.** It is a front-end analytics label
   (`login_trigger_source`). It is absent from the CORS
   `Access-Control-Allow-Headers` whitelist (which echoes only `traceparent`),
   but that is a same-origin-policy constraint on browsers — the server does not
   validate it. Confirmed by wire-level testing once `--qr` existed.
2. **No cookie or auth header is needed to poll.** The status endpoint is
   anonymous.
3. **The `qrcode` field is not a QR code — it is a WeChat mini-program code.**
   This is a hard rendering constraint and was proven three independent ways:
   `zxing-cpp` returns `[]` (no QR symbol); all three QR finder-pattern corners
   measure `0.000` dark; and the finest dark run in the 430 px image is **1 px**,
   where a QR module would be many pixels wide.

Finding 3 is why the CLI does **not** claim to draw a scannable QR. It writes a
PNG and tells the user to scan *the image*, stating plainly that the terminal
drawing is a preview whose dots are finer than a terminal cell. The earlier
behaviour — printing a terminal drawing and calling it a QR code — was a lie, and
commit `36cc305` removed it.

I did **not** invent a GitCode Device Code API. No `/oauth/device/code` endpoint
was fabricated; that flow was not found to exist and is not used.

---

## 7. QR implementation

`src/opencsi/auth/gitcode_qr.py` (577 lines) — `GitCodeQrAuthenticator`,
`QrChallenge`, `QrLoginResult`, `QrStatus`, `QrLoginStatus`, `QrProtocolError`
(exit 33).

* `MAX_QR_REFRESHES = 1` — an expired code is re-created **once**, never in a loop.
* `DEFAULT_POLL_INTERVAL = 1.5 s`, `DEFAULT_MAX_WAIT = 180 s`.
* The `scene_id` is a credential-adjacent value: it is declared with
  `field(repr=False)` and a custom `__repr__` prints `scene_id=<redacted>` and
  truncates the image payload to 32 characters, so it cannot leak through a
  traceback or a log line.

`src/opencsi/auth/qr_render.py` renders the code without adding a runtime
dependency: `segno` and `Pillow` are used only if present, and the renderer falls
back to a luminance-ramp preview (`" .:-=+*#%@"`) otherwise. Saved login codes are
pruned to the newest 3 (`KEEP_CODES = 3`) so the local directory cannot grow
without bound.

### Live run

```
$ opencsi login --qr --qr-wait 6 --no-proxy
Requesting a GitCode login code...

Sign in to GitCode by scanning this with WeChat (微信扫一扫):
   <terminal preview>
Open this file and scan it with WeChat: C:\Users\jcshi\AppData\Local\OpenCSI\
login-code\opencsi-login-code-5hygag28.png
Scan the image, not the terminal drawing above: this is a WeChat mini-program
code, and its dots are finer than a terminal cell.

Waiting for scan...

error: QR sign-in did not complete (TIMEOUT).
       the QR login did not finish in time
EXIT=13
```

The challenge was really created against the live server, the image was really
written, and the timeout is a distinguishable exit code — not a success.

**Scope honesty:** `--qr` success means *"GitCode signed in"*, not *"openCsiTool
session established"*. openCsiTool's own `token` comes from openCsiTool's own
OAuth callback, which needs a browser session. The command states this plainly
rather than implying it replaces the browser path.

The `CdpCookieProvider` + `BrowserOAuthRenewer` pair is kept as the primary and
fallback path even though QR exists. QR removes the browser dependency for
GitCode; it does not remove it for openCsiTool.

---

## 8. Windows tray

`src/opencsi/tray/` (1,225 lines) — `app.py` (pystray host), `presenter.py`
(pure formatting), `icons.py` (Pillow-drawn icons), `__main__.py`, `__init__.py`.

**The tray is a view, not a client.** It imports `MonitorService` and
`SessionManager` directly and calls `tick()`. It never spawns
`opencsi usage --json` and never re-parses JSON. This is asserted structurally by
a test.

**Layering.** Everything that can be tested without a Windows message loop is
pushed into `monitor/` and `tray/presenter.py`, which are pure. `app.py` only
hands already-computed values to pystray. That is why 656 tests run offline while
the tray itself is verified on a real machine.

**States** (`MonitorState`): `STARTING`, `OK`, `REFRESHING`, `RENEWING`,
`LOGIN_REQUIRED`, `OFFLINE`, `SERVER_ERROR`, `AUTH_ERROR` — with Chinese labels
`启动中 / 正常 / 刷新中 / 续期中 / 需要登录 / 离线 / 服务异常 / 会话失效`.

**Deliberate precision asymmetry.** The tooltip compresses (`36.3亿 tokens`); the
menu shows exact figures (`3,634,063,175 tokens / 26,566 次请求`). The tooltip is
capped at 127 characters by the Windows shell, the menu is not, and the exact
numbers are what a user actually needs. Both behaviours are test-locked.

**Notification policy — a latch, not a comparison.** Exactly one balloon on the
*transition into* `LOGIN_REQUIRED` or `AUTH_ERROR`; none while the state persists;
one more only after recovery followed by a relapse; never for `OFFLINE` or
`SERVER_ERROR`. The first implementation compared the new state to the previous
one, which is true on *every* poll because `_refresh_once` publishes a transient
`REFRESHING` first — it fired 5 times in 5 polls. The fix uses an explicit
`_attention_latched` flag and a `_TRANSIENT_STATES` set. The failing case is
`test_repeated_polls_do_not_re_notify`.

Notification failures (missing attribute, raising `notify`, no icon yet) are
swallowed: the tooltip already conveys the state, and a tray that crashes because
a balloon failed is worse than one that stays quiet.

### Live runs

```
$ opencsi tray --once --no-proxy
state: OK
total tokens: 3,634,063,175
requests:     26,566
pull requests:  249
generated:    3,150 lines
adopted:      120 lines
adoption:     3.8%
server data:  2026-09-21T20:32:04+08:00
EXIT=0

$ opencsi tray --check --no-proxy
tray: ok
state: STARTING
menu items: 6
tooltip: OpenCSI | 启动中 / 等待首次更新
EXIT=0

$ opencsi tray --startup-status --no-proxy
start at sign-in: disabled
would run: ...\pythonw.exe -m opencsi.tray
EXIT=0
```

Start-at-sign-in is opt-in (`--install-startup` / `--remove-startup`) and writes
to `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`. The machine was left
clean: **no Run entry, 0 tray processes** between runs.

---

## 9. Tests

**656 tests: 655 passed, 1 skipped, 49 subtests passed.**

```
$ pytest
655 passed, 1 skipped, 49 subtests passed in 36.91s

$ python -m unittest discover -s tests -q
Ran 656 tests in 38.734s
OK (skipped=1)
```

Both runners are green on the same tree. The suite runs entirely **offline** —
no test touches the network; the live evidence in §5–§8 comes from the probe
scripts and manual runs, not from the suite.

New coverage this phase:

| Area | What it locks |
| --- | --- |
| `tests/test_monitor.py` | `AttentionNotificationTest` (8 tests): one balloon per transition, none on repeat, re-arm after recovery, `tick()` is public |
| `tests/test_monitor.py` | Stuck-`RENEWING` regression, no-op renewal, `RENEWING` → `OK` on success |
| `tests/test_tray.py` | `ChineseUnitTest`, `NotificationTest`, `SignInActionTest`, icon colour/shape semantics |
| `tests/test_packaging.py` | `DeclaredScriptTest` — parses `[project.scripts]` and resolves every target (see §10) |
| `tests/test_client.py` | AST-based read-only guard: the business client can only reach a `GET` |
| `tests/test_cli.py` | Output streams are reconfigured to UTF-8; an unrepresentable character does not crash; **every `EXIT_*` constant is unique** |

---

## 10. Security and correctness audit

### Read-only promise, proven structurally

An AST scan over the whole `src/` tree finds **exactly two** mutating HTTP verbs:

```
src\opencsi\auth\gitcode_qr.py:363  POST
src\opencsi\auth\gitcode_qr.py:412  POST
total 2
```

Both are GitCode **authentication** endpoints (create challenge, exchange
credentials). There is no business `POST`/`PUT`/`PATCH`/`DELETE` anywhere.
`OpenCsiToolClient._attempt` is the single business request path and always calls
`get_json`. `sync` appears in the source only as the response field name
`syncStatus`. Commit `ded9f93` added the structural guard so this cannot regress
behind a behavioural test that happens to pass.

### Secret handling

`register_secret`, `scrub_text`, the `Secret` wrapper, `redact_mapping`,
`RedactingFilter` and `install_logging_redaction` are all applied on the
renewal/QR paths. `scene_id` and the QR payload are redacted in `repr`. Verbose
logging prints request **paths** only — never query strings, never cookies.

### Seven real defects found and fixed this round

1. **The icon stranded on "Renewing".** `_maybe_renew` returned on success   without leaving `RENEWING`, so the tray showed a renewing state for up to 5
   minutes after the renewal had already succeeded. Caught by a live probe
   showing `state: RENEWING` on an already-renewed session.
2. **The class docstring promised a public `tick()` that did not exist** (only
   `_tick_once`). This is *why* defect 1 survived: the autonomous path could not
   be driven without waiting for a real 5-minute timer. Fixed by making `tick()`
   public.
3. **The attention latch was a comparison.** See §8 — 5 notifications in 5 polls.
4. **The frozen EXE mangled Chinese.** `dist\opencsi.exe` printed
   `OpenCSI | ������ / �ȴ��״θ���` on code page 936 while `python -m opencsi`
   printed correctly. Root cause: `_make_output_robust` set `errors="replace"` but
   never the *encoding*, and a frozen build does not honour `PYTHONIOENCODING`.
   Fixed by pinning UTF-8. The old test asserted the weaker "a `?` appears"
   property, which is why it passed throughout — a test that documented the bug
   instead of catching it.
5. **`pyproject.toml` declared an entry point that did not exist.**
   `opencsi-monitor = "opencsi.tray.app:main"` had no matching function →
   `AttributeError`. Undetected because the machine's installed `opencsi.exe`
   predated the declaration. Fixed, and `DeclaredScriptTest` now resolves every
   declared script so a missing target fails the suite rather than the user.

Defects 4 and 5 share a shape worth naming: **the tests were checking the
declaration, not the artifact.** The encoding test checked that output was
*non-crashing* rather than *correct*; there was no test at all that the entry
point resolved. Both are now tested against the thing that actually runs.

6. **`QrProtocolError` shared exit code 31 with `ServerError`.** A script
   branching on 31 could not tell an openCsiTool 5xx from a malformed body
   returned by GitCode — two failures with different causes and different fixes.
   This is the *exact* conflation the project had already rejected once, when
   code 32 was added rather than folding a business-level failure into the
   network or server bucket. QR protocol errors now get code 33.

   The reason it survived review is the interesting part: `ExitCodeTest` *did*
   assert "documented codes are distinct", but it listed **six constants by
   hand**, so every constant added after it was written was unchecked by
   construction. The test now enumerates the module for `EXIT_*` integers, so a
   new constant is validated when it is added rather than when someone remembers
   to extend the list. Verified by reintroducing `exit_code = 31`: the new test
   fails with `AssertionError: 31 == 31`. The two bare literals `30`/`31` in the
   QR status map were also replaced with named constants.

Defects 4, 5 and 6 are all the same underlying mistake in different clothes: a
test that enumerates a *sample* of a set, or checks a declaration rather than the
artifact, passes forever while the set grows past it. Three separate instances of
one pattern is a pattern, not a coincidence.

7. **The frozen tray ignored its own arguments.** `opencsi-tray.exe --once`
   printed nothing and then stayed resident forever: the entry script discarded
   `sys.argv` and always started the blocking GUI, so "print one snapshot and
   exit" silently became "run a tray until you kill me". Found by running the
   *rebuilt binary* rather than the source tree.

   This is the sharpest instance of the pattern above. Every existing test drove
   `opencsi tray --once` through the **console** binary, which goes through
   argparse and was correct; nothing exercised the **windowed** binary's own
   entry script. The bug lived precisely in the gap between the two, and no
   amount of testing the source tree could have found it — only executing the
   artefact could.

   The first version of the regression test *hung the suite* rather than failing,
   because it stubbed only the CLI and the regression then called the real GUI
   entry point. The tests now stub the tray entry too, so a regression fails in
   milliseconds. A test that reproduces the hang is not a test.

### Packaging traps handled

* Optional dependencies are imported **lazily inside functions**, so `opencsi
  --help` and `opencsi doctor` work with no extras installed. PyInstaller's static
  analysis cannot see through that, producing a build that *succeeds* and then
  fails on the user's machine. Every such import is listed in `hiddenimports`.
* `pip install -e . --no-deps` fails on build-dependency installation in this
  environment; `--no-build-isolation` is required.
* The `opencsi-monitor` entry point was verified by installing editable and
  running it (process 12308, 4.60 MB resident).

---

## 11. Commits

26 commits, oldest first, covering `cf34c1b` through `12d77aa`. All authored as
`opencsi contributors <contributors@opencsi.invalid>`.

The two commits that write and amend this report are excluded, because a commit
cannot list its own SHA. Every commit that changes source, tests or packaging is
present, and `542fe4f` — the last of those — is the anchor named in §2.

| SHA | Subject |
| --- | --- |
| `cf34c1b` | refactor: separate credential reload from session renewal |
| `48f05a4` | feat: add silent browser OAuth session renewal |
| `5197a67` | feat: add login --status and login --renew, and teach doctor about renewal |
| `ad18b1d` | research: document the GitCode QR login protocol |
| `5ee2200` | feat: add a pure-HTTP GitCode QR login and terminal QR rendering |
| `faaf8c0` | feat: add the Windows 11 notification-area tray |
| `ad61bfb` | fix: report a cold-start renewal as renewed, not as already-valid or timed out |
| `a052019` | fix: make the tray's sign-in entry point actually work, and accept common flags |
| `36cc305` | fix(qr): stop claiming GitCode's login code is a scannable QR |
| `489b1d2` | build: produce standalone Windows binaries with PyInstaller |
| `7767932` | docs: document the session, QR and tray features; fix a status lie |
| `addcaa0` | fix(tray): make the "Sign in..." menu item actually sign you in |
| `3c9926d` | fix(tray): keep the sign-in poll off the monitor's worker thread |
| `22733e3` | fix(monitor): stop stranding the icon on "Renewing", and notify once |
| `1213836` | docs: document the autonomous renewal path and the notification policy |
| `23c1d10` | test(tray): cover the notification path and split a mis-nested test class |
| `033b5d4` | feat(tray): show usage in Chinese units, and keep the menu exact |
| `6344683` | fix(packaging): stop the frozen EXE mangling Chinese output |
| `a81e7eb` | fix(tray): add the missing opencsi-monitor entry point |
| `80d1f14` | docs: correct the test count in the architecture overview |
| `ded9f93` | test(client): assert the read-only promise structurally, not just behaviourally |
| `73b10f0` | test(tray): lock the icon's colour and shape semantics |
| `5ab3ad3` | docs: refresh the architecture test count to 651 |
| `39967ad` | fix(errors): stop the QR protocol error borrowing the server-error code |
| `542fe4f` | fix(packaging): make the frozen tray honour its own arguments |
| `12d77aa` | docs: record the frozen tray's arguments and the two new exit codes |
---

## 12. User instructions

### Install

```powershell
git clone https://github.com/jingchangshi/OpenCSIToolMonitor
cd OpenCSIToolMonitor
pip install -e . --no-deps --no-build-isolation
```

The core has **zero** runtime dependencies. Add the tray extra only if you want
the desktop icon:

```powershell
pip install "opencsi[tray]"
```

### Sign in

```powershell
opencsi login              # opens the browser (default)
opencsi login --qr         # no browser: scan a WeChat code
opencsi login --manual     # paste the cookie yourself
opencsi login --status     # report the session, change nothing
```

### Keep it alive

Renewal is automatic whenever a command or the tray notices the session is within
5 minutes of expiring. Nothing to configure.

```powershell
opencsi login --renew                  # force a silent renewal now
opencsi login --renew --renew-timeout 60
opencsi status --no-renew              # never renew; just report the expiry
```

`$env:OPENCSI_NO_RENEW=1` disables renewal globally.

### Run the tray

```powershell
opencsi tray                        # show the icon
opencsi tray --once                 # one snapshot, print, exit
opencsi tray --check                # verify it can start
opencsi tray --install-startup      # start at sign-in
opencsi tray --remove-startup
opencsi tray --startup-status
```

Or run the standalone binary, which needs no Python at all. It takes the same
flags as the sub-command, with `tray` implied:

```powershell
dist\opencsi-tray.exe                      # show the icon (no console window)
dist\opencsi-tray.exe --once               # one snapshot, print, exit
dist\opencsi-tray.exe --check              # verify it can start
dist\opencsi-tray.exe --startup-status     # report the start-at-sign-in entry
```

A bare launch registers the frozen EXE's own path with `--install-startup`, so
the machine does not need Python on `PATH` to keep the tray running.

### If something is wrong

```powershell
opencsi doctor --no-proxy
```

`--no-proxy` matters: a local proxy on `127.0.0.1:7890` **breaks TLS** to
`opencsitool.com`. `urllib` honours the Windows registry proxy even when `curl`
ignores it, so this is the single most common failure on a developer machine.

**The session lasts about an hour.** That is a property of the site, not a
shortcut here — there is no refresh token. With the tray running, or with any
command run at least once an hour, you will never notice. Without both, you will
need to sign in again.

**Exit codes** (branch on these, not on prose):

| Code | Meaning |
| --- | --- |
| 0 | success |
| 1 | unclassified error |
| 2 | invalid arguments / client misconfiguration / tray unavailable |
| 10 | CDP endpoint unavailable |
| 11 | no usable browser target |
| 12 | not logged in (no `token` cookie) |
| 13 | session expired, or a renewal/QR wait that needs a human |
| 20 | permission denied (403) |
| 30 | network error |
| 31 | server error (HTTP 5xx) |
| 32 | business error (HTTP 200, `code != 200`) |
| 33 | GitCode QR protocol error |
| 130 | interrupted (Ctrl+C) |

The full table lives in `src/opencsi/errors.py` and is test-locked for
uniqueness, so no two failure modes can share a code.
