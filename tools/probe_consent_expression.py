"""Evaluate the production consent expression in a real page.

The probe builds its JavaScript by string concatenation, so a syntax error would
not be caught by any test -- the fake server answers `Runtime.evaluate` without
parsing anything. A malformed expression would simply raise in the browser, the
renewer would catch it and treat it as "no consent form", and the defect would be
back with every test still green.

This calls the *real* ``_consent_pending`` against real pages with known buttons,
printing the raw CDP result including any exception. Nothing is clicked, and the
tab it opens is closed.

Read-only apart from opening and closing its own background tab.
"""

from __future__ import annotations

import base64
import sys
import time

sys.path.insert(0, "src")

from opencsi.auth.cdp import discover_cdp_endpoint  # noqa: E402
from opencsi.auth.oauth_browser import BrowserOAuthRenewer  # noqa: E402
from opencsi.ws import CdpConnection  # noqa: E402

CASES = {
    "no buttons": "<p>nothing here</p>",
    "cancel only": "<button>取消</button>",
    "approve": "<button>授权</button>",
    "approve disabled": "<button disabled>授权</button>",
    "approve as input": "<input type=submit value=授权>",
    "english": "<button>Authorize</button>",
    "approve + cancel": "<button>取消</button><button>授权</button>",
}


def _page(body: str) -> str:
    """A page with an explicit charset.

    Without ``<meta charset>`` a ``data:`` URL has no encoding declaration, and
    Chromium on a Chinese Windows falls back to GBK -- so the UTF-8 bytes for
    授权 arrive as ``鎺堟潈`` and every Chinese case reads as "no approval
    control". That is a harness artifact, not a product finding, and it produced
    exactly that false conclusion on the first run of this probe.
    """
    return (
        "<!doctype html><html><head><meta charset='utf-8'></head>"
        f"<body>{body}</body></html>"
    )


class _RecordingConnection:
    """A CdpConnection proxy that records the last evaluate result verbatim."""

    def __init__(self, inner):
        self._inner = inner
        self.last = None

    def call(self, method, params=None, **kwargs):
        result = self._inner.call(method, params, **kwargs)
        if method == "Runtime.evaluate":
            self.last = result
        return result


def main() -> int:
    renewer = BrowserOAuthRenewer("http://127.0.0.1:9222", timeout=5.0)
    endpoint = discover_cdp_endpoint(ports=(9222,))

    with CdpConnection(endpoint.browser_ws_url(), timeout=15.0) as conn:
        target_id = conn.call(
            "Target.createTarget", {"url": "about:blank", "background": True}, timeout=15.0
        ).get("targetId")
        session_id = conn.call(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}, timeout=15.0
        ).get("sessionId")
        proxy = _RecordingConnection(conn)

        for label, html in CASES.items():
            encoded = base64.b64encode(_page(html).encode()).decode()
            conn.call(
                "Page.navigate",
                {"url": f"data:text/html;base64,{encoded}"},
                session_id=session_id,
                timeout=15.0,
            )
            time.sleep(0.6)
            proxy.last = None
            pending = renewer._consent_pending(proxy, str(session_id))

            raw = proxy.last or {}
            if "exceptionDetails" in raw:
                note = f"EXCEPTION: {raw['exceptionDetails'].get('text')}"
            else:
                note = ""
            print(f"  {label:20s} -> pending={pending!s:5s} {note}")

        conn.call("Target.closeTarget", {"targetId": target_id}, timeout=10.0)

    print()
    print("expected: approve=True; every other case False")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
