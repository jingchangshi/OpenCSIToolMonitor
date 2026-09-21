"""Read-only protocol probe for the openCsiTool -> GitCode OAuth entry point.

Prints only statuses, header *names* of interest and URLs (with query values
masked). It never prints a cookie or token value.

Run:  python tools/probe_oauth.py
"""

from __future__ import annotations

import re
import sys
import urllib.error
import urllib.request
from typing import Any

BASE = "https://opencsitool.com"
OAUTH = "/opencsitool/rest/v1/oauth2/authorization/gitcode"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"

_SECRETY = re.compile(
    r"(?i)(token|code|state|ticket|nonce|secret|key|password|session|auth)=([^&\s]+)"
)


def mask(url: str) -> str:
    return _SECRETY.sub(lambda m: f"{m.group(1)}=<masked:{len(m.group(2))}>", url)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


USE_PROXY = "--proxy" in sys.argv


def fetch(url: str, *, follow: bool, cookie: str | None = None, timeout: float = 20.0):
    handlers: list[Any] = [] if follow else [NoRedirect()]
    if not USE_PROXY:
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    headers = {"User-Agent": UA, "Accept": "text/html,application/json,*/*"}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read(4000)
            return resp.status, dict(resp.headers.items()), body, resp.geturl()
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read(4000)
        except Exception:
            pass
        loc = exc.headers.get("Location") if exc.headers else None
        out = exc.close()
        return exc.code, {"Location": loc} if loc else {}, body, url


def show(label: str, status, headers, body, final):
    print(f"\n=== {label} ===")
    print(f"status  : {status}")
    print(f"final   : {mask(final)}")
    loc = headers.get("Location")
    if loc:
        print(f"location: {mask(loc)}")
    sc = headers.get("Set-Cookie") or headers.get("set-cookie")
    if sc:
        names = [c.split("=", 1)[0].strip() for c in sc.split(",") if "=" in c]
        print(f"set-cookie names: {names}")
    ct = headers.get("Content-Type") or headers.get("content-type") or ""
    print(f"content-type: {ct}")
    text = body.decode("utf-8", "replace")
    print(f"body[{len(body)}]: {mask(text[:300]).replace(chr(10), ' ')}")


def main() -> int:
    url = f"{BASE}{OAUTH}?redirect=%2FmyTools"
    show("A. oauth entry (no redirect follow)", *fetch(url, follow=False))

    status, headers, body, final = fetch(url, follow=True)
    show("B. oauth entry (follow)", status, headers, body, final)

    # Does the API answer a bare request without cookies? (login-state probe)
    show(
        "C. getUserInfo without cookie",
        *fetch(f"{BASE}/opencsitool/rest/v1/user/getUserInfo", follow=False),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
