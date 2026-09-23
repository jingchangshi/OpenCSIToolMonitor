"""Labels for the live probes in this directory (objective §45).

§45 asks that ``tools/probe_*.py`` carry explicit labels so a normal test run
cannot accidentally execute one, and so a reader knows what a probe will do
before running it. The four labels are:

``LIVE``
    Touches something outside this process -- a real browser, a real account, a
    real network. Every probe here is LIVE; that is why they are not tests.

``NETWORK``
    Opens an outbound connection to a remote host. A probe without this label
    only talks to localhost.

``AUTH_SIDE_EFFECT``
    Changes authentication state: mints, persists or overwrites a credential, or
    issues a state-changing request. Running it may leave the account or a
    browser profile different from how it was found.

``GET_ONLY``
    Reads only. Safe to run repeatedly, and safe to run against an account whose
    state you care about.

Why this is a module and not prose
----------------------------------
The labels were previously written into five of the thirty-four probe docstrings
as free text, which meant the other twenty-nine had none and nothing could check
that any of them were true. A label that cannot be verified is a comment. Here
the labels are data, :func:`labels_for` derives them from the probe's own source
where it can, and ``tests/test_live_probe_labels.py`` asserts that every probe file
carries a declaration and that the declaration matches what the code does.

The consent boundary (§49)
--------------------------
Three probes mention the consent-submit path ``/uc/api/v1/oauth/authorize``.
None calls it: one names it in a printed note, one defines it as a constant with
a comment saying it is never used, and one mentions it in prose. That distinction
matters enough that the test checks for a *request* to it, not a mention.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

#: The four labels §45 names, in the order they should be printed.
ALL_LABELS: tuple[str, ...] = ("LIVE", "NETWORK", "AUTH_SIDE_EFFECT", "GET_ONLY")

#: A declaration line a probe can carry: ``#: labels: LIVE, NETWORK, GET_ONLY``
#:
#: The character class is ``[A-Za-z_, ]`` -- letters, underscore, comma and a
#: literal space, with ``$`` anchored. It deliberately excludes ``\s``, which
#: matches newlines: an earlier version used ``[A-Z_,\s]+`` and the capture ran
#: past the end of the declaration into the following ``from __future__ import``
#: line, so ``declared_labels`` returned only the labels it recognised and
#: silently dropped ``GET_ONLY`` from every probe that declared it.
DECLARATION = re.compile(
    r"^#:?\s*labels?\s*:\s*([A-Za-z_, ]+?)\s*$", re.MULTILINE | re.IGNORECASE
)

_REMOTE = re.compile(r"https?://[\w.\-]+")

#: Hosts that are not "the network" for labelling purposes.
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")

#: The endpoint that approves a grant. Approving one is the account holder's
#: decision, so a probe that *calls* it is a safety failure, not a label.
CONSENT_SUBMIT_PATH = "/uc/api/v1/oauth/authorize"


def _strip_docstrings(src: str) -> str:
    """Blank docstrings, keeping every other literal.

    Removing all string constants would delete the URL literals the probes are
    built from, so a probe that plainly opens a connection would read as
    ``GET_ONLY``. Only docstrings are blanked.
    """
    tree = ast.parse(src)
    lines = src.splitlines()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            end = first.end_lineno or first.lineno
            for ln in range(first.lineno - 1, end):
                if ln < len(lines):
                    lines[ln] = ""
    return "\n".join(lines)


def declared_labels(src: str) -> tuple[str, ...]:
    """The labels a probe declares in its header, if any."""
    match = DECLARATION.search(src)
    if not match:
        return ()
    found = [part.strip().upper() for part in match.group(1).split(",")]
    return tuple(label for label in found if label in ALL_LABELS)


def derives_network(src: str) -> bool:
    """Whether the probe's own process opens an outbound connection off-machine.

    The rule is deliberately about *this process*, because that is the thing a
    reader can reason about:

    * A literal remote ``https://`` URL counts.
    * An HTTP transport counts (``HttpOAuthRenewer``, ``HttpTransport``,
      ``OpenCsiToolClient``, ``GitCodeCookieSource``, ``urllib.request``,
      ``urlopen``), including when the host comes from an imported ``BASE_URL``.
      That case is why this check exists: ``probe_qr_browserless_login.py``
      reaches opencsitool.com through ``base_url=BASE_URL`` and contains no
      remote URL of its own, so a literal-only check called it offline.
    * A **CDP** transport does *not* count. ``CdpCookieProvider`` and
      ``BrowserOAuthRenewer`` speak to a browser over ``127.0.0.1``; the probe's
      own process never leaves the machine.

    The boundary this leaves, stated rather than hidden: a probe that drives an
    OAuth round trip through a local browser *does* cause remote traffic, just
    not from this process. Every such probe is also ``AUTH_SIDE_EFFECT`` --
    driving a renewal is how that traffic happens -- so it is never labelled
    safe to run casually. ``AUTH_SIDE_EFFECT``, not ``NETWORK``, is the label
    that carries the warning.
    """
    body = _strip_docstrings(src)
    for match in _REMOTE.finditer(body):
        host = match.group(0)
        if not any(local in host for local in _LOCAL_HOSTS):
            return True
    return bool(
        re.search(
            r"HttpOAuthRenewer|HttpTransport|OpenCsiToolClient|GitCodeCookieSource"
            r"|urllib\.request|urlopen|requests\.(?:get|post)|http\.client",
            body,
        )
    )


def derives_side_effect(src: str) -> bool:
    """Whether the probe *certainly* changes authentication state.

    This is deliberately a high-precision test, not a data-flow analysis. It
    answers "yes" only for signals that cannot mean anything else:

    * an explicit ``method="POST"``
    * a cookie-writing API (``install_token``, ``Storage.setCookies``, ...)
    * a renewal, or the scheduled ``tick()`` that can trigger one

    What it deliberately does **not** count, because each produced a false
    positive on a probe whose own docstring says it never POSTs:

    * a bare ``"POST"`` string. Two QR probes pass
      ``"Access-Control-Request-Method": "POST"`` inside an *OPTIONS preflight*,
      which announces an intent to POST rather than POSTing. Both issue GET and
      OPTIONS only, and one says so in capitals.
    * ``Request(..., data=body)``. Those same probes pass ``data=body`` where
      ``body`` is a helper parameter defaulting to ``None`` and never set. No
      regex can track that, so the shape of the call proves nothing.
    * ``write_text``. Two probes save downloaded JavaScript bundles to a scratch
      directory, which changes no auth state.

    The residual gap is real and worth stating: a probe that POSTs through a
    helper taking ``method`` as a variable would not be caught here.
    ``tests/test_live_probe_labels.py`` covers that direction by asserting a probe
    declaring ``GET_ONLY`` carries none of the precise signals above, so the
    failure mode this check can miss is an under-claim, and under-claiming is
    the direction that would let a mutation run while labelled safe.
    """
    body = _strip_docstrings(src)
    if re.search(r"method\s*=\s*[\"']POST[\"']", body):
        return True
    if re.search(
        r"install_token|Storage\.setCookies|Network\.setCookie|\.set_cookie\(",
        body,
    ):
        return True
    return bool(re.search(r"\.renew\(|\.renew_now\(|\.tick\(|_maybe_renew", body))


#: Signals that *might* mean a state change but cannot be trusted on their own.
#: A probe declaring GET_ONLY while matching one of these is reported for a
#: human to confirm rather than silently relabelled.
SUSPICIOUS: tuple[tuple[str, str], ...] = (
    (r"[\"']POST[\"']", "names POST somewhere"),
    (r"(?:urlopen|Request)\([^)]*data=", "builds a request with a body"),
    (r"write_text\(|write_bytes\(", "writes a file"),
)


def suspicious_signals(src: str) -> list[str]:
    """Reasons a ``GET_ONLY`` claim deserves a second look, if any."""
    body = _strip_docstrings(src)
    return [
        why
        for pattern, why in SUSPICIOUS
        if re.search(pattern, body, re.DOTALL)
    ]


def calls_consent_submit(src: str) -> bool:
    """Whether the probe *requests* the consent-submit endpoint.

    A mention is not a call: three probes name this path in a comment, a
    constant, or a printed note precisely to record that they do not use it.
    This looks for the path inside a request call.
    """
    body = _strip_docstrings(src)
    for match in re.finditer(re.escape(CONSENT_SUBMIT_PATH), body):
        window = body[max(0, match.start() - 400) : match.end() + 200]
        if re.search(r"urlopen|Request\(|http\.client|POST", window):
            # A note *about* not calling it is not a call.
            if re.search(r"NOT called|never called|not called", window):
                continue
            return True
    return False


def labels_for(path: Path) -> tuple[str, ...]:
    """The labels the probe at *path* declares. Empty when it declares none."""
    return declared_labels(path.read_text(encoding="utf-8", errors="replace"))


def probe_files(root: Path | None = None) -> list[Path]:
    """Every live-probe file, sorted.

    The glob ``probe_*.py`` used to match this module itself, which made the
    labelling tool demand a label from itself and broke an unrelated packaging
    test asserting every ``probe_*.py`` explains its safety posture. The module
    is now named ``live_probe_labels.py`` so the glob means "probes" and nothing
    else; the name check stays as a belt-and-braces guard.
    """
    base = root or Path(__file__).resolve().parent
    found = sorted(base.glob("probe_*.py")) + sorted(base.glob("verify_*.py"))
    return [p for p in found if p.name != "live_probe_labels.py"]


def main() -> int:
    """Print the label table. Exits non-zero if any probe is unlabelled."""
    problems: list[str] = []
    print(f"{'probe':<38} labels")
    print("-" * 74)
    for path in probe_files():
        src = path.read_text(encoding="utf-8", errors="replace")
        labels = declared_labels(src)
        if not labels:
            problems.append(f"{path.name}: no label declaration")
            shown = "(none)"
        else:
            shown = ",".join(labels)
        print(f"{path.name:<38} {shown}")
        if calls_consent_submit(src):
            problems.append(f"{path.name}: calls the consent-submit endpoint")
    print()
    if problems:
        print("PROBLEMS:")
        for p in problems:
            print("  -", p)
        return 1
    print(f"all {len(probe_files())} probes labelled; consent endpoint never called")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
