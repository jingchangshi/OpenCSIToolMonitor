"""Static analysis of the GitCode ``/oauth/authorize`` single-page app.

The previous investigation proved that a plain ``urllib`` client following
redirects only ever receives the SPA shell. That proved the *client* fails; it
said nothing about whether the backend calls the SPA makes can be reproduced.
This probe answers the second question by reading the SPA's own JavaScript.

What it does, and nothing else:

1. ``GET`` the GitCode login shell and the openCsiTool OAuth entry point, and
   record the ``<script src=...>`` tags (no query strings are printed).
2. Download each asset to a scratch directory *outside* the repository.
3. Recover the ``/api/...`` paths the authorize page calls, together with the
   HTTP method, the parameter names and the headers, by reading the call sites.
4. Resolve the minified import aliases the authorize chunk uses, so a path
   printed here can be attributed to a named function rather than to a guess.
5. Report whether ``.js.map`` source maps are published.

It never authenticates, never POSTs, never submits the consent form, and never
prints a cookie or token value. Every request is a ``GET`` of a public asset.

LIVE / NETWORK / read-only / NO_AUTH
------------------------------------
LIVE and NETWORK: it issues GETs to gitcode.com and opencsitool.com. read-only:
it touches no authenticated endpoint and no business API, and it never POSTs.
NO_AUTH: it neither authenticates nor approves anything, so it has no
authentication side effect.

Usage
-----
    python tools/probe_oauth_spa_static.py [--scratch DIR] [--json OUT]
"""
#: labels: LIVE, NETWORK, GET_ONLY

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)

ENTRY = (
    "https://opencsitool.com/opencsitool/rest/v1/oauth2/authorization/gitcode"
    "?redirect=%2FmyTools"
)
LOGIN = "https://gitcode.com/login"

#: The chunks the authorize route pulls in, read out of the main bundle's route
#: table. Kept explicit so the download list is auditable rather than discovered
#: by a crawler that could wander onto a business endpoint.
AUTHORIZE_CHUNKS = (
    "authorize-54c99276.js",
    "index.vue_vue_type_script_setup_true_lang-4133cdcb.js",
    "index-0ccfd32c.js",
    "_plugin-vue_export-helper-1b428a4d.js",
    "index.vue_vue_type_style_index_0_lang-0df246bc.js",
    "index-b3190741.js",
    "login-8974df96.js",
)

CDN = "https://cdn-static.gitcode.com/assets/"

#: ``Bb({url:"/api/...",method:"...",...})`` -- the single API wrapper every
#: request in this bundle funnels through.
CALL_RE = re.compile(
    r"Bb\(\{\s*url:\s*(?P<url>[\"'`][^\"'`]+[\"'`]|`[^`]*`)"
    r"(?P<rest>.{0,400}?)\}\s*(?:,\s*\{[^}]*\})?\s*\)",
    re.S,
)

SCRIPT_SRC_RE = re.compile(r'<script[^>]*\bsrc="([^"]+)"', re.I)
MODULEPRELOAD_RE = re.compile(r'modulepreload[^>]*\bhref="([^"]+)"', re.I)


def fingerprint(text: str) -> str:
    """``len=…, sha256=<first 12>`` -- comparable without being disclosive."""
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]
    return f"len={len(text):<6d} sha256={digest}"


def strip_query(url: str) -> str:
    """Host + path only. A query string can carry ``code``/``state``."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def fetch(url: str, *, timeout: float = 60.0) -> tuple[int, str]:
    """GET one public asset. Never raises; returns ``(status, body)``."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "*/*"}
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:  # noqa: F821 - imported below
        return exc.code, ""
    except Exception as exc:  # noqa: BLE001 - a probe must not raise
        return 0, f"{type(exc).__name__}: {exc}"


def script_tags(html: str) -> list[str]:
    found = SCRIPT_SRC_RE.findall(html) + MODULEPRELOAD_RE.findall(html)
    seen: list[str] = []
    for url in found:
        if url not in seen:
            seen.append(url)
    return seen


def recover_calls(bundle: str) -> list[dict[str, Any]]:
    """Every ``Bb({url:..., method:..., data:..., params:..., headers:...})``."""
    calls: list[dict[str, Any]] = []
    for match in CALL_RE.finditer(bundle):
        raw_url = match.group("url").strip("\"'`")
        rest = match.group("rest")
        method = re.search(r'method:\s*"([a-zA-Z]+)"', rest)
        params = re.search(r"params:\s*\{([^}]*)\}", rest, re.S)
        data = re.search(r"data:\s*([A-Za-z0-9_$]+|\{[^}]*\})", rest, re.S)
        headers = re.search(r"headers:\s*\{([^}]*)\}", rest, re.S)
        calls.append(
            {
                "url": raw_url,
                "method": (method.group(1).upper() if method else "GET"),
                "params": sorted(set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*:", params.group(1))))
                if params
                else [],
                "data": data.group(1)[:120] if data else None,
                "headers": sorted(set(re.findall(r"([\"'A-Za-z][^\"':]*)\s*:", headers.group(1))))
                if headers
                else [],
                "offset": match.start(),
            }
        )
    return calls


def resolve_exports(bundle: str, names: set[str]) -> dict[str, str]:
    """Map a minified export alias back to its internal symbol.

    The authorize chunk imports ``{j9 as P}`` and calls ``P(i)``; the export map
    at the end of the main bundle says ``Gv as j9``. Without this step, naming
    the function behind a URL would be a guess.
    """
    blocks = list(re.finditer(r"export\{", bundle))
    if not blocks:
        return {}
    tail = bundle[blocks[-1].start() :]
    mapping: dict[str, str] = {}
    for internal, alias in re.findall(r"([A-Za-z0-9_$]+) as ([A-Za-z0-9_$]+)", tail):
        if alias in names:
            mapping[alias] = internal
    return mapping


def definition_of(bundle: str, symbol: str) -> str:
    """The ``function symbol(...)`` body, so a URL can be tied to its caller."""
    match = re.search(rf"function {re.escape(symbol)}\(", bundle)
    if not match:
        return ""
    return bundle[match.start() : match.start() + 400]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scratch",
        default=str(Path.home() / "AppData/Local/Temp/oauth-spa"),
        help="scratch directory OUTSIDE the repository for downloaded bundles",
    )
    parser.add_argument("--json", default="", help="also write the report as JSON here")
    args = parser.parse_args()

    scratch = Path(args.scratch)
    scratch.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {"method": {}, "bundles": [], "calls": [], "source_maps": []}
    out: list[str] = []

    def emit(line: str = "") -> None:
        print(line)
        out.append(line)

    emit("GitCode /oauth/authorize SPA -- static analysis")
    emit("=" * 72)

    # --- 1. the two shells -------------------------------------------------
    for label, url in (("login", LOGIN), ("authorize", ENTRY)):
        status, body = fetch(url)
        path = scratch / f"{label}.html"
        path.write_text(body, encoding="utf-8")
        scripts = script_tags(body)
        report["method"][label] = {
            "url": strip_query(url),
            "status": status,
            "bytes": len(body),
            "sha256_12": hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()[:12],
            "scripts": scripts,
        }
        emit(f"\n[{label}] {strip_query(url)}")
        emit(f"  status={status} bytes={len(body)} sha256={report['method'][label]['sha256_12']}")
        for script in scripts:
            emit(f"  script: {script}")

    # --- 2. download the assets -------------------------------------------
    assets: dict[str, str] = {}
    to_fetch = list(dict.fromkeys(
        [s for s in report["method"]["authorize"]["scripts"] if s.endswith(".js")]
        + [CDN + name for name in AUTHORIZE_CHUNKS]
    ))
    emit("\n[assets]")
    for url in to_fetch:
        name = url.rsplit("/", 1)[-1]
        target = scratch / name
        if target.exists() and target.stat().st_size > 0:
            text = target.read_text(encoding="utf-8", errors="replace")
            status = 200
            cached = True
        else:
            status, text = fetch(url)
            cached = False
            if status == 200:
                target.write_text(text, encoding="utf-8")
        assets[name] = text
        entry = {"url": url, "status": status, "bytes": len(text), "cached": cached}
        report["bundles"].append(entry)
        emit(f"  {status} {len(text):>9d} {'(cached)' if cached else '        '} {name}")

    # --- 3. source maps ----------------------------------------------------
    emit("\n[source maps]")
    for entry in list(report["bundles"]):
        if not entry["url"].endswith(".js"):
            continue
        map_url = entry["url"] + ".map"
        status, body = fetch(map_url, timeout=30.0)
        report["source_maps"].append({"url": map_url, "status": status, "bytes": len(body)})
        emit(f"  {status} {map_url.rsplit('/', 1)[-1]}")

    # --- 4. call sites in the main bundle ---------------------------------
    main_bundle = assets.get("index-a97e2b06.js", "")
    calls = recover_calls(main_bundle)
    oauth_calls = [
        c
        for c in calls
        if "oauth" in c["url"].lower() or "authorize" in c["url"].lower()
    ]
    emit(f"\n[oauth call sites in index-a97e2b06.js] {len(oauth_calls)} of {len(calls)} calls")
    for call in oauth_calls:
        emit(
            f"  {call['method']:5s} {call['url']:52s} "
            f"params={call['params']} data={call['data']} headers={call['headers']}"
        )
    report["calls"] = oauth_calls

    # --- 5. resolve the aliases the authorize chunk imports ---------------
    # The chunk writes ``import{j9 as P}from"./index-a97e2b06.js"``: it takes
    # the *export* named ``j9`` and calls it ``P`` locally. So the lookup key is
    # the exported name, and the local name is only what the call site shows.
    imported = exported_names(_chunk_imports(assets), "./index-a97e2b06.js")
    resolved = resolve_exports(main_bundle, imported)
    local_to_export = {
        local: exported
        for exported, local in re.findall(
            r"([A-Za-z0-9_$]+) as ([A-Za-z0-9_$]+)", _chunk_imports(assets)
        )
    }
    emit(f"\n[alias resolution] {len(resolved)} of {len(imported)} imports resolved")
    for exported in sorted(resolved):
        internal = resolved[exported]
        definition = definition_of(main_bundle, internal)
        url = re.search(r'url:\s*[`"\']([^`"\']+)', definition)
        local = next((k for k, v in local_to_export.items() if v == exported), "?")
        emit(
            f"  {local:3s} (export {exported:3s}) -> {internal:6s} "
            f"{url.group(1) if url else '(not a URL factory)'}"
        )
    report["aliases"] = resolved
    report["alias_local_names"] = local_to_export

    if args.json:
        Path(args.json).write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        emit(f"\nwritten: {args.json}")
    return 0


def _chunk_imports(assets: dict[str, str]) -> str:
    return assets.get("authorize-54c99276.js", "")


def exported_names(chunk: str, module: str) -> set[str]:
    """The *export* names a chunk imports from one module.

    ``import{a as b, c as d}from"./x.js"`` imports exports ``a`` and ``c``; the
    ``b``/``d`` are local aliases and are not what the export map is keyed by.
    """
    names: set[str] = set()
    for match in re.finditer(
        r"import\{([^}]*)\}from\"" + re.escape(module) + r"\"", chunk
    ):
        for exported, _local in re.findall(
            r"([A-Za-z0-9_$]+) as ([A-Za-z0-9_$]+)", match.group(1)
        ):
            names.add(exported)
    return names


if __name__ == "__main__":
    import urllib.error  # noqa: F401 - referenced by fetch()

    sys.exit(main())
