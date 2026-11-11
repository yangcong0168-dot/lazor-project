"""Check frontier pruning against an independent complete-layout oracle."""

import itertools
import random
import unittest
from collections import Counter
from dataclasses import replace
from unittest.mock import patch

from lazor.models import Block, Laser, Puzzle
from lazor.search import (_possible_predecessors, clear_search_caches,
                         configure_search_database, solve_search)
from lazor.simulation import SimulationError, _cell_ahead, simulate


def puzzle(rows, counts=None, lasers=None, targets=None):
    return Puzzle(
        tuple(tuple(row) for row in rows),
        {kind: (counts or {}).get(kind, 0) for kind in "ABC"},
        tuple(Laser(*ray) for ray in (lasers or [(0, 1, 1, 1)])),
        frozenset(targets or [(1, 2)]),
    )


def exhaustive_exists(board):
    positions = board.placeable_positions
    for labels in itertools.product("oABC", repeat=len(positions)):
        inventory = Counter(labels)
        if any(inventory[kind] != board.block_counts[kind] for kind in "ABC"):
            continue
        placed = {position: label for position, label in zip(positions, labels)
                  if label != "o"}
        if simulate(board, placed).is_solution:
            return True
    return False


class FrontierSearchTests(unittest.TestCase):
    def setUp(self):
        # Construction/deadline mocks must exercise cold tables regardless
        # of which other test populated the process-wide cache first.
        previous_database = configure_search_database(None)
        self.addCleanup(configure_search_database, previous_database)
        clear_search_caches()
        self.addCleanup(clear_search_caches)

    def assert_matches_oracle(self, board):
        expected = exhaustive_exists(board)
        original = board.grid, dict(board.block_counts)
        result = solve_search(board, timeout=None)
        self.assertEqual(result.solved, expected, (board, result))
        self.assertEqual(result.status, "solved" if expected else "no_solution")
        if expected:
            self.assertTrue(simulate(board, result.solution.layout.placements).is_solution)
            self.assertEqual(Counter(result.solution.layout.placements.values()),
                             Counter({k: v for k, v in board.block_counts.items() if v}))
        self.assertEqual(original, (board.grid, board.block_counts))

    def test_all_two_by_two_inventories_against_complete_assignments(self):
        targets = [[(3, 4)], [(4, 1)], [(1, 2), (3, 0)], [(0, 3), (4, 3)]]
        for a in range(5):
            for b in range(5 - a):
                for c in range(5 - a - b):
                    for target in targets:
                        with self.subTest(a=a, b=b, c=c, target=target):
                            self.assert_matches_oracle(puzzle(
                                ["oo", "oo"], dict(A=a, B=b, C=c), targets=target))

    def test_seeded_mixed_boards_multiple_sources_and_targets(self):
        rng = random.Random(20261110)
        points = [(x, y) for x in range(5) for y in range(5) if (x + y) % 2]
        for case in range(80):
            rows = ["".join(rng.choice("oooxABC") for _ in range(2)) for _ in range(2)]
            slots = sum(row.count("o") for row in rows)
            counts = Counter(rng.choice("oABC") for _ in range(slots))
            lasers = [(*rng.choice(points), rng.choice((-1, 1)), rng.choice((-1, 1)))
                      for _ in range(1 + case % 3)]
            with self.subTest(rows=rows, counts=counts, lasers=lasers):
                self.assert_matches_oracle(puzzle(rows, counts, lasers, rng.sample(points, 2)))

    def test_three_by_two_assignments_cover_deeper_backtracking(self):
        rng = random.Random(6014)
        points = [(x, y) for x in range(7) for y in range(5) if (x + y) % 2]
        for case in range(30):
            counts = Counter(rng.choice("oABC") for _ in range(6))
            with self.subTest(case=case, counts=counts):
                self.assert_matches_oracle(puzzle(
                    ["ooo", "ooo"], counts,
                    [(0, 1, 1, 1), (6, 3, -1, -1)], rng.sample(points, 3)))

    def test_splitter_and_rear_cell_are_both_dependencies(self):
        for rear in "ABCo":
            self.assert_matches_oracle(puzzle(
                [rear + "o", "oo"], {"C": 1}, [(2, 1, 1, 1)], [(4, 3), (0, 3)]))

    def test_source_connected_loop(self):
        result = solve_search(puzzle(
            ["oAo", "AoA", "oAo"], lasers=[(3, 2, 1, -1)],
            targets=[(3, 2), (4, 3), (3, 4), (2, 3)]))
        self.assertTrue(result.solved)
        self.assertGreater(result.solution.repeated_states, 0)

    def test_restart_resets_frontier_and_inventory_before_complete_retry(self):
        restarted_statuses = set()
        with patch("lazor.search._RESTART_AFTER", 1):
            for counts in ({"A": 1}, {"A": 2}, {"C": 1}, {"A": 1, "C": 1},
                           {"A": 1, "B": 1, "C": 1}):
                for targets in ([(3, 4)], [(0, 3), (4, 3)],
                                [(1, 2), (3, 0)], [(0, 1), (3, 4)]):
                    board = puzzle(["oo", "oo"], counts,
                                   [(0, 1, 1, 1), (4, 3, -1, -1)], targets)
                    with self.subTest(counts=counts, targets=targets):
                        expected = exhaustive_exists(board)
                        result = solve_search(board, timeout=None)
                        self.assertEqual(result.status, "solved" if expected else "no_solution")
                        self.assertLessEqual(result.restarts, 3)
                        if result.restarts:
                            restarted_statuses.add(result.status)
                            self.assertGreater(result.states_expanded, 1)
                        if result.solved:
                            self.assertTrue(simulate(board, result.solution.layout.placements).is_solution)
            # Each target is individually possible, but one mirror cannot
            # cover both. The optimistic root accepts; the retry must finish
            # its complete tree before reporting infeasibility.
            board = puzzle(["oo", "oo"], {"A": 1},
                           targets=[(0, 1), (1, 0), (1, 2)])
            self.assertFalse(exhaustive_exists(board))
            result = solve_search(board, timeout=None)
            self.assertEqual(result.status, "no_solution")
            self.assertEqual(result.restarts, 3)
            restarted_statuses.add(result.status)
        self.assertEqual(restarted_statuses, {"solved", "no_solution"})

    def test_retry_uses_the_original_deadline(self):
        board = puzzle(["oo", "oo"], {"A": 1}, targets=[(1, 2)])
        with patch("lazor.search._RESTART_AFTER", 1), patch(
            "lazor.search.perf_counter", side_effect=itertools.count()
        ):
            result = solve_search(board, timeout=5)
        self.assertEqual(result.status, "timeout")
        self.assertEqual(result.restarts, 1)
        self.assertEqual(result.states_expanded, 2)
        self.assertEqual(result.candidates_tested, 0)

    def test_second_retry_also_uses_original_deadline_and_cumulative_counters(self):
        board = puzzle(["oo", "oo"], {"A": 1},
                       targets=[(0, 1), (1, 0), (1, 2)])
        with patch("lazor.search._RESTART_AFTER", 1), patch(
            "lazor.search.perf_counter", side_effect=itertools.count()
        ):
            result = solve_search(board, timeout=7)
        self.assertEqual(result.status, "timeout")
        self.assertEqual(result.restarts, 2)
        self.assertEqual(result.states_expanded, 4)
        self.assertGreater(result.ray_states_processed, 0)
        self.assertEqual(result.candidates_tested, 0)

    def test_target_ordering_and_cache_eviction_do_not_change_feasibility(self):
        rng = random.Random(92611)
        points = [(x, y) for x in range(7) for y in range(5) if (x + y) % 2]
        with patch("lazor.search._RESTART_AFTER", 0), patch(
            "lazor.search._PRIORITY_CACHE_LIMIT", 1
        ):
            for case in range(25):
                counts = Counter(rng.choice("oABC") for _ in range(6))
                with self.subTest(case=case, counts=counts):
                    self.assert_matches_oracle(puzzle(
                        ["ooo", "ooo"], counts,
                        [(0, 1, 1, 1), (6, 3, -1, -1)], rng.sample(points, 3)))

    def test_optical_work_budget_can_restart_before_decision_budget(self):
        board = puzzle(["oo", "oo"], {"A": 1},
                       targets=[(0, 1), (1, 0), (1, 2)])
        with patch("lazor.search._RESTART_RAY_STATES", 0), patch(
            "lazor.search._RESTART_AFTER", 1000000
        ):
            result = solve_search(board, timeout=None)
        self.assertEqual(result.status, "no_solution")
        self.assertEqual(result.restarts, 3)

    def test_reverse_guidance_finds_verified_solution_before_fallback(self):
        board = puzzle(["oo", "oo"], {"A": 1, "C": 1},
                       targets=[(0, 1), (1, 2), (2, 1)])
        with patch("lazor.search._RESTART_AFTER", 2), patch(
            "lazor.search._possible_predecessors", wraps=_possible_predecessors
        ) as reverse:
            result = solve_search(board, timeout=None)
        self.assertTrue(result.solved)
        self.assertEqual(result.restarts, 1)  # Reverse stage, before fallback.
        self.assertGreater(reverse.call_count, 0)
        self.assertTrue(simulate(board, result.solution.layout.placements).is_solution)

    def test_deadline_includes_reverse_graph_work(self):
        board = puzzle(["oo", "oo"], {"A": 1, "C": 1},
                       targets=[(0, 1), (1, 2), (2, 1)])
        clock = [0.0]

        def costly_reverse(*args):
            clock[0] = 2.0
            return _possible_predecessors(*args)

        with patch("lazor.search._RESTART_AFTER", 2), patch(
            "lazor.search.perf_counter", side_effect=lambda: clock[0]
        ), patch("lazor.search._possible_predecessors", side_effect=costly_reverse):
            result = solve_search(board, timeout=1)
        self.assertEqual(result.status, "timeout")
        self.assertEqual(result.restarts, 1)
        self.assertIsNone(result.solution)

    def test_reverse_prefers_splitter_when_both_successors_hit_targets(self):
        # Straight already reaches the active (left) goal. C must retain the
        # right-going branch as well, without waiting for the C-only pass.
        board = puzzle(["oo", "oo"], {"C": 1}, [(2, 1, -1, 1)],
                       [(1, 2), (3, 2)])
        with patch("lazor.search._RESTART_AFTER", 1):
            result = solve_search(board, timeout=None)
        self.assertTrue(result.solved)
        self.assertEqual(result.restarts, 1)
        self.assertTrue(simulate(board, result.solution.layout.placements).is_solution)

    def test_alternative_nearby_goal_pass_can_finish_before_full_fallback(self):
        board = puzzle(["oo", "oo"], {"A": 1, "C": 2},
                       targets=[(0, 1), (1, 0), (1, 2)])
        with patch("lazor.search._RESTART_AFTER", 2):
            result = solve_search(board, timeout=None)
        self.assertTrue(result.solved)
        self.assertEqual(result.restarts, 2)
        self.assertTrue(simulate(board, result.solution.layout.placements).is_solution)

    def test_singleton_guided_trace_does_not_build_unused_reverse_distances(self):
        board = puzzle(["oooo"] * 4, targets=[(7, 8)])
        with patch("lazor.search._RESTART_AFTER", 0), patch(
            "lazor.search._possible_predecessors",
            side_effect=AssertionError("One ray and one value need no reverse ordering"),
        ):
            result = solve_search(board, timeout=None)
        self.assertTrue(result.solved)
        self.assertEqual(result.restarts, 1)
        self.assertEqual(result.assignment_writes, 0)
        self.assertEqual(result.forced_assignments, 0)
        self.assertGreater(result.implicit_cell_reads, 0)

    def test_implicit_reflector_uses_known_transparent_rear(self):
        board = puzzle(["xo"], {"A": 1}, [(2, 1, 1, 1)], [(1, 2)])
        result = solve_search(board, timeout=None)
        self.assertTrue(result.solved)
        self.assertEqual(result.solution.layout.placements, {(1, 0): "A"})
        self.assertEqual(result.assignment_writes, 0)
        self.assertEqual(result.forced_assignments, 0)
        self.assertEqual(result.implicit_cell_reads, 1)

    def test_reverse_edges_match_independent_forward_optics(self):
        directions = ((-1, -1), (-1, 1), (1, -1), (1, 1))
        width = height = 2
        stride = 5
        states = [(x, y, vx, vy) for x in range(5) for y in range(5)
                  if (x + y) % 2 for vx, vy in directions]

        def encode(x, y, vx, vy):
            return (((y * stride + x) >> 1) << 2) | ((vx > 0) << 1) | (vy > 0)

        def cell_at(x, y, vx, vy):
            column, row = _cell_ahead((x, y, vx, vy))[0]
            return row * width + column if 0 <= column < width and 0 <= row < height else 4

        self.assertEqual(len({encode(*state) for state in states}), len(states))
        self.assertEqual(max(encode(*state) for state in states), len(states) - 1)
        for x, y, vx, vy in states:
            point = ((encode(x, y, vx, vy) >> 2) << 1) | 1
            self.assertEqual(divmod(point, stride), (y, x))

        # Include an unknown-domain relaxation and opposing solid surfaces.
        for masks in ((1, 2, 4, 8, 1), (10, 9, 11, 1, 1), (2, 8, 4, 2, 1),
                      (15, 15, 15, 15, 1)):
            expected = {encode(*state): set() for state in states}
            for state in states:
                x, y, vx, vy = state
                front, face = _cell_ahead(state)
                for code, kind in enumerate("oABC"):
                    if not masks[cell_at(*state)] & (1 << code):
                        continue
                    outgoing = ((vx, vy),) if code == 0 else Block(kind).interact((vx, vy), face)
                    for dx, dy in outgoing:
                        if not masks[cell_at(x, y, dx, dy)] & 9:
                            continue
                        if 0 <= x + dx <= 4 and 0 <= y + dy <= 4:
                            expected[encode(x + dx, y + dy, dx, dy)].add(encode(*state))
            for state, incoming in expected.items():
                actual = _possible_predecessors(state, stride, 4, 4, cell_at,
                                                encode, masks.__getitem__)
                self.assertEqual(set(actual), incoming, (masks, state))

    def test_off_path_inventory_is_completed_without_enumeration(self):
        board = puzzle(["o" * 44] * 44, {"A": 10, "C": 10},
                       [(0, 1, -1, -1)], [(0, 1)])
        result = solve_search(board)
        self.assertTrue(result.solved)
        self.assertEqual(result.states_expanded, 1)
        self.assertEqual(result.candidates_tested, 1)
        self.assertEqual(len(result.solution.layout.placements), 20)

    def test_large_board_many_source_paths_without_recursive_dfs(self):
        lasers = ([(0, y, 1, 1) for y in range(1, 88, 2)]
                  + [(x, 0, 1, 1) for x in range(1, 88, 2)])
        targets = ([(88 - y, 88) for y in range(1, 88, 2)]
                   + [(88, 88 - x) for x in range(1, 88, 2)])
        for count in (0, 1):
            # Deliberately suppress early retries here so this regression
            # exercises an actual >1,900-decision stack, not its shortcut.
            with self.subTest(splitters=count), patch(
                "lazor.search._RESTART_RAY_STATES", 1000000000
            ):
                result = solve_search(puzzle(["o" * 44] * 44, {"C": count},
                                             lasers=lasers, targets=targets))
                self.assertTrue(result.solved)
                if count:
                    self.assertGreater(result.states_expanded, 1900)
                else:
                    self.assertEqual(result.forced_assignments, 0)
                    self.assertEqual(result.assignment_writes, 0)
                    self.assertGreaterEqual(result.implicit_cell_reads, 1936)

    def test_unreachable_targets_are_pruned_before_any_candidate(self):
        board = puzzle(["oooo"] * 4, {"A": 4, "C": 4}, targets=[(1, 1)])
        result = solve_search(board)
        self.assertEqual(result.status, "no_solution")
        self.assertEqual(result.candidates_tested, 0)
        self.assertEqual(result.branches_pruned, 1)
        self.assertEqual(result.optimistic_checks, 0)

    def test_zero_timeout_is_distinct_from_unsatisfiable(self):
        result = solve_search(puzzle(["oo", "oo"]), timeout=0)
        self.assertEqual(result.status, "timeout")
        self.assertEqual(result.candidates_tested, 0)

    def test_invalid_timeouts(self):
        for timeout in (-1, float("nan"), float("inf"), True, "1"):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                solve_search(puzzle(["oo", "oo"]), timeout=timeout)

    def test_invalid_inventory_and_geometry(self):
        board = puzzle(["oo", "oo"])
        for counts in ({"A": -1}, {"A": True}, {"A": 5}):
            with self.subTest(counts=counts), self.assertRaises(ValueError):
                solve_search(replace(board, block_counts=counts))
        with self.assertRaises(SimulationError):
            solve_search(replace(board, lasers=(Laser(1, 1, 1, 1),)))


if __name__ == "__main__":
    unittest.main()
