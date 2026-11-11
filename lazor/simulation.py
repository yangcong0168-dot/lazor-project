"""Trace diagonal rays on a supplied Lazor board; no placement search.

A state sits on a cell-face midpoint, just before entering the cell ahead.
One half-block step crosses that cell to another face midpoint. Consequently
a transmitted ray sees the next cell on its next step, not the C it just left.
"""

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from time import perf_counter

from .layout import BoardLayout, Position, build_layout
from .models import Puzzle

State = tuple[int, int, int, int]
Segment = tuple[Position, Position]


class SimulationError(ValueError):
    """An input ray cannot be simulated using the face-midpoint geometry."""


@dataclass(frozen=True)
class SimulationResult:
    """Complete ray coverage for one layout, including branches and loops.

    Segments are directed half-block steps, deduplicated in discovery order.
    Repeated states can mean a loop OR merging branches/sources. They are
    safely skipped because their entire future path has already been queued.
    """

    layout: BoardLayout
    segments: tuple[Segment, ...]
    visited_points: frozenset[Position]
    hit_targets: frozenset[Position]
    missed_targets: frozenset[Position]
    states_processed: int
    repeated_states: int
    elapsed_seconds: float

    @property
    def all_targets_hit(self) -> bool:
        """Whether the union of all light paths covers every target."""
        return not self.missed_targets

    @property
    def remaining_blocks(self) -> dict[str, int]:
        """Return a copy of inventory still available after placement."""
        return dict(self.layout.remaining_blocks)

    @property
    def is_solution(self) -> bool:
        """Whether all targets are hit and all inventory is legally placed."""
        return self.all_targets_hit and self.layout.complete


def _cell_ahead(state: State) -> tuple[Position, str]:
    """Identify the cell the ray is entering and that cell's entry face."""
    x, y, vx, vy = state
    if x % 2 == 0:
        return (x // 2 - (vx < 0), y // 2), "vertical"
    return (x // 2, y // 2 - (vy < 0)), "horizontal"


def simulate(
    puzzle: Puzzle,
    placements: Mapping[Position, str] | None = None,
) -> SimulationResult:
    """Trace all rays for fixed blocks plus a supplied movable placement.

    Sources must start on face midpoints (x+y odd) with diagonal directions
    (+/-1, +/-1). Corners have no unique surface normal and are rejected.
    All target coordinates accepted by the parser remain valid: unreachable
    points simply remain missed. Sources and boundary endpoints count as hits.

    A ray stops when it leaves the board, is absorbed, or repeats a complete
    (x, y, vx, vy) state. No arbitrary step limit is used. All in-board paths
    are collected even after every target has already been hit.
    """
    started = perf_counter()
    layout = build_layout(puzzle, placements)
    max_x, max_y = 2 * puzzle.width, 2 * puzzle.height
    pending: deque[State] = deque()
    for laser in puzzle.lasers:
        values = (laser.x, laser.y, laser.vx, laser.vy)
        if any(type(value) is not int for value in values):
            raise SimulationError("Laser coordinates/directions must be ints.")
        if laser.vx not in (-1, 1) or laser.vy not in (-1, 1):
            raise SimulationError("Laser direction components must be +/-1.")
        if not (0 <= laser.x <= max_x and 0 <= laser.y <= max_y):
            raise SimulationError("Laser origin is outside the board.")
        if (laser.x + laser.y) % 2 != 1:
            raise SimulationError(
                f"Laser at ({laser.x}, {laser.y}) must start on a face "
                "midpoint: one coordinate even, the other odd."
            )
        pending.append(values)

    seen: set[State] = set()
    points: set[Position] = set()
    segments: dict[Segment, None] = {}
    repeated = 0
    grid = layout.grid
    width, height = puzzle.width, puzzle.height

    while pending:
        state = pending.popleft()
        if state in seen:
            repeated += 1
            continue
        seen.add(state)
        x, y, vx, vy = state
        points.add((x, y))
        # Directions and block kinds were validated at the API boundary.
        # The hot loop uses the grid directly instead of allocating cell
        # tuples and repeatedly validating Block.interact arguments.
        vertical = not (x & 1)
        column, row = x // 2, y // 2
        if vertical:
            column -= vx < 0
        else:
            row -= vy < 0
        kind = (grid[row][column]
                if 0 <= column < width and 0 <= row < height else "o")
        if kind == "B":
            continue
        reflected = (-vx, vy) if vertical else (vx, -vy)
        outgoing = ((reflected,) if kind == "A" else
                    ((vx, vy), reflected) if kind == "C" else ((vx, vy),))

        for next_vx, next_vy in outgoing:
            # A source can sit between two occupied cells. Its reflected
            # branch must not travel through an A/B behind the source. B
            # absorbs; touching mirrors bounce at zero distance and add no
            # new spatial path. C is transmissive and may be crossed here.
            next_x, next_y = x + next_vx, y + next_vy
            if not (0 <= next_x <= max_x and 0 <= next_y <= max_y):
                continue
            # Transmission already established that the front is empty/C.
            # Only reflection needs the extra rear-cell collision check.
            if (next_vx, next_vy) != (vx, vy):
                rear_column, rear_row = x // 2, y // 2
                if vertical:
                    rear_column -= next_vx < 0
                else:
                    rear_row -= next_vy < 0
                if (0 <= rear_column < width and 0 <= rear_row < height
                        and grid[rear_row][rear_column] in ("A", "B")):
                    continue
            segment = ((x, y), (next_x, next_y))
            segments[segment] = None
            pending.append((next_x, next_y, next_vx, next_vy))

    visited = frozenset(points)
    return SimulationResult(
        layout=layout,
        segments=tuple(segments),
        visited_points=visited,
        hit_targets=puzzle.targets & visited,
        missed_targets=puzzle.targets - visited,
        states_processed=len(seen),
        repeated_states=repeated,
        elapsed_seconds=perf_counter() - started,
    )
