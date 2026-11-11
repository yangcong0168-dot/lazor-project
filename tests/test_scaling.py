"""Verify fixture provenance, cross-product coverage, and honest timing reports."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lazor.bff import load_bff
from lazor.scaling import FAMILIES, generate_suite, make_case, run_suite, _worker
from lazor.simulation import simulate


class ScalingTests(unittest.TestCase):
    def setUp(self):
        platform_patch = patch("lazor.scaling.platform.platform", return_value="test-platform")
        platform_patch.start()
        self.addCleanup(platform_patch.stop)

    def test_planted_witnesses_verify_across_dimensions_and_full_boards(self):
        for size, a_count, c_count in ((4, 4, 4), (4, 6, 10), (7, 10, 10), (44, 10, 10)):
            for family in FAMILIES:
                with self.subTest(size=size, a=a_count, c=c_count, family=family):
                    puzzle, witness, metadata = make_case(size, a_count, c_count, family)
                    placement = {(x, y): kind for x, y, kind in witness["placements"]}
                    core = replace(puzzle, targets=frozenset(map(tuple, witness["core_targets"])))
                    self.assertTrue(simulate(core, placement).is_solution)
                    self.assertEqual(sum(kind == "A" for kind in placement.values()), a_count)
                    self.assertEqual(sum(kind == "C" for kind in placement.values()), c_count)
                    self.assertGreater(metadata["non_source_targets"], 0)
                    self.assertGreater(metadata["witness_branch_points"], 0)
                    self.assertGreater(metadata["witness_reflection_transitions"], 0)
                    if family == "parity_unsat":
                        self.assertFalse(simulate(puzzle, placement).is_solution)
                        self.assertTrue(any((x + y) % 2 == 0 for x, y in puzzle.targets))
                        self.assertEqual(metadata["difficulty_class"], "easy_unsat_control")
                    else:
                        self.assertTrue(simulate(puzzle, placement).is_solution)

    def test_generation_is_deterministic_and_crosses_independent_inventories(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first"
            second = Path(directory) / "second"
            left = generate_suite(first, sizes=(4, 5), inventories=(4, 10))
            right = generate_suite(second, sizes=(4, 5), inventories=(4, 10))
            self.assertEqual(left, right)
            self.assertEqual(left["requested_inventory_pairs"], 8)
            self.assertEqual(left["case_count"], 21)
            self.assertEqual(len(left["skipped_inventory_pairs"]), 1)
            pairs = {(case["size"], case["A"], case["C"]) for case in left["cases"]}
            self.assertIn((4, 4, 10), pairs)
            self.assertIn((4, 10, 4), pairs)
            self.assertNotIn((4, 10, 10), pairs)
            for case in left["cases"]:
                data = (first / case["board"]).read_bytes()
                self.assertEqual(data, (second / case["board"]).read_bytes())
                self.assertEqual(hashlib.sha256(data).hexdigest(), case["input_sha256"])
                puzzle = load_bff(first / case["board"])
                self.assertEqual(puzzle.width, case["size"])
            with self.assertRaises(FileExistsError):
                generate_suite(first, sizes=(4,), inventories=(4,))

    def test_generation_rejects_invalid_inventory_and_duplicate_dimensions(self):
        for arguments in ((4, 10, 10, "open"), (3, 4, 4, "open"), (4, 4, 4, "missing")):
            with self.assertRaises(ValueError):
                make_case(*arguments)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                generate_suite(Path(directory) / "duplicate", sizes=(4, 4))

    @staticmethod
    def fake_timeout_worker(command, **kwargs):
        output = Path(command[command.index("--output") + 1])
        output.write_text(json.dumps({"status": "timeout", "elapsed_seconds": 0.01,
                                      "solution_verified": False}), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    def test_resumable_trials_preserve_timeouts_and_refuse_code_or_input_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            suite = Path(directory) / "suite"
            generate_suite(suite, sizes=(4,), inventories=(4,), families=("open",))
            manifest = suite / "manifest.json"
            output = Path(directory) / "report.json"
            with patch("lazor.scaling.subprocess.run", side_effect=self.fake_timeout_worker) as worker:
                report = run_suite(manifest, output, methods=("smt",), timeout=0.01)
                self.assertEqual(worker.call_count, 1)
                report = run_suite(manifest, output, methods=("smt",), timeout=0.01, resume=True)
                self.assertEqual(worker.call_count, 1)
            self.assertTrue(report["completed"])
            self.assertEqual(report["summary"]["smt"]["status_counts"], {"timeout": 1})
            self.assertEqual(report["summary"]["smt"]["correct_decisions"], 0)
            self.assertIsNone(report["summary"]["smt"]["median_correct_decision_seconds"])
            with self.assertRaises(ValueError):
                run_suite(manifest, output, methods=("smt",), timeout=0.02, resume=True)
            with patch("lazor.scaling._code_hashes", return_value={"changed": "hash"}):
                with self.assertRaises(ValueError):
                    run_suite(manifest, output, methods=("smt",), timeout=0.01, resume=True)
            case = json.loads(manifest.read_text(encoding="utf-8"))["cases"][0]
            (suite / case["board"]).write_text("changed", encoding="utf-8")
            with self.assertRaises(ValueError):
                run_suite(manifest, output, methods=("smt",), timeout=0.01, resume=True)

    def test_parent_kills_overbudget_workers_without_claiming_unsat(self):
        with tempfile.TemporaryDirectory() as directory:
            suite = Path(directory) / "suite"
            generate_suite(suite, sizes=(4,), inventories=(4,), families=("open",))
            with patch("lazor.scaling.subprocess.run", side_effect=subprocess.TimeoutExpired("worker", 2)):
                report = run_suite(suite / "manifest.json", Path(directory) / "result.json",
                                   methods=("dfs",), timeout=0)
            record = report["results"][0]
            self.assertEqual(record["status"], "timeout")
            self.assertTrue(record["hard_timeout"])
            self.assertFalse(record["wrong_decision"])
            self.assertIsNone(record["elapsed_seconds"])

    def test_worker_independently_checks_a_claimed_placement(self):
        with tempfile.TemporaryDirectory() as directory:
            suite = Path(directory) / "suite"
            manifest = generate_suite(suite, sizes=(4,), inventories=(4,), families=("open",))
            board = suite / manifest["cases"][0]["board"]
            puzzle = load_bff(board)
            false_solution = simulate(puzzle)
            claimed = SimpleNamespace(status="solved", solved=True, solution=false_solution,
                                      reason="", elapsed_seconds=0)
            with patch("lazor.dfs_baseline.solve_dfs", return_value=claimed):
                record = _worker(board, "dfs", 1)
            self.assertEqual(record["status"], "error")
            self.assertIn("replay failed", record["error"])


if __name__ == "__main__":
    unittest.main()
