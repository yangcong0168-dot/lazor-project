"""Verify saved layouts, status labels, and ray diagram geometry."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

from lazor.bff import load_bff, parse_bff
from lazor.output import format_report, write_bff, write_report, write_svg
from lazor.simulation import simulate


def sample_result():
    """Place a C block whose transmitted and reflected rays hit targets."""
    puzzle = parse_bff(
        "GRID START\no o\no o\nGRID STOP\nC 1\n"
        "L 2 1 -1 1\nP 1 2\nP 3 2\n"
    )
    return simulate(puzzle, {(0, 0): "C"})


def sample_search(result):
    """Supply known statistics independently of the planner implementation."""
    return SimpleNamespace(
        status="solved", solution=result, elapsed_seconds=0.125,
        build_seconds=0.020, solver_seconds=0.100, verification_seconds=0.005,
        state_count=24, transition_count=32, variable_count=52,
        constraint_count=81, solver_version="5.3.3.0", solver_checks=1,
        reason="", reachable_states=frozenset({(2, 1, -1, 1)}),
    )


class PlanningOutputTests(unittest.TestCase):
    """Automatically found solutions must carry truthful planning metadata."""

    def test_report_separates_search_statistics_from_final_simulation(self):
        result = sample_result()
        report = format_report(result, search=sample_search(result))
        self.assertIn("LAZOR CONSTRAINT SOLUTION REPORT", report)
        self.assertIn("Algorithm: SMT state-transition planning (Z3)", report)
        self.assertIn("Planning time: 0.125000 seconds", report)
        self.assertIn("Model build time: 0.020000 seconds", report)
        self.assertIn("Constraint solve time: 0.100000 seconds", report)
        self.assertIn("Verification time: 0.005000 seconds", report)
        self.assertIn("Solver version: 5.3.3.0", report)
        self.assertIn("Optical states: 24", report)
        self.assertIn("Conditional transitions: 32", report)
        self.assertIn("Model variables: 52", report)
        self.assertIn("Model constraints: 81", report)
        self.assertIn("Solver checks: 1", report)
        self.assertIn("Reachable states: 1", report)
        self.assertIn("Simulation time:", report)
        self.assertNotIn("no search", report)
        self.assertNotIn("BFS", report)
        self.assertNotIn("Candidates tested", report)

    def test_search_exports_keep_metadata_and_replay_the_solution(self):
        result = sample_result()
        search = sample_search(result)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            text_path = write_report(
                result, folder / "solution.txt", search=search
            )
            svg_path = write_svg(
                result, folder / "solution.svg", search=search
            )
            bff_path = write_bff(
                result, folder / "solution.bff", search=search
            )
            self.assertEqual(
                text_path.read_text("utf-8"),
                format_report(result, search=search),
            )
            svg_text = " ".join(ET.parse(svg_path).getroot().itertext())
            self.assertIn("Lazor constraint solution", svg_text)
            self.assertIn("SMT: 0.125000 s", svg_text)
            self.assertIn("24 states | 81 constraints", svg_text)
            self.assertNotIn("no search", svg_text)
            bff_text = bff_path.read_text("utf-8")
            self.assertIn(
                "# Algorithm: SMT state-transition planning (Z3)", bff_text
            )
            self.assertIn("# Planning time: 0.125000 seconds", bff_text)
            replay = simulate(load_bff(bff_path))
            self.assertTrue(replay.is_solution)
            self.assertEqual(replay.segments, result.segments)

    def test_mismatched_or_unsolved_search_is_rejected_before_writing(self):
        result = sample_result()
        mismatched = sample_search(sample_result())
        unsolved = sample_search(result)
        unsolved.status = "timeout"
        incomplete = simulate(parse_bff(
            "GRID START\no o\nGRID STOP\nA 1\n"
            "L 0 1 1 1\nP 1 2\n"
        ))
        cases = [
            (result, mismatched), (result, unsolved),
            (incomplete, sample_search(incomplete)),
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "should_not_exist"
            for layout_result, search in cases:
                with self.subTest(status=search.status):
                    with self.assertRaises(ValueError):
                        format_report(layout_result, search=search)
                    for writer in (write_report, write_svg, write_bff):
                        with self.assertRaises(ValueError):
                            writer(layout_result, output, search=search)
                        self.assertFalse(output.exists())


class ReportTests(unittest.TestCase):
    """Reports must distinguish success, partial inventory, and missed rays."""

    def test_complete_success_includes_board_and_metadata(self):
        result = sample_result()
        report = format_report(result)
        self.assertIn("Status: VALID SOLUTION", report)
        self.assertIn("GRID START\nC o\no o\nGRID STOP", report)
        self.assertIn("C at (0, 0)", report)
        self.assertIn("Remaining movable blocks: A=0, B=0, C=0", report)
        self.assertIn("Hit targets: (1, 2), (3, 2)", report)
        self.assertIn("Missed targets: none", report)
        self.assertIn("L1: start=(2, 1), direction=(-1, 1)", report)
        self.assertIn("no search was performed", report)

    def test_unplaced_inventory_prevents_valid_solution_status(self):
        puzzle = parse_bff(
            "GRID START\no o\nGRID STOP\nA 1\nL 0 1 1 1\nP 1 2\n"
        )
        result = simulate(puzzle)
        report = format_report(result)
        self.assertIn("TARGETS HIT - INVENTORY INCOMPLETE", report)
        self.assertNotIn("VALID SOLUTION", report)
        self.assertIn("Remaining movable blocks: A=1, B=0, C=0", report)

    def test_missed_target_does_not_claim_puzzle_has_no_solution(self):
        puzzle = parse_bff(
            "GRID START\nB\nGRID STOP\nL 0 1 1 1\nP 1 2\n"
        )
        report = format_report(simulate(puzzle))
        self.assertIn("Status: TARGETS MISSED", report)
        self.assertIn("Missed targets: (1, 2)", report)
        self.assertIn("Original fixed blocks:\n  B at (0, 0)", report)
        self.assertNotIn("NO SOLUTION", report)
        self.assertNotIn("VALID SOLUTION", report)

    def test_text_writer_creates_parent_directory(self):
        result = sample_result()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "report.txt"
            self.assertEqual(write_report(result, path), path)
            self.assertEqual(path.read_text("utf-8"), format_report(result))


class SnapshotTests(unittest.TestCase):
    """Saved BFF snapshots must reproduce the traced layout."""

    def test_completed_snapshot_replays_same_rays_and_target_hits(self):
        result = sample_result()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "snapshot.bff"
            self.assertEqual(write_bff(result, path), path)
            text = path.read_text("utf-8")
            restored = load_bff(path)
            replay = simulate(restored)
        self.assertEqual(restored.grid, result.layout.grid)
        self.assertEqual(restored.block_counts, {"A": 0, "B": 0, "C": 0})
        self.assertEqual(restored.lasers, result.layout.puzzle.lasers)
        self.assertEqual(restored.targets, result.layout.puzzle.targets)
        self.assertEqual(replay.hit_targets, result.hit_targets)
        self.assertEqual(replay.segments, result.segments)
        self.assertTrue(replay.is_solution)
        self.assertIn("# Newly placed: C (0, 0)", text)
        self.assertIn("not the original movable puzzle", text)

    def test_partial_snapshot_retains_inventory_and_fixed_block_origins(self):
        puzzle = parse_bff(
            "GRID START\nA o o\no o o\nGRID STOP\nA 1\nC 1\n"
            "L 2 3 1 -1\nP 3 2\n"
        )
        result = simulate(puzzle, {(1, 0): "C"})
        with tempfile.TemporaryDirectory() as directory:
            path = write_bff(result, Path(directory) / "partial.bff")
            text = path.read_text("utf-8")
            restored = load_bff(path)
            replay = simulate(restored)
        self.assertEqual(restored.block_counts, {"A": 1, "B": 0, "C": 0})
        self.assertEqual(replay.segments, result.segments)
        self.assertEqual(replay.hit_targets, result.hit_targets)
        self.assertIn("# Original fixed: A (0, 0)", text)
        self.assertIn("# Newly placed: C (1, 0)", text)
        self.assertFalse(replay.is_solution)


class DiagramTests(unittest.TestCase):
    """SVG geometry must use the same half-block coordinates as the tracer."""

    def test_svg_is_valid_xml_and_paths_match_simulated_segments(self):
        result = sample_result()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "board.svg"
            self.assertEqual(write_svg(result, path), path)
            root = ET.parse(path).getroot()
        ns = {"s": "http://www.w3.org/2000/svg"}
        self.assertEqual(root.tag, f"{{{ns['s']}}}svg")
        rays = root.findall("s:g[@id='rays']/s:line", ns)
        self.assertEqual(len(rays), len(result.segments))
        for node, (start, end) in zip(rays, result.segments):
            coordinates = tuple(int(node.get(key)) for key in (
                "x1", "y1", "x2", "y2"
            ))
            self.assertEqual(coordinates, (
                72 + 40 * start[0], 144 + 40 * start[1],
                72 + 40 * end[0], 144 + 40 * end[1],
            ))
        targets = root.findall("s:g[@id='targets']/s:circle", ns)
        self.assertEqual(len(targets), 2)
        self.assertEqual(
            {node.get("data-status") for node in targets}, {"hit"}
        )
        placed = root.find("s:g[@id='grid']/s:rect[@data-kind='C']", ns)
        self.assertIsNotNone(placed)
        self.assertEqual(placed.get("stroke-dasharray"), "6 4")
        self.assertIn("VALID SOLUTION", " ".join(root.itertext()))

    def test_missed_targets_and_unplaced_inventory_remain_visible(self):
        puzzle = parse_bff(
            "GRID START\no o\nGRID STOP\nA 1\nL 0 1 1 1\nP 3 0\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = write_svg(simulate(puzzle), Path(directory) / "miss.svg")
            root = ET.parse(path).getroot()
        ns = {"s": "http://www.w3.org/2000/svg"}
        target = root.find("s:g[@id='targets']/s:circle", ns)
        self.assertEqual(target.get("data-status"), "missed")
        text = " ".join(root.itertext())
        self.assertIn("TARGETS MISSED", text)
        self.assertIn("1 blocks unplaced", text)
        self.assertNotIn("VALID SOLUTION", text)


if __name__ == "__main__":
    unittest.main()
