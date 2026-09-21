"""Frozen-build entry point for the tray.

PyInstaller needs a *script* to analyse, and the console-script shim that
``pip`` generates for ``opencsi-monitor`` is not a file in this repository. This
module is that script: it hands control to :func:`opencsi.tray.__main__.main`,
which is the same code path ``python -m opencsi.tray`` takes.

Keeping it this thin is deliberate. A frozen build that ran different code from
the source install would be a build that is only tested in its frozen form,
which is the opposite of what a packaging step should do.

Built with ``--windowed``: the tray has no console, and a console window
appearing at every sign-in would be worse than useless.

Arguments
---------
With no arguments -- the double-click and start-at-sign-in case -- this starts
the tray, exactly as before.

With arguments it delegates to the CLI's ``tray`` sub-command. It used to ignore
them entirely, which meant ``opencsi-tray.exe --once`` printed nothing and then
sat there as a resident tray forever: the user asked for one snapshot and got a
process that never exits. Silently discarding the request is worse than failing
it, because there is nothing to notice and nothing to report.

The sub-command name is supplied here rather than required from the caller so
that the intuitive ``opencsi-tray.exe --once`` works, matching the console
binary's ``opencsi tray --once``. Both then run the *same* handler.
"""

from __future__ import annotations

import sys


def main() -> int:
    argv = list(sys.argv[1:])

    if argv:
        # ``opencsi-tray --once`` and ``opencsi tray --once`` are the same
        # request; only the first has the sub-command name implied.
        from opencsi.cli.app import main as cli_main

        return cli_main(["tray", *argv])

    from opencsi.tray.__main__ import main as tray_main

    return tray_main()


if __name__ == "__main__":
    sys.exit(main())
