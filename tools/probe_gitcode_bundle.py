"""Download the GitCode login bundle and extract QR/login protocol evidence.

Read-only. Writes findings to docs/_gitcode_qr_evidence.txt and prints a summary.
Never prints cookie or token values.

Run:  python tools/probe_gitcode_bundle.py
"""

from __future__ import annotations

import re
import sys
import urllib.request
from pathlib import Path
from typing import Any

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
OUT = Path("docs/_gitcode_qr_evidence.txt")

_ENTRY = (
    "https://opencsitool.com/opencsitool/rest/v1/oauth2/authorization/gitcode"
    "?redirect=%2FmyTools"
)
_BUNDLE = "https://cdn-static.gitcode.com/assets/index-a97e2b06.js"

#: Endpoint-ish strings that mention QR / login / scan / wechat / poll.
PATH_RE = re.compile(
    r"[\"'`](/[A-Za-z0-9_\-./{}$]*(?:qr|login|scan|wechat|wx|poll|ticket|uuid)"
    r"[A-Za-z0-9_\-./{}$]*)[\"'`]",
    re.I,
)
#: Absolute URLs to gitcode hosts carrying those words.
URL_RE = re.compile(
    r"[\"'`](https?://[A-Za-z0-9_\-.]*gitcode[A-Za-z0-9_\-.]*/[^\"'`\s]*"
    r"(?:qr|login|scan|wechat|poll|ticket|uuid)[^\"'`\s]*)[\"'`]",
    re.I,
)
STATE_RE = re.compile(
    r"(?i)(WAITING|SCANNED|CONFIRMED|EXPIRED|SUCCESS|CANCEL|TIMEOUT|PENDING|"
    r"NOT_SCAN|ALREADY_SCAN|AUTHORIZED)"
)


def fetch(url: str, *, cookie: str | None = None, timeout: float = 40.0) -> tuple[int, str]:
    handlers: list[Any] = [urllib.request.ProxyHandler({})]
    opener = urllib.request.build_opener(*handlers)
    headers = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"}
    if cookie:
        headers["Cookie"] = cookie
    try:
        with opener.open(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def main() -> int:
    lines: list[str] = []

    def emit(s: str = "") -> None:
        print(s)
        lines.append(s)

    emit("GitCode QR / login protocol evidence (static bundle analysis)")
    emit("=" * 70)

    status, html = fetch(_ENTRY)
    emit(f"\n[oauth entry] status={status}")
    loc = ""
    for m in re.finditer(r'Location:\s*(\S+)', html, re.I):
        loc = m.group(1)
    emit(f"[note] entry fetch returned a page, not a redirect header (urllib followed it)")

    emit(f"\n[bundle] {_BUNDLE}")
    status, js = fetch(_BUNDLE)
    emit(f"status={status} bytes={len(js)}")
    if status != 200:
        OUT.write_text("\n".join(lines), encoding="utf-8")
        return 1

    paths = sorted(set(PATH_RE.findall(js)))
    emit(f"\n[candidate API paths] {len(paths)}")
    for p in paths:
        emit(f"  {p}")

    urls = sorted(set(URL_RE.findall(js)))
    emit(f"\n[absolute gitcode URLs] {len(urls)}")
    for u in urls:
        emit(f"  {u}")

    # Context around each QR path: 200 chars either side tells us the method.
    emit("\n[context around qr endpoints]")
    for p in paths:
        for m in re.finditer(re.escape(p), js):
            ctx = js[max(0, m.start() - 220) : m.end() + 220]
            ctx = re.sub(r"\s+", " ", ctx)
            emit(f"  --- {p}")
            emit(f"      {ctx[:420]}")
            break

    states = sorted(set(s.upper() for s in STATE_RE.findall(js)))
    emit(f"\n[state-like tokens] {states}")

    # Which HTTP verbs appear near qr paths.
    verbs = sorted(set(re.findall(r"\b(GET|POST|PUT|DELETE)\b", js)))
    emit(f"[verbs present in bundle] {verbs}")

    OUT.write_text("\n".join(lines), encoding="utf-8")
    emit(f"\nwritten: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
