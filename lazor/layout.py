"""Apply a supplied block placement without searching for one."""

from collections.abc import Mapping
from dataclasses import dataclass

from .models import Block, Puzzle

Position = tuple[int, int]


@dataclass(frozen=True)
class BoardLayout:
    """An initial puzzle plus a validated, possibly partial placement.

    Cell positions use (column, row), not half-block laser coordinates.
    Dictionaries are owned snapshots and should be treated as read-only.
    """

    puzzle: Puzzle
    grid: tuple[tuple[str, ...], ...]
    placements: dict[Position, str]
    remaining_blocks: dict[str, int]
    blocks: dict[Position, Block]

    @property
    def complete(self) -> bool:
        """Whether all the supplied movable inventory has been placed."""
        return not any(self.remaining_blocks.values())


def build_layout(
    puzzle: Puzzle,
    placements: Mapping[Position, str] | None = None,
) -> BoardLayout:
    """Validate manual placements and return a new board without mutation.

    Blocks may only replace 'o' cells and may not exceed the inventory.
    Partial placements are useful while debugging; their remaining inventory
    is reported explicitly and they cannot qualify as complete solutions.
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
            raise ValueError(
                f"Cannot place at {position}: only 'o' cells are available."
            )
        if remaining.get(kind, 0) <= 0:
            raise ValueError(f"Not enough movable {kind} blocks.")
        remaining[kind] -= 1
        grid[row][column] = kind
        blocks[position] = Block(kind)

    return BoardLayout(
        puzzle=puzzle,
        grid=tuple(tuple(row) for row in grid),
        placements=placed,
        remaining_blocks=remaining,
        blocks=blocks,
    )

