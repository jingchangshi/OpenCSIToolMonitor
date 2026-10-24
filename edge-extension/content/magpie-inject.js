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
// The Usage container does not exist in Magpie's HTML: upstream's kernel.js
// creates it dynamically (main.id = "view-" + key) when the usage plugin
// registers, and the running version on the VPS may differ from upstream.
// So the mount strategy is defensive, in order:
//   1. an existing #view-usage;
//   2. a container created later, caught by a MutationObserver;
// and a childList observer re-attaches the panel if a re-render ever
// detaches it. A single panel per page, always: a dataset flag and an id
// guard make double-mounting impossible.
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
  if (window.__opencsiUsagePanelInstalled) return;
  window.__opencsiUsagePanelInstalled = true;

  const panelUrl = (root) => {
    try {
      return root.chrome.runtime.getURL("panel/index.html");
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
    frame.allow = "";
    host.appendChild(frame);
    return host;
  }

  function isMounted(container) {
    return !!container.querySelector(":scope > #" + PANEL_ID);
  }

  function mount(container, url) {
    if (isMounted(container)) return true;
    // Detach a stale copy anywhere else in the document first: a re-rendered
    // container must never end up with two panels.
    for (const stale of document.querySelectorAll("#" + PANEL_ID)) {
      if (stale.parentElement !== container) stale.remove();
    }
    container.prepend(buildHost(url));
    return true;
  }

  function findUsageContainer() {
    return document.getElementById("view-usage");
  }

  function tryMount(url) {
    const container = findUsageContainer();
    if (container && container.isConnected) {
      mount(container, url);
      return true;
    }
    return false;
  }

  function start() {
    const url = panelUrl(window);
    if (!url) return; // no runtime API (should not happen in a content script)

    if (!tryMount(url)) {
      // The usage page is registered by plugin code that runs after this
      // content script. Watch for the container's creation.
      const observer = new MutationObserver(() => {
        if (tryMount(url)) observer.disconnect();
      });
      observer.observe(document.documentElement, { childList: true, subtree: true });
      // The observer must not outlive its usefulness forever if the page is
      // something unexpected (an older Magpie without a usage plugin): give
      // up after a minute rather than scanning every DOM change all day.
      setTimeout(() => observer.disconnect(), 60000);
    }

    // Re-attach if a re-render detaches the panel from the container, and
    // keep the guard cheap: one observer on the usage container only.
    const guard = new MutationObserver(() => {
      const container = findUsageContainer();
      if (container && container.isConnected && !isMounted(container)) {
        mount(container, url);
      }
    });
    const attachGuard = () => {
      const container = findUsageContainer();
      if (container) {
        guard.observe(container, { childList: true });
        if (!isMounted(container)) mount(container, url);
        return true;
      }
      return false;
    };
    if (!attachGuard()) {
      const late = new MutationObserver(() => {
        if (attachGuard()) late.disconnect();
      });
      late.observe(document.documentElement, { childList: true, subtree: true });
      setTimeout(() => late.disconnect(), 60000);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
})();
