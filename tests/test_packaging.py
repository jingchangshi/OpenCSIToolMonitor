"""The frozen build must run the same code as the source install.

A packaging step is easy to get wrong in a way that only shows up on a user's
machine: the build succeeds, the EXE starts, and then it dies because a lazily
imported module was never collected. PyInstaller cannot see a lazy import, so
the only defence is to assert the packaging *inputs* are right.

These tests never invoke PyInstaller -- it is a build-only dependency, and a
suite that requires it would fail for anyone who just wants to run the tests.
What they check is the thing that actually goes wrong: the entry points exist,
the spec lists the modules the code imports lazily, and the code imports them
lazily in the first place (so the hidden-import list is genuinely needed rather
than decorative).
"""

from __future__ import annotations

import ast
import contextlib
import re
import sys
import unittest
from pathlib import Path

import helpers  # noqa: F401  (imported for its sys.path side effect)

ROOT = Path(__file__).resolve().parent.parent


class EntryPointTest(unittest.TestCase):
    """PyInstaller analyses a *script*, so both entry scripts must exist."""

    def test_the_cli_entry_point_exists_and_calls_the_cli(self) -> None:
        path = ROOT / "packaging" / "cli_entry.py"
        self.assertTrue(path.exists(), "the CLI entry script is missing")
        source = path.read_text(encoding="utf-8")
        self.assertIn("opencsi.cli.app", source)

    def test_the_tray_entry_point_exists_and_calls_the_tray(self) -> None:
        path = ROOT / "packaging" / "tray_entry.py"
        self.assertTrue(path.exists(), "the tray entry script is missing")
        source = path.read_text(encoding="utf-8")
        self.assertIn("opencsi.tray.__main__", source)

    def test_the_tray_entry_reuses_the_module_entry_point(self) -> None:
        """The frozen tray must run the same code as ``python -m opencsi.tray``.

        A build that ran a parallel implementation would be a build that is only
        ever exercised in its frozen form. This is also what went wrong before:
        the argument handling lived in the entry script, so fixing it there left
        the declared console script and the module form broken.
        """
        path = ROOT / "packaging" / "tray_entry.py"
        source = path.read_text(encoding="utf-8")
        self.assertIn("opencsi.tray", source)
        self.assertIn("__main__", source)
        # The delegation must not reimplement the branch.
        self.assertNotIn("cli.app", source, "the entry script reimplements the argv branch")


class TrayEntryArgumentsTest(unittest.TestCase):
    """Every way of starting the tray must honour its arguments.

    The entry used to ignore argv entirely and always start the resident GUI. So
    ``opencsi-tray.exe --once`` -- which reads as "print one snapshot and exit"
    -- printed nothing and then sat in the notification area forever.

    That was fixed in ``packaging/tray_entry.py``, which turned out to be the
    wrong place: the logic was *duplicated* there, so the declared
    ``opencsi-monitor`` console script and ``python -m opencsi.tray`` both kept
    the bug. ``opencsi-monitor --help`` hung forever -- the same defect, still
    live, in two entry points nobody had run.

    The logic now lives once, in ``opencsi.tray.__main__``. These tests drive
    that function directly, and a separate test proves the frozen script adds
    nothing of its own.
    """

    def _entry(self):
        """Load ``packaging/tray_entry.py`` as a module."""
        import importlib.util

        path = ROOT / "packaging" / "tray_entry.py"
        spec = importlib.util.spec_from_file_location("_tray_entry_under_test", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @contextlib.contextmanager
    def _gui_is_forbidden(self):
        """Make the resident-GUI branch raise instead of blocking forever.

        This is not tidiness. Reintroducing the defect -- discarding the
        arguments and falling through to the GUI -- made these tests *hang*
        rather than fail, because the fallback is a real pystray message loop.
        A test that hangs when the bug returns is worse than no test: it reports
        nothing and blocks the suite, which is exactly how the original defect
        survived. With this guard the regression fails in milliseconds.
        """
        from unittest import mock

        from opencsi.tray import app as tray_app

        def refuse(*_args, **_kwargs):
            raise AssertionError(
                "the GUI was started when an argument should have reached the CLI"
            )

        with mock.patch.object(tray_app, "TrayApp", refuse):
            yield

    def test_no_arguments_starts_the_tray(self) -> None:
        """The double-click and start-at-sign-in case must be unchanged."""
        from unittest import mock

        import opencsi.tray.__main__ as tray_main

        entry = self._entry()
        calls = []

        def fake_tray_main() -> int:
            calls.append("tray")
            return 0

        with mock.patch.object(tray_main, "main", fake_tray_main), mock.patch.object(
            sys, "argv", ["opencsi-tray.exe"]
        ):
            code = entry.main()

        self.assertEqual(code, 0)
        self.assertEqual(calls, ["tray"], "a bare launch must start the tray")

    def test_once_is_forwarded_to_the_cli_tray_command(self) -> None:
        """The reported bug: ``--once`` must reach the CLI, not be discarded."""
        from unittest import mock

        import opencsi.cli.app as cli_app

        from opencsi.tray import __main__ as tray_main

        seen = []

        def fake_cli_main(argv=None) -> int:
            seen.append(list(argv) if argv is not None else None)
            return 0

        with self._gui_is_forbidden(), mock.patch.object(cli_app, "main", fake_cli_main):
            code = tray_main.main(["--once", "--no-proxy"])

        self.assertEqual(code, 0)
        self.assertEqual(
            seen,
            [["tray", "--once", "--no-proxy"]],
            "the tray sub-command name must be supplied and the flags preserved",
        )

    def test_the_arguments_are_not_reordered_or_dropped(self) -> None:
        from unittest import mock

        import opencsi.cli.app as cli_app

        from opencsi.tray import __main__ as tray_main

        seen = []
        flags = ["--interval", "60", "--json"]

        with self._gui_is_forbidden(), mock.patch.object(
            cli_app, "main", lambda argv=None: seen.append(list(argv)) or 0
        ):
            tray_main.main(flags)

        self.assertEqual(seen, [["tray", *flags]])

    def test_the_exit_code_from_the_cli_is_returned(self) -> None:
        """A failing one-shot must not be reported as success."""
        from unittest import mock

        import opencsi.cli.app as cli_app

        from opencsi.tray import __main__ as tray_main

        with self._gui_is_forbidden(), mock.patch.object(
            cli_app, "main", lambda argv=None: 13
        ):
            self.assertEqual(tray_main.main(["--once"]), 13)

    def test_help_does_not_start_a_resident_tray(self) -> None:
        """The defect, stated as a property: an argument must not be ignored.

        ``opencsi-monitor --help`` printed nothing and hung forever, because the
        argument was discarded and the GUI started. Whatever the flag means, a
        request that carries one must reach the CLI.
        """
        from unittest import mock

        import opencsi.cli.app as cli_app

        from opencsi.tray import __main__ as tray_main

        reached = []
        with self._gui_is_forbidden(), mock.patch.object(
            cli_app, "main", lambda argv=None: reached.append(list(argv)) or 0
        ):
            tray_main.main(["--help"])

        self.assertEqual(reached, [["tray", "--help"]])

    def test_every_documented_flag_reaches_the_cli(self) -> None:
        """The tray's own flags must survive the hop, not just ``--once``."""
        from unittest import mock

        import opencsi.cli.app as cli_app

        from opencsi.tray import __main__ as tray_main

        for flag in (
            ["--check"],
            ["--once", "--json"],
            ["--startup-status"],
            ["--install-startup"],
            ["--interval", "60"],
            ["--auto-recover-browser"],
        ):
            with self.subTest(flag=flag):
                seen = []
                with self._gui_is_forbidden(), mock.patch.object(
                    cli_app, "main", lambda argv=None: seen.append(list(argv)) or 0
                ):
                    tray_main.main(flag)
                self.assertEqual(seen, [["tray", *flag]])


class DeclaredScriptTest(unittest.TestCase):
    """Every target in ``[project.scripts]`` must actually resolve.

    This is the bug class that produced a broken ``opencsi-monitor``:
    ``pyproject.toml`` declared ``opencsi.tray.app:main`` and no such function
    existed. Nothing failed, because the machine's installed console script
    predated the declaration, so the target was never resolved by anything --
    a fresh ``pip install`` would have created a command that dies on import with
    an ``AttributeError`` about a missing attribute.

    A declared entry point that does not exist is worse than a missing one: the
    failure surfaces after installation, in the user's shell, as a traceback
    about an attribute rather than a message about the tool. Resolving them here
    costs nothing and cannot regress.
    """

    def _declared(self) -> dict[str, str]:
        """Parse ``[project.scripts]`` without needing a TOML library.

        ``tomllib`` is 3.11+, and this project supports 3.10, so the suite cannot
        depend on it. The section is a flat map of ``name = "module:attr"``, which
        is simple enough to read directly.
        """
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(
            r"^\[project\.scripts\]\s*$(.*?)(?=^\[|\Z)",
            text,
            re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(match, "pyproject.toml has no [project.scripts]")
        scripts: dict[str, str] = {}
        for line in match.group(1).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, _, value = line.partition("=")
            scripts[name.strip()] = value.strip().strip('"').strip("'")
        return scripts

    def test_the_scripts_section_is_not_empty(self) -> None:
        """Guards the parser: a regex that matched nothing would pass vacuously."""
        scripts = self._declared()
        self.assertIn("opencsi", scripts)
        self.assertIn("opencsi-monitor", scripts)

    def test_every_declared_target_is_importable_and_callable(self) -> None:
        import importlib

        for name, target in self._declared().items():
            with self.subTest(script=name, target=target):
                module_name, _, attr = target.partition(":")
                self.assertTrue(module_name, f"{name} has no module")
                self.assertTrue(attr, f"{name} has no attribute")
                module = importlib.import_module(module_name)
                func = getattr(module, attr, None)
                self.assertIsNotNone(
                    func,
                    f"{name} points at {target}, which does not exist -- a fresh "
                    "install would create a command that fails on import",
                )
                self.assertTrue(callable(func), f"{name} -> {target} is not callable")

    def test_the_tray_console_script_takes_no_arguments(self) -> None:
        """``opencsi-monitor`` is a GUI entry point; argv is not parsed.

        It must match ``python -m opencsi.tray``, which is what the Windows
        startup registration runs, so both routes behave identically.
        """
        import inspect

        from opencsi.tray.app import main

        self.assertEqual(len(inspect.signature(main).parameters), 0)


class SpecTest(unittest.TestCase):
    """The spec must agree with what the source actually imports lazily."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.spec = (ROOT / "packaging" / "opencsi.spec").read_text(encoding="utf-8")

    def test_both_binaries_are_declared(self) -> None:
        self.assertIn('name="opencsi"', self.spec)
        self.assertIn('name="opencsi-tray"', self.spec)

    def test_the_tray_binary_has_no_console(self) -> None:
        """A console window at every sign-in is the bug this prevents."""
        tray_block = self.spec.split('name="opencsi-tray"')[1]
        self.assertIn("console=False", tray_block)

    def test_the_cli_binary_has_a_console(self) -> None:
        cli_block = self.spec.split('name="opencsi"')[1].split('name="opencsi-tray"')[0]
        self.assertIn("console=True", cli_block)

    def test_the_lazily_imported_packages_are_declared(self) -> None:
        """Each hidden import must correspond to a real lazy import.

        This is the test that matters. Drop ``pystray`` from the list and the
        tray still builds -- and then fails on the user's machine with a missing
        module, long after the build reported success.
        """
        for name in ("pystray", "PIL.Image", "PIL.ImageOps", "winreg"):
            self.assertIn(
                f'"{name}"',
                self.spec,
                f"{name} is imported lazily and must be a hidden import",
            )

    def test_upx_is_disabled(self) -> None:
        """UPX compression is a well-known antivirus false-positive trigger."""
        self.assertIn("upx=False", self.spec)

    def test_the_spec_does_not_require_uninstalled_extras(self) -> None:
        """A missing optional extra must not fail the build.

        ``segno`` is not installed here, and every extra is genuinely optional,
        so the spec filters its hidden imports by availability instead of
        hard-requiring them.
        """
        self.assertIn("find_spec", self.spec)


class LazyImportTest(unittest.TestCase):
    """The spec's hidden-import list is only needed because the imports are lazy.

    If someone moves these imports to module scope, the hidden imports become
    unnecessary -- and, more importantly, importing ``opencsi.tray`` would start
    requiring pystray, which breaks ``opencsi tray --help`` on a bare install.
    These tests pin the laziness so that regression cannot happen quietly.
    """

    def _imports_at_module_scope(self, relative: str) -> set[str]:
        """Module names imported at the top level of a source file."""
        path = ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module)
        return found

    def test_the_tray_app_does_not_import_pystray_at_module_scope(self) -> None:
        imports = self._imports_at_module_scope("src/opencsi/tray/app.py")
        self.assertNotIn(
            "pystray",
            imports,
            "pystray at module scope would break `opencsi tray --help` on a "
            "machine without the extra",
        )

    def test_the_qr_renderer_does_not_import_pillow_at_module_scope(self) -> None:
        imports = self._imports_at_module_scope("src/opencsi/auth/qr_render.py")
        self.assertNotIn(
            "PIL",
            imports,
            "Pillow at module scope would make `opencsi login` need the qr extra",
        )
        self.assertNotIn("PIL.Image", imports)

    def test_the_startup_manager_does_not_import_winreg_at_module_scope(self) -> None:
        """``winreg`` does not exist off Windows, and doctor runs everywhere."""
        imports = self._imports_at_module_scope("src/opencsi/tray/startup.py")
        self.assertNotIn("winreg", imports)


class BuildScriptTest(unittest.TestCase):
    """``tools/build_exe.py`` is the documented way to build."""

    def test_the_build_script_reports_a_missing_pyinstaller_clearly(self) -> None:
        source = (ROOT / "tools" / "build_exe.py").read_text(encoding="utf-8")
        self.assertIn("PyInstaller is not installed", source)
        self.assertIn("opencsi[build]", source)

    def test_the_build_script_verifies_its_own_output(self) -> None:
        """A build that exits 0 without artefacts is the failure worth catching."""
        source = (ROOT / "tools" / "build_exe.py").read_text(encoding="utf-8")
        self.assertIn("EXPECTED", source)
        self.assertIn("did not produce", source)

    def test_the_declared_artefacts_match_the_spec(self) -> None:
        source = (ROOT / "tools" / "build_exe.py").read_text(encoding="utf-8")
        spec = (ROOT / "packaging" / "opencsi.spec").read_text(encoding="utf-8")
        for name in re.findall(r'"(opencsi[a-z-]*\.exe)"', source):
            self.assertIn(
                f'name="{name[:-4]}"',
                spec,
                f"{name} is expected by the build script but not produced by the spec",
            )


class PyInstallerIsNotARuntimeDependencyTest(unittest.TestCase):
    """The shipped tool must run from a plain Python install."""

    def test_the_core_has_no_runtime_dependencies(self) -> None:
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        block = text.split("dependencies = [", 1)[1].split("]", 1)[0]
        self.assertEqual(
            block.strip(),
            "",
            "the core dependency list must stay empty",
        )

    def test_pyinstaller_is_a_build_extra_not_a_runtime_one(self) -> None:
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        build_line = [
            line for line in text.splitlines() if line.startswith("build = [")
        ]
        self.assertTrue(build_line, "the build extra is missing")
        self.assertIn("pyinstaller", build_line[0].lower())

    def test_nothing_in_src_imports_pyinstaller(self) -> None:
        for path in (ROOT / "src").rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn(
                "import PyInstaller",
                source,
                f"{path} imports a build-only tool at runtime",
            )


class LiveProbeTest(unittest.TestCase):
    """The live probes are evidence, so they must stay runnable and honest.

    These tools are what backs the claims in the implementation report that the
    offline suite cannot reach: a real OAuth round trip, a real Windows message
    loop. A probe that has silently stopped importing is worse than no probe,
    because the report still cites it.

    Nothing here *runs* a probe -- each needs a browser, a network and a live
    session, so requiring them would make the suite unrunnable offline. What is
    checked is the contract that makes them trustworthy: they parse, they carry
    a docstring explaining what they prove, and the ones that can fail to prove
    anything say so instead of reporting success.
    """

    def _probes(self) -> list[Path]:
        return sorted((ROOT / "tools").glob("probe_*.py"))

    def test_the_probes_are_present(self) -> None:
        names = {path.name for path in self._probes()}
        for expected in (
            "probe_autonomous_renewal.py",
            "probe_renewal_gate.py",
            "probe_tray_live.py",
        ):
            with self.subTest(probe=expected):
                self.assertIn(expected, names)

    def test_every_probe_parses_and_explains_itself(self) -> None:
        for path in self._probes():
            with self.subTest(probe=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                docstring = ast.get_docstring(tree)
                self.assertTrue(docstring, f"{path.name} has no docstring")
                # A one-line title is not an explanation. The bar is low on
                # purpose -- these probes differ wildly in scope -- but a reader
                # must be able to tell what running it would prove.
                self.assertGreaterEqual(
                    len(docstring.split()),
                    12,
                    f"{path.name} does not explain what it proves",
                )

    def test_every_probe_declares_its_safety_posture(self) -> None:
        """A probe runs against the live site, so it must state its own limits.

        Each of these either performs network calls against a real account or
        drives a real GUI, so "what could this touch?" has to be answerable from
        the file itself. The acceptable declarations differ -- a QR probe is
        read-only, the tray probe uses an offline stub, the soak probe writes
        nothing but a line per sample -- so this checks that one of them is
        present rather than demanding a single wording.
        """
        declarations = (
            r"read-only",
            r"GET only",
            r"GET-only",
            r"GET/OPTIONS",
            r"never POST",
            r"no state-mutating",
            r"never prints",
            r"touches no network",
            r"offline stub",
            r"writes nothing",
        )
        for path in self._probes():
            with self.subTest(probe=path.name):
                docstring = ast.get_docstring(
                    ast.parse(path.read_text(encoding="utf-8"))
                ) or ""
                self.assertTrue(
                    any(
                        re.search(pattern, docstring, re.IGNORECASE)
                        for pattern in declarations
                    ),
                    f"{path.name} does not declare what it may touch",
                )

    def test_the_gate_probe_can_refuse_to_claim_success(self) -> None:
        """A probe that cannot reach its precondition must not report PASS.

        `probe_renewal_gate.py` proves the renewal gate stays *shut*. With an
        almost-expired credential the gate should be open, so the probe can
        prove nothing -- and it must say that rather than exiting 0, which would
        turn "I could not test this" into "this is fine".
        """
        source = (ROOT / "tools" / "probe_renewal_gate.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("return 2", source, "the SKIP path is gone")
        self.assertIn("SKIP", source)
        # It must take the margin from the real config, not retype it, or it
        # would keep passing after the production policy changed.
        self.assertIn("MonitorConfig().renew_margin", source)

    def test_the_gate_probe_checks_both_directions(self) -> None:
        """Growth means a renewal ran; decay means it correctly did not."""
        source = (ROOT / "tools" / "probe_renewal_gate.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("drift", source)
        self.assertIn("GROWTH_TOLERANCE", source)

    def test_the_soak_does_not_double_count_its_own_renewals(self) -> None:
        """One renewal must not be reported as two.

        A renewal this process performs is visible twice: the wrapper around
        ``session.renew`` counts the call, and the lifetime then jumps because a
        new cookie was issued. Adding those two counters reported a single real
        renewal as "2 silent renewal(s)" and exited 0 on the inflated number --
        which is the worst kind of probe bug, because the exit code said the
        stronger claim had been proven.

        The counters are therefore tracked separately: ``jumps`` is every jump
        observed, ``external_jumps`` only those no local renewal explains, and the
        total that decides success uses ``renewals + external_jumps``.
        """
        source = (ROOT / "tools" / "probe_renewal_soak.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("external_jumps", source, "the attribution split is gone")
        self.assertIn(
            "renewals + external_jumps",
            source,
            "success is again decided by adding the raw counters, which "
            "double-counts a renewal this process performed",
        )
        # The raw sum must not appear in the decision paths any more.
        self.assertNotIn(
            "renewals + jumps",
            source,
            "the double-counting sum is back",
        )


if __name__ == "__main__":
    unittest.main()
