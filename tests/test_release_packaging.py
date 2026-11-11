"""Regression checks for verification of shipped ZIPs, not working trees."""

from contextlib import redirect_stdout
import ctypes
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_verifier", ROOT / "scripts/verify_release.py")
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)
FINALIZER_SPEC = importlib.util.spec_from_file_location("release_finalizer", ROOT / "scripts/finalize_release.py")
finalizer = importlib.util.module_from_spec(FINALIZER_SPEC)
FINALIZER_SPEC.loader.exec_module(finalizer)


class ReleasePackagingTests(unittest.TestCase):
    def test_native_library_can_be_recovered_after_generated_binding_cleanup(self):
        actual_library = ctypes.pythonapi

        def version(_elems=SimpleNamespace(f=SimpleNamespace(_objects={"0": actual_library}))):
            pass

        core = SimpleNamespace(Z3_get_version=version)
        z3 = SimpleNamespace(z3core=core)
        self.assertIs(verifier._native_library(z3), actual_library)
        core._lib = actual_library
        self.assertIs(verifier._native_library(z3), actual_library)

    def test_native_library_cannot_be_guessed_from_unowned_path(self):
        def version(_elems=SimpleNamespace(f=SimpleNamespace(_objects={}))):
            pass

        z3 = SimpleNamespace(z3core=SimpleNamespace(Z3_get_version=version))
        with self.assertRaisesRegex(RuntimeError, "Cannot identify the native library"):
            verifier._native_library(z3)

    def test_old_source_only_zip_cannot_borrow_z3_from_pythonpath(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            archive = base / "old-release.zip"
            with zipfile.ZipFile(archive, "w") as zipped:
                for path in (ROOT / "lazor").glob("*.py"):
                    zipped.write(path, "old release/lazor/" + path.name)
                zipped.write(ROOT / "boards/mad_1.bff", "old release/boards/mad_1.bff")
            external = base / "external dependency"
            (external / "z3").mkdir(parents=True)
            marker = base / "external-z3-imported"
            (external / "z3/__init__.py").write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('leaked')\n"
                "raise RuntimeError('External Z3 must not be imported')\n", encoding="utf-8")
            with patch.dict(os.environ, {"PYTHONPATH": str(external)}):
                result = verifier.verify_release(archive, timeout=15)
            self.assertFalse(result["passed"])
            self.assertFalse(marker.exists())
            self.assertTrue(result["extraction_path_contains_space_and_chinese"])
            self.assertEqual([probe["mode"] for probe in result["probes"]], ["direct", "editor"])
            for probe in result["probes"]:
                self.assertFalse(probe["passed"])
                self.assertTrue(probe["isolated"])
                self.assertTrue(probe["no_site"])
                self.assertIn("z3", probe["error"].lower())

    def test_archive_traversal_is_rejected_before_any_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            archive = base / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as zipped:
                zipped.writestr("../escaped.txt", "bad")
            with self.assertRaisesRegex(ValueError, "Unsafe ZIP entry"):
                verifier.extract_release(archive, base / "extracted")
            self.assertFalse((base / "escaped.txt").exists())

    def test_multiple_project_roots_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            archive = base / "ambiguous.zip"
            with zipfile.ZipFile(archive, "w") as zipped:
                for name in ("first", "second"):
                    zipped.writestr(name + "/lazor/solver.py", "")
                    zipped.writestr(name + "/boards/mad_1.bff", "")
            with self.assertRaisesRegex(ValueError, "exactly one release root"):
                verifier.extract_release(archive, base / "extracted")


class ReleaseFinalizerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.version = "1.2.3"
        for platform in finalizer.PLATFORMS:
            archive = self.archive(platform)
            archive.write_bytes(("unique verified bytes for " + platform).encode())
            self.write_report(platform, {
                "passed": True, "archive_sha256": finalizer.sha256(archive),
                "probes": [{"mode": mode, "passed": True, "isolated": True,
                            "no_site": True, "runtime_platform": platform,
                            "process_exit_code": 0} for mode in ("direct", "editor")],
            })
        self.alias = self.directory / f"lazor-project-v{self.version}.zip"
        self.alias.write_bytes(self.archive("windows-x64").read_bytes())
        for name in ("lazor-search-tables.zip", "lazor-validation.zip"):
            (self.directory / name).write_bytes(name.encode())

    def archive(self, platform):
        return self.directory / f"lazor-project-v{self.version}-{platform}.zip"

    def report(self, platform="windows-x64"):
        return self.directory / f"verification-{platform}.json"

    def write_report(self, platform, content):
        self.report(platform).write_text(json.dumps(content), encoding="utf-8")

    def finish(self):
        with redirect_stdout(io.StringIO()):
            finalizer.finalize(self.directory, self.version)

    def assert_rejected(self, message):
        with self.assertRaisesRegex(ValueError, message):
            self.finish()
        self.assertFalse((self.directory / "SHA256SUMS.txt").exists())

    def test_all_six_exact_archives_and_reports_are_in_checksum_manifest(self):
        self.finish()
        checksums = (self.directory / "SHA256SUMS.txt").read_text(encoding="utf-8")
        lines = checksums.splitlines()
        self.assertEqual(len(lines), 15)
        for line in lines:
            digest, name = line.split("  ", 1)
            self.assertEqual(digest, finalizer.sha256(self.directory / name))

    def test_changed_archive_after_verification_is_rejected(self):
        self.archive("linux-arm64").write_bytes(b"changed after successful verification")
        self.assert_rejected("mismatched isolated verification")

    def test_missing_or_failed_probe_is_rejected(self):
        original = json.loads(self.report().read_text(encoding="utf-8"))
        for mutation in ("missing", "failed", "not-isolated", "site-enabled", "nonzero-exit"):
            with self.subTest(mutation=mutation):
                report = json.loads(json.dumps(original))
                if mutation == "missing":
                    report["probes"].pop()
                else:
                    field, value = {"failed": ("passed", False),
                                    "not-isolated": ("isolated", False),
                                    "site-enabled": ("no_site", False),
                                    "nonzero-exit": ("process_exit_code", 1)}[mutation]
                    report["probes"][1][field] = value
                self.write_report("windows-x64", report)
                self.assert_rejected("mismatched isolated verification")

    def test_success_report_from_a_different_platform_is_rejected(self):
        report = json.loads(self.report().read_text(encoding="utf-8"))
        report["probes"][1]["runtime_platform"] = "windows-arm64"
        self.write_report("windows-x64", report)
        self.assert_rejected("mismatched isolated verification")

    def test_duplicate_successful_probe_cannot_hide_a_failed_probe(self):
        report = json.loads(self.report().read_text(encoding="utf-8"))
        report["probes"].insert(0, {**report["probes"][0], "passed": False})
        self.write_report("windows-x64", report)
        self.assert_rejected("mismatched isolated verification")

    def test_default_alias_cannot_differ_from_verified_windows_archive(self):
        self.alias.write_bytes(b"unverified default release")
        self.assert_rejected("default ZIP must be identical")

    def test_unverified_extra_asset_is_rejected(self):
        (self.directory / "unverified.zip").write_bytes(b"extra")
        self.assert_rejected("Unexpected release assets")


if __name__ == "__main__":
    unittest.main()
