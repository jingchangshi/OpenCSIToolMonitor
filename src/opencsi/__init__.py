"""opencsi -- standalone read-only client for openCsiTool "My Tools".

Public API
----------
>>> from opencsi import OpenCsiToolClient, ManualCookieProvider
>>> with OpenCsiToolClient(ManualCookieProvider(cookie)) as client:
...     snapshot = client.get_my_tools()
...     print(snapshot.total_tokens)

The client talks to an internal web API observed from the user's own
authenticated openCsiTool session. It is not an official public API client.
"""

from __future__ import annotations

from .aggregation import CostEstimate, CostLine, Summary, estimate_usage_cost, summarise
from .auth import CredentialProvider, CredentialStatus, ManualCookieProvider
from .client import OpenCsiToolClient
from .errors import (
    BadAuthHeaderError,
    BusinessApiError,
    CdpUnavailableError,
    ConfigError,
    ContractDriftError,
    CookieNotFoundError,
    MissingParamError,
    NetworkError,
    NoBrowserTargetError,
    OpenCsiError,
    PermissionDeniedError,
    ServerError,
    SessionExpiredError,
    UsageError,
    exit_code_for,
)
from .models import (
    Identity,
    ModelPrice,
    MyToolsSnapshot,
    SyncStatus,
    TokenBudget,
    TokenTrendPoint,
    ToolGrant,
)
from .version import __version__

__all__ = [
    # client
    "OpenCsiToolClient",
    # auth
    "CredentialProvider",
    "CredentialStatus",
    "ManualCookieProvider",
    # models
    "Identity",
    "ModelPrice",
    "MyToolsSnapshot",
    "SyncStatus",
    "TokenBudget",
    "TokenTrendPoint",
    "ToolGrant",
    # aggregation
    "Summary",
    "CostEstimate",
    "CostLine",
    "summarise",
    "estimate_usage_cost",
    # errors
    "OpenCsiError",
    "UsageError",
    "ConfigError",
    "BadAuthHeaderError",
    "CdpUnavailableError",
    "NoBrowserTargetError",
    "CookieNotFoundError",
    "SessionExpiredError",
    "PermissionDeniedError",
    "NetworkError",
    "ServerError",
    "BusinessApiError",
    "ContractDriftError",
    "MissingParamError",
    "exit_code_for",
    "__version__",
]
