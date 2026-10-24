// background/auth-manager.js — the session state machine, and the one piece
// of recovery this extension may do by itself: re-walking OpenCSI's OAuth
// entry with the browser's own GitCode SSO session, in a background tab.
//
//   VALID ──401──▶ SESSION_EXPIRED ──auto──▶ RENEWING ──┬─callback landed──▶ VALID
//                                                       ├─consent page─────▶ CONSENT_REQUIRED
//                                                       └─login page────────▶ LOGIN_REQUIRED
//
// The guardrails, each earned the hard way in the Python client's audit:
//  * single-flight — concurrent 401s wait on one renewal, they do not open
//    three tabs;
//  * one automatic attempt per failure episode — after it fails, automatic
//    retries stop until either the cooldown passes or the user acts;
//  * a page that needs a human (login, approval) stops the automatic flow.
//    The tab is kept open when a human can complete it there, and the user is
//    told what to do — consent is never submitted by this extension;
//  * a tab that could not be used is closed — the extension does not litter
//    the browser with abandoned authentication tabs.
(function (root) {
  "use strict";

  const AUTH_STATE = {
    VALID: "VALID",
    SESSION_EXPIRED: "SESSION_EXPIRED",
    RENEWING: "RENEWING",
    LOGIN_REQUIRED: "LOGIN_REQUIRED",
    CONSENT_REQUIRED: "CONSENT_REQUIRED",
  };

  const OAUTH_ENTRY =
    "https://opencsitool.com/opencsitool/rest/v1/oauth2/authorization/gitcode?redirect=%2FmyTools";

  const DEFAULTS = {
    tabTimeoutMs: 45000,
    consentGraceMs: 12000,
    retryCooldownMs: 5 * 60 * 1000,
  };

  /**
   * @param deps {{tabs: chrome.tabs, now: () => number, log: (msg: string) => void}}
   *        injected so tests can drive the tab lifecycle without a browser.
   */
  class AuthManager {
    constructor(deps = {}) {
      this._tabs = deps.tabs || (root.chrome ? root.chrome.tabs : null);
      this._now = deps.now || (() => Date.now());
      this._log = deps.log || (() => {});
      this._options = { ...DEFAULTS, ...(deps.options || {}) };

      this.state = AUTH_STATE.VALID;
      this.lastError = null;
      this._renewing = null; // the in-flight promise, for single-flight
      this._lastAttemptAt = 0;
      this._listenersAttached = false;
      this._watchers = new Set(); // per-attempt tab watchers
    }

    /** True when a fetch may proceed without recovery first. */
    get isUsable() {
      return this.state === AUTH_STATE.VALID;
    }

    /** The recovery the UI should offer, if any. */
    get recovery() {
      if (this.state === AUTH_STATE.LOGIN_REQUIRED) {
        return "点击「重新登录 OpenCSI」，在打开的页面完成登录后回到本页";
      }
      if (this.state === AUTH_STATE.CONSENT_REQUIRED) {
        return "在已打开的授权页上点击一次批准，然后回到本页刷新";
      }
      if (this.state === AUTH_STATE.RENEWING) return "正在恢复登录，请稍候";
      return null;
    }

    /**
     * Mark that a fetch was refused. Returns whether an automatic renewal may
     * start now (cooldown and single-episode rules applied by the caller
     * awaiting {@link maybeRenew}).
     */
    onSessionExpired(reason) {
      if (this.state === AUTH_STATE.VALID || this.state === AUTH_STATE.SESSION_EXPIRED) {
        this.state = AUTH_STATE.SESSION_EXPIRED;
        this.lastError = reason || "OpenCSI 拒绝了当前会话 Cookie";
      }
      this._log(`session expired: ${this.state}`);
    }

    /** The user completed a login somewhere; the next fetch decides the truth. */
    onUserAction() {
      this.state = AUTH_STATE.VALID;
      this.lastError = null;
      this._lastAttemptAt = 0;
    }

    /**
     * Attempt a silent SSO renewal, single-flight. Resolves with:
     *   "VALID"            — a probe afterwards can expect the cookie to work
     *   "LOGIN_REQUIRED"   — GitCode asked for a sign-in; a human must act
     *   "CONSENT_REQUIRED" — an approval page is waiting for the human
     *   "FAILED"           — the flow could not be driven (timeout, no tabs API)
     * Never throws.
     */
    maybeRenew() {
      if (this._renewing) return this._renewing;
      const now = this._now();
      if (now - this._lastAttemptAt < this._options.retryCooldownMs) {
        this._log("renewal skipped: within the cooldown of the last attempt");
        return Promise.resolve(this.state === AUTH_STATE.VALID ? "VALID" : "FAILED");
      }
      this._lastAttemptAt = now;
      this.state = AUTH_STATE.RENEWING;
      this._renewing = this._driveOAuthTab()
        .then((outcome) => {
          if (outcome === "VALID") {
            this.state = AUTH_STATE.VALID;
            this.lastError = null;
          } else if (outcome === "LOGIN_REQUIRED" || outcome === "CONSENT_REQUIRED") {
            // The machine's part is over; the state records who is needed.
            // Automatic retries are throttled by the cooldown, and the user's
            // login (via the button) calls onUserAction() to lift this at once.
            this.state = outcome;
            this.lastError =
              outcome === "LOGIN_REQUIRED"
                ? "GitCode 需要重新登录"
                : "GitCode 正在等待应用授权确认";
          } else {
            this.state = AUTH_STATE.SESSION_EXPIRED;
            this.lastError = "静默恢复未完成（超时或浏览器限制），可点击重新登录";
          }
          return outcome;
        })
        .finally(() => {
          this._renewing = null;
        });
      return this._renewing;
    }

    /**
     * Drive one background tab through the OAuth entry. The tab lifecycle is
     * observed through chrome.tabs.onUpdated URL changes:
     *
     *   opencsitool.com/<anything>   — the callback completed; the cookie was
     *                                  minted by the callback response itself
     *   gitcode.com/login …          — the SSO session is gone; a human must
     *                                  sign in (tab closed: our flow is over)
     *   gitcode.com/oauth/authorize  — possibly the consent page. Wait a grace
     *                                  period for the SPA to auto-approve; if
     *                                  it is still there, keep the tab open
     *                                  (the human can act in it) and report
     *                                  CONSENT_REQUIRED.
     */
    async _driveOAuthTab() {
      if (!this._tabs || !this._tabs.create) {
        this._log("chrome.tabs unavailable; silent renewal is not possible");
        return "FAILED";
      }
      let tab;
      try {
        tab = await this._tabs.create({ url: OAUTH_ENTRY, active: false });
      } catch (error) {
        this._log(`could not create the renewal tab: ${error && error.message}`);
        return "FAILED";
      }
      const tabId = tab.id;
      this._log(`renewal tab ${tabId} created (inactive)`);

      return new Promise((resolve) => {
        let settled = false;
        let consentSince = 0;
        const finish = (outcome, keepTab) => {
          if (settled) return;
          settled = true;
          this._watchers.delete(watcher);
          if (this._tabs.onUpdated && this._tabs.onUpdated.removeListener) {
            this._tabs.onUpdated.removeListener(watcher);
          }
          if (!keepTab && tabId !== undefined) {
            this._tabs.remove(tabId).catch(() => {});
          }
          this._log(`renewal tab ${tabId}: ${outcome}`);
          resolve(outcome);
        };

        const watcher = (changedTabId, changeInfo) => {
          if (changedTabId !== tabId || !changeInfo.url) return;
          let url;
          try {
            url = new URL(changeInfo.url);
          } catch {
            return;
          }
          this._log(`renewal tab nav: ${url.host}${url.pathname.slice(0, 40)}`);

          if (url.hostname === "opencsitool.com") {
            // The OAuth callback ran and the entry redirected home; the session
            // cookie was minted by the callback response. The probe after this
            // decides the truth; the tab is ours, so it goes.
            finish("VALID", false);
            return;
          }
          if (url.hostname.endsWith("gitcode.com")) {
            if (/^\/login/.test(url.pathname)) {
              finish("LOGIN_REQUIRED", false);
              return;
            }
            if (/^\/oauth\/authorize/.test(url.pathname)) {
              if (!consentSince) {
                consentSince = this._now();
                return;
              }
              if (this._now() - consentSince >= this._options.consentGraceMs) {
                // Still on the authorize page after the grace period: the SPA
                // is waiting for the human. Keep the tab so they can act there.
                finish("CONSENT_REQUIRED", true);
              }
            }
          }
        };

        if (this._tabs.onUpdated && this._tabs.onUpdated.addListener) {
          this._tabs.onUpdated.addListener(watcher);
          this._watchers.add(watcher);
        } else {
          finish("FAILED", true);
          return;
        }

        setTimeout(() => finish("FAILED", false), this._options.tabTimeoutMs);
      });
    }

    /** The session state, redacted, for diagnostics. Never carries tokens. */
    describe() {
      return {
        authState: this.state,
        lastError: this.lastError,
        lastAttemptAt: this._lastAttemptAt || null,
      };
    }
  }

  const api = { AUTH_STATE, OAUTH_ENTRY, DEFAULTS, AuthManager };
  root.OpenCSIBackground = root.OpenCSIBackground || {};
  root.OpenCSIBackground.auth = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof self !== "undefined" ? self : globalThis);
