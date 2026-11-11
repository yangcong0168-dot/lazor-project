"""Preserve tracing order and loop counters against the object-based oracle."""

import random
import unittest
from collections import deque

from lazor.layout import build_layout
from lazor.models import Laser, Puzzle
from lazor.simulation import _cell_ahead, simulate


def reference_trace(board, placement):
    layout = build_layout(board, placement)
    pending = deque((ray.x, ray.y, ray.vx, ray.vy) for ray in board.lasers)
    seen, points, segments = set(), set(), {}
    repeated = 0
    while pending:
        state = pending.popleft()
        if state in seen:
            repeated += 1
            continue
        seen.add(state)
        x, y, vx, vy = state
        points.add((x, y))
        cell, face = _cell_ahead(state)
        block = layout.blocks.get(cell)
        outgoing = block.interact((vx, vy), face) if block else ((vx, vy),)
        for dx, dy in outgoing:
            travel_cell, _ = _cell_ahead((x, y, dx, dy))
            travel_block = layout.blocks.get(travel_cell)
            if travel_block and travel_block.kind in ("A", "B"):
                continue
            end = (x + dx, y + dy)
            if not (0 <= end[0] <= 2 * board.width and 0 <= end[1] <= 2 * board.height):
                continue
            segments[((x, y), end)] = None
            pending.append((*end, dx, dy))
    return tuple(segments), frozenset(points), len(seen), repeated


class OptimizedSimulationTests(unittest.TestCase):
    def test_seeded_placements_match_reference_including_discovery_order(self):
        rng = random.Random(118009)
        for case in range(150):
            width, height = rng.randint(1, 6), rng.randint(1, 6)
            grid = tuple(tuple(rng.choice("ooooxABC") for _ in range(width))
                         for _ in range(height))
            positions = [(x, y) for y, row in enumerate(grid)
                         for x, kind in enumerate(row) if kind == "o"]
            placement = {point: rng.choice("ABC") for point in positions
                         if rng.random() < .45}
            counts = {kind: list(placement.values()).count(kind) for kind in "ABC"}
            faces = [(x, y) for x in range(2 * width + 1)
                     for y in range(2 * height + 1) if (x + y) % 2]
            lasers = tuple(Laser(*rng.choice(faces), rng.choice((-1, 1)), rng.choice((-1, 1)))
                           for _ in range(rng.randint(1, 6)))
            board = Puzzle(grid, counts, lasers, frozenset(rng.sample(faces, min(3, len(faces)))))
            expected = reference_trace(board, placement)
            actual = simulate(board, placement)
            with self.subTest(case=case, board=board):
                self.assertEqual((actual.segments, actual.visited_points,
                                  actual.states_processed, actual.repeated_states), expected)


if __name__ == "__main__":
    unittest.main()
