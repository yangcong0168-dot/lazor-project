"""Independent behavioral checks for the batched SMT-LIB representation."""

import itertools
import random
import unittest
from collections import Counter

from lazor.simulation import simulate
from lazor.solver import _load_z3, build_planning_model, solve_constraints
from tests.test_solver import (
    exhaustive_solution_exists, make_puzzle, traced_states,
)


class BatchedEncodingTests(unittest.TestCase):
    def test_seeded_fixed_assignments_preserve_exact_ray_closure(self):
        rng = random.Random(260611)
        z3 = _load_z3()
        points = [(x, y) for x in range(5) for y in range(5) if (x + y) % 2]
        checked = 0
        for case in range(80):
            rows = [" ".join(rng.choice("ooooABCx") for _ in range(2))
                    for _ in range(2)]
            slot_count = sum(row.count("o") for row in rows)
            inventory = Counter(rng.choice("oABC") for _ in range(slot_count))
            lasers = [(*rng.choice(points), rng.choice((-1, 1)),
                       rng.choice((-1, 1))) for _ in range(1 + case % 3)]
            puzzle = make_puzzle(rows, {k: inventory[k] for k in "ABC"},
                                 lasers, [lasers[0][:2]])
            model = build_planning_model(puzzle)
            positions = puzzle.placeable_positions
            for labels in itertools.product("oABC", repeat=slot_count):
                counts = Counter(labels)
                if any(counts[kind] != inventory[kind] for kind in "ABC"):
                    continue
                with self.subTest(case=case, labels=labels):
                    placement = {position: kind for position, kind
                                 in zip(positions, labels) if kind != "o"}
                    model.solver.push()
                    model.solver.add([
                        model.cell_vars[position] == "oABC".index(kind)
                        for position, kind in zip(positions, labels)
                    ])
                    self.assertEqual(model.solver.check(), z3.sat)
                    assignment = model.solver.model()
                    actual = frozenset(
                        state for state in model.states if z3.is_true(
                            assignment.eval(model.reachable[state])
                        )
                    )
                    self.assertEqual(
                        actual, traced_states(puzzle, simulate(puzzle, placement)),
                    )
                    model.solver.pop()
                    checked += 1
        self.assertGreater(checked, 100)

    def test_seeded_free_placement_solutions_match_exhaustive_oracle(self):
        rng = random.Random(680227)
        points = [(x, y) for x in range(7) for y in range(5) if (x + y) % 2]
        for case in range(40):
            rows = [" ".join(rng.choice("ooooABCx") for _ in range(3))
                    for _ in range(2)]
            slots = sum(row.count("o") for row in rows)
            inventory = Counter(rng.choice("oABC") for _ in range(slots))
            lasers = [(*rng.choice(points), rng.choice((-1, 1)),
                       rng.choice((-1, 1))) for _ in range(1 + case % 2)]
            targets = rng.sample(points, 1 + case % 3)
            puzzle = make_puzzle(rows, {k: inventory[k] for k in "ABC"},
                                 lasers, targets)
            with self.subTest(case=case, rows=rows):
                expected = exhaustive_solution_exists(puzzle)
                result = solve_constraints(puzzle, timeout=None)
                self.assertEqual(result.status, "solved" if expected else "no_solution")
                if expected:
                    replay = simulate(puzzle, result.solution.layout.placements)
                    self.assertTrue(replay.is_solution)
                    self.assertEqual(result.reachable_states, traced_states(puzzle, replay))


if __name__ == "__main__":
    unittest.main()
