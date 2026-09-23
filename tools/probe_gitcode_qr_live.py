"""Read-only live probe of the GitCode QR endpoints.

GET only. Never POSTs/PUTs/DELETEs. Never prints cookie/token/secret VALUES.
Run: python tools/probe_gitcode_qr_live.py
"""
#: labels: LIVE, NETWORK, GET_ONLY
from __future__ import annotations

import http.cookiejar
import json
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)
ENTRY = (
    "https://opencsitool.com/opencsitool/rest/v1/oauth2/authorization/gitcode"
    "?redirect=%2FmyTools"
)

_SECRET_Q = re.compile(
    r"(?i)\b(state|code|ticket|token|scene_id|client_id|access_token|refresh_token|"
    r"nonce|secret|session|signature|sig)=([^&\s\"']+)"
)
_SECRET_KV = re.compile(
    r"(?i)\b(access_token|refresh_token|token|ticket|secret|password|session|sessionid|"
    r"scene_id|signature|sig|nonce)\b(\"?\s*[:=]\s*\"?)([^\"'&\s,}]{4,})"
)


def mask(text: str) -> str:
    text = _SECRET_Q.sub(lambda m: f"{m.group(1)}=<masked:{len(m.group(2))}>", text)
    text = _SECRET_KV.sub(lambda m: f"{m.group(1)}{m.group(2)}<masked:{len(m.group(3))}>", text)
    return text


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def build_opener(jar: http.cookiejar.CookieJar | None = None, follow: bool = False):
    handlers: list[Any] = [urllib.request.ProxyHandler({})]
    if jar is not None:
        handlers.append(urllib.request.HTTPCookieProcessor(jar))
    if not follow:
        handlers.append(NoRedirect())
    ctx = ssl.create_default_context()
    handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


def req(
    opener, url: str, *, method: str = "GET", extra: dict[str, str] | None = None,
    timeout: float = 30.0,
):
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://gitcode.com/",
    }
    if extra:
        headers.update(extra)
    r = urllib.request.Request(url, headers=headers, method=method)
    try:
        with opener.open(r, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), resp.read(), resp.geturl()
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read()
        except Exception:
            pass
        hdrs = dict(exc.headers.items()) if exc.headers else {}
        code = exc.code
        exc.close()
        return code, hdrs, body, url
    except Exception as exc:  # noqa: BLE001
        return 0, {}, f"{type(exc).__name__}: {exc}".encode(), url


def shape(obj: Any, depth: int = 0) -> Any:
    """Replace leaf values with a type/shape descriptor; keeps keys + small enums."""
    if depth > 6:
        return "<deep>"
    if isinstance(obj, dict):
        return {k: shape(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [shape(obj[0], depth + 1)] if obj else []
    if isinstance(obj, bool):
        return f"<bool:{obj}>"
    if isinstance(obj, int):
        return f"<int:{obj}>" if abs(obj) < 100000 else "<int>"
    if isinstance(obj, float):
        return "<float>"
    if isinstance(obj, str):
        if len(obj) <= 24 and re.fullmatch(r"[A-Z_0-9]+", obj):
            return f"<enum:{obj}>"
        if obj.startswith("data:"):
            return f"<data-uri:{obj.split(';')[0]}:{len(obj)}B>"
        if obj.startswith("http"):
            return f"<url:len={len(obj)}>"
        return f"<str:len={len(obj)}>"
    if obj is None:
        return None
    return f"<{type(obj).__name__}>"


def main() -> int:
    out: list[str] = []

    def emit(s: str = "") -> None:
        print(s)
        out.append(s)

    emit("GitCode QR protocol — read-only live probe (GET only)")
    emit("=" * 74)

    # ---------------------------------------------------------------- step 1
    jar = http.cookiejar.CookieJar()
    op = build_opener(jar, follow=False)
    st, hdrs, _, _ = req(op, ENTRY)
    loc = hdrs.get("Location", "")
    emit(f"\n[1] GET {mask(ENTRY)}")
    emit(f"    -> {st}")
    emit(f"    Location: {mask(loc)}")

    # ---------------------------------------------------------------- step 2
    if loc:
        op2 = build_opener(jar, follow=False)
        st2, hdrs2, body2, _ = req(op2, loc)
        emit(f"\n[2] GET authorize page (no-follow) -> {st2}  ({len(body2)} B)")
        sc = [h for h in hdrs2 if h.lower() == "set-cookie"]
        emit(f"    Set-Cookie header count: {len(sc)}")
        names = sorted({c.name for c in jar})
        emit(f"    cookie NAMES set: {names}")
        for h in hdrs2:
            emit(f"    hdr: {h}")
        html = body2.decode("utf-8", "replace")
        srcs = re.findall(r'<script[^>]+src="([^"]+)"', html, re.I)
        emit(f"    script srcs ({len(srcs)}):")
        for s in srcs:
            emit(f"      {mask(s)}")
        # Hoisted out of the f-string: Python before 3.12 rejects a backslash
        # inside an f-string *expression*, and `r'\s+'` is one. Not a style
        # choice -- on 3.10 this file failed to parse at all, so the probe could
        # not even be inspected for its safety posture.
        collapsed = re.sub(r"\s+", " ", html)
        emit(f"    html sample: {mask(collapsed)[:400]}")

    # ---------------------------------------------------------------- step 3
    emit("\n[3] GET status-poll with BOGUS scene_id (harmless, no state change)")
    for host in ("https://web-api.gitcode.com", "https://gitcode.com"):
        url = f"{host}/uc/api/v1/qrcode/wechat_mini_program?scene_id=PROBE0000BOGUS"
        opx = build_opener(http.cookiejar.CookieJar(), follow=False)
        st3, hdrs3, body3, _ = req(opx, url)
        emit(f"\n    GET {url}")
        emit(f"      -> {st3}  ({len(body3)} B)")
        for h in ("content-type", "server", "x-request-id", "access-control-allow-origin"):
            v = next((v for k, v in hdrs3.items() if k.lower() == h), None)
            if v:
                emit(f"      {h}: {v}")
        txt = body3.decode("utf-8", "replace")
        try:
            j = json.loads(txt)
            emit(f"      body shape: {json.dumps(shape(j), ensure_ascii=False)}")
        except Exception:
            emit(f"      body (masked, 400 chars): {mask(re.sub(chr(92)+'s+', ' ', txt))[:400]}")

    # ---------------------------------------------------------------- step 4
    emit("\n[4] GET with a bogus scene_id on the OTHER poll path (401 vs 404 check)")
    for url in (
        "https://web-api.gitcode.com/api/v1/user/oauth/login/qrcode/wechat_mini_program"
        "?scene_id=PROBE0000BOGUS",
        "https://web-api.gitcode.com/uc/api/v1/captcha/config",
    ):
        opx = build_opener(http.cookiejar.CookieJar(), follow=False)
        st4, _, body4, _ = req(opx, url)
        emit(f"\n    GET {url}")
        emit(f"      -> {st4}  ({len(body4)} B)")
        txt = body4.decode("utf-8", "replace")
        try:
            emit(f"      body shape: {json.dumps(shape(json.loads(txt)), ensure_ascii=False)}")
        except Exception:
            emit(f"      body (masked, 300): {mask(re.sub(chr(92)+'s+', ' ', txt))[:300]}")

    emit("\nNOTE: POST /uc/api/v1/qrcode/wechat_mini_program was NOT issued (would mutate state).")
    emit("NOTE: no cookie/token/secret values were printed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
