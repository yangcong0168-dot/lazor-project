"""Check the transparent timing baseline independently of SMT solving."""

import unittest
from math import comb

from lazor.bff import parse_bff
from lazor.dfs_baseline import solve_dfs
from lazor.simulation import simulate


def make_puzzle(rows, counts, lasers, targets):
    lines = ["GRID START", *rows, "GRID STOP"]
    lines.extend(f"{kind} {count}" for kind, count in counts.items())
    lines.extend("L " + " ".join(map(str, ray)) for ray in lasers)
    lines.extend(f"P {x} {y}" for x, y in targets)
    return parse_bff("\n".join(lines))


class DFSBaselineTests(unittest.TestCase):
    """Enumeration must respect inventory, optics, and timeout semantics."""

    def test_all_distinct_layouts_are_counted_without_same_kind_repeats(self):
        puzzle = make_puzzle(
            ["o o o", "o o o"], {"A": 2, "B": 1, "C": 1},
            [(0, 1, 1, 1)], [(1, 1)],
        )
        result = solve_dfs(puzzle, timeout=None)
        self.assertEqual(result.status, "no_solution")
        self.assertEqual(
            result.candidates_tested, comb(6, 2) * comb(4, 1) * comb(3, 1)
        )
        self.assertGreater(result.ray_states_processed, 0)
        self.assertGreater(result.states_expanded, 0)
        self.assertIsNone(result.solution)

    def test_finds_splitter_placement_and_replays_all_targets(self):
        puzzle = make_puzzle(
            ["o o o", "o o o", "o o o"], {"C": 1},
            [(1, 2, 1, 1)], [(3, 4), (5, 6), (0, 5)],
        )
        result = solve_dfs(puzzle)
        self.assertTrue(result.solved)
        replay = simulate(puzzle, result.solution.layout.placements)
        self.assertTrue(replay.is_solution)
        self.assertEqual(replay.hit_targets, puzzle.targets)
        self.assertEqual(set(replay.segments), set(result.solution.segments))

    def test_fixed_blocks_and_forbidden_cells_are_preserved(self):
        puzzle = make_puzzle(
            ["A x o", "o B o"], {"A": 1, "C": 1},
            [(0, 1, -1, -1)], [(0, 1)],
        )
        original = (puzzle.grid, dict(puzzle.block_counts))
        result = solve_dfs(puzzle)
        self.assertTrue(result.solved)
        self.assertEqual(result.candidates_tested, 1)
        self.assertEqual(result.solution.layout.grid[0][:2], ("A", "x"))
        self.assertEqual(result.solution.layout.grid[1][1], "B")
        self.assertTrue(all(
            position in puzzle.placeable_positions
            for position in result.solution.layout.placements
        ))
        self.assertEqual(original, (puzzle.grid, puzzle.block_counts))
        self.assertTrue(simulate(
            puzzle, result.solution.layout.placements
        ).is_solution)

    def test_multiple_splitters_are_placed_and_both_branches_verified(self):
        puzzle = make_puzzle(
            ["o o o", "o o o", "o o o"], {"C": 2},
            [(2, 1, 1, 1)], [(6, 5), (5, 0), (0, 3)],
        )
        result = solve_dfs(puzzle)
        self.assertTrue(result.solved)
        self.assertEqual(
            list(result.solution.layout.placements.values()).count("C"), 2
        )
        self.assertTrue(simulate(
            puzzle, result.solution.layout.placements
        ).is_solution)

    def test_source_connected_loop_terminates_as_one_candidate(self):
        puzzle = make_puzzle(
            ["o A o", "A o A", "o A o"], {},
            [(3, 2, 1, -1)], [(3, 2), (4, 3), (3, 4), (2, 3)],
        )
        result = solve_dfs(puzzle)
        self.assertTrue(result.solved)
        self.assertEqual(result.candidates_tested, 1)
        self.assertGreater(result.solution.repeated_states, 0)

    def test_disconnected_cycle_cannot_hit_a_target(self):
        puzzle = make_puzzle(
            ["o A o", "A o A", "o A o"], {},
            [(0, 1, -1, -1)], [(3, 2)],
        )
        result = solve_dfs(puzzle)
        self.assertEqual(result.status, "no_solution")
        self.assertEqual(result.candidates_tested, 1)

    def test_zero_timeout_is_not_reported_as_no_solution(self):
        puzzle = make_puzzle(["o"], {}, [(0, 1, 1, 1)], [(1, 2)])
        result = solve_dfs(puzzle, timeout=0)
        self.assertEqual(result.status, "timeout")
        self.assertEqual(result.candidates_tested, 0)
        self.assertFalse(result.solved)

    def test_invalid_timeouts_are_rejected(self):
        puzzle = make_puzzle(["o"], {}, [(0, 1, 1, 1)], [(1, 2)])
        for timeout in (-1, float("nan"), float("inf"), True, "1"):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    solve_dfs(puzzle, timeout=timeout)


if __name__ == "__main__":
    unittest.main()
