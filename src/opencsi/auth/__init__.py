"""Credential providers and session lifecycle.

The client depends only on :class:`CredentialProvider`; it has no knowledge of
Chrome, Edge, WebSockets, CDP targets or any browser automation. This is the
architectural boundary described in the project brief (§16).

:mod:`opencsi.auth.session` adds the three-way split between *credential
reload*, *session renewal* and *interactive login* that the original single
``refresh()`` verb conflated -- the root cause of the one-hour login problem.
"""

from .base import CredentialProvider, CredentialStatus
from .cdp import CdpCookieProvider, CdpEndpoint, discover_cdp_endpoint
from .gitcode_qr import (
    GitCodeQrAuthenticator,
    QrChallenge,
    QrLoginResult,
    QrLoginStatus,
    QrProtocolError,
    QrStatus,
)
from .manual import ManualCookieProvider
from .oauth_browser import (
    BrowserOAuthRenewer,
    RenewalCapability,
    RenewalEvidence,
    make_cdp_renewer,
    renewal_capability,
)
from .session import (
    DEFAULT_RENEW_MARGIN,
    InteractiveAuthenticator,
    LoginResult,
    LoginStatus,
    RenewalResult,
    RenewalStatus,
    SessionManager,
    SessionRenewer,
)

__all__ = [
    "CredentialProvider",
    "CredentialStatus",
    "CdpCookieProvider",
    "CdpEndpoint",
    "discover_cdp_endpoint",
    "ManualCookieProvider",
    "BrowserOAuthRenewer",
    "RenewalEvidence",
    "make_cdp_renewer",
    "RenewalCapability",
    "renewal_capability",
    "SessionManager",
    "SessionRenewer",
    "InteractiveAuthenticator",
    "RenewalResult",
    "RenewalStatus",
    "LoginResult",
    "LoginStatus",
    "DEFAULT_RENEW_MARGIN",
    "GitCodeQrAuthenticator",
    "QrChallenge",
    "QrLoginResult",
    "QrLoginStatus",
    "QrProtocolError",
    "QrStatus",
]
