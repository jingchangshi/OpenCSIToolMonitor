"""Read-only GET-only probe round 3: parameter-requirement discovery.

Every call is GET or OPTIONS. No state-mutating verb is issued.
"""
#: labels: LIVE, NETWORK, GET_ONLY
from __future__ import annotations

import http.cookiejar
import json
import re
import sys
import urllib.error
import urllib.request

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)
BASE = "https://web-api.gitcode.com"

_SECRET = re.compile(
    r"(?i)\b(access_token|refresh_token|token|ticket|secret|password|session|sessionid|"
    r"scene_id|captcha_id|signature|sig|nonce|state|code)\b(\"?\s*[:=]\s*\"?)([^\"'&\s,}]{4,})"
)


def mask(t: str) -> str:
    return _SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}<masked:{len(m.group(3))}>", t)


def call(url: str, method: str = "GET", headers: dict[str, str] | None = None):
    op = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
    )
    h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
         "Accept-Language": "zh-CN,zh;q=0.9", "Referer": "https://gitcode.com/"}
    if headers:
        h.update(headers)
    r = urllib.request.Request(url, headers=h, method=method)
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


CASES = [
    ("GET", f"{BASE}/uc/api/v1/qrcode/wechat_mini_program", "no scene_id at all"),
    ("GET", f"{BASE}/uc/api/v1/qrcode/wechat_mini_program?scene_id=", "empty scene_id"),
    ("GET", f"{BASE}/uc/api/v1/qrcode/", "no platform, no scene_id"),
    ("OPTIONS", f"{BASE}/api/v1/user/oauth/login/qrcode/wechat_mini_program", "preflight login path"),
    ("GET", f"{BASE}/api/v1/user/oauth/login/qrcode/wechat_mini_program", "login path, no scene_id"),
    ("GET", f"{BASE}/uc/api/v1/qrcode/wechat_mini_program?scene_id=PROBE&platform=x", "extra param"),
]


def main() -> int:
    print("Read-only GET/OPTIONS parameter-requirement probe")
    print("=" * 74)
    for method, url, note in CASES:
        st, hd, b = call(url, method)
        txt = b.decode("utf-8", "replace")
        print(f"\n[{method}] {url}\n    ({note})")
        print(f"    -> {st}  ({len(b)} B)  ct={hd.get('Content-Type')}")
        if txt:
            try:
                j = json.loads(txt)
                print(f"    json keys: {list(j) if isinstance(j, dict) else type(j).__name__}")
                print(f"    body (masked): {mask(json.dumps(j, ensure_ascii=False))[:300]}")
            except Exception:
                print(f"    body (masked): {mask(re.sub(r'\\s+', ' ', txt))[:300]}")
    print("\nNOTE: no POST/PUT/DELETE issued. No secret values printed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
