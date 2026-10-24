// core/resize.js — the panel's height protocol, as pure decisions.
//
// The iframe is the only place usage data is rendered, but its height belongs
// to the host page's layout. The iframe measures itself and posts a resize
// request; the content script validates and applies it. Both halves share the
// clamp and the message shape from here, so the two ends cannot drift.
//
// The message carries exactly one number — no tokens, no costs, no identity:
//
//   { type: "OPENCSI_PANEL_RESIZE", height: 168 }
//
// The clamp's ceiling is a layout budget, not a content裁剪: the expanded
// model list scrolls internally, so the reported height stays within budget
// by construction.
(function (root) {
  "use strict";

  const DEFAULTS = { min: 88, max: 560 };

  function clampPanelHeight(height, limits) {
    const { min, max } = { ...DEFAULTS, ...(limits || {}) };
    if (typeof height !== "number" || !Number.isFinite(height)) return null;
    return Math.min(max, Math.max(min, Math.round(height)));
  }

  /**
   * Validate an incoming message as a resize request from OUR panel.
   * `originOk` and `sourceOk` are decided by the caller (it owns the iframe
   * reference and the extension origin); this side checks the shape.
   */
  function isValidResizeMessage(data) {
    return (
      !!data &&
      typeof data === "object" &&
      data.type === "OPENCSI_PANEL_RESIZE" &&
      typeof data.height === "number" &&
      Number.isFinite(data.height)
    );
  }

  const api = { DEFAULTS, clampPanelHeight, isValidResizeMessage };
  root.OpenCSICore = root.OpenCSICore || {};
  root.OpenCSICore.resize = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof self !== "undefined" ? self : globalThis);
