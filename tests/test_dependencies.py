"""Verify bundled native dependency selection and import-failure recovery."""

import importlib
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

from lazor import dependencies


class PlatformTests(unittest.TestCase):
    def test_supported_process_architectures(self):
        for system, name in (("Windows", "windows"), ("Darwin", "macos"), ("Linux", "linux")):
            for machine, architecture in (("AMD64", "x64"), ("x86_64", "x64"),
                                          ("ARM64", "arm64"), ("aarch64", "arm64")):
                with self.subTest(system=system, machine=machine), \
                        patch.object(dependencies.platform, "system", return_value=system), \
                        patch.object(dependencies.platform, "machine", return_value=machine), \
                        patch.object(dependencies.sysconfig, "get_platform", return_value="unknown"), \
                        patch.object(dependencies.struct, "calcsize", return_value=8):
                    self.assertEqual(dependencies.runtime_platform(), f"{name}-{architecture}")

    def test_windows_uses_python_build_architecture_under_emulation(self):
        for build, expected in (("win-amd64", "windows-x64"), ("win-arm64", "windows-arm64")):
            with self.subTest(build=build), \
                    patch.object(dependencies.platform, "system", return_value="Windows"), \
                    patch.object(dependencies.platform, "machine", return_value="ARM64"), \
                    patch.object(dependencies.sysconfig, "get_platform", return_value=build), \
                    patch.object(dependencies.struct, "calcsize", return_value=8):
                self.assertEqual(dependencies.runtime_platform(), expected)

    def test_32_bit_python_is_not_mistaken_for_a_64_bit_bundle(self):
        with patch.object(dependencies.platform, "system", return_value="Windows"), \
                patch.object(dependencies.platform, "machine", return_value="AMD64"), \
                patch.object(dependencies.struct, "calcsize", return_value=4):
            self.assertIn("unsupported-amd64-32bit", dependencies.runtime_platform())


class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="lazor dependency ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.local = self.root / ".deps"
        self.local.mkdir()
        self.saved_modules = dependencies._z3_modules()
        self.saved_path = sys.path[:]
        self.addCleanup(self.restore_imports)
        for name in self.saved_modules:
            sys.modules.pop(name, None)
        self.root_patch = patch.object(dependencies, "_project_root", return_value=self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def restore_imports(self):
        for name in dependencies._z3_modules():
            sys.modules.pop(name, None)
        sys.modules.update(self.saved_modules)
        sys.path[:] = self.saved_path
        importlib.invalidate_caches()

    def local_package(self, text="Solver = lambda: None\nget_version = lambda: (5, 3, 3, 0)\n"):
        directory = self.local / "z3"
        directory.mkdir(exist_ok=True)
        (directory / "__init__.py").write_text(text, encoding="utf-8")

    def marker(self, **changes):
        marker = dict(schema_version=1, distribution="z3-solver", version="5.3.3.0",
                      platform=dependencies.runtime_platform())
        marker.update(changes)
        (self.local / "lazor_bundle.json").write_text(json.dumps(marker), encoding="utf-8")

    def test_installed_dependency_remains_supported_without_marker(self):
        installed = ModuleType("z3")
        installed.Solver = lambda: None
        sys.modules["z3"] = installed
        self.local_package("raise AssertionError('manual package should not override installed')\n")
        self.assertIs(dependencies.load_z3(), installed)
        self.assertEqual(sys.path, self.saved_path)

    def test_manual_local_install_when_global_dependency_is_missing(self):
        self.local_package()
        real_import = importlib.import_module

        def import_missing_global(name):
            if str(self.local) not in sys.path:
                raise ModuleNotFoundError("No module named 'z3'", name="z3")
            return real_import(name)

        with patch.object(dependencies.importlib, "import_module", side_effect=import_missing_global):
            module = dependencies.load_z3()
        self.assertTrue(dependencies._inside_package(module, self.local))
        self.assertEqual(sys.path, self.saved_path)

    def test_marked_bundle_overrides_unrelated_global_package(self):
        unrelated = ModuleType("z3")
        sys.modules["z3"] = unrelated
        sys.modules["z3.unrelated"] = ModuleType("z3.unrelated")
        self.local_package()
        self.marker()
        module = dependencies.load_z3()
        self.assertIsNot(module, unrelated)
        self.assertTrue(callable(module.Solver))
        self.assertNotIn("z3.unrelated", sys.modules)
        self.assertIs(dependencies.load_z3(), module)
        self.assertEqual(sys.path, self.saved_path)

    def test_bad_markers_never_import_or_fall_back(self):
        self.local_package()
        for changes in (dict(platform="wrong-platform"), dict(platform="linux-x64"),
                        dict(schema_version=True), dict(version="0.0.0"), dict(distribution="z3")):
            with self.subTest(changes=changes), \
                    patch.object(dependencies, "runtime_platform", return_value="windows-x64"), \
                    patch.object(dependencies.importlib, "import_module") as imported:
                self.marker(**changes)
                with self.assertRaises(RuntimeError):
                    dependencies.load_z3()
                imported.assert_not_called()

    def test_malformed_marker_and_missing_bundle_are_actionable(self):
        marker = self.local / "lazor_bundle.json"
        marker.write_text("not JSON", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "same Python interpreter"):
            dependencies.load_z3()
        self.marker()
        with self.assertRaisesRegex(RuntimeError, "missing .deps/z3/__init__.py"):
            dependencies.load_z3()

    def test_native_load_failure_restores_modules_and_allows_retry(self):
        installed = ModuleType("z3")
        old_child = ModuleType("z3.original")
        sys.modules.update({"z3": installed, "z3.original": old_child})
        self.local_package("from . import partial\n"
                           "class Z3Exception(Exception): pass\n"
                           "raise Z3Exception('wrong native architecture')\n")
        (self.local / "z3/partial.py").write_text("value = 1\n", encoding="utf-8")
        self.marker()
        with self.assertRaisesRegex(RuntimeError, "wrong native architecture") as raised:
            dependencies.load_z3()
        self.assertIn(sys.executable, str(raised.exception))
        self.assertEqual(dependencies._z3_modules(), {"z3": installed, "z3.original": old_child})
        self.assertEqual(sys.path, self.saved_path)
        self.local_package()
        module = dependencies.load_z3()
        self.assertTrue(callable(module.Solver))
        self.assertNotIn("z3.partial", sys.modules)

    def test_bundle_version_mismatch_does_not_fall_back(self):
        self.local_package("Solver = lambda: None\nget_version = lambda: (4, 0, 0, 0)\n")
        self.marker()
        with self.assertRaisesRegex(RuntimeError, "expected 5.3.3.0"):
            dependencies.load_z3()
        self.assertEqual(dependencies._z3_modules(), {})

    def test_missing_global_and_broken_transitive_import_are_distinct(self):
        for missing in ("z3", "some_native_dependency"):
            with self.subTest(missing=missing), \
                    patch.object(dependencies.importlib, "import_module",
                                 side_effect=ModuleNotFoundError("missing dependency", name=missing)) as imported:
                with self.assertRaisesRegex(RuntimeError, "Cannot load z3-solver"):
                    dependencies.load_z3()
                self.assertEqual(imported.call_count, 1)
                self.assertEqual(sys.path, self.saved_path)


if __name__ == "__main__":
    unittest.main()
