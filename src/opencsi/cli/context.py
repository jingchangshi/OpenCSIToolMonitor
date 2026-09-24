"""Shared CLI plumbing: argument handling, output, client construction.

Every sub-command receives a :class:`CliContext`. It owns three things the
commands must not each re-implement:

* **Output discipline.** ``--json`` switches to machine-readable output; text
  goes to stdout and diagnostics to stderr, so ``opencsi usage > file`` is
  never polluted by warnings.
* **Client construction.** The credential provider is chosen once, from
  ``--cdp`` / ``OPENCSI_CDP_URL`` / auto-discovery, and the resulting
  :class:`OpenCsiToolClient` is the only thing a command talks to.
* **Secret safety.** Nothing in this module can print a cookie: the provider
  only ever hands the value to the transport, and every rendered object is
  secret-free by construction.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any, TextIO

from ..auth import (
    BrowserOAuthRenewer,
    CdpCookieProvider,
    CredentialProvider,
    ManualCookieProvider,
    SessionManager,
)
from ..auth.session import SessionRenewer
from ..auth.stored import (
    CompositeCredentialProvider,
    StoredGitCodeCredentialSource,
    StoredOpenCsiCredentialProvider,
)
from ..client import BASE_URL, OpenCsiToolClient
from ..errors import ConfigError, UsageError
from ..formatting import Table, to_json
from ..version import USER_AGENT, __version__

ENV_CDP_URL = "OPENCSI_CDP_URL"
ENV_BASE_URL = "OPENCSI_BASE_URL"
ENV_NO_RENEW = "OPENCSI_NO_RENEW"
ENV_PROXY = "OPENCSI_PROXY"
#: Disable the durable credential store, for debugging the browser path. Named
#: rather than undocumented so a user who needs it can find it, and so nobody
#: has to guess why a stored credential is or is not being used.
ENV_NO_STORE = "OPENCSI_NO_STORE"

log = logging.getLogger("opencsi.cli")


@dataclass
class CliContext:
    """Per-invocation state shared by all sub-commands."""

    args: argparse.Namespace
    stdout: TextIO
    stderr: TextIO

    # ── output ────────────────────────────────────────────────────────────
    @property
    def json(self) -> bool:
        return bool(getattr(self.args, "json", False))

    @property
    def stdin_is_tty(self) -> bool:
        """Whether stdin is an interactive terminal.

        ``getpass`` falls back to a plain read when it is not, which is fine for
        piping a cookie in but would break ``--json`` output ordering.
        """
        try:
            return bool(sys.stdin.isatty())
        except Exception:
            return False

    @property
    def verbose(self) -> bool:
        return bool(getattr(self.args, "verbose", False))

    @property
    def use_proxy(self) -> bool:
        """Whether production HTTP should honour configured proxies.

        Direct connections are the default. urllib otherwise inherits
        HTTP_PROXY/HTTPS_PROXY and, on Windows, the registry proxy; on the
        development machine that silently routed openCsiTool through
        127.0.0.1:7890, where TLS failed. Proxying is therefore an explicit
        opt-in via --proxy (or OPENCSI_PROXY=1). --no-proxy is retained as a
        backwards-compatible explicit spelling of the default.
        """
        if bool(getattr(self.args, "no_proxy", False)):
            return False
        if bool(getattr(self.args, "proxy", False)):
            return True
        value = os.environ.get(ENV_PROXY, "").strip().lower()
        return value in {"1", "true", "yes", "on"}

    def out(self, text: str = "") -> None:
        """Write a line to stdout."""
        print(text, file=self.stdout)

    def err(self, text: str = "") -> None:
        """Write a line to stderr.

        stdout is flushed first. When output is redirected, stdout is
        block-buffered while stderr is not, so without this flush an
        interleaved sequence (a check, then its hint, then the next check)
        would print all the stdout lines and then all the stderr lines --
        detaching each hint from the check it belongs to.
        """
        try:
            self.stdout.flush()
        except (ValueError, OSError):  # pragma: no cover - closed stream
            pass
        print(text, file=self.stderr)

    def emit_json(self, payload: Any) -> None:
        """Print ``payload`` as JSON (secret-free) to stdout."""
        self.out(to_json(payload, pretty=True))

    def emit(self, payload: Any, text_renderer) -> None:
        """Emit JSON when ``--json``, otherwise call ``text_renderer()``."""
        if self.json:
            self.emit_json(payload)
        else:
            text_renderer()

    def table(self, table: Table) -> None:
        self.out(table.render())

    def blank(self) -> None:
        if not self.json:
            self.out()

    # ── client ────────────────────────────────────────────────────────────
    def make_provider(self) -> CredentialProvider:
        """Choose a credential provider from the parsed arguments.

        Order, and why it is this order:

        1. ``--cdp`` -- an explicit endpoint the user named. Honoured first and
           never overridden, so the flag keeps working exactly as it did.
        2. ``--no-discover`` with ``$OPENCSI_CDP_URL`` -- also explicit, just
           supplied through the environment.
        3. **the secure store** -- the normal path. No browser, no port, no
           process to have started first.
        4. CDP auto-discovery -- the migration path. A user who is already signed
           in through a browser profile has nothing in the store yet, and must
           keep working rather than being told to sign in again.

        The store is tried *before* auto-discovery, which is the change this
        round makes. Auto-discovery used to be the default, so every command
        silently required a browser with a debug port to already be running --
        including the tray, which is exactly the "Tray -> must start Chrome
        first" dependency this architecture removes.

        The two explicit cases stay in front of the store so that a user who
        deliberately points at a browser still gets that browser. A stored
        credential silently winning over ``--cdp`` would make the flag a lie.
        """
        explicit = getattr(self.args, "cdp", None)
        if explicit:
            return CdpCookieProvider(explicit, discover=False)

        env = os.environ.get(ENV_CDP_URL)
        if getattr(self.args, "no_discover", False):
            if not env:
                raise ConfigError(
                    "--no-discover requires --cdp or the "
                    f"{ENV_CDP_URL} environment variable"
                )
            return CdpCookieProvider(env, discover=False)

        ports = getattr(self.args, "ports", None)
        browser = CdpCookieProvider(ports=ports or None)
        stored = self.make_stored_provider()
        if stored is None:
            return browser
        # ``$OPENCSI_CDP_URL`` without ``--no-discover`` is still an explicit
        # statement of intent, so it stays ahead of the store.
        if env:
            return CompositeCredentialProvider(
                [CdpCookieProvider(env, discover=False), stored, browser]
            )
        return CompositeCredentialProvider([stored, browser])

    def make_stored_provider(self) -> "StoredOpenCsiCredentialProvider | None":
        """The secure-store provider, or ``None`` when there is no secure store.

        ``--no-store`` and ``$OPENCSI_NO_STORE`` disable it, which exists for two
        reasons: a user debugging the browser path wants to *see* what that path
        does without a stored credential short-circuiting it, and a test must be
        able to run against a browser without touching a real credential file.
        """
        if getattr(self.args, "no_store", False) or os.environ.get(ENV_NO_STORE):
            return None
        from ..auth.stored import StoredOpenCsiCredentialProvider
        from ..auth.windows_store import open_default_store

        store = open_default_store()
        if store is None:
            # No secure store on this platform. Returning None rather than a
            # weaker store is deliberate: there is deliberately no plaintext
            # fallback, and inventing one here would put a live credential in
            # the clear on a machine whose owner expected encryption.
            return None
        return StoredOpenCsiCredentialProvider(
            store, ttl=float(getattr(self.args, "store_ttl", 5.0) or 5.0)
        )

    def make_stored_source(self) -> "StoredGitCodeCredentialSource | None":
        """The secure-store GitCode source, or ``None`` when there is no store."""
        if getattr(self.args, "no_store", False) or os.environ.get(ENV_NO_STORE):
            return None
        from ..auth.stored import StoredGitCodeCredentialSource
        from ..auth.windows_store import open_default_store

        store = open_default_store()
        if store is None:
            return None
        return StoredGitCodeCredentialSource(store)

    def _provider_is_store_backed(self, provider: CredentialProvider) -> bool:
        """Whether ``provider`` is (or contains) the secure-store provider.

        A composite hides which source answered, so it is looked into. The
        question being asked is "could this credential have come from the store?",
        and only the provider itself can answer it -- assuming the store is
        relevant because the store *exists* is how a pasted token would end up
        renewing against a stored identity.
        """
        from ..auth.stored import StoredOpenCsiCredentialProvider

        if isinstance(provider, StoredOpenCsiCredentialProvider):
            return True
        members = getattr(provider, "_providers", None)
        if isinstance(members, (list, tuple)):
            return any(
                isinstance(m, StoredOpenCsiCredentialProvider) for m in members
            )
        return False

    def _stored_provider_from(
        self, provider: CredentialProvider
    ) -> "StoredOpenCsiCredentialProvider | None":
        """The secure-store provider inside ``provider``, if it is there.

        ``make_renewer`` receives whatever ``make_provider`` returned, which may be
        a bare :class:`StoredOpenCsiCredentialProvider` or a
        :class:`CompositeCredentialProvider` wrapping one alongside a browser.
        The write half of the store is needed in both cases, and the caller must
        not have to know which shape it got.

        Returns ``None`` -- rather than falling back to opening the store afresh --
        when the active provider is *not* store-backed. That distinction is the
        whole point: an explicitly chosen browser must not have its session
        written into a stored identity, and a second, independent handle on the
        same file would make that mistake invisible.

        Introspection is confined here so the private ``_providers`` attribute is
        read in exactly one place.
        """
        from ..auth.stored import StoredOpenCsiCredentialProvider

        if isinstance(provider, StoredOpenCsiCredentialProvider):
            return provider
        members = getattr(provider, "_providers", None)
        if isinstance(members, (list, tuple)):
            for member in members:
                if isinstance(member, StoredOpenCsiCredentialProvider):
                    return member
        return None

    def make_renewer(
        self, provider: CredentialProvider, *, base_url: str
    ) -> "SessionRenewer | None":
        """Build a silent renewer for ``provider``, or ``None`` if inapplicable.

        A separate seam from :meth:`make_session` because ``login --renew``
        needs to *inspect* the renewer's evidence, not just hand it to a
        manager -- and because a test should be able to script a renewal
        without standing up a browser.

        **The HTTP renewer now has two possible credential sources, and both are
        tried before any browser is.** That is the change this round makes:

        * the secure store's GitCode credential -- no browser at all, and the
          normal path after a QR login;
        * the CDP provider, when the provider *is* a browser reading a GitCode
          session out of a profile -- the migration path for a user who has not
          scanned yet.

        :class:`~opencsi.auth.oauth_browser.BrowserOAuthRenewer` is now last and
        only reached when no HTTP source can help. It is not deleted: it covers
        a first-time consent that a human has to approve, which is the one thing
        HTTP cannot do.
        """
        from ..auth.http_oauth import HttpOAuthRenewer
        from ..auth.session import FallbackRenewer

        timeout = float(getattr(self.args, "renew_timeout", 45.0) or 45.0)
        use_proxy = self.use_proxy

        # A manually supplied token has no upstream session of its own, and it
        # must never borrow one. Falling through to the store here would renew the
        # *pasted* token by writing a session minted from a completely unrelated
        # stored identity -- the request would succeed and the user would be
        # signed in as someone else. Refusing is the only safe answer.
        from ..auth.manual import ManualCookieProvider

        if isinstance(provider, ManualCookieProvider):
            return None

        http_renewers: list[Any] = []

        # A composite provider hides which source it used, so ask it: when the
        # value came from the store, the store is the credential source to hand
        # the HTTP renewer as well.
        #
        # This is only consulted when the provider itself is store-backed or a
        # browser. A provider that is neither (see the guard above) never reaches
        # here, so the store cannot be used as a substitute for a credential the
        # caller did not present.
        if self._provider_is_store_backed(provider) or isinstance(
            provider, CdpCookieProvider
        ):
            stored_source = self.make_stored_source()
            if stored_source is not None:
                # The renewer needs a *sink* as well as a source: it writes the
                # minted session back through `remember_token`. A bare
                # StoredGitCodeCredentialSource has no such method, and the
                # renewer reaches for it with `getattr`, so passing the source
                # alone renewed successfully and persisted nothing -- the next
                # process kept reading the old token. The adapter pairs the
                # read-only source with the store's writing half.
                #
                # Both halves come from the same `open_default_store()` call that
                # built the active provider, so the session is written to the very
                # file this process reads.
                stored_sink = self._stored_provider_from(provider)
                if stored_sink is None:
                    # `--cdp` / env / auto-discovery chose a browser as the active
                    # identity, so there is no stored provider to be its sink.
                    # Handing it the store would write a session minted for
                    # whatever browser is running into a stored identity that had
                    # nothing to do with it. Source stays read-only instead: the
                    # browser path keeps using itself as its own sink below.
                    stored_renewer_source = stored_source
                else:
                    from ..auth.stored import StoredOAuthCredentialAdapter

                    stored_renewer_source = StoredOAuthCredentialAdapter(
                        stored_source, stored_sink
                    )

                stored_http = HttpOAuthRenewer(
                    stored_renewer_source,
                    base_url=base_url,
                    timeout=timeout,
                    use_proxy=use_proxy,
                )

                if stored_sink is not None:
                    # Keep the *GitCode* credential alive across the renewal that
                    # spends it. Wrapped only for the store-backed source: that is
                    # the only case where a stored GitCode credential exists to
                    # refresh, and a browser identity must not have its own
                    # credential silently rewritten by this process.
                    #
                    # The refresh is gated on near-expiry inside the refresher, so
                    # a healthy GitCode token costs no request here.
                    from ..auth.gitcode_refresh import (
                        GitCodeTokenRefresher,
                        RefreshingGitCodeRenewer,
                        StoredGitCodeRefresher,
                    )

                    stored_http = RefreshingGitCodeRenewer(
                        StoredGitCodeRefresher(
                            stored_sink.store,
                            refresher=GitCodeTokenRefresher(use_proxy=use_proxy),
                        ),
                        stored_http,
                    )

                http_renewers.append(stored_http)

        if isinstance(provider, CdpCookieProvider):
            http_renewers.append(
                HttpOAuthRenewer(
                    provider, base_url=base_url, timeout=timeout, use_proxy=use_proxy
                )
            )

        if not http_renewers:
            return None

        browser = BrowserOAuthRenewer(
            getattr(self.args, "cdp", None),
            base_url=base_url,
            timeout=timeout,
            ports=getattr(self.args, "ports", None) or None,
        )
        return FallbackRenewer([*http_renewers, browser])

    def make_session(
        self,
        provider: CredentialProvider,
        *,
        base_url: str,
        want_renewer: bool = True,
    ) -> SessionManager:
        """Attach a silent renewer to ``provider`` when that makes sense.

        A renewer is only meaningful for a browser-backed credential: a manual
        token has no upstream SSO session to re-run OAuth against. ``--no-renew``
        and ``$OPENCSI_NO_RENEW`` disable it, which is what keeps
        ``opencsi doctor`` able to *report* an expired session rather than
        silently fixing it, and what makes the read-only contract auditable.
        """
        renewer = None
        if (
            want_renewer
            and not getattr(self.args, "no_renew", False)
            and not os.environ.get(ENV_NO_RENEW)
        ):
            renewer = self.make_renewer(provider, base_url=base_url)
        return SessionManager(provider, renewer=renewer)

    def make_client(
        self,
        *,
        provider: CredentialProvider | None = None,
        renew: bool = True,
    ) -> OpenCsiToolClient:
        """Build the client for this invocation.

        ``renew=False`` attaches no renewer. Commands that exist to *inspect* or
        *establish* the session (``login``, ``doctor``) pass it: a diagnostic
        that silently re-ran OAuth would both surprise the user and hide the
        expiry they asked about.
        """
        base_url = (
            getattr(self.args, "base_url", None)
            or os.environ.get(ENV_BASE_URL)
            or BASE_URL
        )
        if not base_url.startswith(("http://", "https://")):
            raise ConfigError(f"base URL must start with http:// or https://: {base_url!r}")

        timeout = float(getattr(self.args, "timeout", 15.0) or 15.0)
        if timeout <= 0:
            raise UsageError("--timeout must be greater than zero")

        # ``--refresh`` and ``--no-cache`` both defeat the *data* cache.
        if getattr(self.args, "refresh", False) or getattr(self.args, "no_cache", False):
            cache_ttl = 0.0
        else:
            cache_ttl = float(getattr(self.args, "cache_ttl", 300.0) or 0.0)

        provider = provider if provider is not None else self.make_provider()

        # ``--refresh`` additionally re-reads the credential -- but only when the
        # provider can actually re-read one. ``invalidate()`` on a
        # ManualCookieProvider is *permanent* (there is no source to go back to),
        # so calling it here would destroy the only credential the user has and
        # turn a refresh into a spurious "session expired". Refresh therefore
        # uses ``refresh()``, whose contract is "re-read if you can".
        if getattr(self.args, "refresh", False):
            try:
                provider.refresh()
            except Exception:  # pragma: no cover - defensive
                log.debug("credential refresh failed; the request will report it")

        session = self.make_session(provider, base_url=base_url, want_renewer=renew)

        return OpenCsiToolClient(
            provider,
            base_url=base_url,
            timeout=timeout,
            cache_ttl=cache_ttl,
            verbose=self.verbose,
            use_proxy=self.use_proxy,
            session=session,
        )


def add_common_options(parser: argparse.ArgumentParser) -> None:
    """Attach the options every sub-command accepts."""
    group = parser.add_argument_group("output")
    group.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of text",
    )
    group.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="log HTTP request paths to stderr (never query strings or cookies)",
    )

    conn = parser.add_argument_group("connection")
    conn.add_argument(
        "--no-store",
        action="store_true",
        help=(
            "ignore the encrypted credential store and use the browser path "
            f"only (also settable via ${ENV_NO_STORE}); for debugging"
        ),
    )
    conn.add_argument(
        "--store-ttl",
        type=float,
        default=5.0,
        metavar="SECONDS",
        help=(
            "how long a credential read from the secure store is reused before "
            "the file is re-read (default: 5)"
        ),
    )
    conn.add_argument(
        "--cdp",
        metavar="URL",
        help=(
            "Chrome DevTools endpoint, e.g. http://127.0.0.1:9222 "
            f"(default: auto-discover, or ${ENV_CDP_URL})"
        ),
    )
    conn.add_argument(
        "--no-discover",
        action="store_true",
        help="do not scan local ports; require --cdp or $OPENCSI_CDP_URL",
    )
    conn.add_argument(
        "--ports",
        type=_port_list,
        metavar="P[,P...]",
        help="restrict auto-discovery to these ports (default: 9222,9223,9224)",
    )
    conn.add_argument(
        "--base-url",
        metavar="URL",
        help=f"override the API origin (default: ${ENV_BASE_URL} or {BASE_URL})",
    )
    conn.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        metavar="SECONDS",
        help="per-request timeout (default: 15)",
    )
    conn.add_argument(
        "--cache-ttl",
        type=float,
        default=300.0,
        metavar="SECONDS",
        help="in-process response cache lifetime (default: 300)",
    )
    conn.add_argument(
        "--no-cache",
        action="store_true",
        help="disable the in-process response cache",
    )
    conn.add_argument(
        "--refresh",
        action="store_true",
        help=(
            "force fresh data: bypass the cache and re-read the credential. "
            "Only business data is ever cached, never the cookie"
        ),
    )
    proxy_mode = conn.add_mutually_exclusive_group()
    proxy_mode.add_argument(
        "--proxy",
        action="store_true",
        help=(
            "honour HTTP_PROXY/HTTPS_PROXY and the system proxy. Direct "
            "connections are the default; also settable via $OPENCSI_PROXY"
        ),
    )
    proxy_mode.add_argument(
        "--no-proxy",
        action="store_true",
        help=(
            "explicitly use direct connections and ignore configured proxies "
            "(this is already the default; retained for compatibility)"
        ),
    )

    renew = parser.add_argument_group("session renewal")
    renew.add_argument(
        "--no-renew",
        action="store_true",
        help=(
            "never re-run OAuth to renew an expiring session; report the "
            f"expiry instead (also settable via ${ENV_NO_RENEW})"
        ),
    )
    renew.add_argument(
        "--renew-timeout",
        type=float,
        default=45.0,
        metavar="SECONDS",
        help="budget for one silent OAuth renewal (default: 45)",
    )


def _port_list(text: str) -> tuple[int, ...]:
    """Parse ``9222,9223`` into a validated port tuple."""
    ports: list[int] = []
    for chunk in text.replace(" ", "").split(","):
        if not chunk:
            continue
        try:
            port = int(chunk)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not a port number: {chunk!r}") from None
        if not (1 <= port <= 65535):
            raise argparse.ArgumentTypeError(f"port out of range: {port}")
        ports.append(port)
    if not ports:
        raise argparse.ArgumentTypeError("no ports given")
    return tuple(ports)


def add_date_options(parser: argparse.ArgumentParser) -> None:
    """Attach ``--start-date`` / ``--end-date``.

    These filter ``tokenTrend`` only; ``requestList`` is always returned in
    full by the server (report §18).
    """
    group = parser.add_argument_group("date filter")
    group.add_argument(
        "--start-date",
        metavar="YYYY-MM-DD",
        help="trend window start (affects tokenTrend only)",
    )
    group.add_argument(
        "--end-date",
        metavar="YYYY-MM-DD",
        help="trend window end (affects tokenTrend only)",
    )


def validated_dates(args: argparse.Namespace) -> tuple[str | None, str | None]:
    """Return ``(start, end)`` after checking the ISO shape and ordering.

    The two options must be given together: the endpoint's own signature is
    ``?startDate=&endDate=``, and a half-open window is nearly always a typo
    rather than an intent. Rejecting it here produces a clear message instead of
    a silently different result set.
    """
    start = getattr(args, "start_date", None)
    end = getattr(args, "end_date", None)
    for label, value in (("--start-date", start), ("--end-date", end)):
        if value and not _looks_like_date(value):
            raise UsageError(f"{label} must look like YYYY-MM-DD, got {value!r}")
    if bool(start) != bool(end):
        missing = "--end-date" if start else "--start-date"
        raise UsageError(
            f"{missing} is required when the other date is given "
            "(the trend window needs both ends)"
        )
    if start and end and start > end:
        raise UsageError(f"--start-date {start} is after --end-date {end}")
    return start, end


def _looks_like_date(text: str) -> bool:
    parts = text.split("-")
    return (
        len(parts) == 3
        and all(p.isdigit() for p in parts)
        and len(parts[0]) == 4
        and len(parts[1]) == 2
        and len(parts[2]) == 2
    )


def build_parser(prog: str = "opencsi") -> argparse.ArgumentParser:
    """Construct the full argument parser.

    Imported lazily by :mod:`opencsi.cli.app` so that ``opencsi --version``
    never imports a sub-command module.
    """
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Read-only command line client for openCsiTool \"My Tools\". "
            "Uses the session cookie of a browser you are already signed in to."
        ),
        epilog=(
            "This tool calls an internal web API observed from an authenticated "
            "openCsiTool session. It is not an official openCsiTool API client. "
            f"User-Agent: {USER_AGENT}"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    add_common_options(parser)

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    from . import (
        contract,
        doctor,
        login,
        logout,
        logs,
        prices,
        status,
        tools,
        tray,
        trend,
        usage,
    )

    for module in (
        status,
        tools,
        usage,
        trend,
        prices,
        logs,
        doctor,
        login,
        logout,
        tray,
        contract,
    ):
        module.register(subparsers)

    parser.set_defaults(_parser=parser)
    return parser


def require_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> str:
    """Return the chosen command, or raise a usage error with help text."""
    command = getattr(args, "command", None)
    if not command:
        raise UsageError("no command given; try 'opencsi --help'")
    return str(command)


def make_context(argv: list[str] | None = None) -> tuple[CliContext, argparse.Namespace]:
    """Parse ``argv`` and wrap it in a :class:`CliContext`."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return CliContext(args=args, stdout=sys.stdout, stderr=sys.stderr), args
