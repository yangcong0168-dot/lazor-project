"""Regressions for graph pruning, cardinality domains, and build budgets."""

import itertools
import tempfile
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from lazor.models import Laser
from lazor.simulation import SimulationError, simulate
from lazor.solver import _load_z3, build_planning_model, solve_constraints
from tests.test_solver import make_puzzle, traced_states


class PlanningOptimizationTests(unittest.TestCase):
    def test_impossible_parity_skips_optical_encoding_and_exports_unsat(self):
        puzzle = make_puzzle(
            ["o " * 43 + "o"] * 44, {"A": 10, "C": 10},
            [(0, 1, 1, 1)], [(1, 1)],
        )
        with patch("lazor.solver._forward_cell", side_effect=AssertionError(
            "Impossible parity must not traverse the optimistic ray graph"
        )):
            model = build_planning_model(puzzle)
        self.assertEqual(set(model.cell_vars), set(puzzle.placeable_positions))
        self.assertEqual(model.constraint_count, 1)
        self.assertFalse(model.edges)
        z3 = _load_z3()
        self.assertEqual(model.solver.check(), z3.unsat)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "impossible.smt2"
            result = solve_constraints(puzzle, model_path=path)
            self.assertEqual(result.status, "no_solution")
            self.assertEqual(result.solver_checks, 1)
            reloaded = z3.Solver()
            reloaded.from_file(str(path))
            self.assertEqual(reloaded.check(), z3.unsat)

    def test_disconnected_target_skips_symbolic_graph_constraints(self):
        puzzle = make_puzzle(
            ["o o o o"] * 4, {"A": 4, "C": 4},
            [(0, 1, -1, -1)], [(8, 7)],
        )
        model = build_planning_model(puzzle)
        self.assertEqual(model.constraint_count, 1)
        self.assertFalse(model.edges)
        self.assertEqual(solve_constraints(puzzle).status, "no_solution")

    def test_parity_shortcut_does_not_hide_invalid_input(self):
        puzzle = make_puzzle(
            ["o o", "o o"], {}, [(0, 1, 1, 1)], [(1, 1)],
        )
        with self.assertRaises(SimulationError):
            solve_constraints(replace(puzzle, lasers=(Laser(1, 1, 1, 1),)))
        for inventory in ({"A": -1}, {"A": 5}, {"C": True}):
            with self.subTest(inventory=inventory), self.assertRaises(ValueError):
                solve_constraints(replace(puzzle, block_counts=inventory))

    def test_inventory_zero_types_do_not_expand_the_source_graph(self):
        puzzle = make_puzzle(
            ["o o o o"] * 4, {}, [(0, 1, 1, 1)], [(0, 1)],
        )
        model = build_planning_model(puzzle)
        replay = simulate(puzzle)
        self.assertEqual(set(model.states), traced_states(puzzle, replay))
        self.assertEqual(len(model.states), 8)
        self.assertEqual(len(model.edges), 7)
        self.assertEqual(set(model.cell_vars), set(puzzle.placeable_positions))

    def test_disconnected_states_are_removed_before_symbolic_allocation(self):
        puzzle = make_puzzle(
            ["o o o o"] * 4, {"A": 4, "C": 4},
            [(0, 1, -1, -1)], [(0, 1)],
        )
        model = build_planning_model(puzzle)
        self.assertEqual(model.states, ((0, 1, -1, -1),))
        self.assertFalse(model.edges)
        result = solve_constraints(puzzle)
        self.assertTrue(result.solved)
        self.assertTrue(result.solution.is_solution)

    def test_pruned_graph_preserves_every_legal_tiny_board_assignment(self):
        z3 = _load_z3()
        inventories = (
            {"A": 1, "C": 1}, {"A": 2, "C": 2}, {"B": 1},
            {"C": 2}, {}, {"A": 1, "B": 1, "C": 1},
        )
        for inventory in inventories:
            puzzle = make_puzzle(
                ["o o", "o o"], inventory,
                [(0, 1, 1, 1), (4, 3, -1, -1)], [(0, 1)],
            )
            model = build_planning_model(puzzle)
            positions = puzzle.placeable_positions
            for labels in itertools.product("oABC", repeat=4):
                actual_counts = Counter(labels)
                if any(actual_counts[kind] != inventory.get(kind, 0)
                       for kind in "ABC"):
                    continue
                with self.subTest(inventory=inventory, labels=labels):
                    placement = {
                        position: kind for position, kind in zip(positions, labels)
                        if kind != "o"
                    }
                    model.solver.push()
                    model.solver.add([
                        model.cell_vars[position] == "oABC".index(kind)
                        for position, kind in zip(positions, labels)
                    ])
                    self.assertEqual(model.solver.check(), z3.sat)
                    assignment = model.solver.model()
                    reached = frozenset(
                        state for state in model.states
                        if z3.is_true(assignment.eval(model.reachable[state]))
                    )
                    self.assertEqual(
                        reached, traced_states(puzzle, simulate(puzzle, placement)),
                    )
                    model.solver.pop()

    def test_build_budget_expiry_returns_timeout_without_solver_check(self):
        puzzle = make_puzzle(
            ["o o", "o o"], {"A": 1}, [(0, 1, 1, 1)], [(1, 2)],
        )
        with patch("lazor.solver.perf_counter", side_effect=itertools.count()):
            result = solve_constraints(puzzle, timeout=2)
        self.assertEqual(result.status, "timeout")
        self.assertEqual(result.solver_checks, 0)
        self.assertIn("modeling", result.reason)
        self.assertIsNone(result.solution)


if __name__ == "__main__":
    unittest.main()
