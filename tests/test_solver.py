"""Check the state-transition planning model against independent ray traces."""

import itertools
import random
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from lazor.bff import parse_bff
from lazor.simulation import SimulationError, simulate
from lazor.solver import _load_z3, build_planning_model, solve_constraints


def make_puzzle(rows, inventory, lasers, targets):
    """Create small, inspectable planning problems through the BFF parser."""
    records = ["GRID START", *rows, "GRID STOP"]
    records.extend(f"{kind} {count}" for kind, count in inventory.items())
    records.extend("L " + " ".join(map(str, ray)) for ray in lasers)
    records.extend(f"P {x} {y}" for x, y in targets)
    return parse_bff("\n".join(records))


def mirror_ring(lasers, targets):
    """The center cell is surrounded by four reflecting blocks."""
    return make_puzzle(
        ["o A o", "A o A", "o A o"], {}, lasers, targets
    )


def exhaustive_solution_exists(puzzle):
    """Tiny-board test oracle using full assignments, never production BFS.

    This intentionally shares no symbolic constraints or variable encoding
    with the planner. Ray behavior is checked by the independent simulator.
    """
    cells = puzzle.placeable_positions
    for labels in itertools.product(("o", "A", "B", "C"), repeat=len(cells)):
        counts = Counter(labels)
        if any(
            counts[kind] != puzzle.block_counts[kind]
            for kind in ("A", "B", "C")
        ):
            continue
        placement = {
            cell: kind for cell, kind in zip(cells, labels) if kind != "o"
        }
        if simulate(puzzle, placement).is_solution:
            return True
    return False


def traced_states(puzzle, result):
    """Recover all processed ray states from sources and segment arrivals."""
    states = {
        (laser.x, laser.y, laser.vx, laser.vy) for laser in puzzle.lasers
    }
    states.update(
        (end[0], end[1], end[0] - start[0], end[1] - start[1])
        for start, end in result.segments
    )
    return frozenset(states)


class ConstraintPlannerTests(unittest.TestCase):
    """A model must encode real reachability, including cycles and splits."""

    def assert_valid_solution(self, puzzle, plan):
        self.assertEqual(plan.status, "solved")
        self.assertTrue(plan.solved)
        self.assertIsNotNone(plan.solution)
        result = plan.solution
        self.assertTrue(result.is_solution)
        placement = result.layout.placements
        counts = Counter(placement.values())
        for kind in ("A", "B", "C"):
            self.assertEqual(counts[kind], puzzle.block_counts[kind])
        for column, row in placement:
            self.assertEqual(puzzle.grid[row][column], "o")
        for row, cells in enumerate(puzzle.grid):
            for column, symbol in enumerate(cells):
                if symbol != "o":
                    self.assertEqual(result.layout.grid[row][column], symbol)
        replay = simulate(puzzle, dict(placement))
        self.assertTrue(replay.is_solution)
        self.assertEqual(replay.hit_targets, puzzle.targets)
        self.assertEqual(set(replay.segments), set(result.segments))
        self.assertEqual(plan.reachable_states, traced_states(puzzle, replay))
        self.assertGreaterEqual(plan.elapsed_seconds, 0)
        self.assertGreaterEqual(plan.build_seconds, 0)
        self.assertGreaterEqual(plan.solver_seconds, 0)
        self.assertGreaterEqual(plan.verification_seconds, 0)
        self.assertGreater(plan.state_count, 0)
        self.assertGreater(plan.constraint_count, 0)
        self.assertTrue(plan.solver_version)

    def test_finds_splitter_placement_without_manual_hints(self):
        puzzle = make_puzzle(
            ["o o o", "o o o", "o o o"], {"C": 1},
            [(1, 2, 1, 1)], [(3, 4), (5, 6), (0, 5)],
        )
        original_grid = puzzle.grid
        original_inventory = dict(puzzle.block_counts)
        self.assert_valid_solution(puzzle, solve_constraints(puzzle))
        self.assertEqual(puzzle.grid, original_grid)
        self.assertEqual(puzzle.block_counts, original_inventory)

    def test_mixed_inventory_preserves_fixed_blocks_and_multiple_sources(self):
        puzzle = make_puzzle(
            ["o o A", "o o B", "C o x"], {"A": 1, "B": 1, "C": 1},
            [(0, 1, 1, 1), (6, 5, 1, -1)], [(1, 2), (6, 5)],
        )
        self.assert_valid_solution(puzzle, solve_constraints(puzzle))

    def test_source_reachable_mirror_cycle_is_allowed(self):
        targets = [(3, 2), (4, 3), (3, 4), (2, 3)]
        puzzle = mirror_ring([(3, 2, 1, -1)], targets)
        plan = solve_constraints(puzzle)
        self.assert_valid_solution(puzzle, plan)
        self.assertGreater(plan.solution.repeated_states, 0)

    def test_disconnected_cycle_cannot_create_light_without_a_source(self):
        puzzle = mirror_ring([(0, 1, -1, -1)], [(3, 2)])
        plan = solve_constraints(puzzle)
        self.assertEqual(plan.status, "no_solution")
        self.assertFalse(plan.solved)
        self.assertIsNone(plan.solution)

    def test_extra_source_does_not_invalidate_a_real_cycle(self):
        puzzle = mirror_ring(
            [(0, 1, -1, -1), (3, 2, 1, -1)],
            [(0, 1), (3, 2), (4, 3), (3, 4), (2, 3)],
        )
        self.assert_valid_solution(puzzle, solve_constraints(puzzle))

    def test_splitter_closure_keeps_both_branches(self):
        puzzle = make_puzzle(
            ["o o o", "o C o", "o o o"], {}, [(2, 3, 1, 1)],
            [(5, 6), (0, 5)],
        )
        plan = solve_constraints(puzzle)
        self.assert_valid_solution(puzzle, plan)
        self.assertIn(((2, 3), (3, 4)), plan.solution.segments)
        self.assertIn(((2, 3), (1, 4)), plan.solution.segments)

    def test_adjacent_splitters_have_separate_transitions(self):
        puzzle = make_puzzle(
            ["o C o", "o C o", "o o o"], {}, [(2, 1, 1, 1)],
            [(6, 5), (5, 0), (0, 3)],
        )
        self.assert_valid_solution(puzzle, solve_constraints(puzzle))

    def test_reflected_segment_cannot_cross_a_solid_rear_cell(self):
        for rear in ("A", "B"):
            with self.subTest(rear=rear):
                puzzle = make_puzzle(
                    [f"{rear} A", "o o"], {}, [(2, 1, 1, 1)], [(0, 3)],
                )
                self.assertEqual(
                    solve_constraints(puzzle).status, "no_solution"
                )

    def test_outward_source_hits_its_own_boundary_point(self):
        puzzle = make_puzzle(["o"], {}, [(0, 1, -1, -1)], [(0, 1)])
        plan = solve_constraints(puzzle)
        self.assert_valid_solution(puzzle, plan)
        self.assertEqual(plan.solution.segments, ())

    def test_target_outside_the_state_domain_is_not_ignored(self):
        puzzle = make_puzzle(["o"], {}, [(0, 1, 1, 1)], [(1, 1)])
        self.assertEqual(solve_constraints(puzzle).status, "no_solution")

    def test_matches_independent_full_assignments_on_tiny_boards(self):
        scenarios = [
            ({"A": 1}, [(3, 4)]),
            ({"A": 1}, [(4, 1)]),
            ({"A": 2}, [(3, 4)]),
            ({"B": 1, "C": 1}, [(1, 2)]),
            ({"A": 1, "C": 1}, [(1, 2), (3, 4)]),
            ({"A": 2, "C": 1}, [(3, 0), (0, 3)]),
        ]
        for inventory, targets in scenarios:
            with self.subTest(inventory=inventory, targets=targets):
                puzzle = make_puzzle(
                    ["o o", "o o"], inventory, [(0, 1, 1, 1)], targets,
                )
                expected = exhaustive_solution_exists(puzzle)
                plan = solve_constraints(puzzle, timeout=None)
                self.assertEqual(plan.solved, expected)
                if expected:
                    self.assert_valid_solution(puzzle, plan)
                else:
                    self.assertEqual(plan.status, "no_solution")

    def test_fixed_random_boards_match_every_simulated_reachable_state(self):
        z3 = _load_z3()
        randomizer = random.Random(20261105)
        for case in range(12):
            rows = [
                " ".join(randomizer.choice("oxABC") for _ in range(2))
                for _ in range(2)
            ]
            lasers = [(0, 1, 1, randomizer.choice((-1, 1)))]
            if case % 3 == 0:
                lasers.append((4, 3, -1, randomizer.choice((-1, 1))))
            puzzle = make_puzzle(rows, {}, lasers, [(0, 1)])
            with self.subTest(rows=rows, lasers=lasers):
                model = build_planning_model(puzzle)
                self.assertEqual(model.solver.check(), z3.sat)
                assignment = model.solver.model()
                actual = frozenset(
                    state for state, reachable in model.reachable.items()
                    if z3.is_true(assignment.eval(
                        reachable, model_completion=True
                    ))
                )
                simulation = simulate(puzzle)
                self.assertEqual(actual, traced_states(puzzle, simulation))
                self.assertEqual(
                    {state[:2] for state in actual}, simulation.visited_points
                )

    def test_cell_domains_do_not_allow_an_extra_block_type(self):
        z3 = _load_z3()
        puzzle = make_puzzle(
            ["o x", "A o"], {"C": 1}, [(0, 1, -1, 1)], [(0, 1)],
        )
        model = build_planning_model(puzzle)
        self.assertEqual(set(model.cell_vars), set(puzzle.placeable_positions))
        model.solver.add(model.cell_vars[(0, 0)] == 4)
        self.assertEqual(model.solver.check(), z3.unsat)

    def test_model_export_can_be_reloaded_by_z3(self):
        z3 = _load_z3()
        puzzle = make_puzzle(
            ["o o", "o o"], {"C": 1}, [(2, 1, -1, 1)], [(1, 2), (3, 2)],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.smt2"
            plan = solve_constraints(puzzle, model_path=path)
            self.assert_valid_solution(puzzle, plan)
            restored = z3.Solver()
            restored.from_file(str(path))
            self.assertEqual(restored.check(), z3.sat)
        text = build_planning_model(puzzle).to_smt2()
        self.assertIn("(assert", text)
        self.assertIn("(check-sat)", text)

    def test_zero_time_budget_is_not_unsatisfiable(self):
        puzzle = make_puzzle(["o"], {}, [(0, 1, 1, 1)], [(1, 2)])
        plan = solve_constraints(puzzle, timeout=0)
        self.assertEqual(plan.status, "timeout")
        self.assertFalse(plan.solved)
        self.assertIsNone(plan.solution)
        self.assertEqual(plan.solver_checks, 0)

    def test_unknown_result_is_not_reported_as_unsatisfiable(self):
        z3 = _load_z3()
        puzzle = make_puzzle(["o"], {}, [(0, 1, 1, 1)], [(1, 2)])
        model = build_planning_model(puzzle)
        with patch("lazor.solver.build_planning_model", return_value=model):
            with patch.object(model.solver, "check", return_value=z3.unknown):
                with patch.object(model.solver, "reason_unknown",
                                  return_value="synthetic incomplete result"):
                    plan = solve_constraints(puzzle)
        self.assertEqual(plan.status, "unknown")
        self.assertFalse(plan.solved)
        self.assertIsNone(plan.solution)

    def test_invalid_timeouts_are_rejected(self):
        puzzle = make_puzzle(["o"], {}, [(0, 1, 1, 1)], [(1, 2)])
        for timeout in (-1, float("inf"), float("nan"), True, "10"):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    solve_constraints(puzzle, timeout=timeout)

    def test_unsupported_source_is_not_reported_as_unsatisfiable(self):
        puzzle = make_puzzle(
            ["o o", "o o"], {"A": 1}, [(0, 0, 1, 1)], [(3, 4)]
        )
        with self.assertRaises(SimulationError):
            solve_constraints(puzzle)


if __name__ == "__main__":
    unittest.main()
