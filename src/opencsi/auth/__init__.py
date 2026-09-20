"""Credential providers.

The client depends only on :class:`CredentialProvider`; it has no knowledge of
Chrome, Edge, WebSockets, CDP targets or any browser automation. This is the
architectural boundary described in the project brief (§16).
"""

from .base import CredentialProvider, CredentialStatus
from .cdp import CdpCookieProvider, CdpEndpoint, discover_cdp_endpoint
from .manual import ManualCookieProvider

__all__ = [
    "CredentialProvider",
    "CredentialStatus",
    "CdpCookieProvider",
    "CdpEndpoint",
    "discover_cdp_endpoint",
    "ManualCookieProvider",
]
