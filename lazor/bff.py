"""Parse the course's text-based .bff puzzle format."""

from pathlib import Path

from .models import Laser, Puzzle


class BFFParseError(ValueError):
    """A malformed .bff file, with its source and relevant line number."""


def load_bff(path: str | Path) -> Puzzle:
    """Read a UTF-8 .bff file and return its initial puzzle.

    UTF-8 files with a BOM are accepted. File access errors propagate normally;
    malformed content raises BFFParseError.
    """
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise BFFParseError(f"{source}: expected a UTF-8 text file.") from exc
    return parse_bff(text, source=str(source))


def parse_bff(text: str, *, source: str = "<string>") -> Puzzle:
    """Parse .bff text, accepting blank lines and # comments.

    Exactly one rectangular GRID START / GRID STOP section is required.
    Outside it, A/B/C declare movable inventory, L declares a laser, and P
    declares a target. Inventory defaults to zero for omitted block kinds.
    At least one laser and one target are required.
    """
    grid: list[tuple[str, ...]] = []
    counts = {"A": 0, "B": 0, "C": 0}
    declared_counts: set[str] = set()
    laser_records: list[tuple[int, Laser]] = []
    target_records: list[tuple[int, tuple[int, int]]] = []
    grid_started = False
    grid_finished = False

    def error(message: str, line: int | None = None) -> BFFParseError:
        location = source if line is None else f"{source}:{line}"
        return BFFParseError(f"{location}: {message}")

    def integers(tokens: list[str], line: int) -> tuple[int, ...]:
        try:
            return tuple(int(token) for token in tokens)
        except ValueError as exc:
            raise error("Expected integer values.", line) from exc

    lines = text.removeprefix("\ufeff").splitlines()
    for line_number, raw_line in enumerate(lines, 1):
        # Remove comments before splitting so both whole-line and inline
        # comments work, regardless of spaces or tabs.
        tokens = raw_line.split("#", 1)[0].split()
        if not tokens:
            continue

        if tokens == ["GRID", "START"]:
            if grid_started:
                raise error("Only one grid section is allowed.", line_number)
            grid_started = True
            continue
        if tokens == ["GRID", "STOP"]:
            if not grid_started or grid_finished:
                raise error("GRID STOP must close an open grid.", line_number)
            if not grid:
                raise error("Grid must not be empty.", line_number)
            grid_finished = True
            continue

        if grid_started and not grid_finished:
            if any(token not in ("x", "o", "A", "B", "C") for token in tokens):
                raise error("Grid cells must be x/o/A/B/C.", line_number)
            if grid and len(tokens) != len(grid[0]):
                raise error("Grid rows must have equal widths.", line_number)
            grid.append(tuple(tokens))
            continue

        tag = tokens[0]
        if tag in counts:
            if len(tokens) != 2:
                raise error(f"{tag} requires one block count.", line_number)
            if tag in declared_counts:
                raise error(f"Duplicate inventory for {tag}.", line_number)
            (count,) = integers(tokens[1:], line_number)
            if count < 0:
                raise error("Block counts must be nonnegative.", line_number)
            counts[tag] = count
            declared_counts.add(tag)
        elif tag == "L":
            if len(tokens) != 5:
                raise error("L requires x y vx vy.", line_number)
            x, y, vx, vy = integers(tokens[1:], line_number)
            if vx not in (-1, 1) or vy not in (-1, 1):
                raise error("Direction components must be +/-1.", line_number)
            laser_records.append((line_number, Laser(x, y, vx, vy)))
        elif tag == "P":
            if len(tokens) != 3:
                raise error("P requires x y.", line_number)
            x, y = integers(tokens[1:], line_number)
            target_records.append((line_number, (x, y)))
        else:
            raise error(f"Unknown record {tag!r}.", line_number)

    if not grid_started:
        raise error("Missing GRID START.")
    if not grid_finished:
        raise error("Missing GRID STOP.")
    if not laser_records:
        raise error("At least one laser (L) is required.")
    if not target_records:
        raise error("At least one target (P) is required.")

    max_x, max_y = 2 * len(grid[0]), 2 * len(grid)

    def check_position(x: int, y: int, line: int) -> None:
        if not (0 <= x <= max_x and 0 <= y <= max_y):
            raise error(
                f"Position ({x}, {y}) is outside the board; "
                f"expected 0 <= x <= {max_x}, 0 <= y <= {max_y}.",
                line,
            )

    for line_number, laser in laser_records:
        check_position(laser.x, laser.y, line_number)
    for line_number, (x, y) in target_records:
        check_position(x, y, line_number)

    available_cells = sum(row.count("o") for row in grid)
    if sum(counts.values()) > available_cells:
        raise error("Movable block count exceeds the number of 'o' cells.")

    return Puzzle(
        grid=tuple(grid),
        block_counts=counts,
        lasers=tuple(laser for _, laser in laser_records),
        targets=frozenset(point for _, point in target_records),
    )

