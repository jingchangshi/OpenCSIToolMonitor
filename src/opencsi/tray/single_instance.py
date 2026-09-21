"""Single-instance guard for the tray.

Two tray icons in the notification area is a confusing, self-inflicted bug, and
the second instance would double the API traffic. Windows named mutexes are the
native mechanism and cost nothing.

The mutex is held for the process's lifetime and released by the OS on exit, so
a crash cannot leave a stale lock behind -- which is exactly why a *file* lock
would be the wrong choice here.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("opencsi.tray.single")

#: ``Local\`` scopes the mutex to the login session, so two different Windows
#: users can each run their own tray -- correct, because each has their own
#: credentials.
MUTEX_NAME = r"Local\OpenCsiToolMonitorTray"

_ERROR_ALREADY_EXISTS = 183


class SingleInstance:
    """Hold a Windows named mutex for the process's lifetime.

    On a non-Windows platform (or if the mutex cannot be created) this degrades
    to "always allow": refusing to start because a lock is unavailable would
    trade a cosmetic problem for a broken feature.
    """

    def __init__(self, name: str = MUTEX_NAME) -> None:
        self._name = name
        self._handle = None
        self._acquired = False

    @property
    def acquired(self) -> bool:
        """Whether this process owns the lock."""
        return self._acquired

    def acquire(self) -> bool:
        """Try to become the only instance.

        Returns ``True`` when this process may proceed. Returns ``False`` only
        when another instance is *definitely* running.
        """
        if os.name != "nt":
            # No named mutexes off Windows; the tray is Windows-only anyway.
            self._acquired = True
            return True
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = [
                wintypes.LPVOID,
                wintypes.BOOL,
                wintypes.LPCWSTR,
            ]
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            handle = kernel32.CreateMutexW(None, True, self._name)
            if not handle:
                log.debug("CreateMutexW failed; allowing this instance")
                self._acquired = True
                return True
            if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
                kernel32.CloseHandle(handle)
                self._acquired = False
                return False
            self._handle = handle
            self._acquired = True
            return True
        except Exception as exc:  # noqa: BLE001 - never block startup on this
            log.debug("could not create the single-instance mutex: %s", type(exc).__name__)
            self._acquired = True
            return True

    def release(self) -> None:
        """Release the mutex. Idempotent."""
        if self._handle is not None and os.name == "nt":
            try:
                import ctypes

                ctypes.WinDLL("kernel32").CloseHandle(self._handle)
            except Exception:  # noqa: BLE001
                pass
        self._handle = None
        self._acquired = False

    def __enter__(self) -> "SingleInstance":
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()
