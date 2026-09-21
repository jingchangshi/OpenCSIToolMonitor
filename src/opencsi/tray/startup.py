"""Start the tray automatically at sign-in, via the per-user Run key.

``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`` is the right place:
it is per-user (so no administrator rights, and no effect on other accounts),
it is what Windows' own Task Manager "Startup" tab reads, and disabling it there
or here is the same operation. A scheduled task would be more powerful and would
also be more to explain, more to go wrong and harder for a user to undo.

Nothing is written without an explicit ``enable()``. An installer that silently
adds a startup entry is the kind of behaviour users are right to resent.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass

log = logging.getLogger("opencsi.tray.startup")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

#: The value name Windows shows in Task Manager's Startup tab.
VALUE_NAME = "OpenCSIToolMonitor"


def _quote(path: str) -> str:
    """Quote a command path for the Run key.

    Windows splits the value on spaces unless it is quoted, and a path under
    ``C:\\Program Files`` would otherwise be read as a command plus arguments.
    """
    return f'"{path}"' if " " in path and not path.startswith('"') else path


def default_command() -> str:
    """The command that starts the tray.

    Prefers a frozen executable (the PyInstaller build) and falls back to
    ``pythonw.exe -m opencsi.tray``. ``pythonw`` rather than ``python`` because
    the console variant would flash a black window at every sign-in.
    """
    if getattr(sys, "frozen", False):
        return _quote(sys.executable)

    executable = sys.executable or "python"
    # Prefer pythonw.exe: no console window on startup.
    candidate = os.path.join(os.path.dirname(executable), "pythonw.exe")
    if os.path.exists(candidate):
        executable = candidate
    return f"{_quote(executable)} -m opencsi.tray"


@dataclass(frozen=True)
class StartupStatus:
    """Whether the tray is registered to start at sign-in."""

    supported: bool
    enabled: bool = False
    command: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"supported": self.supported, "enabled": self.enabled}
        if self.command:
            out["command"] = self.command
        if self.detail:
            out["detail"] = self.detail
        return out


class StartupManager:
    """Read and write the per-user startup entry.

    All registry access is lazily imported and fully guarded: this class is used
    by ``opencsi doctor`` and by tests on any platform, so an unavailable
    registry must produce a clear "not supported", never a crash.
    """

    def __init__(self, *, value_name: str = VALUE_NAME, key: str = RUN_KEY) -> None:
        self._value_name = value_name
        self._key = key

    @property
    def supported(self) -> bool:
        return os.name == "nt"

    def _open(self, *, write: bool):
        import winreg

        access = winreg.KEY_SET_VALUE if write else winreg.KEY_READ
        return winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._key, 0, access)

    def status(self) -> StartupStatus:
        """Whether the entry exists, and what it would run."""
        if not self.supported:
            return StartupStatus(supported=False, detail="only available on Windows")
        try:
            import winreg

            with self._open(write=False) as key:
                try:
                    command, _kind = winreg.QueryValueEx(key, self._value_name)
                except FileNotFoundError:
                    return StartupStatus(supported=True, enabled=False)
            return StartupStatus(
                supported=True, enabled=True, command=str(command)
            )
        except OSError as exc:
            return StartupStatus(
                supported=True, detail=f"could not read the startup key ({exc.errno})"
            )
        except Exception as exc:  # noqa: BLE001
            return StartupStatus(
                supported=True, detail=f"could not read the startup key ({type(exc).__name__})"
            )

    def enable(self, command: str | None = None) -> StartupStatus:
        """Register the tray to start at sign-in. Idempotent."""
        if not self.supported:
            return StartupStatus(supported=False, detail="only available on Windows")
        value = command or default_command()
        try:
            import winreg

            with self._open(write=True) as key:
                winreg.SetValueEx(key, self._value_name, 0, winreg.REG_SZ, value)
            log.info("registered the tray to start at sign-in")
            return StartupStatus(supported=True, enabled=True, command=value)
        except OSError as exc:
            return StartupStatus(
                supported=True, detail=f"could not write the startup key ({exc.errno})"
            )
        except Exception as exc:  # noqa: BLE001
            return StartupStatus(
                supported=True, detail=f"could not write the startup key ({type(exc).__name__})"
            )

    def disable(self) -> StartupStatus:
        """Remove the entry. Idempotent: removing a missing value is a no-op."""
        if not self.supported:
            return StartupStatus(supported=False, detail="only available on Windows")
        try:
            import winreg

            with self._open(write=True) as key:
                try:
                    winreg.DeleteValue(key, self._value_name)
                except FileNotFoundError:
                    pass
            log.info("removed the tray startup entry")
            return StartupStatus(supported=True, enabled=False)
        except OSError as exc:
            return StartupStatus(
                supported=True, detail=f"could not update the startup key ({exc.errno})"
            )
        except Exception as exc:  # noqa: BLE001
            return StartupStatus(
                supported=True, detail=f"could not update the startup key ({type(exc).__name__})"
            )

    def set_enabled(self, enabled: bool) -> StartupStatus:
        return self.enable() if enabled else self.disable()
