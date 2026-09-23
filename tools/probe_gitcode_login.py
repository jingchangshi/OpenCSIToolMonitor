"""Read-only probe of the GitCode authorize page reached from openCsiTool OAuth.

Captures the HTML/JS the login page loads and lists candidate QR endpoints.
Prints no cookie or token values.

Run:  python tools/probe_gitcode_login.py
"""
#: labels: LIVE, NETWORK, GET_ONLY

from __future__ import annotations

import re
import sys
import urllib.error
import urllib.request
from typing import Any

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"

_SECRETY = re.compile(r"(?i)(token|code|state|ticket|nonce|secret|key|password|session|auth)=([^&\s]+)")
_SCRIPT_SRC = re.compile(r'<script[^>]+src="([^"]+)"', re.I)
_QR_WORDS = re.compile(r"(?i)(qrcode|qr_code|qr-code|/qr|wechat|scan|poll|uuid|ticket)")


def mask(url: str) -> str:
    return _SECRETY.sub(lambda m: f"{m.group(1)}=<masked:{len(m.group(2))}>", url)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def get(url: str, *, cookie: str | None = None, timeout: float = 25.0, follow: bool = True):
    handlers: list[Any] = [urllib.request.ProxyHandler({})]
    if not follow:
        handlers.append(NoRedirect())
    opener = urllib.request.build_opener(*handlers)
    headers = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), resp.read(), resp.geturl()
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read()
        except Exception:
            pass
        hdrs = dict(exc.headers.items()) if exc.headers else {}
        exc.close()
        return exc.code, hdrs, body, url
    except Exception as exc:  # noqa: BLE001
        return 0, {}, f"{type(exc).__name__}: {exc}".encode(), url


def main() -> int:
    entry = (
        "https://opencsitool.com/opencsitool/rest/v1/oauth2/authorization/gitcode"
        "?redirect=%2FmyTools"
    )
    status, headers, _, _ = get(entry, follow=False)
    loc = headers.get("Location", "")
    print(f"[1] oauth entry -> {status}")
    print(f"    authorize URL: {mask(loc)}")
    if not loc:
        return 1

    status, headers, body, final = get(loc, follow=False)
    print(f"\n[2] authorize page -> {status} ({len(body)} bytes) {mask(final)}")
    setc = headers.get("Set-Cookie") or headers.get("set-cookie") or ""
    print(f"    set-cookie names: {[c.split('=',1)[0].strip() for c in setc.split(',') if '=' in c]}")
    html = body.decode("utf-8", "replace")

    scripts = _SCRIPT_SRC.findall(html)
    print(f"\n[3] scripts ({len(scripts)}):")
    for s in scripts:
        print(f"    {mask(s)}")

    inline = re.findall(r"<script(?![^>]*src)[^>]*>(.*?)</script>", html, re.S | re.I)
    print(f"\n[4] inline scripts: {len(inline)}")
    for i, blob in enumerate(inline):
        hits = sorted(set(_QR_WORDS.findall(blob)))
        if hits:
            print(f"    inline#{i} ({len(blob)} B) keywords={hits}")
        for m in re.finditer(r"[\"'](/[A-Za-z0-9_\-./]*(?:qr|login|scan|wechat)[A-Za-z0-9_\-./]*)[\"']", blob, re.I):
            print(f"      path candidate: {m.group(1)}")

    print("\n[5] HTML form/action + hidden inputs:")
    for m in re.finditer(r"<form[^>]*>", html, re.I):
        print(f"    {mask(m.group(0)[:200])}")
    for m in re.finditer(r'<input[^>]*type="hidden"[^>]*>', html, re.I):
        print(f"    {mask(m.group(0)[:200])}")

    print("\n[6] body text sample:")
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    print(f"    {text[:600]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
