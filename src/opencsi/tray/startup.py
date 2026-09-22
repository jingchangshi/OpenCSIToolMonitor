"""Start the tray automatically at sign-in, via the per-user Run key.

``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`` is the right place:
it is per-user (so no administrator rights, and no effect on other accounts),
it is what Windows' own Task Manager "Startup" tab reads, and disabling it there
or here is the same operation. A scheduled task would be more powerful and would
also be more to explain, more to go wrong and harder for a user to undo.

Nothing is written without an explicit ``enable()``. An installer that silently
adds a startup entry is the kind of behaviour users are right to resent.

Which command gets registered
-----------------------------
There are three ways this code runs, and they need three different commands. The
first version of this module had one branch for "frozen" and got two of the three
wrong:

==============  =======================  ===================================
How it runs     ``sys.executable``       what must be registered
==============  =======================  ===================================
source checkout ``…\\python.exe``        ``…\\pythonw.exe -m opencsi.tray``
frozen CLI      ``…\\opencsi.exe``       ``"…\\opencsi-tray.exe"``
frozen tray     ``…\\opencsi-tray.exe``  ``"…\\opencsi-tray.exe"``
==============  =======================  ===================================

The defect was in the frozen branch, and it had two halves:

* it registered ``sys.executable`` **with no arguments**, so from the frozen
  *CLI* it registered ``opencsi.exe`` -- which, with no sub-command, prints usage
  and exits. Sign-in would have run a program that did nothing.
* it registered the **console** binary, so even had it passed ``tray``, every
  sign-in would have flashed a black console window. That is precisely what the
  separate windowed ``opencsi-tray.exe`` exists to prevent, and the packaging
  spec builds it.

:func:`default_command` now resolves the windowed sibling when one exists, falls
back to ``sys.executable tray`` when it does not, and reports which of the three
shapes it chose so ``opencsi tray --startup-status`` can be checked against what
the registry actually holds.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("opencsi.tray.startup")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

#: The value name Windows shows in Task Manager's Startup tab.
VALUE_NAME = "OpenCSIToolMonitor"

#: The windowed tray binary's name, as produced by ``packaging/opencsi.spec``.
#: Kept as a constant so the launcher, the spec and the documentation cannot
#: drift apart -- the test suite asserts the spec really does emit this name.
TRAY_BINARY_NAME = "opencsi-tray.exe"

#: How the command was derived. Reported rather than inferred, because "which
#: binary will Windows actually start?" is the question this module exists to
#: answer and a caller should not have to re-derive it from a string.
#:
#: There are four labels for the three running contexts, because the frozen CLI
#: has two outcomes and they are not the same fact:
#:
#: ``frozen-tray``      this process *is* the windowed tray binary
#: ``frozen-cli-tray``  this process is the frozen CLI, and the tray was found
#:                      beside it -- the normal shape of a real install
#: ``frozen-cli``       this process is the frozen CLI and no tray exists, so the
#:                      console build is registered with a ``tray`` sub-command
#: ``source``           running from a checkout
#:
#: Collapsing the middle two into ``frozen-tray`` was a defect, not a shortcut:
#: ``frozen-tray`` is documented as describing *this build*, and it was being
#: reported by a process that was demonstrably not the tray binary. A user running
#: ``opencsi.exe tray --startup-status`` was told ``derived from: frozen-tray``,
#: which reads as "this is the tray" and is false.
SOURCE_FROZEN_TRAY = "frozen-tray"
SOURCE_FROZEN_CLI_TRAY = "frozen-cli-tray"
SOURCE_FROZEN_CLI = "frozen-cli"
SOURCE_SOURCE_INSTALL = "source"


def _quote(path: str) -> str:
    """Quote a command path for the Run key.

    Windows splits the value on spaces unless it is quoted, and a path under
    ``C:\\Program Files`` would otherwise be read as a command plus arguments.
    """
    return f'"{path}"' if " " in path and not path.startswith('"') else path


def _frozen_tray_sibling() -> Path | None:
    """The windowed tray binary next to a frozen CLI, when it exists.

    ``packaging/opencsi.spec`` emits ``opencsi.exe`` and ``opencsi-tray.exe``
    side by side, so the sibling is the normal case for a real install. It is
    looked for by name rather than assumed present because a build that produced
    only the CLI is still a legitimate build, and registering a path that does
    not exist would make sign-in fail silently -- the one outcome a startup entry
    must never have.
    """
    executable = getattr(sys, "executable", None)
    if not executable:
        return None
    try:
        candidate = Path(executable).resolve().parent / TRAY_BINARY_NAME
    except OSError:
        return None
    try:
        return candidate if candidate.is_file() else None
    except OSError:
        return None


def _is_frozen_tray() -> bool:
    """Whether *this* process is the windowed tray binary.

    Distinguished from the frozen CLI by name. It matters because the tray binary
    takes no sub-command: registering ``"opencsi-tray.exe" tray`` would start a
    tray that tries to interpret ``tray`` as an argument.
    """
    executable = getattr(sys, "executable", None)
    if not executable:
        return False
    return Path(executable).name.lower() == TRAY_BINARY_NAME.lower()


def startup_command_for_tray() -> tuple[str, str]:
    """The command to register so that Windows sign-in starts the tray.

    Returns ``(command, source)``. The source is one of
    :data:`SOURCE_FROZEN_TRAY`, :data:`SOURCE_FROZEN_CLI_TRAY`,
    :data:`SOURCE_FROZEN_CLI` or :data:`SOURCE_SOURCE_INSTALL`, so a caller can
    report *why* it chose what it did instead of just printing a path.

    Named for the tray rather than for "this process", because that is the whole
    point: the Run entry always means *start the tray*. An earlier generic name
    (``default_command``) invited the reading "restart whatever called me", and
    that reading is what produced the original defect -- from the frozen CLI it
    registered ``opencsi.exe`` with no sub-command, which prints usage and exits,
    so sign-in ran a program that did nothing at all.
    """
    if getattr(sys, "frozen", False):
        if _is_frozen_tray():
            return _quote(sys.executable), SOURCE_FROZEN_TRAY

        sibling = _frozen_tray_sibling()
        if sibling is not None:
            # The correct answer for a real install: the windowed binary, run
            # with no arguments. Reported as its own source because *this*
            # process is the CLI, not the tray -- see the constants above.
            return _quote(str(sibling)), SOURCE_FROZEN_CLI_TRAY

        # A frozen CLI with no tray binary beside it. The sub-command is
        # mandatory here -- without it this registers a program that prints
        # usage and exits, which is exactly the defect being fixed.
        return f"{_quote(sys.executable)} tray", SOURCE_FROZEN_CLI

    executable = sys.executable or "python"
    # Prefer pythonw.exe: no console window on startup.
    candidate = os.path.join(os.path.dirname(executable), "pythonw.exe")
    if os.path.exists(candidate):
        executable = candidate
    return f"{_quote(executable)} -m opencsi.tray", SOURCE_SOURCE_INSTALL


#: Back-compatible alias. Kept because the name is referenced from the CLI, the
#: tray and the docs, and because the two names answer the same question -- but
#: ``startup_command_for_tray`` is the one to use in new code, since it says what
#: the command is *for* rather than merely that it is a default.
def startup_command() -> tuple[str, str]:
    """Alias of :func:`startup_command_for_tray`."""
    return startup_command_for_tray()


def default_command() -> str:
    """The command that starts the tray. See :func:`startup_command_for_tray`."""
    return startup_command_for_tray()[0]


@dataclass(frozen=True)
class StartupStatus:
    """Whether the tray is registered to start at sign-in."""

    supported: bool
    enabled: bool = False
    command: str | None = None
    detail: str | None = None
    #: How :func:`startup_command` derived the command it would register. Not
    #: read back from the registry: it describes *this* build, and comparing it
    #: with ``command`` is how a stale or wrong entry becomes visible.
    source: str | None = None

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"supported": self.supported, "enabled": self.enabled}
        if self.command:
            out["command"] = self.command
        if self.source:
            out["source"] = self.source
        if self.detail:
            out["detail"] = self.detail
        return out

    @property
    def matches_this_build(self) -> bool:
        """Whether the registered command is the one this build would register.

        A registered entry pointing at a different binary is the failure this
        whole module is about: sign-in starts something that is not the tray, and
        nothing says so. ``False`` when nothing is registered.
        """
        if not self.enabled or not self.command:
            return False
        return self.command.strip() == default_command().strip()


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
                    return StartupStatus(
                        supported=True,
                        enabled=False,
                        source=startup_command_for_tray()[1],
                    )
            return StartupStatus(
                supported=True,
                enabled=True,
                command=str(command),
                source=startup_command_for_tray()[1],
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
        """Register the tray to start at sign-in. Idempotent.

        When ``command`` is not supplied the value comes from
        :func:`startup_command`, which is what makes the frozen-CLI case work:
        from ``opencsi.exe`` it registers the *windowed* sibling, not itself.
        """
        if not self.supported:
            return StartupStatus(supported=False, detail="only available on Windows")
        derived, source = startup_command_for_tray()
        value = command or derived
        try:
            import winreg

            with self._open(write=True) as key:
                winreg.SetValueEx(key, self._value_name, 0, winreg.REG_SZ, value)
            log.info("registered the tray to start at sign-in")
            return StartupStatus(
                supported=True, enabled=True, command=value, source=source
            )
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
