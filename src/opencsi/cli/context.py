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
import os
import sys
from dataclasses import dataclass
from typing import Any, TextIO

from ..auth import CdpCookieProvider, CredentialProvider, ManualCookieProvider
from ..client import BASE_URL, OpenCsiToolClient
from ..errors import ConfigError, UsageError
from ..formatting import Table, to_json
from ..version import USER_AGENT, __version__

ENV_CDP_URL = "OPENCSI_CDP_URL"
ENV_BASE_URL = "OPENCSI_BASE_URL"


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
        """Choose a credential provider from the parsed arguments."""
        explicit = getattr(self.args, "cdp", None)
        if explicit:
            return CdpCookieProvider(explicit, discover=False)

        if getattr(self.args, "no_discover", False):
            env = os.environ.get(ENV_CDP_URL)
            if not env:
                raise ConfigError(
                    "--no-discover requires --cdp or the "
                    f"{ENV_CDP_URL} environment variable"
                )
            return CdpCookieProvider(env, discover=False)

        ports = getattr(self.args, "ports", None)
        return CdpCookieProvider(ports=ports or None)

    def make_client(self, *, provider: CredentialProvider | None = None) -> OpenCsiToolClient:
        """Build the client for this invocation."""
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

        cache_ttl = 0.0 if getattr(self.args, "no_cache", False) else float(
            getattr(self.args, "cache_ttl", 300.0) or 0.0
        )

        return OpenCsiToolClient(
            provider if provider is not None else self.make_provider(),
            base_url=base_url,
            timeout=timeout,
            cache_ttl=cache_ttl,
            verbose=self.verbose,
            use_proxy=not bool(getattr(self.args, "no_proxy", False)),
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
        "--no-proxy",
        action="store_true",
        help=(
            "ignore HTTP_PROXY/HTTPS_PROXY and the system proxy. Use when a "
            "local proxy cannot reach opencsitool.com (urllib honours the "
            "Windows registry proxy, unlike curl)"
        ),
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
    from . import doctor, login, logs, prices, status, tools, trend, usage, contract

    for module in (status, tools, usage, trend, prices, logs, doctor, login, contract):
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
