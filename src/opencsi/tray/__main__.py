"""``python -m opencsi.tray`` -- start the tray, or run a tray sub-command.

Exists so the Windows startup entry can be ``pythonw.exe -m opencsi.tray``
without needing the console script to be on ``PATH``.

**Why failures are shown in a message box.** This module used to say "the log
file is the place for that", but no log file is configured anywhere in the
project -- so in the frozen ``--windowed`` build, which has no console either,
every diagnostic written here reached nobody. The visible symptom was a
double-click that produced no icon and no message: indistinguishable from a
broken program. A second launch while the tray is already running is the worst
case, because that is a normal thing to do and the recovery step (quit the icon
that is already in the notification area) is not guessable. The paths that exit
without ever showing an icon now raise a native message box, with stderr as the
fallback when there is no GUI.

Arguments
---------
With no arguments -- the double-click and start-at-sign-in case -- this starts
the resident tray.

With arguments it delegates to the CLI's ``tray`` sub-command, so
``python -m opencsi.tray --once`` and ``opencsi tray --once`` are the same
request.

It used to ignore ``sys.argv`` entirely and always start the GUI. That was fixed
once for the frozen entry script, but the fix lived in ``packaging/tray_entry.py``
-- so the *declared console script* ``opencsi-monitor`` and the module form both
kept the bug. ``opencsi-monitor --help`` printed nothing and then sat in the
notification area forever, and a user who asked for one snapshot got a process
that never returns. Discarding the request is worse than failing it, because
there is nothing to notice and nothing to report.

The delegation now lives here, and the frozen entry script calls this function
rather than repeating the logic -- so there is one place that decides, which is
what the rest of the tray already assumes.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    """Start the tray, or forward ``argv`` to the CLI's ``tray`` sub-command.

    ``argv`` defaults to ``sys.argv[1:]``. Passing it explicitly is for tests,
    which otherwise have to mutate global state to exercise the branch that
    matters.
    """
    args = list(sys.argv[1:] if argv is None else argv)

    if args:
        # ``opencsi-monitor --once`` and ``opencsi tray --once`` are the same
        # request; only the first has the sub-command name implied.
        #
        # A leading ``tray`` is tolerated because this binary *is* the tray, so
        # the sub-command name is redundant here -- and a user who has just read
        # `opencsi tray --check` in the docs will type `opencsi-tray.exe tray
        # --check`. Without this the answer is "unrecognized arguments: tray",
        # which names the problem but not the fix. It cost a CI job its first
        # draft, which is a fair sign it would cost a user a support question.
        if args[0] == "tray":
            args = args[1:]
            if not args:
                # ``opencsi-tray.exe tray`` on its own means "start the tray",
                # which is what the no-argument branch below does.
                return _run_resident()

        from ..cli.app import main as cli_main

        return cli_main(["tray", *args])

    return _run_resident()


def _run_resident() -> int:
    """Start the resident tray. The double-click and start-at-sign-in case."""
    service = None
    try:
        from ..cli.context import make_context
        from ..monitor import MonitorService
        from .app import ALREADY_RUNNING_EXIT, TrayApp, TrayUnavailableError

        # ``[]`` rather than sys.argv: this branch takes no arguments, and
        # parsing the real argv would make an unrelated flag an error.
        ctx, _args = make_context([])
        service = MonitorService(ctx.make_client())
    except Exception as exc:  # noqa: BLE001
        _report(exc)
        _show_message(
            "OpenCSI Monitor could not start:\n\n"
            f"{type(exc).__name__}: {exc}\n\n"
            "Run 'opencsi doctor' in a terminal for details."
        )
        return 1

    try:
        code = TrayApp(service).run()
        if code == ALREADY_RUNNING_EXIT:
            # The one outcome that must be *shown*, not logged: a second launch
            # produces no icon, so silence here looks like a broken program.
            _show_message(_already_running_message())
        return code
    except TrayUnavailableError as exc:
        _report(exc)
        _show_message(f"OpenCSI Monitor could not start:\n\n{exc}")
        return 2
    except KeyboardInterrupt:
        service.stop()
        return 0
    except Exception as exc:  # noqa: BLE001
        _report(exc)
        _show_message(
            "OpenCSI Monitor stopped unexpectedly:\n\n"
            f"{type(exc).__name__}: {exc}\n\n"
            "Run 'opencsi doctor' in a terminal for details."
        )
        return 1


def _already_running_message() -> str:
    """What to tell a user who started a second tray.

    The windowed build has no console, so ``log.error`` reaches nobody -- and
    there is no log file either, despite an earlier version of this module's
    docstring claiming otherwise. A double-click that produces no icon and no
    message is indistinguishable from a broken program, and the recovery step
    (quit the tray that is already in the notification area, or use its Exit menu
    item) is not guessable.
    """
    return (
        "OpenCSI Monitor is already running.\n\n"
        "Look for its icon in the notification area (you may need to expand the "
        "hidden-icons arrow). To start it again, right-click that icon and "
        "choose Exit first."
    )


def _report(exc: BaseException) -> None:
    """Best-effort diagnostics: stderr if there is one, always the log."""
    import logging

    logging.getLogger("opencsi.tray").exception("the tray exited with an error")
    if sys.stderr is not None:
        try:
            print(f"opencsi tray: {type(exc).__name__}: {exc}", file=sys.stderr)
        except Exception:  # noqa: BLE001 - a windowed process has no stderr
            pass


def _show_message(text: str) -> None:
    """Report a startup failure to whoever is actually there to read it.

    A **windowed** process is the case this exists for: it has no console, so a
    traceback goes nowhere and the user sees a double-click that did nothing.
    That condition is precisely ``sys.stderr is None``, which is how the frozen
    ``--windowed`` build behaves -- so the dialog is raised *only* there.

    Gating on the platform instead would be wrong in both directions. It would
    open a real modal dialog during any test or script that runs the entry point
    on Windows -- blocking a non-interactive process forever, which is exactly
    what an earlier version of this function did -- and it would stay silent for
    a windowed build on a platform whose message box differs.

    With a usable stderr this writes there instead, so a console user gets the
    message on the stream they are already reading rather than behind a dialog.

    It must never raise: a failure to *explain* a problem cannot be allowed to
    replace the problem.
    """
    stream = sys.stderr
    if stream is not None:
        try:
            print(f"opencsi tray: {text}", file=stream)
            return
        except Exception:  # noqa: BLE001 - fall through to the dialog
            pass

    if sys.platform == "win32":
        try:
            import ctypes

            # MB_OK | MB_ICONINFORMATION | MB_SETFOREGROUND, so the box is not
            # hidden behind whatever the user was doing.
            ctypes.windll.user32.MessageBoxW(
                None, text, "OpenCSI Monitor", 0x00000040 | 0x00010000
            )
        except Exception:  # noqa: BLE001 - never mask the real failure
            pass


if __name__ == "__main__":
    raise SystemExit(main())
