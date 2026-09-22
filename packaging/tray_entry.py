"""Frozen-build entry point for the tray.

PyInstaller needs a *script* to analyse, and the console-script shim that
``pip`` generates for ``opencsi-monitor`` is not a file in this repository. This
module is that script: it hands control to :func:`opencsi.tray.__main__.main`,
which is the same code path both ``python -m opencsi.tray`` and the declared
``opencsi-monitor`` console script take.

Keeping it this thin is deliberate. A frozen build that ran different code from
the source install would be a build that is only tested in its frozen form,
which is the opposite of what a packaging step should do.

Built with ``--windowed``: the tray has no console, and a console window
appearing at every sign-in would be worse than useless.

Arguments
---------
The argument handling used to live *here*, which is how it came to be wrong in
two other places at once. ``opencsi-tray.exe --once`` was fixed to forward its
arguments to the CLI, but the declared ``opencsi-monitor`` console script and
``python -m opencsi.tray`` kept discarding them, so ``opencsi-monitor --help``
hung forever. The logic now lives in one place and this script only calls it.
"""

from __future__ import annotations

import sys


def main() -> int:
    # Imported inside the function, and the *module* rather than the function,
    # so a test can patch `opencsi.tray.__main__.main` and see the effect. A
    # module-level `from ... import main` would bind the name before any patch
    # could apply, which would make the delegation untestable.
    from opencsi.tray import __main__ as tray_main

    return tray_main.main()


if __name__ == "__main__":
    sys.exit(main())

