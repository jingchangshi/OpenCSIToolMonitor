"""Monitoring domain layer: the logic a UI renders, with no UI in it.

The tray is a view over :class:`MonitorService`. Keeping the polling loop,
renewal policy and error classification here -- rather than inside the tray
callback -- is what lets the whole thing be tested with a fake clock and no
Windows session.
"""

from .service import (
    STATE_LABELS,
    MonitorConfig,
    MonitorService,
    MonitorSnapshot,
    MonitorState,
    state_for_error,
)

__all__ = [
    "MonitorService",
    "MonitorSnapshot",
    "MonitorState",
    "MonitorConfig",
    "STATE_LABELS",
    "state_for_error",
]
