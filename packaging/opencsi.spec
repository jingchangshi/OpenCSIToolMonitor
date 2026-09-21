# PyInstaller spec for OpenCSIToolMonitor.
#
# Build:  pyinstaller --clean --noconfirm packaging/opencsi.spec
# Output: dist/opencsi.exe       (console CLI)
#         dist/opencsi-tray.exe  (windowed tray)
#
# Two binaries, not one. They have genuinely different needs: the CLI is a
# console program whose whole point is text output, and the tray is a windowed
# program that must never show a console. ``--windowed`` is per-binary in
# PyInstaller, so a single EXE cannot be both.
#
# The imports below are the interesting part. The project imports its optional
# dependencies (pystray, PIL) *lazily and inside functions*, which is deliberate
# -- it keeps ``opencsi --help`` working on a machine with no extras installed
# -- but it also means PyInstaller's static analysis cannot see them. Without
# the ``hiddenimports`` list the tray would build successfully and then fail at
# runtime with a missing module, which is the worst kind of packaging bug
# because the build itself looks clean.

import importlib.util
import sys
from pathlib import Path

# Imported explicitly rather than relying on the spec namespace: PyInstaller
# injects its helpers into the exec globals, but which ones are present varies
# by version, and `collect_data_files` is absent in 6.22 -- the build failed
# with a NameError that pointed at the spec line rather than at the version.
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# The spec is executed by PyInstaller with an unspecified cwd, so the source
# root is derived from this file's own location rather than assumed.
ROOT = Path(SPECPATH).resolve().parent

#: Modules imported lazily by the code, so they are invisible to static analysis.
#: Each one is here because a *specific* call site needs it, not defensively:
#:   pystray / PIL.Image / PIL.ImageDraw  -> opencsi.tray.app + icons
#:   PIL.ImageOps                         -> opencsi.auth.qr_render (trim_white)
#:   winreg                               -> opencsi.tray.startup
#:   segno                                -> opencsi.auth.qr_render (encode_text)
#: PIL.ImageTk / PIL.ImageQt are deliberately absent: pystray can use them, but
#: on Windows it uses the native backend, and pulling in Tk would add ~10 MB for
#: a code path this build never takes.
_WANTED = [
    "pystray",
    "pystray._win32",
    "PIL.Image",
    "PIL.ImageDraw",
    "PIL.ImageOps",
    "PIL.ImageFont",
    "winreg",
    "segno",
]


def _installed(name: str) -> bool:
    """Whether ``name`` can actually be imported in this build environment.

    A hidden import that does not exist is not fatal -- PyInstaller warns and
    carries on -- but it makes every build print noise about packages the
    builder never asked for, and noise is how real warnings get ignored. Since
    every optional extra is genuinely optional, the list is filtered instead.
    """
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


HIDDEN_IMPORTS = [name for name in _WANTED if _installed(name)]

# pystray picks its backend at import time by *name*, not by a static import, so
# analysis cannot see `pystray._win32` even though it is the one that runs. Its
# submodules are collected wholesale rather than guessed at, because getting
# this list wrong yields a tray that builds cleanly and then dies with
# "no usable backend" on the user's machine.
if _installed("pystray"):
    HIDDEN_IMPORTS += collect_submodules("pystray")

#: pystray and segno ship data (icons, fonts) that analysis will not collect.
#: Only packages that are actually installed contribute; a build without the
#: optional extras is still valid and simply produces a CLI-only-capable pair.
_datas = []
for _package in ("pystray", "segno", "PIL"):
    if _installed(_package):
        _datas += collect_data_files(_package)

_common = dict(
    datas=_datas,
    hiddenimports=HIDDEN_IMPORTS,
    # The source lives in src/, so it is not importable by default; pathex tells
    # PyInstaller where to look without requiring an editable install first.
    pathex=[str(ROOT / "src")],
    # Trim what this project provably never uses. Each exclusion is a real
    # dependency of something in the import graph that is irrelevant here.
    excludes=[
        "tkinter",
        "unittest",
        "pytest",
        "pydoc",
        "doctest",
        "test",
        "distutils",
        "setuptools",
        "pip",
    ],
    # No console for either binary's *own* messages; the CLI re-adds one below.
    # Keeping the two builds separate is what makes this expressible.
    noarchive=False,
)

cli = Analysis(
    [str(ROOT / "packaging" / "cli_entry.py")],
    **_common,
)
cli_pyz = PYZ(cli.pure, cli.zipped_data)  # noqa: F821

cli_exe = EXE(  # noqa: F821
    cli_pyz,
    cli.scripts,
    cli.binaries,
    cli.zipfiles,
    cli.datas,
    name="opencsi",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX compression is a common antivirus false-positive trigger.
    console=True,
    disable_windowed_traceback=False,
)

tray = Analysis(
    [str(ROOT / "packaging" / "tray_entry.py")],
    **_common,
)
tray_pyz = PYZ(tray.pure, tray.zipped_data)  # noqa: F821

tray_exe = EXE(  # noqa: F821
    tray_pyz,
    tray.scripts,
    tray.binaries,
    tray.zipfiles,
    tray.datas,
    name="opencsi-tray",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # The whole reason this is a separate binary: a tray must not flash a
    # console window at every sign-in.
    console=False,
    disable_windowed_traceback=False,
)
