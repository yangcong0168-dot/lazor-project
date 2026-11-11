"""Checkpoint, isolation, watchdog and exact-inventory checks for full runs."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lazor.full_benchmark import (
    PersistentWorker, digest, package_hashes, read_checkpoint, run_full, summary,
)


class FullBenchmarkTests(unittest.TestCase):
    def setUp(self):
        platform_patch = patch("lazor.full_benchmark.platform.platform", return_value="test-platform")
        platform_patch.start()
        self.addCleanup(platform_patch.stop)

    @staticmethod
    def package(directory):
        package = directory / "lazor"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "bff.py").write_text("def parse_bff(text): return text\n", encoding="utf-8")
        (package / "search.py").write_text(
            "from dataclasses import dataclass\nimport time\n"
            "@dataclass\nclass Result:\n"
            "    status: str = 'no_solution'\n"
            "    elapsed_seconds: float = 0.001\n"
            "    solved: bool = False\n"
            "    reason: str = 'frozen package'\n"
            "def solve_search(puzzle, *, timeout):\n"
            "    if 'SLOW' in puzzle: time.sleep(0.3)\n"
            "    return Result()\n", encoding="utf-8")
        return directory

    @staticmethod
    def manifest(directory, *, inventory=False):
        directory.mkdir()
        text = ("GRID START\no\nGRID STOP\nA 1\nL 0 1 -1 -1\nP 0 1\n"
                if inventory else "GRID START\no\nGRID STOP\nL 0 1 1 1\nP 1 2\n")
        board = directory / "case.bff"
        board.write_text(text, encoding="utf-8")
        path = directory / "manifest.json"
        path.write_text(json.dumps({"cases": [{"id": "case", "board": "case.bff",
            "size": 1, "A": int(inventory), "C": 0, "family": "unit",
            "expected_status": "solved", "input_sha256": digest(board.read_bytes())}]}), encoding="utf-8")
        return path

    def test_persistent_real_worker_handles_multiple_requests_and_watchdog_restart(self):
        # Waiting exercises the watchdog while consuming no CPU; solver code
        # is a tiny frozen package so this test has no Z3 dependency.
        with tempfile.TemporaryDirectory() as directory:
            root = self.package(Path(directory) / "frozen")
            worker = PersistentWorker(root, root / "deps", "search", package_hashes(root))
            fast, slow = Path(directory) / "fast.bff", Path(directory) / "slow.bff"
            fast.write_text("FAST", encoding="utf-8")
            slow.write_text("SLOW", encoding="utf-8")

            def request(path, number, hard=1):
                return worker.request({"request_id": str(number), "board": str(path),
                                       "input_sha256": digest(path.read_bytes()), "timeout": .01}, hard)
            try:
                first, second = request(fast, 1), request(fast, 2)
                self.assertEqual(first["status"], "no_solution")
                self.assertEqual(second["worker_generation"], first["worker_generation"])
                timed_out = request(slow, 3, .01)
                self.assertTrue(timed_out["hard_timeout"])
                self.assertEqual(timed_out["status"], "timeout")
                self.assertIsNone(worker.process)
                after = request(fast, 4)
                self.assertEqual(after["status"], "no_solution")
                self.assertEqual(after["request_id"], "4")
                self.assertGreater(after["worker_generation"], first["worker_generation"])
            finally:
                worker.close()

    def test_worker_rejects_frozen_code_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.package(Path(directory) / "frozen")
            worker = PersistentWorker(root, root / "deps", "search", {"wrong": "hash"})
            with self.assertRaises(RuntimeError):
                worker.start()
            self.assertIsNone(worker.process)

    def test_checkpoint_rejects_header_without_newline_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.jsonl"
            configuration = {"version": "frozen"}
            header = json.dumps({"type": "header", "configuration": configuration})
            for data in (b"", header.encode(), header[:-1].encode()):
                with self.subTest(data=data):
                    path.write_bytes(data)
                    with self.assertRaisesRegex(ValueError, "incomplete checkpoint header"):
                        read_checkpoint(path, configuration)
                    self.assertEqual(path.read_bytes(), data)
            path.write_bytes(header.encode() + b"\n")
            self.assertEqual(read_checkpoint(path, configuration), ([], 0))

    def test_checkpoint_repairs_only_incomplete_tail_and_rejects_changed_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.jsonl"
            configuration = {"version": "frozen"}
            header = json.dumps({"type": "header", "configuration": configuration}) + "\n"
            result = json.dumps({"type": "result", "case_id": "one", "trial": 1}) + "\n"
            path.write_bytes((header + result + '{"type":"res').encode())
            rows, lost = read_checkpoint(path, configuration)
            self.assertEqual(len(rows), 1)
            self.assertGreater(lost, 0)
            self.assertEqual(path.read_text(encoding="utf-8"), header + result)
            with self.assertRaises(ValueError):
                read_checkpoint(path, {"version": "changed"})
            path.write_text(header + result + "INVALID\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                read_checkpoint(path, configuration)
            path.write_text(header + result + result, encoding="utf-8")
            with self.assertRaises(ValueError):
                read_checkpoint(path, configuration)

    def test_independent_verification_requires_exact_inventory_and_resume_skips_done(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = self.package(base / "frozen")
            manifest = self.manifest(base / "suite", inventory=True)
            output = base / "run.jsonl"
            false_claim = {"status": "solved", "elapsed_seconds": .001,
                           "placement": [], "process_wall_seconds": .001}
            with patch.object(PersistentWorker, "start", return_value={"type": "ready"}), \
                    patch.object(PersistentWorker, "request", return_value=false_claim.copy()) as request:
                report = run_full(manifest, root, output)
                self.assertEqual(request.call_count, 1)
                report = run_full(manifest, root, output, resume=True)
                self.assertEqual(request.call_count, 1)
            self.assertTrue(report["completed"])
            self.assertEqual(report["summary"]["overall"]["wrong_decisions"], 1)
            self.assertEqual(report["summary"]["overall"]["status_counts"], {"error": 1})
            self.assertFalse(report["summary"]["overall"]["valid"])

    def test_subset_selection_and_input_hash_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = self.package(base / "frozen")
            manifest = self.manifest(base / "suite")
            ids = base / "cases.txt"
            ids.write_text("absent\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                run_full(manifest, root, base / "bad.jsonl", case_ids=ids)
            (manifest.parent / "case.bff").write_text("CHANGED", encoding="utf-8")
            with self.assertRaises(ValueError):
                run_full(manifest, root, base / "bad.jsonl")
            self.assertFalse((base / "bad.jsonl").exists())

    def test_par2_counts_every_timeout_and_invalid_parameters_fail_early(self):
        rows = [{"status": "solved", "elapsed_seconds": .2, "timely_correct": True,
                 "correct_decision": True, "family": "open", "size": 4},
                {"status": "timeout", "elapsed_seconds": None, "hard_timeout": True,
                 "family": "open", "size": 4}]
        report = summary(rows, 1)["overall"]
        self.assertEqual(report["attempted"], 2)
        self.assertAlmostEqual(report["par2_seconds"], 1.1)
        self.assertEqual(report["hard_timeouts"], 1)
        for values in ({"timeout": 0}, {"timeout": float("nan")}, {"grace": 0},
                       {"method": "missing"}, {"repeats": True}, {"limit": 0}):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    run_full("missing", "missing", "unused", **values)


if __name__ == "__main__":
    unittest.main()
