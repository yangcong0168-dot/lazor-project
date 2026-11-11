"""Exercise selectable pruned search, its reports, and replayable exports."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from lazor.bff import load_bff
from lazor.simulation import simulate

ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT / "boards/placement_demo.bff"


class SearchCommandLineTests(unittest.TestCase):
    def run_cli(self, *arguments):
        return subprocess.run(
            [sys.executable, "-m", "lazor", str(BOARD), *map(str, arguments)],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
            timeout=10,
        )

    def test_search_exports_identify_algorithm_and_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            text, svg, bff = [folder / name for name in
                              ("result.txt", "result.svg", "result.bff")]
            result = self.run_cli(
                "--solve", "--algorithm", "search", "--output", text,
                "--svg", svg, "--save-board", bff, "--check",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Algorithm: exact ray-frontier search", text.read_text("utf-8"))
            self.assertIn("Lazor search solution", svg.read_text("utf-8"))
            self.assertNotIn("SMT:", svg.read_text("utf-8"))
            self.assertIn("# Algorithm: exact ray-frontier search", bff.read_text("utf-8"))
            self.assertTrue(simulate(load_bff(bff)).is_solution)

    def test_search_timeout_does_not_export_solution(self):
        with tempfile.TemporaryDirectory() as directory:
            report, svg = Path(directory) / "result.txt", Path(directory) / "result.svg"
            result = self.run_cli("--solve", "--algorithm", "search", "--timeout", "0",
                                  "--output", report, "--svg", svg)
            self.assertEqual(result.returncode, 1)
            self.assertIn("Status: TIMEOUT", report.read_text("utf-8"))
            self.assertFalse(svg.exists())

    def test_algorithm_requires_solve_and_model_requires_smt(self):
        result = self.run_cli("--algorithm", "search")
        self.assertEqual(result.returncode, 2)
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "model.smt2"
            result = self.run_cli("--solve", "--algorithm", "search", "--model", model)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(model.exists())

    def test_table_database_options_require_search_and_are_mutually_exclusive(self):
        for arguments in (("--no-table-db",),
                          ("--solve", "--algorithm", "smt", "--table-db", "missing.sqlite3"),
                          ("--solve", "--algorithm", "search", "--table-db", "missing.sqlite3", "--no-table-db")):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.run_cli(*arguments).returncode, 2)

    def test_disabled_or_missing_database_falls_back_to_exact_search(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.sqlite3"
            for options in (("--no-table-db",), ("--table-db", missing)):
                with self.subTest(options=options):
                    result = self.run_cli("--solve", "--algorithm", "search", *options)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
