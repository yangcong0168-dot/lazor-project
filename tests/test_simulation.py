"""Hand-checked optical paths, independent of any placement search."""

import unittest
from dataclasses import replace

from lazor.bff import parse_bff
from lazor.models import Laser
from lazor.simulation import SimulationError, simulate


def puzzle_from_rows(rows, lasers, targets, inventory=None):
    """Build a small readable fixture using the public BFF parser."""
    records = ["GRID START", *rows, "GRID STOP"]
    records.extend(
        f"{kind} {count}" for kind, count in (inventory or {}).items()
    )
    records.extend("L " + " ".join(map(str, laser)) for laser in lasers)
    records.extend(f"P {x} {y}" for x, y in targets)
    return parse_bff("\n".join(records))


class SimulationTests(unittest.TestCase):
    """Trace actual paths and targets, rather than just block formulas."""

    def test_straight_ray_crosses_empty_and_unplaceable_cells(self):
        puzzle = puzzle_from_rows(
            ["x o o", "o x o"], [(0, 1, 1, 1)],
            [(0, 1), (1, 2), (3, 4), (4, 3)],
        )
        result = simulate(puzzle)

        self.assertEqual(
            result.segments,
            (((0, 1), (1, 2)), ((1, 2), (2, 3)), ((2, 3), (3, 4))),
        )
        self.assertEqual(
            result.visited_points, frozenset({(0, 1), (1, 2), (2, 3), (3, 4)})
        )
        self.assertEqual(result.hit_targets, {(0, 1), (1, 2), (3, 4)})
        self.assertEqual(result.missed_targets, {(4, 3)})
        self.assertFalse(result.all_targets_hit)
        self.assertFalse(result.is_solution)

    def test_reflection_on_each_face_and_incoming_direction(self):
        # Each trajectory touches the middle A block, then reaches a boundary.
        cases = [
            ((2, 3, 1, 1), (1, 4), (0, 5)),
            ((2, 3, 1, -1), (1, 2), (0, 1)),
            ((4, 3, -1, 1), (5, 4), (6, 5)),
            ((4, 3, -1, -1), (5, 2), (6, 1)),
            ((3, 2, 1, 1), (4, 1), (5, 0)),
            ((3, 2, -1, 1), (2, 1), (1, 0)),
            ((3, 4, 1, -1), (4, 5), (5, 6)),
            ((3, 4, -1, -1), (2, 5), (1, 6)),
        ]
        for laser, intermediate, boundary in cases:
            with self.subTest(laser=laser):
                puzzle = puzzle_from_rows(
                    ["o o o", "o A o", "o o o"],
                    [laser], [intermediate, boundary],
                )
                result = simulate(puzzle)
                self.assertEqual(
                    result.segments,
                    ((laser[:2], intermediate), (intermediate, boundary)),
                )
                self.assertTrue(result.all_targets_hit)

    def test_opaque_block_stops_ray_at_its_surface(self):
        puzzle = puzzle_from_rows(
            ["o o o", "o B o", "o o o"], [(0, 1, 1, 1)],
            [(2, 3), (3, 4)],
        )
        result = simulate(puzzle)

        self.assertEqual(
            result.segments, (((0, 1), (1, 2)), ((1, 2), (2, 3)))
        )
        self.assertEqual(result.hit_targets, {(2, 3)})
        self.assertEqual(result.missed_targets, {(3, 4)})
        self.assertNotIn((3, 4), result.visited_points)

    def test_source_cannot_reflect_through_opaque_back_cell(self):
        for rear in ("A", "B"):
            with self.subTest(rear=rear):
                puzzle = puzzle_from_rows(
                    [f"{rear} A", "o o"], [(2, 1, 1, 1)],
                    [(2, 1), (0, 3)],
                )
                result = simulate(puzzle)
                self.assertEqual(result.segments, ())
                self.assertEqual(result.hit_targets, {(2, 1)})
                self.assertEqual(result.missed_targets, {(0, 3)})
                self.assertFalse(result.is_solution)

    def test_splitter_cannot_reflect_through_opaque_back_cell(self):
        for rear in ("A", "B"):
            with self.subTest(rear=rear):
                puzzle = puzzle_from_rows(
                    [f"{rear} C", "o o"], [(2, 1, 1, 1)],
                    [(4, 3), (0, 3)],
                )
                result = simulate(puzzle)
                self.assertEqual(
                    result.segments,
                    (((2, 1), (3, 2)), ((3, 2), (4, 3))),
                )
                self.assertEqual(result.hit_targets, {(4, 3)})
                self.assertEqual(result.missed_targets, {(0, 3)})

    def test_splitter_traces_both_branches_without_resplitting_on_exit(self):
        puzzle = puzzle_from_rows(
            ["o o o", "o C o", "o o o"], [(2, 3, 1, 1)],
            [(5, 6), (0, 5)],
        )
        result = simulate(puzzle)

        self.assertEqual(
            set(result.segments),
            {
                ((2, 3), (3, 4)), ((3, 4), (4, 5)), ((4, 5), (5, 6)),
                ((2, 3), (1, 4)), ((1, 4), (0, 5)),
            },
        )
        self.assertEqual(len(result.segments), 5)
        self.assertTrue(result.is_solution)

    def test_adjacent_splitters_each_create_their_own_branch(self):
        puzzle = puzzle_from_rows(
            ["o C o", "o C o", "o o o"], [(2, 1, 1, 1)],
            [(6, 5), (5, 0), (0, 3)],
        )
        result = simulate(puzzle)

        self.assertEqual(
            set(result.segments),
            {
                ((2, 1), (3, 2)), ((2, 1), (1, 2)),
                ((3, 2), (4, 3)), ((3, 2), (4, 1)),
                ((4, 3), (5, 4)), ((5, 4), (6, 5)),
                ((4, 1), (5, 0)), ((1, 2), (0, 3)),
            },
        )
        self.assertTrue(result.all_targets_hit)

    def test_mirror_beyond_splitter_reflects_transmitted_branch(self):
        puzzle = puzzle_from_rows(
            ["o C o", "o A o", "o o o"], [(2, 1, 1, 1)],
            [(5, 0), (0, 3)],
        )
        result = simulate(puzzle)

        self.assertEqual(
            set(result.segments),
            {
                ((2, 1), (3, 2)), ((3, 2), (4, 1)), ((4, 1), (5, 0)),
                ((2, 1), (1, 2)), ((1, 2), (0, 3)),
            },
        )
        self.assertTrue(result.all_targets_hit)

    def test_outward_boundary_laser_hits_start_without_outside_segments(self):
        puzzle = puzzle_from_rows(
            ["o o", "o o"], [(0, 1, -1, 1)], [(0, 1)],
        )
        result = simulate(puzzle)

        self.assertEqual(result.segments, ())
        self.assertEqual(result.visited_points, {(0, 1)})
        self.assertTrue(result.is_solution)

    def test_multiple_lasers_collectively_hit_all_targets(self):
        puzzle = puzzle_from_rows(
            ["o o o", "o o o"], [(0, 1, 1, 1), (6, 1, -1, 1)],
            [(2, 3), (4, 3)],
        )
        result = simulate(puzzle)

        self.assertEqual(result.hit_targets, {(2, 3), (4, 3)})
        self.assertEqual(result.missed_targets, frozenset())
        self.assertTrue(result.all_targets_hit)

    def test_same_point_in_different_directions_remains_distinct(self):
        puzzle = puzzle_from_rows(
            ["o o o", "o o o"], [(1, 2, 1, -1), (1, 2, 1, 1)],
            [(3, 0), (3, 4)],
        )
        result = simulate(puzzle)

        self.assertTrue(result.all_targets_hit)
        self.assertIn(((1, 2), (2, 1)), result.segments)
        self.assertIn(((1, 2), (2, 3)), result.segments)

    def test_duplicate_laser_does_not_duplicate_path_segments(self):
        puzzle = puzzle_from_rows(
            ["o o o", "o o o"], [(0, 1, 1, 1)], [(3, 4)],
        )
        single = simulate(puzzle)
        duplicate = simulate(replace(puzzle, lasers=puzzle.lasers * 2))

        self.assertEqual(duplicate.segments, single.segments)
        self.assertEqual(duplicate.hit_targets, single.hit_targets)
        self.assertGreaterEqual(duplicate.repeated_states, 1)

    def test_closed_mirror_loop_terminates_without_losing_targets(self):
        # A diamond around the empty center: top -> right -> bottom -> left.
        puzzle = puzzle_from_rows(
            ["o A o", "A o A", "o A o"], [(3, 2, 1, -1)],
            [(3, 2), (4, 3), (3, 4), (2, 3)],
        )
        result = simulate(puzzle)

        self.assertEqual(
            set(result.segments),
            {
                ((3, 2), (4, 3)), ((4, 3), (3, 4)),
                ((3, 4), (2, 3)), ((2, 3), (3, 2)),
            },
        )
        self.assertEqual(result.visited_points, puzzle.targets)
        self.assertTrue(result.all_targets_hit)
        self.assertGreaterEqual(result.repeated_states, 1)
        self.assertLess(result.states_processed, 20)

    def test_targets_hit_is_not_a_solution_with_unused_inventory(self):
        puzzle = puzzle_from_rows(
            ["o o", "o o"], [(0, 1, 1, 1)], [(3, 4)], {"A": 1},
        )
        result = simulate(puzzle)

        self.assertTrue(result.all_targets_hit)
        self.assertFalse(result.is_solution)
        self.assertEqual(result.remaining_blocks, {"A": 1, "B": 0, "C": 0})

    def test_complete_manual_placement_can_be_a_solution(self):
        puzzle = puzzle_from_rows(
            ["o o", "o o"], [(0, 1, 1, 1)], [(3, 4)], {"A": 1},
        )
        placements = {(1, 0): "A"}  # Outside this laser's path.
        result = simulate(puzzle, placements)

        self.assertTrue(result.is_solution)
        self.assertEqual(result.remaining_blocks, {"A": 0, "B": 0, "C": 0})
        self.assertEqual(puzzle.grid, (("o", "o"), ("o", "o")))
        self.assertEqual(puzzle.block_counts, {"A": 1, "B": 0, "C": 0})
        self.assertEqual(placements, {(1, 0): "A"})

    def test_movable_block_uses_same_optical_rule_as_fixed_block(self):
        empty = puzzle_from_rows(
            ["o o o", "o o o", "o o o"], [(2, 3, 1, 1)],
            [(0, 5)], {"A": 1},
        )
        fixed = puzzle_from_rows(
            ["o o o", "o A o", "o o o"], [(2, 3, 1, 1)], [(0, 5)],
        )

        self.assertEqual(
            simulate(empty, {(1, 1): "A"}).segments,
            simulate(fixed).segments,
        )

    def test_illegal_placements_are_rejected(self):
        puzzle = puzzle_from_rows(
            ["A x", "o o"], [(0, 1, 1, 1)], [(0, 1)], {"A": 1},
        )
        cases = [
            {(0, 0): "A"},       # Existing fixed block.
            {(1, 0): "A"},       # Forbidden cell.
            {(2, 1): "A"},       # Outside the board.
            {(0, 1): "B"},       # No B blocks available.
            {(0, 1): "D"},       # Unknown block type.
            {(0, 1): "A", (1, 1): "A"},  # Too many A blocks.
        ]
        for placements in cases:
            with self.subTest(placements=placements):
                with self.assertRaises(ValueError):
                    simulate(puzzle, placements)

    def test_corner_and_center_origins_are_rejected_explicitly(self):
        for position in ((0, 0), (1, 1)):
            with self.subTest(position=position):
                puzzle = puzzle_from_rows(
                    ["o o", "o o"], [(*position, 1, 1)], [(3, 4)],
                )
                with self.assertRaises(SimulationError):
                    simulate(puzzle)

    def test_invalid_direction_in_direct_model_is_rejected(self):
        puzzle = puzzle_from_rows(
            ["o o", "o o"], [(0, 1, 1, 1)], [(3, 4)],
        )
        invalid = replace(puzzle, lasers=(Laser(0, 1, 0, 1),))

        with self.assertRaises(SimulationError):
            simulate(invalid)


if __name__ == "__main__":
    unittest.main()
