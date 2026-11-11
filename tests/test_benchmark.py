"""Check independent solution verification and repeatable benchmark output."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lazor.benchmark import benchmark_files, collect_boards, main
from lazor.bff import load_bff
from lazor.simulation import simulate


BOARD = "GRID START\no\nGRID STOP\nL 0 1 1 1\nP 1 2\n"


class BenchmarkTests(unittest.TestCase):
    """Use tiny boards so tests measure behavior, not search performance."""

    def make_board(self, directory, name="board.bff", text=BOARD):
        path = Path(directory) / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_collects_only_bff_files_in_stable_order(self):
        with tempfile.TemporaryDirectory() as directory:
            second = self.make_board(directory, "z.bff")
            first = self.make_board(directory, "a.bff")
            self.make_board(directory, "notes.txt")
            self.assertEqual(collect_boards(directory), [first, second])
            self.assertEqual(collect_boards(first), [first])

    def test_real_solution_is_replayed_and_timed(self):
        with tempfile.TemporaryDirectory() as directory:
            board = self.make_board(directory)
            report = benchmark_files([board])
        self.assertTrue(report["all_passed"])
        self.assertEqual(report["board_count"], 1)
        result = report["results"][0]
        self.assertEqual(result["status"], "solved")
        self.assertTrue(result["solution_verified"])
        self.assertEqual((result["hit_count"], result["target_count"]), (1, 1))
        self.assertGreaterEqual(result["elapsed_seconds"], 0)
        self.assertIn("not proof", report["scope"])
        self.assertIn("python_version", report)

    def test_bad_input_does_not_prevent_later_board_testing(self):
        with tempfile.TemporaryDirectory() as directory:
            broken = self.make_board(directory, "bad.bff", "INVALID\n")
            good = self.make_board(directory, "good.bff")
            report = benchmark_files([broken, good])
        self.assertFalse(report["all_passed"])
        first, second = report["results"]
        self.assertEqual(first["status"], "error")
        self.assertIn("BFFParseError", first["error"])
        self.assertFalse(first["solution_verified"])
        self.assertTrue(second["passed"])

    def test_timeout_remains_distinct_from_no_solution(self):
        with tempfile.TemporaryDirectory() as directory:
            board = self.make_board(directory)
            report = benchmark_files([board], timeout=0)
        result = report["results"][0]
        self.assertEqual(result["status"], "timeout")
        self.assertFalse(result["solution_verified"])
        self.assertFalse(result["passed"])

    def test_claimed_solution_is_checked_against_original_board(self):
        with tempfile.TemporaryDirectory() as directory:
            board = self.make_board(
                directory, text=BOARD.replace("1 2", "1 0")
            )
            false_solution = simulate(load_bff(board))
            claimed = SimpleNamespace(
                status="solved", solved=True, solution=false_solution,
                elapsed_seconds=0.001, build_seconds=0.0002,
                solver_seconds=0.0007, verification_seconds=0.0001,
                state_count=16, transition_count=8, variable_count=32,
                constraint_count=40, solver_checks=1, solver_version="test",
                reason="",
            )
            with patch(
                "lazor.benchmark.solve_constraints", return_value=claimed
            ):
                report = benchmark_files([board])
        result = report["results"][0]
        self.assertEqual(result["status"], "error")
        self.assertIn("failed verification", result["error"])
        self.assertFalse(result["solution_verified"])

    def test_unknown_solver_status_is_not_proof_of_no_solution(self):
        with tempfile.TemporaryDirectory() as directory:
            board = self.make_board(directory)
            unknown = SimpleNamespace(
                status="unknown", solved=False, solution=None,
                elapsed_seconds=0.001, build_seconds=0.0002,
                solver_seconds=0.0008, verification_seconds=0.0,
                state_count=16, transition_count=8, variable_count=32,
                constraint_count=40, solver_checks=1, solver_version="test",
                reason="incomplete",
            )
            with patch(
                "lazor.benchmark.solve_constraints", return_value=unknown
            ):
                report = benchmark_files([board])
        result = report["results"][0]
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "incomplete")
        self.assertFalse(result["passed"])

    def test_cli_writes_json_and_requires_force_for_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            board = self.make_board(directory)
            output = Path(directory) / "results" / "benchmark.json"
            args = [str(board), "--output", str(output)]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 0)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertTrue(report["all_passed"])
            original = output.read_bytes()
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    main(args)
            self.assertEqual(caught.exception.code, 2)
            self.assertEqual(output.read_bytes(), original)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main([*args, "--force"]), 0)

    def test_cli_rejects_non_json_output_without_overwriting_input(self):
        with tempfile.TemporaryDirectory() as directory:
            board = self.make_board(directory)
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    main([str(board), "--output", str(board), "--force"])
            self.assertEqual(caught.exception.code, 2)
            self.assertEqual(board.read_text(encoding="utf-8"), BOARD)

    def test_cli_timeout_report_returns_one(self):
        with tempfile.TemporaryDirectory() as directory:
            board = self.make_board(directory)
            output = Path(directory) / "benchmark.json"
            with contextlib.redirect_stdout(io.StringIO()):
                code = main([
                    str(board), "--output", str(output), "--timeout", "0"
                ])
            self.assertEqual(code, 1)
            self.assertEqual(
                json.loads(output.read_text("utf-8"))["results"][0]["status"],
                "timeout",
            )

    def test_invalid_parameters_are_rejected(self):
        with self.assertRaises(ValueError):
            benchmark_files([])
        for timeout in (-1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    benchmark_files([Path("unused.bff")], timeout=timeout)


if __name__ == "__main__":
    unittest.main()
