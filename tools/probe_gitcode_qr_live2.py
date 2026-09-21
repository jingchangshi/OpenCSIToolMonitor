"""Read-only follow-up probe: CORS preflight (OPTIONS) + GET polls only.

STRICTLY NO POST/PUT/DELETE is issued by this script: creating a QR scene is a
state mutation, so the QR-create call is left unprobed at the wire level and is
documented from static bundle evidence instead.

Never prints cookie/token/secret values.
"""
from __future__ import annotations

import http.cookiejar
import json
import re
import sys
import urllib.error
import urllib.request
from typing import Any

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)
BASE = "https://web-api.gitcode.com"

_SECRET_KV = re.compile(
    r"(?i)\b(access_token|refresh_token|token|ticket|secret|password|session|sessionid|"
    r"scene_id|captcha_id|signature|sig|nonce)\b(\"?\s*[:=]\s*\"?)([^\"'&\s,}]{4,})"
)
_SECRET_Q = re.compile(
    r"(?i)\b(state|code|ticket|token|scene_id|client_id|nonce|secret|signature|sig)=([^&\s\"']+)"
)


def mask(t: str) -> str:
    t = _SECRET_Q.sub(lambda m: f"{m.group(1)}=<masked:{len(m.group(2))}>", t)
    return _SECRET_KV.sub(lambda m: f"{m.group(1)}{m.group(2)}<masked:{len(m.group(3))}>", t)


def call(url: str, *, method: str = "GET", headers: dict[str, str] | None = None, body: bytes | None = None):
    op = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    h = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://gitcode.com/",
        "Origin": "https://gitcode.com",
    }
    if headers:
        h.update(headers)
    r = urllib.request.Request(url, headers=h, data=body, method=method)
    try:
        with op.open(r, timeout=30) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:
        b = b""
        try:
            b = exc.read()
        except Exception:
            pass
        hd = dict(exc.headers.items()) if exc.headers else {}
        c = exc.code
        exc.close()
        return c, hd, b
    except Exception as exc:  # noqa: BLE001
        return 0, {}, f"{type(exc).__name__}: {exc}".encode()


def main() -> int:
    print("Read-only follow-up probe")
    print("=" * 74)

    url = f"{BASE}/uc/api/v1/qrcode/wechat_mini_program"
    st, hd, b = call(url, method="OPTIONS", headers={
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "x-source,content-type",
    })
    print(f"\n[A] OPTIONS {url}")
    print(f"    -> {st}  ({len(b)} B)")
    for k, v in hd.items():
        if k.lower().startswith(("access-control", "allow", "vary")):
            print(f"    {k}: {v}")

    # Invalid platform, but still a POST -> NOT issued (state-mutating verb).
    print("\n[B] POST /uc/api/v1/qrcode/<platform>  -- SKIPPED BY DESIGN")
    print("    Reason: POST creates a QR scene (state mutation). Documented from")
    print("    static bundle evidence only. Verdict for X-Source gating is therefore")
    print("    'not wire-verified' and is reported as such.")

    # Poll endpoint without cookies, several bogus scene ids, to map state strings.
    print("\n[D] GET status-poll, several bogus scene_ids (no cookies at all)")
    for sid in ("PROBE0001", "00000000-0000-0000-0000-000000000000", "a"):
        u = f"{BASE}/uc/api/v1/qrcode/wechat_mini_program?scene_id={sid}"
        st4, _, b4 = call(u)
        print(f"    scene_id=<{len(sid)} chars> -> {st4}  {mask(b4.decode('utf-8','replace'))[:200]}")

    print("\nNOTE: POST /uc/api/v1/qrcode/wechat_mini_program (real platform) was NOT issued.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
