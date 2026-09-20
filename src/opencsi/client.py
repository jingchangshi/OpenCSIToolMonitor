"""The openCsiTool client.

This class knows about endpoints, JSON envelopes, caching and business errors.
It knows **nothing** about browsers, CDP, WebSockets or automation -- only the
:class:`~opencsi.auth.base.CredentialProvider` protocol. That separation is the
core architectural boundary of the project.

Endpoints used (all GET, all verified in the investigation report):

===================================================  ==========================
Path                                                 Purpose
===================================================  ==========================
``/opencsitool/rest/v1/user/getUserInfo``            identity + org/role view
``.../user/getUserRolesByOrganizationId``            organisation roles
``.../ai/config/cost``                               model prices / display names
``.../ai/operations/personalQueueStatus``            **main data source**
``/opencsitool/llmgateway/rest/v1/users/{id}/call-logs``   call log page
``/opencsitool/llmgateway/rest/v1/users/{id}/key-budget``  employee budget
===================================================  ==========================

No write endpoint is implemented, and no admin endpoint is called.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .aggregation import CostEstimate, Summary, estimate_usage_cost, summarise
from .auth.base import CredentialProvider
from .cache import DEFAULT_TTL, Cache
from .errors import (
    BadAuthHeaderError,
    BusinessApiError,
    ContractDriftError,
    MissingParamError,
    NetworkError,
    OpenCsiError,
    PermissionDeniedError,
    ServerError,
    SessionExpiredError,
    UsageError,
)
from .models import (
    Identity,
    ModelPrice,
    MyToolsSnapshot,
    SyncStatus,
    TokenBudget,
    coerce_grants,
    coerce_prices,
    coerce_trend,
    as_int,
    as_opt_str,
    as_str,
)
from .transport import HttpTransport, Response

log = logging.getLogger("opencsi.client")

BASE_URL = "https://opencsitool.com"
APP_PREFIX = "/opencsitool"
REST = f"{APP_PREFIX}/rest/v1"
LLM_GATEWAY = f"{APP_PREFIX}/llmgateway/rest/v1/users"

DEFAULT_TIMEOUT = 15.0
SLOW_TIMEOUT = 30.0  # call-logs; the page itself uses 30s

#: Success code inside the ``{code, data, message}`` envelope.
ENVELOPE_SUCCESS = 200

#: Failures that mean "we never got to ask the service", as opposed to "the
#: service answered, but said no". Only the first kind propagates out of
#: `contract_check`; an HTTP-level failure is a legitimate check *result*
#: (brief §50 lists HTTP status among the things this command verifies).
_UNREACHABLE = (
    "CDP_UNAVAILABLE",
    "NO_BROWSER_TARGET",
    "OPENCSITOOL_NOT_LOGGED_IN",
    "SESSION_EXPIRED",
    "NETWORK_ERROR",
)


def _reraise_if_unreachable(exc: OpenCsiError) -> None:
    """Re-raise ``exc`` when it means the service was never reached.

    ``contract_check`` records failed checks so a schema change is reported per
    endpoint, and a 4xx/5xx response is such a result. But a missing browser or
    an expired session is not a schema change, and reporting it as one sends the
    user looking for the wrong problem. Those codes propagate instead, with
    their own exit status.
    """
    if getattr(exc, "code", "") in _UNREACHABLE:
        raise exc


class OpenCsiToolClient:
    """Read-only client for openCsiTool "My Tools" data."""

    def __init__(
        self,
        credentials: CredentialProvider,
        *,
        base_url: str = BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        cache_ttl: float = DEFAULT_TTL,
        transport: HttpTransport | None = None,
        verbose: bool = False,
        use_proxy: bool = True,
    ) -> None:
        self.credentials = credentials
        self.base_url = base_url.rstrip("/")
        self.cache = Cache(cache_ttl)
        self._identity: Identity | None = None
        self._prices: tuple[ModelPrice, ...] | None = None

        if transport is not None:
            self.http = transport
        else:
            self.http = HttpTransport(
                self.base_url,
                timeout=timeout,
                use_proxy=use_proxy,
                logger=(lambda line: log.debug("%s", line)) if verbose else None,
            )

    # ── session ───────────────────────────────────────────────────────────
    def login_or_restore_session(self, *, refresh: bool = False) -> Identity:
        """Validate the session and load the signed-in identity.

        Raises :class:`SessionExpiredError` when the credential is missing or
        rejected.
        """
        if self._identity is not None and not refresh:
            return self._identity

        info = self._request_json(f"{REST}/user/getUserInfo")
        if not isinstance(info, Mapping):
            raise ContractDriftError("getUserInfo returned an unexpected payload shape")

        user = info.get("user")
        if not isinstance(user, Mapping):
            user = {}
        third_party = info.get("threePartyUserInfo")
        if not isinstance(third_party, Mapping):
            third_party = {}

        organization_id = as_str(user.get("currentOrganizationId"))
        roles: tuple[str, ...] = ()
        if organization_id:
            try:
                raw_roles = self._request_json(
                    f"{REST}/user/getUserRolesByOrganizationId",
                    params={"organization_id": organization_id},
                )
            except OpenCsiError:
                # Roles are advisory; a failure here must not block the session.
                raw_roles = None
            if isinstance(raw_roles, list):
                roles = tuple(
                    as_str(r.get("name"))
                    for r in raw_roles
                    if isinstance(r, Mapping) and r.get("name")
                )

        self._identity = Identity(
            user_id=as_str(user.get("id")),
            employee_id=as_str(user.get("employeeId")),
            user_name=as_str(user.get("userName")),
            user_email=as_opt_str(user.get("userEmail")),
            account_id=as_str(third_party.get("accountId")),
            account_login=as_str(third_party.get("accountLogin")),
            organization_id=organization_id,
            organization_name=as_opt_str(user.get("currentOrganizationName")),
            role_view=as_opt_str(user.get("currentRoleView")),
            role_view_name=as_opt_str(user.get("currentRoleViewName")),
            roles=roles,
        )
        return self._identity

    @property
    def identity(self) -> Identity | None:
        return self._identity

    def employee_id(self) -> str:
        """Return the bound employee id, loading the session if needed."""
        if self._identity is None:
            self.login_or_restore_session()
        assert self._identity is not None
        if not self._identity.employee_id:
            raise ContractDriftError(
                "the signed-in user has no employeeId; openCsiTool data cannot be scoped"
            )
        return self._identity.employee_id

    def session_status(self) -> dict[str, Any]:
        """Non-raising session probe for ``status`` / ``doctor``."""
        try:
            identity = self.login_or_restore_session(refresh=True)
        except OpenCsiError as exc:
            return {"ok": False, "error": exc.as_dict()}
        return {"ok": True, "identity": identity}

    # ── main data source ──────────────────────────────────────────────────
    def get_my_tools(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        *,
        refresh: bool = False,
    ) -> MyToolsSnapshot:
        """Fetch the complete "My Tools" snapshot.

        ``startDate``/``endDate`` affect only ``tokenTrend``; ``requestList`` is
        always returned in full. Omitting the dates yields the server default.
        """
        params: dict[str, Any] = {}
        if start_date:
            params["startDate"] = start_date
        if end_date:
            params["endDate"] = end_date

        key = f"queue:{start_date or ''}:{end_date or ''}"
        payload = self.cache.get_or_set(
            key,
            lambda: self._request_json(
                f"{REST}/ai/operations/personalQueueStatus", params=params or None
            ),
            refresh=refresh,
        )

        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, Mapping):
            raise ContractDriftError(
                "personalQueueStatus response has no 'data' object"
            )

        summary = data.get("tokenSummary")
        if not isinstance(summary, Mapping):
            summary = {}

        return MyToolsSnapshot(
            identity=self._identity,
            bound_employee_id=as_str(data.get("boundEmployeeId")),
            user_id=as_str(data.get("userId")),
            grants=coerce_grants(data.get("requestList")),
            token_trend=coerce_trend(data.get("tokenTrend")),
            total_tokens=as_int(summary.get("totalTokens")),
            total_request_count=as_int(summary.get("totalRequestCount")),
            sync_status=SyncStatus.from_json(
                data.get("syncStatus") if isinstance(data.get("syncStatus"), Mapping) else None
            ),
            token_budget=TokenBudget.from_json(
                data.get("tokenBudget") if isinstance(data.get("tokenBudget"), Mapping) else None
            ),
            fetched_at=datetime.now(timezone.utc),
            start_date=start_date,
            end_date=end_date,
        )

    def get_summary(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        *,
        refresh: bool = False,
    ) -> tuple[MyToolsSnapshot, Summary]:
        """Convenience wrapper returning both the snapshot and its summary."""
        snapshot = self.get_my_tools(start_date, end_date, refresh=refresh)
        return snapshot, summarise(snapshot)

    # ── auxiliary endpoints ───────────────────────────────────────────────
    def get_model_prices(self, *, refresh: bool = False) -> tuple[ModelPrice, ...]:
        """Model/tool price list from ``ai/config/cost`` (bare JSON array)."""
        if self._prices is not None and not refresh:
            return self._prices
        raw = self.cache.get_or_set(
            "cost",
            lambda: self._request_json(f"{REST}/ai/config/cost"),
            refresh=refresh,
            ttl=max(self.cache.ttl, 3600.0),  # prices change rarely
        )
        prices = coerce_prices(raw if isinstance(raw, list) else None)
        self._prices = prices
        return prices

    def estimate_cost(
        self,
        tokens_by_request_type: Mapping[str, int],
        *,
        refresh: bool = False,
    ) -> CostEstimate:
        """Estimate spend for a ``requestType -> tokens`` mapping."""
        return estimate_usage_cost(tokens_by_request_type, self.get_model_prices(refresh=refresh))

    def get_call_logs(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """Call log page for the bound employee."""
        if page < 1:
            raise UsageError("--page must be >= 1")
        if page_size < 1:
            raise UsageError("--page-size must be >= 1")
        employee_id = self.employee_id()
        params: dict[str, Any] = {"page": page, "pageSize": page_size}
        if start_date:
            params["startDate"] = start_date
        if end_date:
            params["endDate"] = end_date
        raw = self._request_json(
            f"{LLM_GATEWAY}/{employee_id}/call-logs",
            params=params,
            timeout=SLOW_TIMEOUT,
        )
        if not isinstance(raw, Mapping):
            return {"list": [], "total": 0, "page": page, "pageSize": page_size}
        items = raw.get("list")
        return {
            "list": [x for x in items if isinstance(x, Mapping)] if isinstance(items, list) else [],
            "total": as_int(raw.get("total")),
            "page": as_int(raw.get("page"), page),
            "pageSize": as_int(raw.get("pageSize"), page_size),
        }

    def get_key_budget(self) -> TokenBudget:
        """Employee token budget from the llmgateway service."""
        employee_id = self.employee_id()
        raw = self._request_json(f"{LLM_GATEWAY}/{employee_id}/key-budget")
        return TokenBudget.from_json(raw if isinstance(raw, Mapping) else None) or TokenBudget()

    # ── contract check ────────────────────────────────────────────────────
    def contract_check(self) -> dict[str, Any]:
        """Read-only schema check against the live service.

        Verifies HTTP status, envelope shape, required fields and types. It does
        **not** compare dynamic business values, so normal data drift never
        fails it (report §49-50). Unknown extra fields are ignored, not errors.

        Raises :class:`OpenCsiError` when the service cannot be reached at all.
        That distinction matters: a user with no browser must not be told the
        API contract changed, which is what happened when a credential failure
        was recorded as a failed check.
        """
        checks: list[dict[str, Any]] = []

        def record(name: str, ok: bool, detail: str = "") -> None:
            checks.append({"check": name, "ok": ok, "detail": detail})

        # A failure to *obtain a credential* propagates (carrying exit 10/11/12/
        # 13/30), because "no browser" is not a schema change. An HTTP-level
        # failure is a legitimate check result: brief §50 lists HTTP status
        # among the things this command verifies.
        try:
            identity = self.login_or_restore_session(refresh=True)
            record("getUserInfo", bool(identity.user_id), "identity parsed")
        except OpenCsiError as exc:
            _reraise_if_unreachable(exc)
            record("getUserInfo", False, str(exc))
            return {"ok": False, "checks": checks}

        try:
            # Bypass the cache so the check reflects the live service.
            self.cache.invalidate("queue::")
            payload = self._request_json(
                f"{REST}/ai/operations/personalQueueStatus", params=None
            )
            ok_envelope = (
                isinstance(payload, Mapping)
                and "data" in payload
                and "code" in payload
            )
            record("personalQueueStatus envelope", ok_envelope, "code/data present")

            data = payload.get("data") if isinstance(payload, Mapping) else None
            data = data if isinstance(data, Mapping) else {}
            record(
                "personalQueueStatus.data.requestList",
                isinstance(data.get("requestList"), list),
                "list present",
            )
            record(
                "personalQueueStatus.data.tokenSummary",
                isinstance(data.get("tokenSummary"), Mapping),
                "object present",
            )
            record(
                "personalQueueStatus.data.tokenTrend",
                isinstance(data.get("tokenTrend"), list),
                "list present",
            )
            record(
                "personalQueueStatus.data.syncStatus",
                isinstance(data.get("syncStatus"), Mapping),
                "object present",
            )

            grants = coerce_grants(data.get("requestList"))
            required_grant_fields = ("id", "requestType", "status", "accountName")
            missing: list[str] = []
            if grants:
                sample = data["requestList"][0]
                if isinstance(sample, Mapping):
                    missing = [f for f in required_grant_fields if f not in sample]
            record(
                "requestList item fields",
                not missing,
                "all required fields present" if not missing else f"missing: {', '.join(missing)}",
            )
        except OpenCsiError as exc:
            # A service we cannot reach is not contract drift. Propagate it so
            # the caller reports "no browser" / "expired" rather than sending
            # the user to look for a schema change that did not happen.
            _reraise_if_unreachable(exc)
            record("personalQueueStatus", False, str(exc))

        try:
            prices = self.get_model_prices(refresh=True)
            record("ai/config/cost", len(prices) > 0, f"{len(prices)} rows")
            record(
                "price rows have requestType",
                all(p.request_type for p in prices) if prices else False,
                "requestType present on every row",
            )
        except OpenCsiError as exc:
            _reraise_if_unreachable(exc)
            record("ai/config/cost", False, str(exc))

        try:
            logs = self.get_call_logs()
            record(
                "call-logs shape",
                isinstance(logs.get("list"), list) and "total" in logs,
                "list/total present",
            )
        except OpenCsiError as exc:
            _reraise_if_unreachable(exc)
            record("call-logs", False, str(exc))

        try:
            self.get_key_budget()
            record("key-budget", True, "parsed")
        except OpenCsiError as exc:
            _reraise_if_unreachable(exc)
            record("key-budget", False, str(exc))

        return {"ok": all(c["ok"] for c in checks), "checks": checks}

    # ── transport with 401 recovery ───────────────────────────────────────
    def _request_json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Perform a GET and decode the body, recovering once from a 401.

        Recovery sequence (project brief §13):

        * On 401, call ``credentials.invalidate()`` then ``get_token()`` and
          retry **exactly once**.
        * If the retry also fails, raise :class:`SessionExpiredError`. There is
          no loop, so a permanently invalid credential cannot spin.

        A 401 body of ``Invalid Authorization`` is reported as
        :class:`BadAuthHeaderError`, because it means an Authorization header
        was sent -- which this client never does, so it indicates a caller
        misconfiguration worth surfacing distinctly.
        """
        response = self._attempt(path, params, timeout)

        if response.status == 401:
            body = (response.body or "").strip()
            if "Invalid Authorization" in body:
                raise BadAuthHeaderError(
                    "the server rejected an Authorization header; openCsiTool "
                    "authenticates by cookie only"
                )

            log.debug("401 from %s; refreshing credentials once", path)
            self._refresh_credentials()
            response = self._attempt(path, params, timeout)

            if response.status == 401:
                raise SessionExpiredError(
                    "openCsiTool rejected the session cookie (HTTP 401) after a "
                    "credential refresh."
                )
            if "Invalid Authorization" in (response.body or ""):
                raise BadAuthHeaderError(
                    "the server rejected an Authorization header; openCsiTool "
                    "authenticates by cookie only"
                )

        self._raise_for_status(response)
        return self._decode(response)

    def _attempt(
        self,
        path: str,
        params: Mapping[str, Any] | None,
        timeout: float | None,
    ) -> Response:
        """One HTTP attempt with the current credential installed."""
        token = self.credentials.get_token()
        if not token:
            raise SessionExpiredError(
                "no openCsiTool session credential is available."
            )
        self.http.set_cookie(token)
        return self.http.get_json(path, params, timeout=timeout)

    def _refresh_credentials(self) -> None:
        """Invalidate and re-read the credential, if the source supports it."""
        self.credentials.invalidate()
        try:
            self.credentials.refresh()
        except OpenCsiError:
            # The retry attempt will surface the real error.
            log.debug("credential refresh failed; retry will report the outcome")

    @staticmethod
    def _raise_for_status(response: Response) -> None:
        status = response.status
        if 200 <= status < 300:
            return
        message = ""
        try:
            parsed = response.json()
            if isinstance(parsed, Mapping):
                message = as_str(parsed.get("message"))
        except Exception:
            message = ""

        if status == 401:
            raise SessionExpiredError(
                "openCsiTool rejected the session cookie (HTTP 401)",
                http_status=status,
            )
        if status == 403:
            raise PermissionDeniedError(
                message or "permission denied: this account cannot perform that action",
                http_status=status,
            )
        if status == 400:
            raise MissingParamError(
                message or "the server reported a missing parameter",
                http_status=status,
            )
        if 500 <= status < 600:
            raise ServerError(
                message or f"server error (HTTP {status})", http_status=status
            )
        raise NetworkError(
            f"unexpected HTTP {status}: {message or 'no message'}", http_status=status
        )

    @staticmethod
    def _decode(response: Response) -> Any:
        """Decode a successful response and enforce the business envelope.

        An HTTP 200 carrying ``code != 200`` is a business failure, not a
        success (project brief §59).
        """
        try:
            payload = response.json()
        except Exception as exc:
            # A non-JSON body on a 200 means the endpoint is not the API we
            # expect -- typically the SPA shell served for an unknown path.
            # Surfacing a raw JSONDecodeError here would be a poor experience,
            # so classify it as the contract drift it actually is.
            body = (getattr(response, "body", "") or "").strip()
            preview = body[:120].replace("\n", " ")
            raise ContractDriftError(
                "the server returned a non-JSON body where JSON was expected "
                f"(HTTP {response.status}, {len(body)} bytes): {preview!r}",
                hint=(
                    "This usually means the path is not an API endpoint. The "
                    "public site serves its single-page app for unknown paths; "
                    "only the internal /opencsitool/rest/v1 and /llmgateway "
                    "routes return JSON."
                ),
                http_status=response.status,
            ) from exc
        if isinstance(payload, Mapping) and "code" in payload:
            code = payload.get("code")
            if as_int(code, default=-1) != ENVELOPE_SUCCESS:
                message = as_str(payload.get("message")) or "unspecified business error"
                raise BusinessApiError(
                    f"the API reported failure (code={code}): {message}",
                    http_status=response.status,
                )
            return payload
        return payload

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "OpenCsiToolClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            f"OpenCsiToolClient(base_url={self.base_url!r}, "
            f"credential={self.credentials.name!r})"
        )
