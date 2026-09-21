"""CLI entry point.

Responsibilities, in order:

1. Install log redaction **before** anything can log.
2. Parse arguments (``--help`` / ``--version`` must work with no network and no
   browser, so no provider is constructed until a command runs).
3. Dispatch to the selected sub-command.
4. Map any exception to a documented exit code and a redacted message.

Ctrl-C is reported as exit 130 without a traceback, because a user interrupting
a network call is not a bug.
"""

from __future__ import annotations

import sys
from typing import Sequence

from ..errors import EXIT_INTERRUPTED, OpenCsiError, exit_code_for
from ..redaction import install_logging_redaction
from .context import CliContext, build_parser


def _make_output_robust() -> None:
    """Make CLI text output survive a non-UTF-8 console.

    A Chinese Windows console defaults to GBK (code page 936), and the API
    returns free-text fields (``remark``) that can contain a character GBK cannot
    represent. With the default ``strict`` error handler, ``print()`` then raises
    ``UnicodeEncodeError`` -- turning a successful query into a traceback.

    Two separate problems are handled here, and they need different answers:

    * **A character the console cannot represent.** Solved by
      ``errors="replace"``, which degrades that one character to ``?`` instead of
      killing the command.
    * **Which encoding is used at all.** Set explicitly to UTF-8. The tray's
      labels are Chinese by design, and a *frozen* build does not honour
      ``PYTHONIOENCODING`` the way the source tree does -- so the packaged EXE
      emitted GBK bytes and every Chinese label arrived as mojibake, while the
      same code run from source printed correctly. Relying on an environment
      variable the user must set is not a fix for the artefact we actually ship.

    Pinning UTF-8 is safe on a real console: when stdout is a Windows console,
    Python writes through ``WriteConsoleW`` and the encoding is already UTF-8, so
    this is a no-op there. It only changes the piped/redirected case -- which is
    precisely the case that was broken, and where UTF-8 is the right default (and
    already what the JSON path produces).
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError, LookupError):
            # A stream that cannot be reconfigured (for example one replaced by a
            # test harness). Fall back to tolerating un-encodable characters.
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):  # pragma: no cover - detached stream
                pass


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code.

    Always *returns*; it never lets ``SystemExit`` escape. ``argparse`` raises
    ``SystemExit`` for ``--help``, ``--version``, an unknown command and a bad
    option value, and catching it here keeps this function's contract honest --
    it is called directly by tests and by ``python -m opencsi``, and an
    entry point that sometimes returns an int and sometimes raises is a trap.
    """
    install_logging_redaction()
    _make_output_robust()

    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        # argparse has already printed help/usage to the right stream.
        return int(exc.code) if exc.code is not None else 0

    handler = getattr(args, "handler", None)
    if handler is None:
        # No sub-command: show help and fail with the usage code so that a
        # mistyped invocation in a script is not mistaken for success.
        parser.print_help()
        return 2

    ctx = CliContext(args=args, stdout=sys.stdout, stderr=sys.stderr)
    try:
        return int(handler(ctx))
    except KeyboardInterrupt:
        ctx.err("interrupted.")
        return EXIT_INTERRUPTED
    except OpenCsiError as exc:
        if ctx.json:
            ctx.emit_json({"ok": False, "error": exc.as_dict()})
        else:
            ctx.err(f"error: {exc}")
            if exc.hint:
                ctx.err(f"       -> {exc.hint}")
        return exc.exit_code
    except BrokenPipeError:
        # `opencsi usage | head` must not print a traceback.
        return 0
    except Exception as exc:  # pragma: no cover - defensive
        ctx.err(f"unexpected error: {type(exc).__name__}: {exc}")
        ctx.err("       run 'opencsi doctor' and report this output.")
        return exit_code_for(exc)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
