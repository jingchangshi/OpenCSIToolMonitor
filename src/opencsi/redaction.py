"""Secret redaction.

This module is the single choke point that keeps credentials out of logs,
tracebacks, JSON output, CLI output and test snapshots.

Two complementary mechanisms
----------------------------
1. **Pattern scrubbing** -- :func:`scrub_text` recognises the *shapes* secrets
   take in this codebase (``Cookie:``/``Set-Cookie:`` headers, ``Authorization``
   headers, ``sk-`` API keys, ``token=`` pairs, ``virtualKey`` JSON fields) and
   replaces the value with :data:`MASK`.

2. **Value registration** -- :func:`register_secret` records the *actual* live
   values (the session cookie, each ``virtualKey``) in an in-memory registry.
   :func:`scrub_text` then removes them even when they appear with no
   surrounding context, for example inside a raw response body echoed by a
   traceback.

Neither mechanism ever writes a secret anywhere. The registry lives in process
memory only and is intentionally not serialisable.

The registry is bounded so that a pathological caller cannot grow it without
limit; the oldest entries are dropped first.
"""

from __future__ import annotations

import logging
import re
from collections import OrderedDict
from typing import Any

MASK = "<redacted>"

# Values shorter than this are not registered: masking very short strings would
# corrupt unrelated output (for example masking the word "token" itself).
_MIN_SECRET_LEN = 8
_MAX_REGISTERED = 256

_registry: "OrderedDict[str, None]" = OrderedDict()

# Header / key-value shapes that carry a secret in the value position.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Cookie: token=...   /   Set-Cookie: token=...; Path=/; HttpOnly
    (re.compile(r"(?i)\b(set-cookie|cookie)\s*:\s*[^\r\n]*"), r"\1: " + MASK),
    # Authorization: Bearer ...
    (re.compile(r"(?i)\bauthorization\s*:\s*[^\r\n]*"), "Authorization: " + MASK),
    # token=<value> / access_token=<value> / refresh_token=<value>
    (
        re.compile(r"(?i)\b(access_token|refresh_token|token)\s*=\s*([^\s;,&\")']+)"),
        r"\1=" + MASK,
    ),
    # "token": "<value>"  /  'virtualKey': '<value>'  (JSON-ish, both quote styles)
    (
        re.compile(
            r"(?i)([\"'](?:token|virtual_?key|access_?token|refresh_?token|secret|password)"
            r"[\"']\s*:\s*)[\"'][^\"']*[\"']"
        ),
        r"\1\"" + MASK + "\"",
    ),
    # virtualKey: <value>  (unquoted)
    (
        re.compile(r"(?i)\b(virtual_?key)\s*:\s*([^\s,}\"]+)"),
        r"\1: " + MASK,
    ),
    # OpenAI-style / openCsiTool-style API keys
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}"), MASK),
    # JWTs (three dot-separated base64url runs)
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}"), MASK),
    # Long opaque hex/base64url blobs that look like session tokens.
    # Kept conservative (>=48 chars) so real identifiers survive.
    (re.compile(r"\b[A-Za-z0-9_\-]{48,}\b"), MASK),
)


def register_secret(value: str | None) -> None:
    """Record a live secret so it is masked wherever it later appears.

    Safe to call with ``None`` or a short value; those are ignored.
    """
    if not value or not isinstance(value, str) or len(value) < _MIN_SECRET_LEN:
        return
    _registry[value] = None
    _registry.move_to_end(value)
    while len(_registry) > _MAX_REGISTERED:
        _registry.popitem(last=False)


def clear_registry() -> None:
    """Forget all registered secrets. Intended for tests."""
    _registry.clear()


def scrub_text(text: Any) -> str:
    """Return ``text`` as a string with every recognised secret masked."""
    if text is None:
        return ""
    s = text if isinstance(text, str) else str(text)

    # Registered exact values first: they are the highest-confidence matches.
    for secret in _registry:
        if secret in s:
            s = s.replace(secret, MASK)

    for pattern, repl in _PATTERNS:
        s = pattern.sub(repl, s)
    return s


def redact_mapping(data: Any, *, _depth: int = 0) -> Any:
    """Recursively redact sensitive keys in a JSON-like structure.

    Used before emitting ``--json`` output so that a future field addition
    cannot leak a credential through the serialiser.

    The rule is keyed on both the **name** and the **value type**, because name
    matching alone is too blunt in both directions:

    * A credential is always a **string**. So a credential-ish key holding a
      number, list or object is describing data, not holding a secret:
      ``total_tokens: 3061130999``, ``token_trend: [...]``,
      ``token_budget: {...}``. Masking those made ``usage --json`` useless --
      it hid the headline number the command exists to report.
    * A credential-ish key holding a **string** is masked, and a container is
      recursed into so a secret nested deeper is still caught by its own key or
      by the value patterns in :func:`scrub_text`.

    Anything whose name looks credential-ish but whose shape is unfamiliar is
    therefore still handled: strings are masked, containers are walked.
    """
    if _depth > 12:
        return MASK
    #: Key names that hold a secret *value*. Matched as substrings, so
    #: ``access_token`` and ``refreshToken`` are covered too.
    sensitive = (
        "token",
        "cookie",
        "authorization",
        "virtualkey",
        "virtual_key",
        "secret",
        "password",
        "passwd",
        "apikey",
        "api_key",
    )
    #: Key names that *describe* credentials rather than hold one. Masking the
    #: whole subtree here destroyed `status --json` (the credential summary
    #: became the string "<redacted>"), so these recurse instead. A real secret
    #: nested inside is still caught by its own key or by the value patterns.
    containers = ("credential", "credentials")

    def credentialish(key: str) -> bool:
        flat = key.replace("-", "").replace("_", "").lower()
        return any(tok.replace("_", "") in flat for tok in sensitive)

    if isinstance(data, dict):
        out: dict[Any, Any] = {}
        for k, v in data.items():
            key = str(k)
            flat = key.replace("-", "").replace("_", "").lower()
            if any(tok.replace("_", "") in flat for tok in containers):
                if isinstance(v, (dict, list, tuple)):
                    out[k] = redact_mapping(v, _depth=_depth + 1)
                elif isinstance(v, str):
                    # A container-ish name holding a *string* is still a secret:
                    # ``{"credential": "..."}`` must never be emitted.
                    out[k] = MASK
                else:
                    # ...but a number, bool or None is a count, a duration or a
                    # flag. This branch used to mask unconditionally, which made
                    # the tray's documented ``credential_expires_in_seconds``
                    # emit the string ``"<redacted>"`` instead of a number --
                    # the same over-masking that the ``credentialish`` branch
                    # below had already been fixed for, missed here because the
                    # two branches were written separately. See defect 14.
                    out[k] = v
            elif credentialish(key):
                if isinstance(v, (dict, list, tuple)):
                    # Recurse: the container is structure, its leaves are
                    # individually judged.
                    out[k] = redact_mapping(v, _depth=_depth + 1)
                elif isinstance(v, str):
                    out[k] = MASK
                else:
                    # A number, bool or None under a credential-ish name is a
                    # count or a flag, not a credential.
                    out[k] = v
            else:
                out[k] = redact_mapping(v, _depth=_depth + 1)
        return out
    if isinstance(data, (list, tuple)):
        return [redact_mapping(v, _depth=_depth + 1) for v in data]
    if isinstance(data, str):
        return scrub_text(data)
    return data


class RedactingFilter(logging.Filter):
    """Logging filter that scrubs every formatted record.

    Attached to the package root logger by :func:`install_logging_redaction`,
    so that even a careless ``log.debug("resp=%s", resp)`` cannot leak.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            record.msg = scrub_text(record.getMessage())
            record.args = ()
        except Exception:  # pragma: no cover - defensive
            record.msg = MASK
            record.args = ()
        if record.exc_info:
            # Keep the traceback but scrub the exception text itself.
            exc_type, exc, tb = record.exc_info
            record.exc_text = None
            if exc is not None and exc.args:
                try:
                    exc.args = tuple(scrub_text(a) for a in exc.args)
                except Exception:  # pragma: no cover
                    pass
        return True


def install_logging_redaction(logger: logging.Logger | None = None) -> None:
    """Attach :class:`RedactingFilter` to a logger (default: this package)."""
    target = logger or logging.getLogger("opencsi")
    for existing in list(target.filters):
        if isinstance(existing, RedactingFilter):
            return
    target.addFilter(RedactingFilter())


class Secret:
    """Wrapper that holds a secret and refuses to reveal it via ``repr``.

    Used for the session cookie so that an accidental ``print(cookie)``,
    ``repr(state)`` or ``f"{cookie}"`` cannot leak the value. Call
    :meth:`reveal` to obtain the raw value at the single point of use.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value
        register_secret(value)

    def reveal(self) -> str:
        """Return the raw secret. Call only when constructing a request."""
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __len__(self) -> int:
        return len(self._value)

    def __repr__(self) -> str:
        return f"Secret(len={len(self._value)}, value={MASK})"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return format(repr(self), spec)
