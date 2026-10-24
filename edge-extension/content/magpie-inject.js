// content/magpie-inject.js — mount the OpenCSI usage panel into Magpie's
// Usage page. Pure DOM: nothing here touches Magpie's state object, its
// fetch, its router, or its usage numbers.
//
// The panel itself is this extension's iframe (panel/index.html). The page's
// JavaScript can no more read the panel's data than any other cross-origin
// iframe: usage numbers stay between the service worker and the iframe, and
// nothing — no cookies, no API payloads, no employee ids — is written into
// the host page or passed to it.
//
// Re-entry is a first-class path, not an afterthought. The script runs from
// the manifest (fresh navigations) *and* from chrome.scripting retries
// (tabs that were open before the extension loaded, pages whose container a
// re-render replaced). So:
//   * first run installs the observers and mounts;
//   * every later run re-mounts if the panel went missing, then stops —
//     never a second panel, never a second set of observers.
(function () {
  "use strict";

  const PANEL_ID = "opencsi-usage-panel-host";
  const EXPECTED = { protocol: "http:", hostname: "129.153.118.58", port: "3430" };

  // Match patterns cannot express a port, so the declared
  // `http://129.153.118.58/*` matches every port on that host. The brief
  // requires exactly one origin — re-verified here at runtime, because a
  // future service on another port of the same host must not grow a panel.
  if (
    location.protocol !== EXPECTED.protocol ||
    location.hostname !== EXPECTED.hostname ||
    location.port !== EXPECTED.port
  ) {
    return;
  }

  const panelUrl = () => {
    try {
      return chrome.runtime.getURL("panel/index.html");
    } catch {
      return null;
    }
  };

  function buildHost(url) {
    const host = document.createElement("div");
    host.id = PANEL_ID;
    host.style.cssText = [
      "margin:0 0 10px",
      "border-radius:10px",
      "overflow:hidden",
      "border:1px solid rgba(127,127,127,.28)",
    ].join(";");
    const frame = document.createElement("iframe");
    frame.src = url;
    frame.title = "公司 OpenCSI 用量";
    frame.setAttribute("aria-label", "公司 OpenCSI 用量");
    frame.style.cssText =
      "display:block;width:100%;height:300px;border:0;background:transparent";
    host.appendChild(frame);
    return host;
  }

  function isMounted(container) {
    return !!container.querySelector(":scope > #" + PANEL_ID);
  }

  function mount(container, url) {
    // A stale copy anywhere else in the document goes first: a re-rendered
    // container must never end up with two panels.
    for (const stale of document.querySelectorAll("#" + PANEL_ID)) {
      if (stale.parentElement !== container) stale.remove();
    }
    if (!isMounted(container)) container.prepend(buildHost(url));
  }

  function findUsageContainer() {
    return document.getElementById("view-usage");
  }

  /**
   * Make sure the panel is mounted right now. Idempotent; the entry the
   * executeScript retry calls into.
   */
  function ensureMounted() {
    const url = panelUrl();
    if (!url) return false;
    const container = findUsageContainer();
    if (container && container.isConnected) {
      mount(container, url);
      return true;
    }
    return false;
  }

  function start() {
    if (window.__opencsiUsagePanelInstalled) {
      // A retry (chrome.scripting re-run): re-mount if a re-render detached
      // the panel, then leave the existing observers alone.
      window.__opencsiPanelEnsureMounted();
      return;
    }
    window.__opencsiUsagePanelInstalled = true;
    window.__opencsiPanelEnsureMounted = ensureMounted;

    const url = panelUrl();
    if (!url) return;

    if (!ensureMounted()) {
      // The usage page is registered by plugin code that runs after this
      // content script. Watch for the container's creation.
      const creator = new MutationObserver(() => {
        if (ensureMounted()) creator.disconnect();
      });
      creator.observe(document.documentElement, { childList: true, subtree: true });
      // Give up after a minute rather than scanning every DOM change all day
      // on a page that turns out not to be a usage-capable Magpie.
      setTimeout(() => creator.disconnect(), 60000);
    }

    // Re-attach if a re-render detaches the panel from the container, and
    // keep the guard cheap: one observer on the usage container only.
    const attachGuard = () => {
      const container = findUsageContainer();
      if (container && container.isConnected) {
        guard.observe(container, { childList: true });
        if (!isMounted(container)) mount(container, url);
        return true;
      }
      return false;
    };
    const guard = new MutationObserver(() => {
      if (!isMounted(findUsageContainer() || document.body)) ensureMounted();
    });
    if (!attachGuard()) {
      const late = new MutationObserver(() => {
        if (attachGuard()) late.disconnect();
      });
      late.observe(document.documentElement, { childList: true, subtree: true });
      setTimeout(() => late.disconnect(), 60000);
    }
  }

  start();
})();
