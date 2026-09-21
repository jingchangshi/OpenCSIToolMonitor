"""Windows 11 notification-area tray for openCsiTool usage.

The tray is a *view* over :class:`~opencsi.monitor.MonitorService`. It imports
the domain layer directly -- it never shells out to ``opencsi usage --json`` --
so there is one copy of the auth logic, no subprocess to spawn and no JSON to
re-parse.

pystray and Pillow are optional extras. Importing this package must work without
them, so that ``opencsi tray --help`` and ``opencsi doctor`` still run on a
machine that has not installed them; only actually showing the tray requires the
extra. That is why nothing here imports pystray at module scope.
"""

from __future__ import annotations

from .presenter import (
    MAX_TOOLTIP,
    STATE_LABELS_CN,
    Action,
    actions_for,
    format_age,
    format_count,
    format_count_cn,
    format_duration,
    headline_for,
    label_cn,
    menu_signature,
    status_text,
    tooltip_for,
)
from .single_instance import SingleInstance
from .startup import RUN_KEY, VALUE_NAME, StartupManager, StartupStatus, default_command

__all__ = [
    "Action",
    "actions_for",
    "format_age",
    "format_count",
    "format_count_cn",
    "format_duration",
    "headline_for",
    "label_cn",
    "menu_signature",
    "status_text",
    "tooltip_for",
    "MAX_TOOLTIP",
    "STATE_LABELS_CN",
    "SingleInstance",
    "StartupManager",
    "StartupStatus",
    "default_command",
    "RUN_KEY",
    "VALUE_NAME",
    "tray_available",
    "TrayApp",
    "TrayUnavailableError",
]


def tray_available() -> tuple[bool, str | None]:
    """Whether the tray's optional dependencies are installed.

    Defined here (not only in ``app``) so it can be called without importing
    pystray, which is the whole point of the check.
    """
    missing: list[str] = []
    try:
        import pystray  # noqa: F401
    except Exception:  # noqa: BLE001
        missing.append("pystray")
    try:
        import PIL.Image  # noqa: F401
    except Exception:  # noqa: BLE001
        missing.append("Pillow")
    if missing:
        return False, "missing: " + ", ".join(missing)
    return True, None


def __getattr__(name: str):
    """Import the tray app lazily.

    ``TrayApp`` pulls in pystray; keeping it behind ``__getattr__`` means
    ``import opencsi.tray`` works on a machine without the extra.
    """
    if name in ("TrayApp", "TrayUnavailableError"):
        from . import app

        return getattr(app, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
