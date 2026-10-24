// panel/action.js — the toolbar popup: the one surface that always works,
// even when the Magpie page was opened before the extension was loaded and
// the panel never appeared. Every verdict here comes from a real API answer.
"use strict";

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

function setVerdict(text, ok) {
  const el = document.getElementById("verdict");
  el.textContent = text;
  el.className = ok ? "ok" : "bad";
}

async function readActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  const info = document.getElementById("tabInfo");
  const verdict = document.getElementById("verdict");
  if (!tab || !tab.url) {
    info.textContent = "无法读取（浏览器内置页面）";
    verdict.textContent = "请切换到 Magpie Usage 页面";
    verdict.className = "bad";
    return;
  }
  let url;
  try {
    url = new URL(tab.url);
  } catch {
    url = null;
  }
  const hostPort = url
    ? `${url.hostname}${url.port ? ":" + url.port : ""}`
    : tab.url;
  info.textContent = hostPort;
  const isMagpie =
    url &&
    url.protocol === "http:" &&
    url.hostname === "129.153.118.58" &&
    url.port === "3430";
  setVerdict(
    isMagpie ? "匹配 — 可以注入面板" : "不匹配（需要 http://129.153.118.58:3430）",
    isMagpie
  );
}

async function readAuth() {
  const auth = await send({ type: "authInfo" });
  const el = document.getElementById("authInfo");
  el.textContent = auth ? `${auth.authState}${auth.lastError ? " — " + auth.lastError : ""}` : "未知";
}

document.getElementById("retry").addEventListener("click", async () => {
  const button = document.getElementById("retry");
  button.disabled = true;
  button.textContent = "注入中…";
  const result = await send({ type: "retryInject" });
  button.disabled = false;
  button.textContent = "在此标签页重试注入 / 刷新面板";
  if (!result || !result.ok) {
    setVerdict(`注入失败：${(result && result.reason) || "未知原因"}`, false);
    return;
  }
  if (result.containerFound === false) {
    setVerdict("注入完成，但页面没有 #view-usage 容器 —— 这不是可识别的 Magpie Usage 页，请发诊断报告", false);
  } else if (result.hostMounted === false) {
    setVerdict("容器在但面板未挂载 —— 可能仍在加载，请稍后重试或发诊断报告", false);
  } else {
    setVerdict("已注入 — 回到 Magpie 页查看面板", true);
  }
});

document.getElementById("diag").addEventListener("click", async () => {
  const url = chrome.runtime.getURL("panel/diag.html");
  await chrome.tabs.create({ url, active: true });
  window.close();
});

document.getElementById("openMagpie").addEventListener("click", async () => {
  await chrome.tabs.create({
    url: "http://129.153.118.58:3430/?view=usage",
    active: true,
  });
  window.close();
});

readActiveTab();
readAuth();
