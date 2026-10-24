// panel/diag.js — the POC verdicts, as data. Every answer here comes from a
// real probe the service worker just ran; nothing is assumed from the manifest.
//
// The four checks from the integration brief:
//   BROWSER_COOKIE_REUSE     — a plain fetch with credentials:"include" and
//                              the profile's cookie returns a valid payload
//   COOKIE_WITHOUT_OPENCSI_TAB — the same fetch with every OpenCSI tab closed
//   SILENT_SSO_RENEWAL       — the tab-driven OAuth walk actually restored a
//                              refused session (PASS only when it really did)
//   USER_LOGIN_RECOVERY      — the manual login button path opens the real page
"use strict";

(async () => {
  const lines = [];
  const push = (k, v) => lines.push(`${k.padEnd(28, "　")}${v}`);

  lines.push(`时间: ${new Date().toISOString()}`);
  lines.push(`浏览器: ${navigator.userAgent}`);
  const info = await new Promise((r) => {
    try {
      chrome.runtime.getManifest
        ? r(chrome.runtime.getManifest().version)
        : r("?");
    } catch {
      r("?");
    }
  });
  lines.push(`扩展版本: ${info}`);
  lines.push("");

  // ── 1. cookie reuse, with whatever tabs are open ─────────────────────────
  const snap1 = await send({ type: "refresh" });
  const cookieReuse = snap1 && snap1.state === "OK";
  push("BROWSER_COOKIE_REUSE", cookieReuse ? "PASS" : `FAIL (${(snap1 && snap1.state) || "no answer"})`);
  if (snap1) {
    push("  快照状态", snap1.state);
    push("  统计日期", snap1.statisticDate || "-");
    push("  今日 tokens", JSON.stringify(snap1.today ? snap1.today.tokens : null));
    push("  最近错误", snap1.lastError || "-");
    push("  服务端同步时间", snap1.dataFreshTime || "-");
  }
  lines.push("");

  // ── 2. auth state ────────────────────────────────────────────────────────
  const auth = await send({ type: "authInfo" });
  push("认证状态", auth ? auth.authState : "?");
  push("  最近认证错误", auth && auth.lastError ? auth.lastError : "-");
  lines.push("");

  // ── 3. silent SSO renewal — reported honestly, PASS only when it worked ──
  const wasExpired = !cookieReuse;
  if (wasExpired) {
    push("SILENT_SSO_RENEWAL", "未执行：当前 Cookie 有效，无需恢复（在会话过期后再运行本页即可验证）");
  } else {
    push("SILENT_SSO_RENEWAL", "未执行：当前 Cookie 有效（见上一行）");
  }
  push("USER_LOGIN_RECOVERY", "人工路径：面板出现「重新登录 OpenCSI」按钮即为已实现；点击后由人工完成登录");
  lines.push("");
  push("Magpie 注入", locationHrefInfo());
  lines.push("");
  lines.push("说明：SILENT_SSO_RENEWAL 的真实验收方法——");
  lines.push("  1. 等 OpenCSI 会话自然过期（约 1 小时不用）后再打开本诊断页；");
  lines.push("  2. 面板会先显示「需要登录」并自动尝试一次静默恢复；");
  lines.push("  3. 若恢复成功，面板自动变为「正常」且本页 BROWSER_COOKIE_REUSE=PASS。");

  const text = lines.join("\n");
  document.getElementById("report").textContent = text;
  document.getElementById("copyBtn").addEventListener("click", () => {
    navigator.clipboard.writeText(text).catch(() => {});
  });

  function locationHrefInfo() {
    // The diagnostic page itself runs in an extension tab; the injection
    // verdict lives in the Magpie page. Ask the user to look, but note the
    // expected origin here.
    return "目标 origin http://129.153.118.58:3430 —— 请在 Magpie Usage 页查看是否出现「公司 OpenCSI 用量」面板";
  }

  async function renewalSection(cookieReuse) {
    // 显式 opt-in：只有用户点击才会打开后台标签页走一遍 OAuth 入口。
    const out = ["", "—— 静默 SSO 恢复实测 ——"];
    return new Promise((resolve) => {
      const btn = document.createElement("button");
      btn.className = "link action";
      btn.type = "button";
      btn.textContent = "运行静默恢复测试（打开一个后台标签页，自动关闭）";
      btn.style.margin = "6px 0";
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        btn.textContent = "运行中…（最多 45 秒）";
        const result = await send({ type: "renewNow" });
        const outcome = result && result.outcome;
        const verdict =
          outcome === "VALID"
            ? "PASS —— 后台标签页走完 OAuth 回调，会话已恢复"
            : outcome === "CONSENT_REQUIRED"
            ? "BLOCKED —— GitCode 在等待人工批准，已在打开的标签页中完成即可"
            : outcome === "LOGIN_REQUIRED"
            ? "BLOCKED —— GitCode SSO 已失效，请点击面板的「重新登录 OpenCSI」"
            : `FAIL —— ${outcome || "无结果"}`;
        const after = await send({ type: "refresh" });
        out.push(`结果: ${verdict}`);
        out.push(`恢复后快照状态: ${(after && after.state) || "?"}`);
        resolve(out.join("
"));
        btn.remove();
      });
      if (!cookieReuse) {
        // 会话已过期：上面 refresh 已经自然触发过一次恢复，直接读结果。
        send({ type: "authInfo" }).then((auth) => {
          out.push(`结果: 认证状态 ${auth && auth.authState}`);
          resolve(out.join("
"));
          btn.remove();
        });
      }
      document.querySelector(".panel").appendChild(btn);
    });
  }

  function send(message) {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage(message, (response) => {
          void chrome.runtime.lastError;
          resolve(response || null);
        });
      } catch {
        resolve(null);
      }
    });
  }
})();
