"""Frozen-build entry point for the CLI.

The console counterpart of ``tray_entry.py``. PyInstaller's ``--onefile`` build
of this produces a standalone ``opencsi.exe`` that needs no Python install,
which is what makes the tool usable on the air-gapped Windows machines the
project targets.

It imports the CLI lazily inside ``main`` rather than at module scope so that
PyInstaller's analysis follows the import graph from the real entry point
instead of from a partially-initialised module.
"""

from __future__ import annotations

import sys


def main() -> int:
    from opencsi.cli.app import main as cli_main

    return cli_main()


if __name__ == "__main__":
    sys.exit(main())
