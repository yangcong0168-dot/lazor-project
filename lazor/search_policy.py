"""Cheap opening decisions from dimensions and initial optical structure.

Policies only choose implementations, pruning frequency and bounded pass
order. They never infer infeasibility or remove legal inventory choices.
"""

from dataclasses import dataclass
from .bitboard import MAX_STATES as BATCH_MAX_STATES


SMALL_AREA = 64
BATCH_MIN_AREA = 256
LARGE_AREA = 900
SPARSE_FACTOR = 16


@dataclass(frozen=True, slots=True)
class OpeningPolicy:
    name: str
    area: int
    placeable: int
    movable: int
    fixed_or_unplaceable: int
    state_count: int
    use_bitboard: bool
    geometry_database: bool
    predecessor_database: bool
    optimistic_period: int
    early_guidance: bool


def choose_opening(width, height, placeable, movable, target_count=1):
    """Select a bounded strategy without inspecting answers or case IDs."""
    if (any(type(value) is not int for value in (width, height, placeable, movable, target_count))
            or min(width, height, target_count) < 1
            or not 0 <= movable <= placeable <= width * height):
        raise ValueError("Opening features must describe a valid board and inventory.")
    area = width * height
    states = 4 * ((2 * width + 1) * (2 * height + 1) // 2)
    small = area <= SMALL_AREA
    sparse = placeable >= SPARSE_FACTOR * movable
    # Incidence masks use more than linear space. Keep an explicit state
    # bound and conservative guards for untested dense/many-target boards.
    batch = (movable > 0 and area >= BATCH_MIN_AREA and states <= BATCH_MAX_STATES
             and placeable >= 4 * movable and target_count <= 64)
    early = movable > 0 and area >= LARGE_AREA and sparse and placeable < area
    if small:
        name = "small_memory"
    elif early:
        name = "large_constrained_guided"
    elif batch:
        name = ("large_open_batch" if placeable == area else "large_constrained_batch") if area >= LARGE_AREA else "medium_batch"
    else:
        name = "scalar_conservative"
    return OpeningPolicy(name, area, placeable, movable, area - placeable, states,
                         batch, movable > 0 and not small and not batch,
                         movable > 0 and not small, 32, early)


def analyze_opening(puzzle):
    """Expose the same initial classification used by the search solver."""
    return choose_opening(puzzle.width, puzzle.height, len(puzzle.placeable_positions),
                          sum(puzzle.block_counts.values()), len(puzzle.targets))
