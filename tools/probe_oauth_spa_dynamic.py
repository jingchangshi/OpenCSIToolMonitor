"""Observe what the GitCode ``/oauth/authorize`` SPA actually calls, on the wire.

The static analysis in ``probe_oauth_spa_static.py`` says what the JavaScript
*can* call. This probe says what it *does* call, in the one configuration this
machine can actually reach, and records the request sequence in order.

It attaches to a background tab over CDP, enables ``Network`` and ``Page``, and
navigates to the openCsiTool OAuth entry point -- the same navigation silent
renewal performs. Every request the page then makes is recorded as:

    method | host | path (query stripped) | status | content-type

and nothing more. Specifically it does **not** record:

* any query *value* -- only the parameter *names*, because ``code`` and ``state``
  travel there and objective §3 forbids printing them;
* any cookie or header *value* -- only names, and only the names present;
* any response body verbatim. For the handful of endpoints whose response shape
  matters, the body is parsed and reduced to a key/type/length skeleton, so
  "what does this return" can be answered without a token ever reaching a
  terminal or a file.

It never calls a business endpoint, never submits the consent form, and never
approves a grant. The navigation it performs is a ``GET`` of the authorization
entry point -- an authentication action identical to silent renewal, not a
business write.

LIVE / NETWORK / AUTH_SIDE_EFFECT / GET only
---------------------------------------------
LIVE and NETWORK: it drives a real browser against real hosts. AUTH_SIDE_EFFECT:
following the OAuth entry point can mint an openCsiTool session cookie
server-side. GET only: every request it originates is a GET; the POSTs it
records are the page's own, and it neither originates nor replays them.

Usage
-----
    python tools/probe_oauth_spa_dynamic.py --port 9333
    python tools/probe_oauth_spa_dynamic.py --port 9333 --json OUT.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlsplit

sys.path.insert(0, "src")

from opencsi.auth.cdp import discover_cdp_endpoint  # noqa: E402
from opencsi.ws import CdpConnection  # noqa: E402

AUTHORIZE = (
    "https://opencsitool.com/opencsitool/rest/v1/oauth2/authorization/gitcode"
    "?redirect=%2FmyTools"
)

#: Query parameters whose *values* must never be echoed.
SECRET_PARAMS = {"code", "state", "xauth_token", "access_token", "refresh_token", "token"}

#: Response bodies worth reducing to a key skeleton. Everything else is left
#: unread: the fewer bodies pulled into the process, the fewer places a token
#: could surface.
SKELETON_HOSTS = ("gitcode.com", "web-api.gitcode.com")


def fingerprint(value: str) -> str:
    """``len=…, sha256=<first 12>`` -- comparable without being disclosive."""
    digest = hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:12]
    return f"len={len(value):<6d} sha256={digest}"


def split_url(url: str) -> dict[str, Any]:
    """Host, path and parameter *names*. Values are discarded, never stored."""
    parts = urlsplit(url)
    names = sorted({name for name, _value in parse_qsl(parts.query, keep_blank_values=True)})
    secret_present = sorted(set(names) & SECRET_PARAMS)
    return {
        "host": parts.netloc,
        "path": parts.path,
        "param_names": names,
        "secret_params_present": secret_present,
    }


def skeleton(value: Any, *, depth: int = 0) -> Any:
    """A JSON value reduced to keys plus type/length -- never the content.

    ``{"access_token": "eyJ..."}`` becomes ``{"access_token": "str(len=812)"}``,
    which answers "is a credential in this response?" without disclosing it.
    """
    if depth > 4:
        return "<depth-limit>"
    if isinstance(value, Mapping):
        return {str(k): skeleton(v, depth=depth + 1) for k, v in list(value.items())[:40]}
    if isinstance(value, list):
        return [skeleton(value[0], depth=depth + 1), f"...x{len(value)}"] if value else []
    if isinstance(value, str):
        return f"str(len={len(value)})"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "num"
    if value is None:
        return "null"
    return type(value).__name__


class Capture:
    """Accumulates the Network events for one tab, in arrival order."""

    def __init__(self) -> None:
        self.requests: dict[str, dict[str, Any]] = {}
        self.order: list[str] = []
        self.redirects: list[dict[str, Any]] = []
        self.console: list[str] = []
        self.wanted_bodies: dict[str, str] = {}
        self.bodies: dict[str, Any] = {}

    def on_request(self, params: Mapping[str, Any]) -> None:
        request_id = str(params.get("requestId") or "")
        request = params.get("request") or {}
        url = str(request.get("url") or "")
        if not request_id:
            return

        # A redirect reuses the requestId, so the *previous* hop is reported
        # here. Recording it is the only way to see the 302 that started the
        # flow: it never gets its own requestWillBeSent.
        redirect = params.get("redirectResponse")
        if isinstance(redirect, Mapping):
            previous = self.requests.get(request_id) or {}
            self.redirects.append(
                {
                    "from": split_url(str(previous.get("url") or "")),
                    "status": redirect.get("status"),
                    "to": split_url(url),
                }
            )

        if request_id not in self.requests:
            self.order.append(request_id)
        self.requests[request_id] = {
            "method": str(request.get("method") or ""),
            "url": url,
            "resource_type": str(params.get("type") or ""),
            "initiator": str((params.get("initiator") or {}).get("type") or ""),
            "post_field_names": multipart_field_names(str(request.get("postData") or "")),
            "header_names": sorted({str(k).lower() for k in (request.get("headers") or {})}),
            "status": None,
            "content_type": "",
            "mime": "",
            "from_disk_cache": False,
        }

    def on_response(self, params: Mapping[str, Any]) -> None:
        request_id = str(params.get("requestId") or "")
        response = params.get("response") or {}
        entry = self.requests.get(request_id)
        if entry is None:
            return
        entry["status"] = response.get("status")
        headers = {str(k).lower(): str(v) for k, v in (response.get("headers") or {}).items()}
        entry["content_type"] = headers.get("content-type", "")
        entry["mime"] = str(response.get("mimeType") or "")
        entry["from_disk_cache"] = bool(response.get("fromDiskCache"))

        # A body must be pulled while the response is fresh; Chrome evicts it
        # once the renderer moves on. So the interesting ones are marked here
        # and read by the drain loop on its next turn, not at report time.
        path = split_url(entry["url"])["path"]
        if any(marker in path for marker in SKELETON_PATHS):
            self.wanted_bodies[request_id] = path

    def report(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for index, request_id in enumerate(self.order, start=1):
            entry = self.requests[request_id]
            split = split_url(entry["url"])
            rows.append(
                {
                    "seq": index,
                    "method": entry["method"],
                    "host": split["host"],
                    "path": split["path"],
                    "param_names": split["param_names"],
                    "secret_params_present": split["secret_params_present"],
                    "post_field_names": entry["post_field_names"],
                    "status": entry["status"],
                    "content_type": entry["content_type"],
                    "resource_type": entry["resource_type"],
                    "initiator": entry["initiator"],
                }
            )
        return rows


#: Endpoints whose *response shape* is part of the finding. Every other body is
#: left unread. The skeleton reduction means a token in one of these responses
#: is reported as ``str(len=812)`` and never as its value.
#:
#: Matched as substrings, not prefixes: the bundle writes ``/api/v1/...`` and the
#: axios interceptor prepends ``/uc``, so the path actually observed on the wire
#: is ``/uc/api/v1/...``. A prefix test would silently miss every one of them.
SKELETON_PATHS = (
    "oauth/checkOrAuthorize",
    "oauth/client/",
)


def multipart_field_names(post_data: str) -> list[str]:
    """Field names out of a ``multipart/form-data`` or urlencoded body.

    The SPA builds a ``FormData`` and hands it to axios, so the wire body is
    multipart and ``parse_qsl`` sees nothing. Only the ``name="..."`` tokens are
    extracted; the values -- which carry ``state`` -- are discarded here and
    never stored.
    """
    if not post_data:
        return []
    names = re.findall(r'name="([^"]+)"', post_data)
    if names:
        return sorted(set(names))
    if "=" in post_data and "\r\n" not in post_data:
        return sorted({name for name, _v in parse_qsl(post_data, keep_blank_values=True)})
    return []



def drain(conn: CdpConnection, capture: Capture, session_id: str, *, seconds: float) -> None:
    """Read events off the socket for a bounded window.

    ``poll_event`` returns ``None`` on a quiet socket, which is the normal state
    between page requests, so the loop is driven by its own deadline rather than
    by the socket's. Bodies marked interesting by ``on_response`` are fetched on
    the next turn of this loop, while they are still resident.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        event = conn.poll_event(timeout=min(1.0, max(0.05, deadline - time.monotonic())))
        if isinstance(event, Mapping):
            method = str(event.get("method") or "")
            params = event.get("params") or {}
            if method == "Network.requestWillBeSent":
                capture.on_request(params)
            elif method == "Network.responseReceived":
                capture.on_response(params)
            elif method == "Runtime.consoleAPICalled":
                capture.console.append(str(params.get("type") or ""))

        for request_id, path in list(capture.wanted_bodies.items()):
            if request_id in capture.bodies:
                continue
            body = get_body(conn, session_id, request_id)
            if body is None:
                capture.bodies[request_id] = "<evicted>"
                continue
            try:
                capture.bodies[request_id] = skeleton(json.loads(body))
            except ValueError:
                capture.bodies[request_id] = f"<non-JSON {len(body)} bytes>"


def location(conn: CdpConnection, session_id: str) -> str:
    try:
        result = conn.call(
            "Runtime.evaluate",
            {"expression": "location.href", "returnByValue": True},
            session_id=session_id,
            timeout=15.0,
        )
    except Exception:  # noqa: BLE001
        return ""
    value = (result.get("result") or {}).get("value")
    return value if isinstance(value, str) else ""


def get_body(conn: CdpConnection, session_id: str, request_id: str) -> str | None:
    """One response body, for skeleton reduction only.

    The caller must reduce it before printing. This returns ``None`` rather than
    raising when the body has already been evicted, which is normal for a
    request captured minutes earlier.
    """
    try:
        result = conn.call(
            "Network.getResponseBody",
            {"requestId": request_id},
            session_id=session_id,
            timeout=15.0,
        )
    except Exception:  # noqa: BLE001
        return None
    body = result.get("body")
    return body if isinstance(body, str) else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9333)
    parser.add_argument("--wait", type=float, default=25.0)
    parser.add_argument("--json", default="")
    parser.add_argument(
        "--url", default=AUTHORIZE, help="entry point to navigate to (GET only)"
    )
    args = parser.parse_args()

    endpoint = discover_cdp_endpoint(f"http://127.0.0.1:{args.port}", probe=True)
    browser_ws = endpoint.browser_ws_url()
    if not browser_ws:
        print("no browser-level WebSocket on that port")
        return 1

    capture = Capture()
    out: list[str] = []

    def emit(line: str = "") -> None:
        print(line)
        out.append(line)

    with CdpConnection(browser_ws, timeout=30.0) as conn:
        version = conn.call("Browser.getVersion", {}, timeout=15.0)
        emit(f"browser   : {version.get('product')}")

        cookies_before = conn.call("Storage.getCookies", {}, timeout=15.0).get("cookies", [])
        emit(f"cookies before: {len(cookies_before)}")

        created = conn.call(
            "Target.createTarget", {"url": "about:blank", "background": True}, timeout=15.0
        )
        target_id = str(created.get("targetId") or "")
        attached = conn.call(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}, timeout=15.0
        )
        session_id = str(attached.get("sessionId") or "")
        emit(f"tab       : {target_id[:12]}... session {session_id[:12]}...")

        for domain in ("Network.enable", "Page.enable", "Runtime.enable"):
            try:
                conn.call(domain, {}, session_id=session_id, timeout=15.0)
            except Exception as exc:  # noqa: BLE001
                emit(f"{domain}: {type(exc).__name__}")

        emit(f"navigating: {urlsplit(args.url).netloc}{urlsplit(args.url).path}")
        conn.call("Page.navigate", {"url": args.url}, session_id=session_id, timeout=20.0)
        drain(conn, capture, session_id, seconds=max(5.0, args.wait))

        final = location(conn, session_id)
        emit(f"settled on: {urlsplit(final).netloc}{urlsplit(final).path}")

        rows = capture.report()
        emit("")
        emit(f"{'#':>3} {'METHOD':5} {'HOST':26} {'PATH':46} {'STATUS':>6}  CT")
        for row in rows:
            emit(
                f"{row['seq']:>3} {row['method']:5} {row['host'][:26]:26} "
                f"{row['path'][:46]:46} {str(row['status']):>6}  "
                f"{row['content_type'][:30]}"
            )
            if row["secret_params_present"]:
                emit(f"      (query carries: {', '.join(row['secret_params_present'])})")
            if row["post_field_names"]:
                emit(f"      (POST fields: {', '.join(row['post_field_names'])})")

        if capture.redirects:
            emit("")
            emit("redirects (previous hop -> next hop):")
            for hop in capture.redirects:
                emit(
                    f"  {hop['status']} {hop['from']['host']}{hop['from']['path']}"
                    f"  ->  {hop['to']['host']}{hop['to']['path']}"
                )
                if hop["to"]["secret_params_present"]:
                    emit(f"      (next hop query carries: {', '.join(hop['to']['secret_params_present'])})")

        # Response *shapes* for the handful of endpoints whose contract matters.
        # Reduced to key -> type/length; no value is stored or printed.
        for request_id, path in capture.wanted_bodies.items():
            body = capture.bodies.get(request_id, "<not captured>")
            emit(f"\nbody {path}:")
            emit(f"  {json.dumps(body, ensure_ascii=False)}")

        cookies_after = conn.call("Storage.getCookies", {}, timeout=15.0).get("cookies", [])
        before_names = {(c.get("name"), c.get("domain")) for c in cookies_before}
        new = [c for c in cookies_after if (c.get("name"), c.get("domain")) not in before_names]
        emit("")
        emit(f"cookies after : {len(cookies_after)} (+{len(new)} new)")
        for cookie in new:
            emit(
                f"  new cookie {cookie.get('name')} @ {cookie.get('domain')} "
                f"{fingerprint(str(cookie.get('value') or ''))}"
            )

        try:
            conn.call("Target.closeTarget", {"targetId": target_id}, timeout=10.0)
        except Exception:  # noqa: BLE001
            pass

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump({"requests": rows}, handle, indent=2, ensure_ascii=False)
        emit(f"written: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
