// content/magpie-inject.js — mount the OpenCSI usage card INTO Magpie's
// native quota grid, as a first-class .subscription-card.
//
// The card is this extension's iframe wrapped in an
// <article class="subscription-card" data-opencsi-extension> that lives as
// the FIRST child of #subscriptionUsage — Magpie's own masonry grid for the
// Usage page's allowance cards. The investigation in
// docs/magpie-quota-grid.md established why this works without touching
// Magpie's source:
//
//   * packUsage collects `[...list.children]` with no filtering — an external
//     card is measured, placed and packed like any native card;
//   * everything that persists or restyles cards (UsageOrder, drag commit,
//     markUpstream, flash restore) keys on [data-provider] — this card has
//     none, so it is layout-visible and persistence-invisible;
//   * Magpie's own ResizeObserver on the list re-runs packUsage when the
//     card's height changes, so the iframe's height protocol alone is enough
//     to keep the grid laid out;
//   * renderQuotas wipes the container on every data refresh — a childList
//     observer re-inserts the card at position 0 (native cards keep their
//     UsageOrder among themselves), skipping the .sorting window so a drag
//     never races a DOM insertion.
//
// The page's JavaScript can no more read the panel's data than any other
// cross-origin iframe: usage numbers stay between the service worker and the
// iframe, and the only message that crosses to the host page is the panel's
// content height (one number, validated in core/resize.js).
(function () {
  "use strict";

  const PANEL_ID = "opencsi-usage-card";
  const EXPECTED = { protocol: "http:", hostname: "129.153.118.58", port: "3430" };

  // Match patterns cannot express a port, so the declared
  // `http://129.153.118.58/*` matches every port on that host. The brief
  // requires exactly one origin — re-verified here at runtime, because a
  // future service on another port of the same host must not grow a card.
  console.info("[opencsi-inject] origin:", location.protocol, "//", location.hostname + ":" + location.port);
  if (
    location.protocol !== EXPECTED.protocol ||
    location.hostname !== EXPECTED.hostname ||
    location.port !== EXPECTED.port
  ) {
    console.info("[opencsi-inject] origin mismatch; exiting");
    return;
  }

  const CoreResize = window.OpenCSICore && window.OpenCSICore.resize
    ? window.OpenCSICore.resize
    : null;

  const panelUrl = () => {
    try {
      return chrome.runtime.getURL("panel/index.html");
    } catch {
      return null;
    }
  };

  // ── the panel's height protocol ─────────────────────────────────────────
  // The iframe reports its content height; the card wraps it, and Magpie's
  // own layout observer does the rest. The listener reads the current frame
  // through panelFrame — never a stale captured reference.
  let panelFrame = null;
  const EXTENSION_ORIGIN = new URL(chrome.runtime.getURL("")).origin;
  const MAGPIE_TARGET_ORIGIN = "http://129.153.118.58:3430";
  window.addEventListener("message", (event) => {
    if (event.source !== (panelFrame && panelFrame.contentWindow)) return;
    if (event.origin !== EXTENSION_ORIGIN) return;
    if (!CoreResize || !CoreResize.isValidResizeMessage(event.data)) return;
    const height = CoreResize.clampPanelHeight(event.data.height);
    if (height === null || !panelFrame) return;
    // Only real changes touch the DOM, so a ResizeObserver loop is impossible.
    if (parseInt(panelFrame.style.height, 10) === height) return;
    panelFrame.style.height = `${height}px`;
  });

  // ── the card ────────────────────────────────────────────────────────────
  function buildCard(url) {
    // The .subscription-card class carries Magpie's own frame: padding,
    // border, radius, background. The iframe stays transparent and borderless
    // so there is no card-in-card double frame.
    const card = document.createElement("article");
    card.id = PANEL_ID;
    card.className = "subscription-card";
    card.dataset.opencsiExtension = "";
    card.style.padding = "0";
    card.style.overflow = "hidden";
    const frame = document.createElement("iframe");
    frame.src = url;
    frame.title = "OpenCSI";
    frame.setAttribute("aria-label", "OpenCSI 公司模型服务用量");
    frame.style.cssText = "display:block;width:100%;height:150px;border:0";
    card.appendChild(frame);
    panelFrame = frame;
    return card;
  }

  function findQuotaGrid() {
    return document.getElementById("subscriptionUsage");
  }

  /**
   * Make sure the OpenCSI card is the grid's first child. Idempotent, and
   * deliberately a no-op while a card drag is in progress (the .sorting
   * window): dragCards snapshots the children it transforms, and inserting
   * mid-drag would fight the drag's own transforms.
   */
  function ensureCard() {
    const url = panelUrl();
    if (!url) return false;
    const grid = findQuotaGrid();
    if (!grid || !grid.isConnected) return false;
    if (grid.classList.contains("sorting")) return true; // retry when it ends

    let card = document.getElementById(PANEL_ID);
    if (card && card.parentElement !== grid) {
      card.remove();
      card = null;
    }
    if (!card) {
      card = buildCard(url);
      grid.prepend(card); // first card; native cards keep their relative order
    }
    return true;
  }

  // ── lifecycle wiring ────────────────────────────────────────────────────
  // Observer singleton, held in closures rather than on DOM elements: the
  // previous version stored a flag it never set, so its document-wide
  // reattach observer called attachGridGuard on every mutation and piled up
  // one grid observer per mutation — unbounded growth across Magpie's quota
  // refreshes. The closure state is the single source of truth: one grid
  // observer at most, swapped only when the grid ELEMENT itself is replaced.
  let observedGrid = null;
  let gridObserver = null;
  let gridObserverCount = 0; // diagnostics: how many observers were created

  function attachGridGuard(grid) {
    if (grid === observedGrid && gridObserver) return; // same element: no-op
    if (gridObserver) gridObserver.disconnect(); // new element: swap, not stack
    observedGrid = grid;
    gridObserverCount += 1;
    // renderQuotas wipes the grid on every data refresh: re-insert. The
    // class filter is the .sorting guard — watched via attributes so the
    // retry after a drag ends is immediate.
    gridObserver = new MutationObserver(() => {
      const live = findQuotaGrid();
      if (!live || !live.isConnected) return;
      if (live !== observedGrid) return; // a newer element took over
      if (!live.classList.contains("sorting")) ensureCard();
    });
    gridObserver.observe(grid, { childList: true, attributeFilter: ["class"] });
    ensureCard();
    publishObserverDiagnostics();
  }

  // Observer diagnostics for the E2E. Content scripts run in an isolated
  // world, so a page-world evaluate cannot read a window property set here —
  // the count is mirrored onto the card element's dataset (DOM is the one
  // object both worlds see) each time it changes.
  function publishObserverDiagnostics() {
    const card = document.getElementById(PANEL_ID);
    if (!card) return;
    card.dataset.observerCount = String(gridObserverCount);
    card.dataset.observing = gridObserver && observedGrid !== null &&
      observedGrid.isConnected ? "1" : "0";
  }

  function start() {
    if (window.__opencsiUsageCardInstalled) {
      // A chrome.scripting retry: re-attach if a refresh moved things. The
      // closure singleton makes this idempotent — a re-run cannot stack.
      ensureCard();
      const live = findQuotaGrid();
      if (live && live.isConnected) attachGridGuard(live);
      return;
    }
    window.__opencsiUsageCardInstalled = true;

    const grid = findQuotaGrid();
    if (grid && grid.isConnected) {
      attachGridGuard(grid);
    } else {
      // The usage page (and with it the quota grid) is registered by plugin
      // code that runs after this content script. Watch for its creation;
      // attachGridGuard's singleton guard makes the handoff safe even if the
      // creator fires more than once.
      const creator = new MutationObserver(() => {
        const live = findQuotaGrid();
        if (live && live.isConnected) {
          creator.disconnect();
          attachGridGuard(live);
        }
      });
      creator.observe(document.documentElement, { childList: true, subtree: true });
      // A page that is not a quota-capable Magpie must not be scanned forever.
      setTimeout(() => creator.disconnect(), 60000);
    }

    // A re-registered usage page can replace the grid ELEMENT itself; this
    // document-wide watcher swaps the singleton onto the new element. The
    // attach is a no-op while the element is unchanged, so it cannot grow.
    const reattach = new MutationObserver(() => {
      const live = findQuotaGrid();
      if (live && live.isConnected) attachGridGuard(live);
    });
    reattach.observe(document.documentElement, { childList: true, subtree: true });
  }

  start();
})();
