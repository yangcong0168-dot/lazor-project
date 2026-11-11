"""Reconstructed exhaustive DFS baseline for timing comparisons only.

The previous solver source is not available. This is a new, transparent
baseline, not a claim to reproduce that version byte for byte. It visits
combinations in A/B/C order and row-major cell order, simulates only complete
placements, and stops at the first solution. Identical blocks are not labeled.
No SMT hints, optical pruning, or precomputed solution are used.
"""

from collections.abc import Callable
from dataclasses import dataclass
from itertools import combinations
from math import isfinite
from time import perf_counter
from typing import Literal

from .models import Puzzle
from .simulation import SimulationResult, simulate


@dataclass(frozen=True)
class DFSResult:
    """Timing and counters for the reconstructed enumeration baseline."""

    status: Literal["solved", "no_solution", "timeout"]
    solution: SimulationResult | None
    elapsed_seconds: float
    candidates_tested: int
    states_expanded: int
    ray_states_processed: int
    reason: str

    @property
    def solved(self) -> bool:
        """Whether a complete placement passed the common ray simulator."""
        return self.status == "solved"


def solve_dfs(
    puzzle: Puzzle,
    *,
    timeout: float | None = 110.0,
    progress: Callable[[dict], None] | None = None,
) -> DFSResult:
    """Enumerate each distinct full layout at most once, depth first.

    The time budget includes input validation, enumeration, and simulation.
    Counters cover complete candidate simulations and partial search nodes;
    the initial geometry-validation trace is not a candidate. The callback
    receives a progress dictionary approximately every five seconds. Timeout
    means incomplete enumeration, never a proof that the puzzle has no answer.
    """
    if timeout is not None and (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not isfinite(timeout) or timeout < 0
    ):
        raise ValueError("timeout must be finite and nonnegative, or None.")

    started = perf_counter()
    deadline = None if timeout is None else started + timeout
    last_progress = started
    candidates = partial_nodes = ray_states = 0
    solution = None
    timed_out = False

    def checkpoint():
        nonlocal timed_out, last_progress
        now = perf_counter()
        if deadline is not None and now >= deadline:
            timed_out = True
        if progress is not None and now - last_progress >= 5.0:
            progress({
                "elapsed_seconds": now - started,
                "candidates_tested": candidates,
                "states_expanded": partial_nodes,
                "ray_states_processed": ray_states,
            })
            last_progress = now
        return timed_out

    def finish():
        status = "timeout" if timed_out else (
            "solved" if solution is not None else "no_solution"
        )
        reason = (
            "Time budget exhausted before enumeration completed."
            if timed_out else (
                "" if solution is not None
                else "Every distinct complete layout was checked."
            )
        )
        return DFSResult(
            status, solution, perf_counter() - started,
            candidates, partial_nodes, ray_states, reason,
        )

    if checkpoint():
        return finish()
    counts = puzzle.block_counts
    if any(type(counts.get(kind, 0)) is not int
           or counts.get(kind, 0) < 0 for kind in ("A", "B", "C")):
        raise ValueError(
            "Movable inventory must contain nonnegative integers."
        )
    positions = puzzle.placeable_positions
    if sum(counts.values()) > len(positions):
        raise ValueError("Movable inventory exceeds available cells.")
    if not puzzle.lasers or not puzzle.targets:
        raise ValueError("At least one laser and target are required.")
    simulate(puzzle)
    groups = tuple(
        (kind, counts.get(kind, 0)) for kind in ("A", "B", "C")
        if counts.get(kind, 0)
    )
    placement = {}

    def visit(group_index, available):
        nonlocal candidates, partial_nodes, ray_states, solution
        if checkpoint():
            return
        if group_index == len(groups):
            candidates += 1
            result = simulate(puzzle, placement)
            ray_states += result.states_processed
            if checkpoint():
                return
            if result.is_solution:
                solution = result
            return

        partial_nodes += 1
        kind, count = groups[group_index]
        for chosen in combinations(available, count):
            for position in chosen:
                placement[position] = kind
            # The final kind needs no remaining-position allocation.
            remaining = () if group_index + 1 == len(groups) else tuple(
                position for position in available if position not in chosen
            )
            visit(group_index + 1, remaining)
            for position in chosen:
                del placement[position]
            if solution is not None or timed_out:
                return

    visit(0, positions)
    return finish()
