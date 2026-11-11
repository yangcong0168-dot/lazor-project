"""Exercise the actual command-line program, including files and exit codes."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "boards" / "placement_demo.bff"


class CommandLineTests(unittest.TestCase):
    """Keep inspection, simulation, and verification behavior distinct."""

    def run_cli(self, *arguments):
        return subprocess.run(
            [sys.executable, "-m", "lazor", *map(str, arguments)],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
            timeout=10,
        )

    def test_default_command_still_inspects_input(self):
        result = self.run_cli(DEMO)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("3 columns x 3 rows", result.stdout)
        self.assertIn("C=1", result.stdout)
        self.assertIn("Use --simulate", result.stdout)

    def test_check_passes_for_complete_hand_checked_placement(self):
        result = self.run_cli(DEMO, "--place", "C:1,1", "--check")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_planner_solves_and_saves_answer_and_constraint_model(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            report = folder / "solution.txt"
            svg = folder / "solution.svg"
            snapshot = folder / "solution.bff"
            model = folder / "model.smt2"
            result = self.run_cli(
                DEMO, "--solve", "--output", report, "--svg", svg,
                "--save-board", snapshot, "--model", model,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("BFS", report.read_text(encoding="utf-8"))
            self.assertNotIn(
                "no search was performed", report.read_text(encoding="utf-8")
            )
            self.assertTrue(svg.is_file())
            self.assertIn("(assert", model.read_text(encoding="utf-8"))
            self.assertIn("(check-sat)", model.read_text(encoding="utf-8"))
            replay = self.run_cli(snapshot, "--check")
            self.assertEqual(replay.returncode, 0, replay.stderr)

    def test_planning_timeout_writes_no_solution_image(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "search.txt"
            svg = Path(directory) / "solution.svg"
            result = self.run_cli(
                DEMO, "--solve", "--timeout", "0", "--output", report,
                "--svg", svg,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("TIMEOUT", report.read_text(encoding="utf-8"))
            self.assertNotIn("NO_SOLUTION", result.stdout)
            self.assertFalse(svg.exists())

    def test_infeasible_plan_exports_constraints_but_no_solution(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "impossible.bff"
            source.write_text(
                "GRID START\no\nGRID STOP\nL 0 1 1 1\nP 2 1\n",
                encoding="utf-8",
            )
            report = folder / "result.txt"
            svg = folder / "solution.svg"
            model = folder / "model.smt2"
            snapshot = folder / "solution.bff"
            result = self.run_cli(
                source, "--solve", "--output", report, "--svg", svg,
                "--model", model, "--save-board", snapshot,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("NO_SOLUTION", result.stdout)
            self.assertIn("unsatisfiable", report.read_text(encoding="utf-8"))
            self.assertIn("(check-sat)", model.read_text(encoding="utf-8"))
            self.assertFalse(svg.exists())
            self.assertFalse(snapshot.exists())

    def test_planner_rejects_conflicting_modes_and_bad_limits(self):
        options = [
            ("--solve", "--place", "C:1,1"),
            ("--solve", "--simulate"),
            ("--solve", "--timeout", "-1"),
            ("--solve", "--timeout", "nan"),
            ("--model", "unused.smt2"),
        ]
        for arguments in options:
            with self.subTest(arguments=arguments):
                result = self.run_cli(DEMO, *arguments)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("Traceback", result.stderr)

    def test_model_export_cannot_overwrite_input_or_another_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.bff"
            original = DEMO.read_text(encoding="utf-8")
            source.write_text(original, encoding="utf-8")
            rejected = self.run_cli(
                source, "--solve", "--model", source, "--force"
            )
            self.assertEqual(rejected.returncode, 2)
            self.assertEqual(source.read_text(encoding="utf-8"), original)
            shared = Path(directory) / "shared.txt"
            rejected = self.run_cli(
                source, "--solve", "--model", shared, "--output", shared
            )
            self.assertEqual(rejected.returncode, 2)
            self.assertFalse(shared.exists())

    def test_missed_layout_is_neither_error_nor_proof_of_no_solution(self):
        ordinary = self.run_cli(DEMO, "--simulate")
        checked = self.run_cli(DEMO, "--check")
        self.assertEqual(ordinary.returncode, 0, ordinary.stderr)
        self.assertEqual(checked.returncode, 1, checked.stderr)
        self.assertNotIn("unsolvable", checked.stdout.lower())

    def test_writes_report_svg_and_replayable_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "nested"
            report = folder / "report.txt"
            svg = folder / "rays.svg"
            snapshot = folder / "board.bff"
            result = self.run_cli(
                DEMO, "--place", "C:1,1", "--output", report,
                "--svg", svg, "--save-board", snapshot, "--check",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            for path in (report, svg, snapshot):
                self.assertTrue(path.is_file(), path)
            replay = self.run_cli(snapshot, "--check")
            self.assertEqual(replay.returncode, 0, replay.stderr)

    def test_bad_missing_and_duplicate_placements_are_clean_errors(self):
        argument_sets = [
            (DEMO, "--place", "C:bad"),
            (DEMO, "--place", "A:0,0"),
            (DEMO, "--place", "C:1,1", "--place", "C:1,1"),
            (ROOT / "boards" / "missing.bff",),
        ]
        for arguments in argument_sets:
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("Traceback", result.stderr)

    def test_existing_output_requires_force(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.txt"
            output.write_text("keep me", encoding="utf-8")
            rejected = self.run_cli(DEMO, "--output", output)
            self.assertEqual(rejected.returncode, 2)
            self.assertEqual(output.read_text(encoding="utf-8"), "keep me")
            accepted = self.run_cli(DEMO, "--output", output, "--force")
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertNotEqual(output.read_text(encoding="utf-8"), "keep me")

    def test_input_cannot_be_overwritten_even_with_force(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.bff"
            original = DEMO.read_text(encoding="utf-8")
            source.write_text(original, encoding="utf-8")
            result = self.run_cli(
                source, "--save-board", source, "--force"
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(source.read_text(encoding="utf-8"), original)

    def test_output_paths_cannot_collide(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.txt"
            result = self.run_cli(
                DEMO, "--output", output, "--svg", output
            )
            self.assertEqual(result.returncode, 2)
            self.assertFalse(output.exists())

    def test_output_parent_conflict_is_rejected_before_any_write(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "report.txt"
            child = parent / "diagram.svg"
            for report, svg in ((parent, child), (child, parent)):
                with self.subTest(report=report, svg=svg):
                    result = self.run_cli(
                        DEMO, "--output", report, "--svg", svg
                    )
                    self.assertEqual(result.returncode, 2)
                    self.assertFalse(parent.exists())


if __name__ == "__main__":
    unittest.main()
