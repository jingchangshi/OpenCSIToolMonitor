"""Shared test helpers.

The suite is written so that it runs under **both** ``unittest`` and ``pytest``.
This environment has no ``pytest`` installed, so ``unittest`` is the runner of
record; ``pytest`` compatibility is a convenience for contributors who have it.

No test may touch the network. The API is exercised through a
:class:`FakeTransport` that replays the fixtures captured during the API
investigation, and CDP is exercised through an in-process fake WebSocket
server, so the suite is fully offline and deterministic.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FIXTURES = HERE / "fixtures"
SRC = ROOT / "src"

# Make ``import opencsi`` work without an install step.
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

#: The synthetic cookie value used throughout the suite. It is long enough to
#: exercise the redaction heuristics and is never a real credential.
FAKE_TOKEN = "TESTCOOKIE" + "a1b2c3d4e5f6" * 20

#: A synthetic virtual key body, distinct from the cookie.
FAKE_VIRTUAL_KEY = "sk-bM4LUSmEXAMPLE00000000"


def load_fixture(name: str) -> Any:
    """Load a JSON fixture by file name."""
    with open(FIXTURES / name, encoding="utf-8") as handle:
        return json.load(handle)


#: Path fragments -> fixture file. Matched against the request path suffix.
ROUTES: tuple[tuple[str, str], ...] = (
    ("/user/getUserInfo", "get_user_info.json"),
    ("/user/getUserRolesByOrganizationId", "get_user_roles.json"),
    ("/user/getVisibleRoleViews", "visible_role_views.json"),
    ("/ai/config/cost", "config_cost.json"),
    ("/ai/operations/personalQueueStatus", "personal_queue_status.json"),
    ("/call-logs", "call_logs.json"),
    ("/key-budget", "key_budget.json"),
)


class FakeResponse:
    """Minimal stand-in for :class:`opencsi.transport.Response`."""

    def __init__(
        self,
        status: int = 200,
        payload: Any = None,
        *,
        body: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.status = status
        self.body = body if body is not None else json.dumps(payload, ensure_ascii=False)
        self.headers = dict(headers or {})
        self.url = "https://opencsitool.com/test"
        self.elapsed_ms = 1.0

    def json(self) -> Any:
        return json.loads(self.body)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def header(self, name: str, default: str = "") -> str:
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return default


class FakeTransport:
    """Replays fixtures and records the requests it was asked to make.

    Implements the same surface the client uses from
    :class:`opencsi.transport.HttpTransport`, so the client is exercised
    end-to-end apart from the socket.
    """

    def __init__(self, base_url: str = "https://opencsitool.com") -> None:
        self.base_url = base_url.rstrip("/")
        self.cookie: str | None = None
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        #: Overrides keyed by path suffix; consulted before the fixture table.
        self.overrides: dict[str, Any] = {}
        #: When set, every request returns this response.
        self.force: FakeResponse | None = None
        #: Sequence of responses to return in order (for retry tests).
        self.queue: list[FakeResponse] = []
        self.closed = False

    # -- HttpTransport surface -------------------------------------------
    def set_cookie(self, value: str | None) -> None:
        self.cookie = value or None

    def clear_cookie(self) -> None:
        self.cookie = None

    @property
    def has_cookie(self) -> bool:
        return bool(self.cookie)

    def get_json(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> FakeResponse:
        self.calls.append((path, dict(params) if params else None))
        if self.queue:
            return self.queue.pop(0)
        if self.force is not None:
            return self.force
        for fragment, value in self.overrides.items():
            if path.endswith(fragment):
                if isinstance(value, BaseException):
                    raise value
                if isinstance(value, FakeResponse):
                    return value
                return FakeResponse(200, value)
        for fragment, fixture in ROUTES:
            if path.endswith(fragment):
                return FakeResponse(200, load_fixture(fixture))
        return FakeResponse(404, {"message": "路径错误！"})

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> "FakeTransport":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- helpers ----------------------------------------------------------
    def paths(self) -> list[str]:
        return [path for path, _ in self.calls]

    def params_for(self, fragment: str) -> dict[str, Any] | None:
        for path, params in self.calls:
            if path.endswith(fragment):
                return params
        return None


class StubCredentialProvider:
    """A credential provider that hands out a fixed token.

    ``invalidate()`` drops the cached value but the source stays readable, so a
    later ``get_token()`` returns the token again -- the same semantics as
    :class:`~opencsi.auth.cdp.CdpCookieProvider`, and the reason a 401 retry has
    a real chance of succeeding. This is deliberately *not* the manual
    provider's permanent-invalidate behaviour.

    Set ``token=None`` to model "the source exists but holds nothing", and
    ``raises=SomeError()`` to model a source that cannot be read at all.
    """

    name = "stub"

    def __init__(
        self,
        token: str | None = FAKE_TOKEN,
        *,
        raises: BaseException | None = None,
    ) -> None:
        self._token = token
        self._raises = raises
        self.reads = 0
        self.invalidations = 0
        self.refreshes = 0
        # Mirror CdpCookieProvider: a failed read remembers the actionable hint
        # so `doctor` can print the real cause rather than a generic guess.
        self._last_hint = getattr(raises, "hint", None) if raises is not None else None

    @property
    def last_hint(self) -> str | None:
        """Actionable advice from the most recent failed read."""
        return self._last_hint

    def get_token(self) -> str | None:
        self.reads += 1
        if self._raises is not None:
            raise self._raises
        return self._token

    def invalidate(self) -> None:
        """Drop the cache; the source remains readable (unlike a manual token)."""
        self.invalidations += 1

    def refresh(self) -> str | None:
        self.refreshes += 1
        if self._raises is not None:
            raise self._raises
        return self._token

    def status(self):  # pragma: no cover - overridden in specific tests
        from opencsi.auth.base import CredentialStatus

        return CredentialStatus(
            available=self._token is not None and self._raises is None,
            source=self.name,
            cookie_count=1 if self._token else 0,
            detail=str(self._raises) if self._raises else None,
        )


def make_client(**kwargs: Any):
    """Build an :class:`OpenCsiToolClient` wired to a :class:`FakeTransport`.

    Returns ``(client, transport, provider)``.
    """
    from opencsi.client import OpenCsiToolClient

    transport = kwargs.pop("transport", None) or FakeTransport()
    provider = kwargs.pop("provider", None) or StubCredentialProvider()
    client = OpenCsiToolClient(provider, transport=transport, **kwargs)
    return client, transport, provider


def offline_env() -> dict[str, str]:
    """Environment overrides that make accidental network use fail loudly."""
    env = dict(os.environ)
    env.pop("OPENCSI_CDP_URL", None)
    env.pop("OPENCSI_BASE_URL", None)
    return env
