"""``python -m opencsi.tray`` -- start the tray.

Exists so the Windows startup entry can be ``pythonw.exe -m opencsi.tray``
without needing the console script to be on ``PATH``. It exits quietly rather
than printing a traceback, because when Windows launches this at sign-in there
is no console to show it in and nobody to read it -- the log file is the place
for that.

The context is built through the same ``make_context`` the CLI uses, with an
explicitly empty argv, so the tray gets exactly the defaults a user would get
from a bare ``opencsi tray`` and no argument parsing happens twice.
"""

from __future__ import annotations

import sys


def main() -> int:
    service = None
    try:
        from ..cli.context import make_context
        from ..monitor import MonitorService
        from .app import TrayApp, TrayUnavailableError

        # ``[]`` rather than sys.argv: this entry point takes no arguments, and
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
