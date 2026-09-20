"""Allow ``python -m opencsi`` to run the CLI without installation."""

from __future__ import annotations

from .cli.app import main

if __name__ == "__main__":
    raise SystemExit(main())
