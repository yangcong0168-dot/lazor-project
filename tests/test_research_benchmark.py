"""Validate coverage, censored metrics, isolation, and durable resume."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from lazor.research_benchmark import (
    compare_versions, holdout_specs, prepare_panel, reference_specs, summarize,
)
from lazor.scaling import _bff, _digest, _json_write, make_case


class ResearchBenchmarkTests(unittest.TestCase):
    def setUp(self):
        platform_patch = patch("lazor.research_benchmark.platform.platform", return_value="test-platform")
        platform_patch.start()
        self.addCleanup(platform_patch.stop)

    def test_selection_covers_all_dimensions_and_independent_inventory_pairs(self):
        reference, holdout = reference_specs(), holdout_specs()
        self.assertEqual(len(reference), 82)
        self.assertEqual(len(holdout), 49)
        for specs in (reference, holdout):
            self.assertEqual({row[0] for row in specs}, set(range(4, 45)))
            self.assertEqual({(row[1], row[2]) for row in specs},
                             {(a, c) for a in range(4, 11) for c in range(4, 11)})
            self.assertTrue(all(a + c <= size * size for size, a, c, _ in specs))
            counts = [sum(row[3] == family for row in specs)
                      for family in ("open", "obstacles", "parity_unsat")]
            self.assertLessEqual(max(counts) - min(counts), 1)

    @staticmethod
    def write_source(directory, specs):
        directory.mkdir()
        cases = []
        for index, (size, a, c, family) in enumerate(specs):
            puzzle, witness, metadata = make_case(size, a, c, family)
            data = _bff(puzzle).encode("utf-8")
            board, witness_name = f"{index}.bff", f"{index}.json"
            (directory / board).write_bytes(data)
            _json_write(directory / witness_name, witness)
            cases.append({"id": str(index), "size": size, "A": a, "C": c,
                          "family": family, "board": board, "witness": witness_name,
                          "input_sha256": _digest(data), **metadata})
        _json_write(directory / "manifest.json", {"schema_version": 1, "seed": 20261110,
                                                   "cases": cases, "scope": "test"})
        return directory / "manifest.json"

    def test_prepared_panel_preserves_reference_and_isolates_new_seed_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.write_source(root / "source", reference_specs())
            manifest = prepare_panel(source, root / "panel")
            self.assertEqual(manifest["case_count"], 131)
            self.assertEqual(manifest["split_counts"], {"reference": 82, "holdout": 49})
            original = json.loads(source.read_text(encoding="utf-8"))
            original_hashes = {case["input_sha256"] for case in original["cases"]}
            holdout_hashes = {case["input_sha256"] for case in manifest["cases"] if case["split"] == "holdout"}
            self.assertFalse(original_hashes & holdout_hashes)
            for case in manifest["cases"]:
                self.assertEqual(_digest((root / "panel" / case["board"]).read_bytes()), case["input_sha256"])
            with self.assertRaises(ValueError):
                prepare_panel(source, root / "bad", holdout_seed=20261110)

    def test_par2_penalizes_timeouts_and_ratios_use_matched_completions_only(self):
        def row(case, version, elapsed, timely, status="solved"):
            return {"case_id": case, "trial": 1, "method": "search", "version": version,
                    "elapsed_seconds": elapsed, "timely_correct": timely,
                    "correct_decision": timely, "wrong_decision": False,
                    "status": status, "split": "holdout", "family": "open",
                    "expected_status": "solved", "process_wall_seconds": 1}
        rows = [row("both", "baseline", .2, True), row("both", "current", .1, True),
                row("censored", "baseline", None, False, "timeout"),
                row("censored", "current", .3, True)]
        summary = summarize(rows, .5, ("search",))["overall"]["search"]
        self.assertAlmostEqual(summary["baseline"]["par2_seconds"], .6)
        self.assertAlmostEqual(summary["current"]["par2_seconds"], .2)
        self.assertEqual(summary["matched_completions"]["pairs"], 1)
        self.assertEqual(summary["matched_completions"]["median_baseline_over_current"], 2)
        rows[0]["wrong_decision"] = True
        self.assertFalse(summarize(rows, .5, ("search",))["overall"]["search"]["baseline"]["valid"])

    def test_workers_alternate_frozen_versions_and_resume_without_repeating(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = self.write_source(root / "panel", reference_specs()[:2])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for case in manifest["cases"]:
                case["split"] = "reference"
            _json_write(manifest_path, manifest)
            roots = {}
            for version in ("baseline", "current"):
                roots[version] = root / version
                (roots[version] / "lazor").mkdir(parents=True)
                (roots[version] / "lazor" / "__init__.py").write_text("", encoding="utf-8")
            seen = []

            def fake_run(command, **kwargs):
                seen.append(Path(command[3]).name)
                Path(command[-1]).write_text(json.dumps({"status": "timeout", "elapsed_seconds": .01}),
                                            encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            output = root / "comparison.json"
            with patch("lazor.research_benchmark.subprocess.run", side_effect=fake_run):
                report = compare_versions(manifest_path, output, baseline_root=roots["baseline"],
                                          current_root=roots["current"])
                compare_versions(manifest_path, output, baseline_root=roots["baseline"],
                                 current_root=roots["current"], resume=True)
            self.assertEqual(seen, ["baseline", "current", "current", "baseline"])
            self.assertTrue(report["completed"])
            self.assertEqual(len(report["results"]), 4)
            (roots["current"] / "lazor" / "__init__.py").write_text("# changed", encoding="utf-8")
            with self.assertRaises(ValueError):
                compare_versions(manifest_path, output, baseline_root=roots["baseline"],
                                 current_root=roots["current"], resume=True)

    def test_actual_subprocess_bootstrap_uses_each_frozen_package(self):
        # Tiny frozen packages avoid Z3 and isolate the actual -c bootstrap
        # from repository imports. Distinct reasons prove which package ran.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = self.write_source(root / "panel", reference_specs()[:1])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["cases"][0]["split"] = "reference"
            _json_write(manifest_path, manifest)
            roots = {}
            for version in ("baseline", "current"):
                roots[version] = root / version
                package = roots[version] / "lazor"
                package.mkdir(parents=True)
                (package / "__init__.py").write_text("", encoding="utf-8")
                (package / "bff.py").write_text("def load_bff(path): return ('frozen', str(path))\n", encoding="utf-8")
                (package / "search.py").write_text(
                    "from dataclasses import dataclass\n"
                    "@dataclass\n"
                    "class Result:\n"
                    "    status: str = 'timeout'\n"
                    f"    reason: str = {version!r}\n"
                    "    elapsed_seconds: float = 0.01\n"
                    "    solved: bool = False\n"
                    "def solve_search(puzzle, *, timeout): return Result()\n", encoding="utf-8")
            report = compare_versions(manifest_path, root / "report.json",
                                      baseline_root=roots["baseline"], current_root=roots["current"],
                                      timeout=.01)
            self.assertEqual([(r["version"], r["reason"]) for r in report["results"]],
                             [("baseline", "baseline"), ("current", "current")])
            self.assertTrue(all(r["status"] == "timeout" for r in report["results"]))

    def test_missing_worker_output_cannot_reuse_previous_trial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = self.write_source(root / "panel", reference_specs()[:1])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["cases"][0]["split"] = "reference"
            _json_write(manifest_path, manifest)
            package_root = root / "frozen"
            (package_root / "lazor").mkdir(parents=True)
            (package_root / "lazor" / "__init__.py").write_text("", encoding="utf-8")
            calls = []

            def fake_run(command, **kwargs):
                calls.append(command)
                if len(calls) == 1:
                    Path(command[-1]).write_text('{"status":"timeout","elapsed_seconds":0.01}', encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("lazor.research_benchmark.subprocess.run", side_effect=fake_run):
                report = compare_versions(manifest_path, root / "report.json",
                                          baseline_root=package_root, current_root=package_root)
            self.assertEqual([r["status"] for r in report["results"]], ["timeout", "error"])

    def test_invalid_parameters_fail_before_reading_inputs(self):
        for values in ({"timeout": 0}, {"timeout": float("nan")}, {"timeout": True},
                       {"repeats": 0}, {"repeats": True}, {"methods": ()},
                       {"methods": ("search", "search")}, {"limit": 0}, {"split": "missing"}):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    compare_versions("missing.json", "unused.json", baseline_root="missing", **values)


if __name__ == "__main__":
    unittest.main()
