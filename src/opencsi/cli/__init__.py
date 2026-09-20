"""Command line interface for opencsi.

Each sub-command lives in its own module and exposes ``register(subparsers)``.
That keeps ``opencsi --help`` cheap and makes the command set discoverable by
reading this package.
"""

from __future__ import annotations

from .app import main

__all__ = ["main"]
