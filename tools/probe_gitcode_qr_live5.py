"""Read-only GET/OPTIONS probe: confirm the /uc prefix rewrite on the login path.

No state-mutating verb. No cookie/token values printed.
"""
from __future__ import annotations

import http.cookiejar
import sys
import urllib.error
import urllib.request

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)

URLS = [
    "https://web-api.gitcode.com/uc/api/v1/user/oauth/login/qrcode/wechat_mini_program?scene_id=PROBE",
    "https://web-api.gitcode.com/uc/api/v1/oauth/checkOrAuthorize",
]


def call(url: str, method: str = "GET"):
    op = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
    )
    h = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://gitcode.com/",
        "Origin": "https://gitcode.com",
    }
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


def main() -> int:
    print("Confirm /uc prefix rewrite (GET/OPTIONS only)")
    print("=" * 74)
    for u in URLS:
        for m in ("GET", "OPTIONS"):
            st, hd, b = call(u, m)
            print(f"\n[{m}] {u}")
            print(f"    -> {st}  ({len(b)} B)  ct={hd.get('Content-Type')}")
            print(f"    body: {b.decode('utf-8','replace')[:240]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
