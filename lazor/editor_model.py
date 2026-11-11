"""Editable puzzle documents, independent of any graphical toolkit."""

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from .bff import parse_bff
from .models import Laser, Puzzle


@dataclass
class EditorDocument:
    """Mutable authoring state that can also represent an unfinished draft.

    Blocks drawn in the grid are fixed. ``block_counts`` is the separate
    inventory that the solver must place. Drafts may lack sources/targets or
    temporarily exceed available cells; exporting validates the whole puzzle.
    """

    grid: list[list[str]]
    block_counts: dict[str, int]
    lasers: list[Laser]
    targets: set[tuple[int, int]]

    @classmethod
    def new(cls, width: int = 4, height: int = 4) -> "EditorDocument":
        """Create an empty board of positive whole-cell dimensions."""
        if any(type(size) is not int or size < 1 for size in (width, height)):
            raise ValueError("Board dimensions must be positive integers.")
        return cls(
            grid=[["o"] * width for _ in range(height)],
            block_counts={"A": 0, "B": 0, "C": 0},
            lasers=[],
            targets=set(),
        )

    @classmethod
    def from_puzzle(cls, puzzle: Puzzle) -> "EditorDocument":
        """Copy a parsed puzzle so editing does not change the original."""
        return cls(
            grid=[list(row) for row in puzzle.grid],
            block_counts=dict(puzzle.block_counts),
            lasers=list(puzzle.lasers),
            targets=set(puzzle.targets),
        )

    @classmethod
    def from_text(cls, text: str) -> "EditorDocument":
        """Read existing BFF text using the shared parser."""
        return cls.from_puzzle(parse_bff(text, source="<editor import>"))

    def clone(self) -> "EditorDocument":
        """Return an independent snapshot for undo, redo, and worker jobs."""
        return deepcopy(self)

    @property
    def width(self) -> int:
        """Return width in whole-block cells."""
        return len(self.grid[0])

    @property
    def height(self) -> int:
        """Return height in whole-block cells."""
        return len(self.grid)

    def set_cell(self, column: int, row: int, symbol: str) -> None:
        """Paint a fixed block, allowed empty cell, or forbidden empty cell."""
        if any(type(value) is not int for value in (column, row)):
            raise ValueError("Cell coordinates must be integers.")
        if not (0 <= column < self.width and 0 <= row < self.height):
            raise ValueError("Cell is outside the board.")
        if symbol not in ("o", "x", "A", "B", "C"):
            raise ValueError("Cell symbol must be o, x, A, B, or C.")
        self.grid[row][column] = symbol

    def set_inventory(self, kind: str, count: int) -> None:
        """Set movable inventory, allowing an incomplete draft arrangement."""
        if kind not in ("A", "B", "C"):
            raise ValueError("Inventory kind must be A, B, or C.")
        if type(count) is not int or count < 0:
            raise ValueError("Inventory must be a nonnegative integer.")
        self.block_counts[kind] = count

    def _check_point(self, x: int, y: int) -> None:
        """Validate an integer half-block position, including the boundary."""
        if any(type(value) is not int for value in (x, y)):
            raise ValueError("Point coordinates must be integers.")
        if not (0 <= x <= 2 * self.width and 0 <= y <= 2 * self.height):
            raise ValueError("Point is outside the board.")

    def add_laser(self, x: int, y: int, vx: int, vy: int) -> None:
        """Add a diagonal face-midpoint source without exact duplicates."""
        self._check_point(x, y)
        if (x + y) % 2 != 1:
            raise ValueError(
                "Lasers must start at a face midpoint: one odd coordinate "
                "and one even coordinate."
            )
        if any(type(value) is not int or value not in (-1, 1)
               for value in (vx, vy)):
            raise ValueError("Laser direction components must be -1 or 1.")
        laser = Laser(x, y, vx, vy)
        if laser not in self.lasers:
            self.lasers.append(laser)

    def toggle_target(self, x: int, y: int) -> None:
        """Toggle a target at this half-grid point."""
        self._check_point(x, y)
        point = (x, y)
        if point in self.targets:
            self.targets.remove(point)
        else:
            self.targets.add(point)

    def remove_point(self, x: int, y: int) -> None:
        """Remove all sources and the target at a half-grid point."""
        self._check_point(x, y)
        self.lasers[:] = [
            laser for laser in self.lasers if (laser.x, laser.y) != (x, y)
        ]
        self.targets.discard((x, y))

    def to_bff(self, validate: bool = False) -> str:
        """Serialize a draft, optionally requiring an executable test case.

        Unlike a solution snapshot, this preserves the original movable
        inventory. Draft previews can be incomplete, but saved test files
        must pass ``to_puzzle`` validation first.
        """
        lines = [
            "# Lazor test case created by the graphical editor.",
            "# A/B/C inside GRID are fixed blocks.",
            "# A/B/C counts below GRID are movable inventory for the solver.",
            "# Laser/target coordinates use half-block units; +y is down.",
            "GRID START",
            *(" ".join(row) for row in self.grid),
            "GRID STOP",
            "",
        ]
        lines.extend(
            f"{kind} {self.block_counts.get(kind, 0)}"
            for kind in ("A", "B", "C")
        )
        lines.append("")
        lines.extend(
            f"L {laser.x} {laser.y} {laser.vx} {laser.vy}"
            for laser in self.lasers
        )
        lines.append("")
        lines.extend(f"P {x} {y}" for x, y in sorted(self.targets))
        text = "\n".join(lines) + "\n"
        if validate:
            self._parse_executable(text)
        return text

    @staticmethod
    def _parse_executable(text: str) -> Puzzle:
        """Apply parser checks plus the simulator's supported source rule."""
        puzzle = parse_bff(text, source="<editor document>")
        for laser in puzzle.lasers:
            if (laser.x + laser.y) % 2 != 1:
                raise ValueError(
                    f"Laser at ({laser.x}, {laser.y}) must be at a face "
                    "midpoint: one coordinate odd, the other even."
                )
        return puzzle

    def to_puzzle(self) -> Puzzle:
        """Validate this draft and return a runnable puzzle without edits."""
        return self._parse_executable(self.to_bff())


def save_document(document: EditorDocument, path: str | Path) -> Path:
    """Validate and save a test case as UTF-8 BFF, not a solution snapshot.

    The caller handles overwrite confirmation. Validation happens before
    writing, so an unfinished draft cannot replace an existing valid file.
    """
    destination = Path(path)
    if destination.suffix.lower() != ".bff":
        raise ValueError("Test cases must use the .bff extension, not .bif.")
    text = document.to_bff(validate=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")
    return destination
