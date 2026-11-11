"""Standalone block behavior and laser simulation for the Lazor project.

Python 3.10+; standard library only. File reading, placement search, and
result-file output belong to the calling program. Grid positions are
(column, row); laser and target positions use half-block coordinates.
"""

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from time import perf_counter


@dataclass(frozen=True)
class block:
    """A reflects, B absorbs, and C transmits and reflects a ray."""

    kind: str
    fixed: bool = False

    def __post_init__(self) -> None:
        """Check the block type when an object is created."""
        if self.kind not in ("A", "B", "C"):
            raise ValueError(f"Invalid block symbol: {self.kind}")

    def interact(
        self, direction: tuple[int, int], face: str
    ) -> tuple[tuple[int, int], ...]:
        """Return all outgoing directions for one collision."""
        vx, vy = direction
        if vx not in (-1, 1) or vy not in (-1, 1):
            raise ValueError(f"Invalid direction: {direction}")
        if face not in ("vertical", "horizontal"):
            raise ValueError(f"Invalid face: {face}")

        if face == "vertical":
            reflected = (-vx, vy)
        else:
            reflected = (vx, -vy)

        if self.kind == "A":
            return (reflected,)
        if self.kind == "B":
            return ()
        return ((vx, vy), reflected)


@dataclass
class lazor:
    """Store a light source's starting coordinates and direction."""

    x: int
    y: int
    vx: int
    vy: int


# 类型别名必须先定义，后面的标注才能使用。
Position = tuple[int, int]
State = tuple[int, int, int, int]
Segment = tuple[Position, Position]


@dataclass(frozen=True)
class Puzzle:
    """Store a parsed rectangular board, movable inventory, and sources.

    The reader supplies a nonempty grid containing o/x/A/B/C and a
    nonnegative integer inventory. A/B/C already in grid are fixed blocks
    and do not consume block_counts. Targets use half-block coordinates.
    """

    grid: tuple[tuple[str, ...], ...]
    block_counts: dict[str, int]
    lasers: tuple[lazor, ...]
    targets: frozenset[Position]

    @property
    def width(self) -> int:
        """Return the number of columns."""
        return len(self.grid[0])

    @property
    def height(self) -> int:
        """Return the number of rows."""
        return len(self.grid)

    @property
    def placeable_positions(self) -> tuple[Position, ...]:
        """Return the original o cells as (column, row) positions."""
        positions = []
        for row, cells in enumerate(self.grid):
            for column, cell in enumerate(cells):
                if cell == "o":
                    positions.append((column, row))
        return tuple(positions)

    @property
    def fixed_blocks(self) -> dict[Position, block]:
        """Build a fresh dictionary of the original fixed blocks."""
        blocks = {}
        for row, cells in enumerate(self.grid):
            for column, cell in enumerate(cells):
                if cell in ("A", "B", "C"):
                    blocks[(column, row)] = block(cell, fixed=True)
        return blocks


@dataclass(frozen=True)
class BoardLayout:
    """Store one validated placement; treat its dictionaries as read-only."""

    puzzle: Puzzle
    grid: tuple[tuple[str, ...], ...]
    placements: dict[Position, str]
    remaining_blocks: dict[str, int]
    blocks: dict[Position, block]

    @property
    def complete(self) -> bool:
        """Whether every movable block has been placed."""
        return not any(self.remaining_blocks.values())


def build_layout(
    puzzle: Puzzle,
    placements: Mapping[Position, str] | None = None,
) -> BoardLayout:
    """Check and apply one supplied placement without changing the puzzle.

    This function does not search. The caller chooses a placement and may
    supply only part of the inventory while inspecting a layout.
    """
    placed = dict(placements) if placements is not None else {}
    remaining = dict(puzzle.block_counts)
    grid = [list(row) for row in puzzle.grid]
    blocks = puzzle.fixed_blocks

    for position, kind in placed.items():
        if (
            not isinstance(position, tuple)
            or len(position) != 2
            or any(type(value) is not int for value in position)
        ):
            raise ValueError("Placement keys must be integer (column, row).")

        column, row = position
        if not (0 <= column < puzzle.width and 0 <= row < puzzle.height):
            raise ValueError(f"Placement {position} is outside the grid.")
        if kind not in ("A", "B", "C"):
            raise ValueError(f"Unknown block kind {kind!r} at {position}.")
        if puzzle.grid[row][column] != "o":
            raise ValueError(f"Cannot place at {position}: only o is allowed.")
        if remaining.get(kind, 0) <= 0:
            raise ValueError(f"Not enough movable {kind} blocks.")

        remaining[kind] -= 1
        grid[row][column] = kind
        blocks[position] = block(kind)

    return BoardLayout(
        puzzle=puzzle,
        grid=tuple(tuple(row) for row in grid),
        placements=placed,
        remaining_blocks=remaining,
        blocks=blocks,
    )


class SimulationError(ValueError):
    """A source cannot be simulated using diagonal face-midpoint rays."""


@dataclass(frozen=True)
class SimulationResult:
    """Store light paths and target coverage for one supplied layout."""

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
        """Whether all targets were reached by at least one ray."""
        return not self.missed_targets

    @property
    def remaining_blocks(self) -> dict[str, int]:
        """Return a copy of the remaining inventory."""
        return dict(self.layout.remaining_blocks)

    @property
    def is_solution(self) -> bool:
        """Require both complete placement and complete target coverage."""
        return self.all_targets_hit and self.layout.complete


def _cell_ahead(state: State) -> tuple[Position, str]:
    """Find the next cell and its entry face for a valid ray state."""
    x, y, vx, vy = state
    column = x // 2
    row = y // 2

    if x % 2 == 0:
        # 位于竖直边中点；向左时进入左边的格子。
        if vx < 0:
            column -= 1
        return (column, row), "vertical"

    # 位于水平边中点；向上时进入上边的格子。
    if vy < 0:
        row -= 1
    return (column, row), "horizontal"


def simulate(
    puzzle: Puzzle,
    placements: Mapping[Position, str] | None = None,
) -> SimulationResult:
    """Trace all sources and branches on one supplied block placement.

    A state is a cell-face midpoint just before entering the cell ahead.
    One diagonal half-block step reaches another face midpoint. Sources
    and boundary endpoints count as visited points. Repeated complete
    states are skipped, so mirror loops terminate without a step limit.
    """
    started = perf_counter()
    layout = build_layout(puzzle, placements)
    max_x = 2 * puzzle.width
    max_y = 2 * puzzle.height
    pending: deque[State] = deque()

    # 检查光源，然后将每个光源的状态加入队列。
    for laser in puzzle.lasers:
        values = (laser.x, laser.y, laser.vx, laser.vy)
        if any(type(value) is not int for value in values):
            raise SimulationError("Laser coordinates/directions must be ints.")
        if laser.vx not in (-1, 1) or laser.vy not in (-1, 1):
            raise SimulationError("Laser direction components must be +/-1.")
        if not (0 <= laser.x <= max_x and 0 <= laser.y <= max_y):
            raise SimulationError("Laser origin is outside the board.")
        if (laser.x + laser.y) % 2 != 1:
            raise SimulationError("Laser origin must be a face midpoint.")
        pending.append(values)

    seen: set[State] = set()
    points: set[Position] = set()
    segments: dict[Segment, None] = {}
    repeated = 0

    while pending:
        state = pending.popleft()

        # 位置和方向都相同才是重复状态；也可能是分支汇合。
        if state in seen:
            repeated += 1
            continue
        seen.add(state)

        x, y, vx, vy = state
        points.add((x, y))
        cell, face = _cell_ahead(state)
        current_block = layout.blocks.get(cell)

        if current_block is None:
            outgoing = ((vx, vy),)
        else:
            outgoing = current_block.interact((vx, vy), face)

        for next_vx, next_vy in outgoing:
            # 光源可能夹在相邻方块之间。出射光不能穿过 A/B。
            travel_cell, _ = _cell_ahead((x, y, next_vx, next_vy))
            travel_block = layout.blocks.get(travel_cell)
            if travel_block and travel_block.kind in ("A", "B"):
                continue

            next_x = x + next_vx
            next_y = y + next_vy
            if not (0 <= next_x <= max_x and 0 <= next_y <= max_y):
                continue

            # 字典键用来去重，同时保留线段首次出现的顺序。
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


if __name__ == "__main__":
    # 仅用于演示调用；读取、搜索和文件输出由其他模块负责。
    demo_puzzle = Puzzle(
        grid=(
            ("o", "o", "o"),
            ("o", "o", "o"),
            ("o", "o", "o"),
        ),
        block_counts={"A": 1, "B": 0, "C": 0},
        lasers=(lazor(2, 3, 1, 1),),
        targets=frozenset({(0, 5)}),
    )
    demo_result = simulate(demo_puzzle, {(1, 1): "A"})
    print("Is solution:", demo_result.is_solution)
    print("Segments:", demo_result.segments)
    print("Missed targets:", sorted(demo_result.missed_targets))
