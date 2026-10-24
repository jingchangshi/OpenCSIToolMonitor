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
// WHERE the panel lives, and why it must be a *sibling*:
// The usage container is created dynamically by Magpie's kernel, and both the
// kernel and the page's own render passes write the container's innerHTML —
// measured on a real page as "the panel appears, then vanishes when the
// native usage content lands". Magpie also observes the container itself, so
// a child we add is a child its render can (and does) wipe. Mounting as the
// container's previous sibling puts the panel outside every innerHTML blast
// radius; visibility then follows the container's `hidden` attribute, which
// is how Magpie switches views — so the panel shows on Usage and hides on
// every other view without any coupling to Magpie's state.
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

  function findUsageContainer() {
    return document.getElementById("view-usage");
  }

  /**
   * Make sure the panel sits as the usage container's previous sibling and
   * mirrors its visibility. Idempotent; also the re-entry path for
   * chrome.scripting retries.
   */
  function ensureMounted() {
    const url = panelUrl();
    if (!url) return false;
    const container = findUsageContainer();
    if (!container || !container.isConnected) return false;

    // A stale copy anywhere in the document goes first — never two panels.
    let host = document.getElementById(PANEL_ID);
    if (host && host.parentElement !== container.parentElement) {
      host.remove();
      host = null;
    }
    if (!host) {
      host = buildHost(url);
      container.parentElement.insertBefore(host, container);
    }
    // Follow the view switch: Usage visible ⇔ its container is not hidden.
    host.hidden = container.hidden;
    return true;
  }

  function start() {
    if (window.__opencsiUsagePanelInstalled) {
      // A chrome.scripting retry: re-attach if a re-render moved things.
      ensureMounted();
      return;
    }
    window.__opencsiUsagePanelInstalled = true;

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

    // Two persistent guards, both cheap:
    //   * the container's own mutations — its innerHTML rewrites and its
    //     `hidden` flips — re-sync position and visibility;
    //   * if the container element itself is replaced (a re-registered page),
    //     the attribute observation dies with it, so a document-wide observer
    //     re-attaches to the new one.
    const syncGuard = new MutationObserver(() => ensureMounted());
    const attachSyncGuard = () => {
      const container = findUsageContainer();
      if (container && container.isConnected) {
        syncGuard.observe(container, {
          childList: true,
          attributes: true,
          attributeFilter: ["hidden"],
        });
        ensureMounted();
        return true;
      }
      return false;
    };
    if (!attachSyncGuard()) {
      const late = new MutationObserver(() => {
        if (attachSyncGuard()) late.disconnect();
      });
      late.observe(document.documentElement, { childList: true, subtree: true });
      setTimeout(() => late.disconnect(), 60000);
    }

    const reattachGuard = new MutationObserver(() => {
      const container = findUsageContainer();
      if (!container || !container.isConnected) return;
      const host = document.getElementById(PANEL_ID);
      if (!host || host.parentElement !== container.parentElement) {
        ensureMounted();
      }
    });
    reattachGuard.observe(document.documentElement, { childList: true, subtree: true });
  }

  start();
})();
