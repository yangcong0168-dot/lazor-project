"""Semantic regressions for implicit singleton tails and search restoration."""

from collections import Counter
import itertools
import unittest
from unittest.mock import patch

from lazor.models import Laser, Puzzle
from lazor.search import solve_search
from lazor.simulation import simulate


def board(rows, inventory=None, lasers=None, targets=None):
    return Puzzle(tuple(tuple(row) for row in rows),
                  {kind: (inventory or {}).get(kind, 0) for kind in "ABC"},
                  tuple(Laser(*ray) for ray in (lasers or [(0, 1, 1, 1)])),
                  frozenset(targets or [(1, 2)]))


def exhaustive_exists(puzzle):
    positions = puzzle.placeable_positions
    for labels in itertools.product("oABC", repeat=len(positions)):
        counts = Counter(labels)
        if any(counts[kind] != puzzle.block_counts[kind] for kind in "ABC"):
            continue
        placements = {position: kind for position, kind in zip(positions, labels) if kind != "o"}
        if simulate(puzzle, placements).is_solution:
            return True
    return False


class SearchHotpathTests(unittest.TestCase):
    def assert_exact(self, puzzle, result):
        self.assertTrue(result.solved)
        replay = simulate(puzzle, result.solution.layout.placements)
        self.assertTrue(replay.is_solution)
        self.assertEqual(Counter(replay.layout.placements.values()),
                         Counter({k: v for k, v in puzzle.block_counts.items() if v}))

    def test_all_singleton_kinds_fill_exactly_without_assign_writes(self):
        lasers = [(2, 1, -1, 1), (2, 1, 1, 1), (0, 1, 1, 1),
                  (6, 3, -1, -1), (2, 1, -1, 1)]
        for kind in "oABC":
            with self.subTest(kind=kind):
                inventory = {} if kind == "o" else {kind: 6}
                preliminary = board(["ooo", "ooo"], inventory, lasers)
                placements = {} if kind == "o" else {p: kind for p in preliminary.placeable_positions}
                expected = simulate(preliminary, placements)
                puzzle = board(["ooo", "ooo"], inventory, lasers, expected.visited_points)
                result = solve_search(puzzle, timeout=None)
                self.assert_exact(puzzle, result)
                self.assertEqual(result.assignment_writes, 0)
                self.assertEqual(result.forced_assignments, 0)
                self.assertGreater(result.implicit_cell_reads, 0)

    def test_backtrack_recomputes_last_kind_for_front_and_rear(self):
        # C first leaves A behind the source and blocks the reflected branch.
        # The next decision A must observe that the implicit rear is now C.
        for vx, target, mirror, splitter in (
            (1, (1, 0), (1, 0), (0, 0)),
            (-1, (3, 0), (0, 0), (1, 0)),
        ):
            with self.subTest(direction=vx):
                puzzle = board(["oo"], {"A": 1, "C": 1}, [(2, 1, vx, -1)], [target])
                result = solve_search(puzzle, timeout=None)
                self.assert_exact(puzzle, result)
                self.assertEqual(result.solution.layout.placements, {mirror: "A", splitter: "C"})
                self.assertGreaterEqual(result.states_expanded, 3)
                self.assertGreaterEqual(result.assignment_writes, 2)
                self.assertGreaterEqual(result.implicit_cell_reads, 2)

    def test_implicit_cell_revisited_from_multisource_mirror_loop(self):
        puzzle = board(["oAo", "AoA", "oAo"], {},
                       [(3, 2, 1, -1), (3, 2, 1, -1), (2, 3, 1, 1)],
                       [(3, 2), (4, 3), (3, 4), (2, 3)])
        result = solve_search(puzzle, timeout=None)
        self.assert_exact(puzzle, result)
        self.assertGreater(result.solution.repeated_states, 0)
        self.assertEqual(result.assignment_writes, 0)
        self.assertEqual(result.forced_assignments, 0)
        self.assertGreater(result.implicit_cell_reads, 1)

    def test_restarts_and_mixed_domains_match_complete_layout_oracle(self):
        inventories = ({"A": 1, "B": 1, "C": 1}, {"A": 1, "C": 2},
                       {"A": 2, "C": 2}, {"B": 1, "C": 2})
        targets = ([(1, 0), (3, 4)], [(1, 2), (3, 2)],
                   [(0, 1), (4, 3)], [(1, 2), (2, 3), (3, 0)])
        for inventory in inventories:
            for points in targets:
                puzzle = board(["oo", "oo"], inventory,
                               [(2, 1, 1, 1), (2, 3, -1, -1), (2, 1, 1, 1)], points)
                expected = exhaustive_exists(puzzle)
                for restart_after in (0, 1):
                    with self.subTest(inventory=inventory, targets=points, restart=restart_after), \
                            patch("lazor.search._RESTART_AFTER", restart_after):
                        result = solve_search(puzzle, timeout=None)
                        self.assertEqual(result.status, "solved" if expected else "no_solution")
                        if result.solved:
                            self.assert_exact(puzzle, result)


if __name__ == "__main__":
    unittest.main()
