"""``python -m opencsi.tray`` -- start the tray.

Exists so the Windows startup entry can be ``pythonw.exe -m opencsi.tray``
without needing the console script to be on ``PATH``. It exits quietly rather
than printing a traceback, because when Windows launches this at sign-in there
is no console to show it in and nobody to read it -- the log file is the place
for that.
"""

from __future__ import annotations

import sys


def main() -> int:
    from .app import TrayApp, TrayUnavailableError

    try:
        from ..cli.context import CliContext
        from ..monitor import MonitorService

        ctx = CliContext.from_args([])
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
