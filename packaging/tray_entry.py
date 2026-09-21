"""Frozen-build entry point for the tray.

PyInstaller needs a *script* to analyse, and the console-script shim that
``pip`` generates for ``opencsi-monitor`` is not a file in this repository. This
module is that script: it does nothing but hand control to
:func:`opencsi.tray.__main__.main`, which is the same code path
``python -m opencsi.tray`` takes.

Keeping it this thin is deliberate. A frozen build that ran different code from
the source install would be a build that is only tested in its frozen form,
which is the opposite of what a packaging step should do.

Built with ``--windowed``: the tray has no console, and a console window
appearing at every sign-in would be worse than useless.
"""

from __future__ import annotations

import sys


def main() -> int:
    from opencsi.tray.__main__ import main as tray_main

    return tray_main()


if __name__ == "__main__":
    sys.exit(main())
