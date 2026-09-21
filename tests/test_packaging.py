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
        ever exercised in its frozen form.
        """
        path = ROOT / "packaging" / "tray_entry.py"
        source = path.read_text(encoding="utf-8")
        self.assertIn("from opencsi.tray.__main__ import main", source)


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


if __name__ == "__main__":
    unittest.main()
