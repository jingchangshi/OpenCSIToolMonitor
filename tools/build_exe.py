"""Build the standalone Windows binaries.

PyInstaller is driven through the spec at ``packaging/opencsi.spec`` rather than
by command-line flags, because the spec is where the hidden imports live and
those are the difference between a build that works and one that dies at
runtime.

Usage:
    python tools/build_exe.py            # build both binaries
    python tools/build_exe.py --check    # report what would be built

The script exists so the build is one command a human can remember, and so the
extra it needs is reported clearly rather than as an ImportError traceback.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "opencsi.spec"

#: What the spec produces. Kept here so a build that silently stops producing
#: one of them is caught rather than discovered by a user.
EXPECTED = ("opencsi.exe", "opencsi-tray.exe")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the standalone binaries.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="report whether the build can run, and change nothing",
    )
    parser.add_argument(
        "--distpath", default=str(ROOT / "dist"), help="where to put the binaries"
    )
    args = parser.parse_args()

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller is not installed. It is a build-only dependency:")
        print('  pip install "opencsi[build]"')
        return 1

    if not SPEC.exists():
        print(f"the spec file is missing: {SPEC}")
        return 1

    if args.check:
        print(f"spec    : {SPEC}")
        print(f"distpath: {args.distpath}")
        print("PyInstaller is available; run without --check to build.")
        return 0

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--distpath",
        args.distpath,
        "--workpath",
        str(ROOT / "build"),
        str(SPEC),
    ]
    print("building:", " ".join(command))
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode != 0:
        print(f"the build failed (exit {result.returncode})")
        return result.returncode

    missing = [
        name for name in EXPECTED if not (Path(args.distpath) / name).exists()
    ]
    if missing:
        # A build that exits 0 but does not produce the artefacts is the failure
        # mode worth catching: everything looks fine until someone runs it.
        print(f"the build reported success but did not produce: {', '.join(missing)}")
        return 1

    print()
    print("built:")
    for name in EXPECTED:
        path = Path(args.distpath) / name
        size = path.stat().st_size / (1024 * 1024)
        print(f"  {path}  ({size:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
