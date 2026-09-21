"""``opencsi tray`` -- run the Windows notification-area monitor.

The command is a thin shell over :class:`~opencsi.tray.app.TrayApp`, which is a
thin shell over :class:`~opencsi.monitor.MonitorService`. All three are separate
on purpose: the service is testable anywhere, the presenter is testable
anywhere, and only the last few lines need a Windows desktop.
"""

from __future__ import annotations

import argparse
import sys

from ..errors import EXIT_USAGE, UsageError
from .context import CliContext, add_common_options


def register(subparsers) -> None:  # noqa: ANN001 - argparse plumbing
    parser = subparsers.add_parser(
        "tray",
        help="run the Windows notification-area usage monitor",
        description=(
            "Show openCsiTool usage in the Windows notification area. The tray "
            "is a view over the same client the CLI uses: it imports the domain "
            "layer directly and never spawns a subprocess or re-parses JSON."
        ),
        epilog=(
            "requires the tray extra: pip install \"opencsi[tray]\"\n"
            "start at sign-in with: opencsi tray --install-startup"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    behavior = parser.add_argument_group("behavior")
    behavior.add_argument(
        "--check",
        action="store_true",
        help="verify the tray can start, then exit without showing an icon",
    )
    behavior.add_argument(
        "--once",
        action="store_true",
        help="fetch one snapshot and print it, then exit (no icon)",
    )
    behavior.add_argument(
        "--interval",
        type=float,
        default=300.0,
        metavar="SECONDS",
        help="how often to refresh usage data (default: 300)",
    )
    behavior.add_argument(
        "--renew-margin",
        type=float,
        default=300.0,
        metavar="SECONDS",
        help="renew the session when this little of it is left (default: 300)",
    )
    behavior.add_argument(
        "--allow-multiple",
        action="store_true",
        help="skip the single-instance check (for debugging)",
    )

    startup = parser.add_argument_group("start at sign-in")
    startup.add_argument(
        "--install-startup",
        action="store_true",
        help="register the tray to start when you sign in, then exit",
    )
    startup.add_argument(
        "--remove-startup",
        action="store_true",
        help="remove the start-at-sign-in entry, then exit",
    )
    startup.add_argument(
        "--startup-status",
        action="store_true",
        help="report whether the tray starts at sign-in, then exit",
    )
    # Without this the tray would be the one command that cannot honour
    # --no-proxy / --cdp / --timeout, which is exactly the command a user runs
    # when the default connection settings are wrong for their machine.
    add_common_options(parser)
    parser.set_defaults(handler=run)


def run(ctx: CliContext) -> int:
    args = ctx.args

    if args.install_startup or args.remove_startup or args.startup_status:
        return _startup(ctx, args)

    from ..monitor import MonitorConfig, MonitorService
    from ..tray import tray_available

    if not args.once:
        available, reason = tray_available()
        if not available:
            ctx.err(f"error: the tray cannot start: {reason}")
            ctx.err('       install it with: pip install "opencsi[tray]"')
            return EXIT_USAGE

    config = MonitorConfig(
        refresh_interval=float(args.interval),
        renew_margin=float(args.renew_margin),
    )

    if args.once:
        return _once(ctx, config)

    return _run_tray(ctx, config, allow_multiple=bool(args.allow_multiple), check=bool(args.check))


def _once(ctx: CliContext, config) -> int:
    """Fetch one snapshot and print it. No icon, no worker thread.

    Useful for scripting and for verifying on a machine where a tray cannot be
    shown -- including CI.
    """
    from ..monitor import MonitorService

    client = ctx.make_client()
    service = MonitorService(client, config=config)
    snapshot = service.refresh_now(block=True)

    def render() -> None:
        ctx.out(f"state: {snapshot.state.value}")
        if snapshot.has_data:
            ctx.out(f"total tokens: {snapshot.total_tokens:,}")
            ctx.out(f"requests:     {snapshot.requests:,}")
            ctx.out(f"pull requests:{snapshot.prs:>5}")
            ctx.out(f"generated:    {snapshot.generated_lines:,} lines")
            ctx.out(f"adopted:      {snapshot.adopted_lines:,} lines")
            ctx.out(f"adoption:     {snapshot.adoption_rate:.1%}")
            if snapshot.data_fresh_time:
                ctx.out(f"server data:  {snapshot.data_fresh_time}")
        if snapshot.last_error:
            ctx.err(f"note: {snapshot.last_error}")

    ctx.emit(snapshot.as_dict(), render)

    from ..monitor import MonitorState

    if snapshot.state is MonitorState.OK:
        return 0
    if snapshot.state is MonitorState.LOGIN_REQUIRED:
        return 13
    if snapshot.state is MonitorState.OFFLINE:
        return 30
    return 20


def _run_tray(ctx: CliContext, config, *, allow_multiple: bool, check: bool) -> int:
    from ..monitor import MonitorService
    from ..tray.app import TrayApp, TrayUnavailableError

    client = ctx.make_client()
    service = MonitorService(client, config=config)

    if check:
        # Prove the icon and menu can be built *without* entering the message
        # loop and without touching the network. A health check that needs the
        # API cannot distinguish "the tray is broken" from "the network is
        # down", which is the one question it exists to answer.
        app = TrayApp(service)
        try:
            app.run(blocking=False, start_service=False)
            icon_state = service.snapshot.state.value
            tooltip = app.tooltip(service.snapshot)
            menu = app.build_menu()
        except TrayUnavailableError as exc:
            ctx.err(f"error: {exc}")
            return EXIT_USAGE
        finally:
            service.stop()

        def render() -> None:
            ctx.out("tray: ok")
            ctx.out(f"state: {icon_state}")
            ctx.out(f"menu items: {len([m for m in menu if not m.id.startswith('sep')])}")
            ctx.out(f"tooltip: {tooltip.replace(chr(10), ' / ')}")

        ctx.emit(
            {
                "ok": True,
                "state": icon_state,
                "menu": [m.id for m in menu],
                "tooltip": tooltip,
            },
            render,
        )
        return 0

    app = TrayApp(service, on_login=lambda: _sign_in(service))
    try:
        if allow_multiple:
            app._single.acquire = lambda: True  # noqa: SLF001 - debug escape hatch
        return app.run()
    except TrayUnavailableError as exc:
        ctx.err(f"error: {exc}")
        return EXIT_USAGE
    except KeyboardInterrupt:
        service.stop()
        return 0


def _sign_in(service) -> None:
    """Open the login page, then watch for the session it establishes.

    Runs on its own thread (``TrayApp`` starts one), because opening a browser
    and waiting for a login is a tens-of-seconds operation that must not block
    the tray's message loop.

    The refresh afterwards is what makes this more than "open a web page": the
    user signs in, and the tray notices without them having to click anything
    else. It polls for a bounded window rather than checking once, because the
    user has to actually complete the sign-in -- a single immediate check would
    almost always fire before they had typed anything.

    Note the refresh is **enqueued**, never performed here. All fetching happens
    on the monitor's worker thread; ``_refresh_once`` mutates the service's state
    without a lock, so a second thread calling it directly would race the worker
    on both the HTTP call and the bookkeeping. ``refresh_now(block=False)`` hands
    the work to the one thread that owns it.
    """
    import time
    import webbrowser

    from .login import LOGIN_URL

    try:
        webbrowser.open(LOGIN_URL)
    except Exception:  # noqa: BLE001 - the user can open it themselves
        pass

    # Bounded: five minutes, then give up and let the normal poll cycle handle
    # it. The tray must not spin forever on a sign-in the user abandoned.
    deadline = time.monotonic() + 300.0
    while time.monotonic() < deadline:
        time.sleep(10.0)
        service.refresh_now()
        if service.snapshot.has_data:
            return


def _startup(ctx: CliContext, args) -> int:
    """Manage the per-user start-at-sign-in entry."""
    from ..tray.startup import StartupManager, default_command

    manager = StartupManager()

    if args.install_startup:
        status = manager.enable()
        if not status.supported:
            ctx.err(f"error: starting at sign-in is only available on Windows")
            return EXIT_USAGE
        if status.detail and not status.enabled:
            ctx.err(f"error: {status.detail}")
            return 20
        ctx.out("The tray will start when you sign in.")
        ctx.out(f"command: {status.command}")
        ctx.out("")
        ctx.out("Remove it with: opencsi tray --remove-startup")
        ctx.out("It also appears in Task Manager > Startup apps.")
        return 0

    if args.remove_startup:
        status = manager.disable()
        if not status.supported:
            ctx.err("error: starting at sign-in is only available on Windows")
            return EXIT_USAGE
        if status.detail:
            ctx.err(f"error: {status.detail}")
            return 20
        ctx.out("The tray will no longer start when you sign in.")
        return 0

    status = manager.status()
    def render() -> None:
        if not status.supported:
            ctx.out("start at sign-in: not supported on this platform")
            return
        ctx.out(f"start at sign-in: {'enabled' if status.enabled else 'disabled'}")
        if status.command:
            ctx.out(f"command: {status.command}")
        else:
            ctx.out(f"would run: {default_command()}")
        if status.detail:
            ctx.err(f"note: {status.detail}")

    ctx.emit(status.as_dict(), render)
    return 0
