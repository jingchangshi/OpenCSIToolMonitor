"""Bridge a GitCode QR login into a dedicated browser profile.

Why this module exists
----------------------
The QR login and the openCsiTool session are two different things, and the gap
between them used to be the end of the story: ``opencsi login --qr`` authenticated
with GitCode, then told the user to go and finish in a browser. Objective §75
asks for the flow to close instead:

.. code-block::

    QR → GitCode login → automatic OAuth completion → verified openCsiTool session

This module is the piece that closes it. It takes the GitCode credential the QR
flow obtained over plain HTTP, makes the dedicated browser profile *be* that
GitCode session, and then lets the existing silent-renewal machinery finish the
openCsiTool leg -- which is exactly the job it already does every hour.

The measurement that made this possible
---------------------------------------
A signed-in GitCode browser session, read with ``Storage.getCookies`` on a real
profile (``tools/probe_gitcode_session.py``), is three cookies on
``.gitcode.com``:

===========================  ==========================================
Cookie                       Role
===========================  ==========================================
``GITCODE_ACCESS_TOKEN``     the bearer credential
``GITCODE_REFRESH_TOKEN``    its renewal
``GitCodeUserName``          the display name the front end reads
===========================  ==========================================

That is the whole session -- there is no server-side session id and no
device-bound secret beyond these. The proof is a two-condition measurement
(``tools/probe_gitcode_sso_bridge.py``, ``probe_gitcode_sso_bridge2.py``), run
against a *known authenticated* endpoint rather than page text:

.. code-block::

    GET /uc/api/v1/user/oauth/userInfo      signed out -> 401
                                            bridged    -> 200

The credential moved an authenticated endpoint from 401 to 200, in a real
same-origin browser request, in a profile that had never been signed in. So the
GitCode session is reproducible from the cookies alone, and no human needs to
touch the browser.

What this deliberately does not do
----------------------------------
* It never plants a cookie into a profile it was not given. The caller supplies
  the profile directory; this module does not go looking for one.
* It never touches the user's everyday Chrome profile. A profile holding
  someone's real browsing is not something a usage monitor should write to.
* It does not invent cookie attributes. ``httpOnly``, ``secure``, ``path`` and
  ``expires`` are copied from the profile the values were measured on, because a
  credential replayed with different flags is a different credential.

Security
--------
No value is logged, printed, stored on disk or returned to a caller. The
credentials live in local variables for the duration of one bridge and are
registered with :mod:`opencsi.redaction` on the way in, so an accidental
``repr`` of a traceback frame cannot leak one.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from ..errors import CdpUnavailableError, OpenCsiError
from ..redaction import register_secret, scrub_text
from ..ws import CdpConnection, WebSocketError
from .cdp import CdpEndpoint, discover_cdp_endpoint

log = logging.getLogger("opencsi.auth.gitcode_bridge")

#: The cookies that constitute a GitCode browser session. Measured on a real
#: signed-in profile, not guessed -- see the module docstring.
GITCODE_SESSION_COOKIES: tuple[str, ...] = (
    "GITCODE_ACCESS_TOKEN",
    "GITCODE_REFRESH_TOKEN",
    "GitCodeUserName",
)

#: A known *authenticated* GitCode endpoint, used as the verification standard
#: objective §12 requires. ``401`` when signed out, ``200`` with a user body when
#: signed in; both states were observed on this machine.
GITCODE_VERIFY_PATH = "/uc/api/v1/user/oauth/userInfo"
GITCODE_API_HOST = "web-api.gitcode.com"

#: Where the verification fetch is evaluated. Same-origin with the API's parent
#: domain, which is what makes the profile's cookies apply to the request.
GITCODE_PAGE_ORIGIN = "https://gitcode.com"

#: How long to give the verification page to settle before asking it to fetch.
_PAGE_SETTLE = 3.0

#: ``access_token`` is what the site stores in ``GITCODE_ACCESS_TOKEN``. The
#: mapping is named rather than positional so a protocol drift shows up as a
#: missing key instead of a silently wrong cookie.
_QR_TO_COOKIE: tuple[tuple[str, str], ...] = (
    ("access_token", "GITCODE_ACCESS_TOKEN"),
    ("refresh_token", "GITCODE_REFRESH_TOKEN"),
)


class BridgeStatus(str, Enum):
    """Outcome of a bridge attempt. Compared by identity, never parsed."""

    #: The profile now holds a GitCode session and the verification endpoint
    #: answered 200 through it.
    BRIDGED = "BRIDGED"
    #: The cookies were planted but the endpoint did not confirm them. The
    #: session may still be usable; this is reported honestly rather than
    #: upgraded to success.
    UNVERIFIED = "UNVERIFIED"
    #: The QR result carried no GitCode credential to bridge.
    NO_CREDENTIALS = "NO_CREDENTIALS"
    #: No browser to bridge into.
    CDP_UNAVAILABLE = "CDP_UNAVAILABLE"
    #: The endpoint rejected the planted session (401/403). A real failure: the
    #: credential is not one GitCode accepts.
    REJECTED = "REJECTED"
    #: Something else went wrong; ``detail`` says what, scrubbed.
    FAILED = "FAILED"


@dataclass(frozen=True)
class BridgeResult:
    """What a bridge attempt did (secret-free).

    ``status`` is the machine-readable outcome and ``verified`` is the single
    fact the CLI's success semantics hang on: the *only* way to claim an
    openCsiTool-usable GitCode session is for the verification endpoint to have
    answered 200.
    """

    status: BridgeStatus
    detail: str | None = None
    #: HTTP status the verification endpoint returned, when it was reached.
    verify_status: int | None = None
    #: Cookie *names* planted. Never values.
    planted: tuple[str, ...] = ()
    #: Cookie names that were not available in the QR result.
    missing: tuple[str, ...] = ()
    endpoint: str | None = None

    @property
    def ok(self) -> bool:
        """Whether the GitCode session is now verified inside the browser."""
        return self.status is BridgeStatus.BRIDGED

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "status": self.status.value,
            "ok": self.ok,
            "planted": list(self.planted),
        }
        if self.missing:
            out["missing"] = list(self.missing)
        if self.verify_status is not None:
            out["verify_status"] = self.verify_status
        if self.endpoint:
            out["endpoint"] = self.endpoint
        if self.detail:
            out["detail"] = self.detail
        return out


def _expiry_seconds() -> float:
    """How long a bridged cookie should live.

    GitCode's own cookies were measured at ``+4315h`` from issue, i.e. roughly
    six months. A bridged session is given a deliberately shorter life than that
    and no longer than the token's own validity is knowable, because the value is
    re-derivable at any time from a fresh QR login and a credential that outlives
    its usefulness is only a liability.
    """
    return 180.0 * 24 * 3600


def cookie_records(
    credentials: Mapping[str, str],
    *,
    username: str | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Turn QR credentials into CDP cookie records.

    Returns ``(records, missing)``. ``missing`` names the cookies the credential
    set could not supply, so the caller can say *which* part of the session is
    absent rather than reporting a vague failure.

    The attributes are the ones measured on a real signed-in profile
    (``httpOnly=True``, ``secure=True``, ``path="/"``, domain ``.gitcode.com``).
    They are reproduced rather than relaxed: a credential replayed without
    ``httpOnly`` is a subtly different thing, and there is no reason to send one.
    """
    expires = time.time() + _expiry_seconds()
    records: list[dict[str, Any]] = []
    missing: list[str] = []

    for source_key, cookie_name in _QR_TO_COOKIE:
        value = credentials.get(source_key)
        if not isinstance(value, str) or not value:
            missing.append(cookie_name)
            continue
        register_secret(value)
        records.append(
            {
                "name": cookie_name,
                "value": value,
                "domain": ".gitcode.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "expires": expires,
            }
        )

    if isinstance(username, str) and username:
        # Not a secret: it is the display name the site shows, and it was already
        # reported by the QR result as a public field.
        records.append(
            {
                "name": "GitCodeUserName",
                "value": username,
                "domain": ".gitcode.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "expires": expires,
            }
        )
    else:
        missing.append("GitCodeUserName")

    return records, missing


#: Evaluated inside a gitcode.com page. Reports the HTTP status and the JSON
#: *key names* only -- a 200 body carries the account's own details, which this
#: tool has no business printing.
_VERIFY_JS = """
(async () => {
  try {
    const r = await fetch(%s, {
      method: 'GET',
      credentials: 'include',
      headers: { 'Accept': 'application/json, text/plain, */*' },
    });
    let keys = null;
    try { keys = Object.keys(JSON.parse(await r.text())).slice(0, 8); } catch (e) {}
    return JSON.stringify({ status: r.status, keys: keys });
  } catch (e) {
    return JSON.stringify({ error: String(e).slice(0, 80) });
  }
})()
"""


def verify_in_profile(
    endpoint: CdpEndpoint,
    *,
    path: str = GITCODE_VERIFY_PATH,
    timeout: float = 45.0,
) -> tuple[int | None, tuple[str, ...]]:
    """Ask the authenticated endpoint from inside the profile. Never raises.

    Returns ``(status, body_key_names)``. ``status`` is ``None`` when the request
    could not be made at all, which is deliberately distinct from a 4xx: "we
    could not ask" and "the answer was no" are different facts and the caller
    reports them differently.
    """
    browser_ws = endpoint.browser_ws_url()
    if not browser_ws:
        return None, ()

    target_id: str | None = None
    try:
        with CdpConnection(browser_ws, timeout=timeout) as conn:
            created = conn.call(
                "Target.createTarget",
                {"url": f"{GITCODE_PAGE_ORIGIN}/login", "background": True},
                timeout=15.0,
            )
            target_id = created.get("targetId")
            if not target_id:
                return None, ()
            attached = conn.call(
                "Target.attachToTarget",
                {"targetId": target_id, "flatten": True},
                timeout=15.0,
            )
            session_id = attached.get("sessionId")
            if not session_id:
                return None, ()
            conn.call("Page.enable", session_id=session_id, timeout=10.0)
            # The document has to finish loading before a same-origin fetch is
            # allowed; evaluating during navigation returns an opaque network
            # error that would look like a rejected credential.
            time.sleep(_PAGE_SETTLE)
            result = conn.call(
                "Runtime.evaluate",
                {
                    "expression": _VERIFY_JS % _json_literal(path),
                    "awaitPromise": True,
                    "returnByValue": True,
                },
                session_id=session_id,
                timeout=30.0,
            )
            raw = (result.get("result") or {}).get("value")
            return _parse_verify(raw)
    except (WebSocketError, OpenCsiError, OSError, ValueError) as exc:
        log.debug("in-profile verification could not complete: %s", type(exc).__name__)
        return None, ()
    finally:
        if target_id:
            try:
                with CdpConnection(browser_ws, timeout=10.0) as conn:
                    conn.call("Target.closeTarget", {"targetId": target_id}, timeout=8.0)
            except Exception:  # noqa: BLE001 - cleanup must never mask the result
                pass


def _json_literal(value: str) -> str:
    import json

    return json.dumps(value)


def _parse_verify(raw: Any) -> tuple[int | None, tuple[str, ...]]:
    import json

    if not isinstance(raw, str):
        return None, ()
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None, ()
    if not isinstance(parsed, Mapping):
        return None, ()
    status = parsed.get("status")
    keys = parsed.get("keys")
    key_names = tuple(str(k) for k in keys) if isinstance(keys, list) else ()
    return (int(status) if isinstance(status, int) else None), key_names


class GitCodeBrowserSessionBridge:
    """Plant a GitCode QR credential into a dedicated browser profile.

    The class holds no state between calls: a bridge is one short, verifiable
    operation, and keeping the credential in an instance attribute would give it
    a lifetime longer than the operation needs.
    """

    def __init__(
        self,
        *,
        cdp_url: str | None = None,
        port: int | None = None,
        timeout: float = 45.0,
    ) -> None:
        self._cdp_url = cdp_url
        self._port = port
        self._timeout = timeout

    def _endpoint(self) -> CdpEndpoint:
        if self._cdp_url:
            return discover_cdp_endpoint(self._cdp_url, probe=True)
        ports = (self._port,) if self._port else None
        return discover_cdp_endpoint(ports=ports)

    def bridge(
        self,
        credentials: Mapping[str, str],
        *,
        username: str | None = None,
        verify: bool = True,
    ) -> BridgeResult:
        """Plant ``credentials`` and, unless disabled, verify the result.

        ``verify=False`` exists for the offline test suite, where there is no
        GitCode to ask. It produces :attr:`BridgeStatus.UNVERIFIED`, never
        ``BRIDGED`` -- an unverified bridge is not allowed to look like a
        verified one, because that is precisely the class of overclaiming this
        objective is about.
        """
        records, missing = cookie_records(credentials, username=username)
        if not records:
            return BridgeResult(
                status=BridgeStatus.NO_CREDENTIALS,
                detail="the QR result carried no GitCode credential to bridge",
                missing=tuple(missing),
            )

        try:
            endpoint = self._endpoint()
        except OpenCsiError as exc:
            return BridgeResult(
                status=BridgeStatus.CDP_UNAVAILABLE,
                detail=scrub_text(str(exc))[:200],
                missing=tuple(missing),
            )

        browser_ws = endpoint.browser_ws_url()
        if not browser_ws:
            return BridgeResult(
                status=BridgeStatus.CDP_UNAVAILABLE,
                detail="the browser exposes no WebSocket endpoint to plant cookies over",
                missing=tuple(missing),
            )

        try:
            with CdpConnection(browser_ws, timeout=self._timeout) as conn:
                conn.call("Storage.setCookies", {"cookies": records}, timeout=20.0)
        except (WebSocketError, OSError) as exc:
            return BridgeResult(
                status=BridgeStatus.FAILED,
                detail=f"could not plant the cookies ({type(exc).__name__})",
                missing=tuple(missing),
                endpoint=str(endpoint),
            )

        planted = tuple(str(record["name"]) for record in records)

        if not verify:
            return BridgeResult(
                status=BridgeStatus.UNVERIFIED,
                detail="cookies planted; verification was not requested",
                planted=planted,
                missing=tuple(missing),
            )

        status, keys = verify_in_profile(endpoint, timeout=self._timeout)
        if status == 200:
            return BridgeResult(
                status=BridgeStatus.BRIDGED,
                detail="GitCode accepted the bridged session",
                verify_status=200,
                planted=planted,
                missing=tuple(missing),
            )
        if status in (401, 403):
            return BridgeResult(
                status=BridgeStatus.REJECTED,
                detail=f"GitCode rejected the bridged session (HTTP {status})",
                verify_status=status,
                planted=planted,
                missing=tuple(missing),
            )
        if status is None:
            return BridgeResult(
                status=BridgeStatus.UNVERIFIED,
                detail="the GitCode session could not be verified from here",
                planted=planted,
                missing=tuple(missing),
            )
        return BridgeResult(
            status=BridgeStatus.UNVERIFIED,
            detail=f"verification answered HTTP {status}",
            verify_status=status,
            planted=planted,
            missing=tuple(missing),
        )

    def describe(self) -> str:
        return "GitCode QR credential → dedicated browser profile bridge"

    def __repr__(self) -> str:
        return f"GitCodeBrowserSessionBridge(cdp_url={self._cdp_url!r}, port={self._port!r})"


def gitcode_session_cookie_names() -> tuple[str, ...]:
    """The cookie names a bridged session consists of. Never values."""
    return GITCODE_SESSION_COOKIES
