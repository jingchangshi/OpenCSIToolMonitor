"""Credential providers and session lifecycle.

The client depends only on :class:`CredentialProvider`; it has no knowledge of
Chrome, Edge, WebSockets, CDP targets or any browser automation. This is the
architectural boundary described in the project brief (§16).

:mod:`opencsi.auth.session` adds the three-way split between *credential
reload*, *session renewal* and *interactive login* that the original single
``refresh()`` verb conflated -- the root cause of the one-hour login problem.
"""

from .base import CredentialProvider, CredentialStatus
from .auth_host import (
    AUTH_PROFILE_DIRNAME,
    DEFAULT_AUTH_PORT,
    AuthBrowserHost,
    AuthHostMode,
    AuthHostResult,
    AuthHostStatus,
    auth_profile_dir,
    ensure_auth_host,
)
from .browser_launch import (
    BrowserLaunch,
    BrowserLaunchStatus,
    dedicated_profile_dir,
    find_browser,
    launch_debug_browser,
)
from .cdp import CdpCookieProvider, CdpEndpoint, discover_cdp_endpoint
from .gitcode_qr import (
    GitCodeQrAuthenticator,
    QrChallenge,
    QrLoginResult,
    QrLoginStatus,
    QrProtocolError,
    QrStatus,
)
from .gitcode_bridge import (
    BridgeResult,
    BridgeStatus,
    GitCodeBrowserSessionBridge,
    GITCODE_SESSION_COOKIES,
    cookie_records,
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
    LoginStage,
    LoginStatus,
    RenewalResult,
    RenewalStatus,
    SessionManager,
    SessionRenewer,
)

__all__ = [
    "CredentialProvider",
    "CredentialStatus",
    "BrowserLaunch",
    "BrowserLaunchStatus",
    "launch_debug_browser",
    "find_browser",
    "dedicated_profile_dir",
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
    "LoginStage",
    "DEFAULT_RENEW_MARGIN",
    "GitCodeQrAuthenticator",
    "QrChallenge",
    "QrLoginResult",
    "QrLoginStatus",
    "QrProtocolError",
    "QrStatus",
    "GitCodeBrowserSessionBridge",
    "BridgeResult",
    "BridgeStatus",
    "GITCODE_SESSION_COOKIES",
    "cookie_records",
    "AuthBrowserHost",
    "AuthHostResult",
    "AuthHostStatus",
    "AuthHostMode",
    "auth_profile_dir",
    "ensure_auth_host",
    "AUTH_PROFILE_DIRNAME",
    "DEFAULT_AUTH_PORT",
]
