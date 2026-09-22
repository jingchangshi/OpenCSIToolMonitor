"""``python -m opencsi.tray`` -- start the tray, or run a tray sub-command.

Exists so the Windows startup entry can be ``pythonw.exe -m opencsi.tray``
without needing the console script to be on ``PATH``. It exits quietly rather
than printing a traceback, because when Windows launches this at sign-in there
is no console to show it in and nobody to read it -- the log file is the place
for that.

Arguments
---------
With no arguments -- the double-click and start-at-sign-in case -- this starts
the resident tray.

With arguments it delegates to the CLI's ``tray`` sub-command, so
``python -m opencsi.tray --once`` and ``opencsi tray --once`` are the same
request.

It used to ignore ``sys.argv`` entirely and always start the GUI. That was fixed
once for the frozen entry script, but the fix lived in ``packaging/tray_entry.py``
-- so the *declared console script* ``opencsi-monitor`` and the module form both
kept the bug. ``opencsi-monitor --help`` printed nothing and then sat in the
notification area forever, and a user who asked for one snapshot got a process
that never returns. Discarding the request is worse than failing it, because
there is nothing to notice and nothing to report.

The delegation now lives here, and the frozen entry script calls this function
rather than repeating the logic -- so there is one place that decides, which is
what the rest of the tray already assumes.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    """Start the tray, or forward ``argv`` to the CLI's ``tray`` sub-command.

    ``argv`` defaults to ``sys.argv[1:]``. Passing it explicitly is for tests,
    which otherwise have to mutate global state to exercise the branch that
    matters.
    """
    args = list(sys.argv[1:] if argv is None else argv)

    if args:
        # ``opencsi-monitor --once`` and ``opencsi tray --once`` are the same
        # request; only the first has the sub-command name implied.
        from ..cli.app import main as cli_main

        return cli_main(["tray", *args])

    service = None
    try:
        from ..cli.context import make_context
        from ..monitor import MonitorService
        from .app import TrayApp, TrayUnavailableError

        # ``[]`` rather than sys.argv: this branch takes no arguments, and
        # parsing the real argv would make an unrelated flag an error.
        ctx, _args = make_context([])
        service = MonitorService(ctx.make_client())
    except Exception as exc:  # noqa: BLE001
        _report(exc)
        return 1

    try:
        return TrayApp(service).run()
    except TrayUnavailableError as exc:
        _report(exc)
        return 2
    except KeyboardInterrupt:
        service.stop()
        return 0
    except Exception as exc:  # noqa: BLE001
        _report(exc)
        return 1


def _report(exc: BaseException) -> None:
    """Best-effort diagnostics: stderr if there is one, always the log."""
    import logging

    logging.getLogger("opencsi.tray").exception("the tray exited with an error")
    if sys.stderr is not None:
        try:
            print(f"opencsi tray: {type(exc).__name__}: {exc}", file=sys.stderr)
        except Exception:  # noqa: BLE001 - a windowed process has no stderr
            pass


if __name__ == "__main__":
    raise SystemExit(main())
