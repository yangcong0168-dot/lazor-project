"""Data models shared by the parser, simulator, and constraint planner.

Grid positions use (column, row). Laser and target positions use half-block
coordinates: the origin is at the top left, x points right, and y points down.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Block:
    """An A (reflect), B (opaque), or C (split) block.

    ``fixed`` describes whether the block is part of the initial board.
    It does not change the block's optical behavior.
    """

    kind: str
    fixed: bool = False

    def __post_init__(self) -> None:
        """Reject unsupported block symbols."""
        if self.kind not in ("A", "B", "C"):
            raise ValueError("Block kind must be A, B, or C.")

    def interact(
        self, direction: tuple[int, int], face: str
    ) -> tuple[tuple[int, int], ...]:
        """Return outgoing directions for one incoming diagonal ray.

        ``face`` is 'vertical' or 'horizontal', describing the surface hit.
        A reflects, B absorbs, and C transmits and reflects. This method only
        computes directions; the simulator must locate collisions and
        advance each outgoing ray to its next position.
        """
        vx, vy = direction
        if vx not in (-1, 1) or vy not in (-1, 1):
            raise ValueError("Each direction component must be -1 or 1.")
        if face not in ("vertical", "horizontal"):
            raise ValueError("Face must be 'vertical' or 'horizontal'.")

        reflected = (-vx, vy) if face == "vertical" else (vx, -vy)
        if self.kind == "A":
            return (reflected,)
        if self.kind == "B":
            return ()
        return ((vx, vy), reflected)


@dataclass(frozen=True)
class Laser:
    """A laser's half-block starting coordinates and direction."""

    x: int
    y: int
    vx: int
    vy: int


@dataclass(frozen=True)
class Puzzle:
    """A parsed initial board, inventory, lasers, and target coordinates.

    ``block_counts`` counts movable blocks only. A/B/C symbols already in
    ``grid`` represent fixed blocks and are excluded from that inventory.
    """

    grid: tuple[tuple[str, ...], ...]
    block_counts: dict[str, int]
    lasers: tuple[Laser, ...]
    targets: frozenset[tuple[int, int]]

    @property
    def width(self) -> int:
        """Return the board width in whole blocks."""
        return len(self.grid[0])

    @property
    def height(self) -> int:
        """Return the board height in whole blocks."""
        return len(self.grid)

    @property
    def placeable_positions(self) -> tuple[tuple[int, int], ...]:
        """Return available cells as (column, row) positions."""
        return tuple(
            (column, row)
            for row, cells in enumerate(self.grid)
            for column, cell in enumerate(cells)
            if cell == "o"
        )

    @property
    def fixed_blocks(self) -> dict[tuple[int, int], Block]:
        """Return initial fixed blocks keyed by (column, row)."""
        return {
            (column, row): Block(cell, fixed=True)
            for row, cells in enumerate(self.grid)
            for column, cell in enumerate(cells)
            if cell in ("A", "B", "C")
        }

