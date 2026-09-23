"""Read-only GET/OPTIONS probe: full response headers on the 401 login path.

No state-mutating verb is issued. No cookie/token values are printed.
"""
#: labels: LIVE, NETWORK, GET_ONLY
from __future__ import annotations

import http.cookiejar
import sys
import urllib.error
import urllib.request

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)
URL = "https://web-api.gitcode.com/api/v1/user/oauth/login/qrcode/wechat_mini_program?scene_id=PROBE"


def call(method: str, headers: dict[str, str] | None = None):
    op = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
    )
    h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*"}
    if headers:
        h.update(headers)
    r = urllib.request.Request(URL, headers=h, method=method)
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


VARIANTS = [
    ("GET", {}),
    ("GET", {"Referer": "https://gitcode.com/", "Origin": "https://gitcode.com"}),
    ("GET", {"X-Source": "toolbar_login"}),
    ("OPTIONS", {"Access-Control-Request-Method": "POST"}),
]


def main() -> int:
    print("Full-header probe on the login-completion path (GET/OPTIONS only)")
    print("=" * 74)
    for method, extra in VARIANTS:
        st, hd, b = call(method, extra)
        print(f"\n[{method}] extra={list(extra) or 'none'}")
        print(f"    -> {st}  ({len(b)} B)")
        for k in sorted(hd, key=str.lower):
            if k.lower() == "set-cookie":
                print(f"    {k}: <present, names only: n/a>")
                continue
            print(f"    {k}: {hd[k]}")
        print(f"    body: {b.decode('utf-8','replace')[:220]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
