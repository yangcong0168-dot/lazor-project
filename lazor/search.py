"""Exact inventory search driven by the laser frontier, with safe pruning.

Only cells whose values can affect a reached ray are assigned. Once every
target is reached, unused inventory can be placed in undecided cells without
changing those established paths. The full public simulator verifies the
completed placement before it is returned.
"""

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite
from time import perf_counter
from typing import Literal

from .models import Puzzle
from .search_policy import choose_opening, analyze_opening
from .search_tables import (TABLES as _TABLES, clear_search_caches, search_cache_info,
                            configure_search_database, search_database_info)
from .simulation import SimulationResult, simulate

_KINDS = ("o", "A", "B", "C")
_CODES = {kind: code for code, kind in enumerate(_KINDS)}
_DIRECTIONS = ((-1, -1), (-1, 1), (1, -1), (1, 1))
# The three guided passes have decision and optical-work budgets. The final
# pass has no restart cutoff, preserving complete infeasibility proofs.
_RESTART_AFTER = 4096
_RESTART_RAY_STATES = 65536
_PRIORITY_CACHE_LIMIT = 4096
# Input-independent lookup tables. Cell code -1 indexes the last entry,
# whose optimistic behavior depends on the available inventory mask.
_TRANSMITS = tuple((True, False, False, True, bool(mask & 9)) for mask in range(16))
_REFLECTS = tuple((False, True, False, True, bool(mask & 10)) for mask in range(16))
_SINGLETON_KIND = tuple(mask.bit_length() - 1 if mask and not mask & (mask - 1) else -1
                        for mask in range(16))
_CHOICE_TABLES = {order: tuple(tuple(code for code in order if mask & (1 << code))
                             for mask in range(16))
                 for order in ((0, 3, 1, 2), (3, 0, 1, 2),
                               (3, 1, 0, 2), (1, 3, 0, 2))}
_BYTE_MASKS = (1, 2, 4, 8, 16, 32, 64, 128)


def _possible_predecessors(state, stride, max_x, max_y,
                           cell_at, state_id, static_mask):
    """Invert one optimistic optical step, including the rear-cell guard.

    The endpoint fixes the previous point and outgoing direction. Its two
    possible incoming directions are transmission and reflection. Compact
    state IDs store only odd-parity face points.
    """
    y, x = divmod(((state >> 2) << 1) | 1, stride)
    vx, vy = _DIRECTIONS[state & 3]
    px, py = x - vx, y - vy
    previous = []
    if 0 <= px <= max_x and 0 <= py <= max_y:
        travel = cell_at(px, py, vx, vy)
        if static_mask(travel) & 9:
            previous.append(state_id(px, py, vx, vy))
            rx, ry = (-vx, vy) if not (px & 1) else (vx, -vy)
            front = cell_at(px, py, rx, ry)
            if static_mask(front) & 10:
                previous.append(state_id(px, py, rx, ry))
    return tuple(previous)


@dataclass(frozen=True)
class SearchResult:
    """Search work counters; a timeout is never an infeasibility proof.

    forced_assignments counts actual forced writes to cells/the undo trail.
    assignment_writes counts all explicit value assignments, excluding undo.
    implicit_cell_reads counts reads of the sole remaining kind at unresolved
    cells; repeated reads of one cell are counted separately and do not write
    to cells, inventory or the undo trail.
    """

    status: Literal["solved", "no_solution", "timeout"]
    solution: SimulationResult | None
    elapsed_seconds: float
    candidates_tested: int
    states_expanded: int
    ray_states_processed: int
    branches_pruned: int
    optimistic_checks: int
    forced_assignments: int
    reason: str
    restarts: int = 0
    assignment_writes: int = 0
    implicit_cell_reads: int = 0
    opening_strategy: str = "unclassified"
    optimistic_period: int = 32
    bitboard_enabled: bool = False
    optimistic_batches: int = 0
    bitboard_setup_seconds: float = 0.0
    bitboard_cache_hit: bool = False

    @property
    def solved(self) -> bool:
        return self.status == "solved"


class _Deadline(Exception):
    pass


def solve_search(
    puzzle: Puzzle,
    *,
    timeout: float | None = 110.0,
    progress: Callable[[dict], None] | None = None,
) -> SearchResult:
    """Search each optically relevant assignment, preserving exact inventory.

    Search state is compact integer ray IDs, bit sets, inventory counters and
    an undo trail. Iterative DFS avoids Python's recursion limit on large
    boards. An optimistic ray traversal permits every remaining kind at an
    undecided cell; a missed target in that superset safely prunes the branch.
    The optimistic check runs at the root and periodically, rather than
    repeating a whole-board trace at every assignment. Bounded passes try
    empty-first, distant-goal guidance favoring C, then nearby-goal guidance
    favoring A for reflection. The final complete pass processes rays near
    an unhit target first. Distances only
    order decisions and never remove legal choices. A no-solution result
    requires exhausting an entire traversal; all work counts include retries
    and reverse BFS work.
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
    candidates = nodes = ray_states = pruned = checks = forced = restarts = 0
    assignment_writes = implicit_reads = 0
    opening_strategy = "unclassified"
    optimistic_period = 32
    bitboard_enabled = bitboard_cache_hit = False
    optimistic_batches = 0
    bitboard_setup_seconds = 0.0

    def checkpoint():
        nonlocal last_progress
        now = perf_counter()
        if deadline is not None and now >= deadline:
            raise _Deadline
        if progress is not None and now - last_progress >= 5.0:
            progress({
                "elapsed_seconds": now - started,
                "candidates_tested": candidates,
                "states_expanded": nodes,
                "ray_states_processed": ray_states,
                "branches_pruned": pruned,
                "restarts": restarts,
                "assignment_writes": assignment_writes,
                "implicit_cell_reads": implicit_reads,
            })
            last_progress = now

    def finish(status, solution=None):
        return SearchResult(
            status, solution, perf_counter() - started,
            candidates, nodes, ray_states, pruned, checks, forced,
            "Time budget exhausted before search completed."
            if status == "timeout" else
            "All optically relevant assignments were exhausted."
            if status == "no_solution" else "",
            restarts,
            assignment_writes,
            implicit_reads,
            opening_strategy, optimistic_period, bitboard_enabled,
            optimistic_batches, bitboard_setup_seconds, bitboard_cache_hit,
        )

    try:
        checkpoint()
        counts = puzzle.block_counts
        if any(type(counts.get(kind, 0)) is not int
               or counts.get(kind, 0) < 0 for kind in ("A", "B", "C")):
            raise ValueError("Movable inventory must contain nonnegative integers.")
        positions = puzzle.placeable_positions
        if sum(counts.values()) > len(positions):
            raise ValueError("Movable inventory exceeds available cells.")
        if not puzzle.lasers or not puzzle.targets:
            raise ValueError("At least one laser and target are required.")
        # Validate geometry and manual-layout invariants once, not per node.
        simulate(puzzle)
        checkpoint()
        # A diagonal step preserves odd x+y parity. Corners and centers
        # therefore cannot be reached, regardless of the block placement.
        if any((x + y) % 2 == 0 for x, y in puzzle.targets):
            pruned += 1
            opening_strategy = "parity_reject"
            return finish("no_solution")
        width, height = puzzle.width, puzzle.height
        policy = choose_opening(width, height, len(positions), sum(counts.values()), len(puzzle.targets))
        opening_strategy = policy.name
        optimistic_period = policy.optimistic_period
        bitboard_enabled = policy.use_bitboard
        max_x, max_y = 2 * width, 2 * height
        stride = max_x + 1
        cells = [
            -1 if kind == "o" else _CODES.get(kind, 0)
            for row in puzzle.grid for kind in row
        ]
        outside = len(cells)
        cells.append(0)
        variables = tuple(row * width + column for column, row in positions)
        remaining = [len(positions) - sum(counts.values())] + [
            counts.get(kind, 0) for kind in ("A", "B", "C")
        ]
        inventory_mask = sum(1 << code for code, count in enumerate(remaining) if count)
        targets = {point: 1 << index
                   for index, point in enumerate(sorted(puzzle.targets))}
        all_hits = (1 << len(targets)) - 1
        state_count = 4 * (stride * (max_y + 1) // 2)
        singleton_kind, choice_tables, byte_masks = _SINGLETON_KIND, _CHOICE_TABLES, _BYTE_MASKS
        optimistic_marks = [0] * state_count
        optimistic_epoch = 0
        geometry = [None] * state_count
        shared_geometry = _TABLES.geometry(width, height, state_count, checkpoint=checkpoint,
                                           use_database=policy.geometry_database)
        point_targets = {(y * stride + x) >> 1: bit for (x, y), bit in targets.items()}

        def state_id(x, y, vx, vy):
            # stride is odd and x+y is odd, so the linear point index is
            # always odd. Packing p>>1 halves the largest seen-bit position.
            return (((y * stride + x) >> 1) << 2) | ((vx > 0) << 1) | (vy > 0)

        def cell_at(x, y, vx, vy):
            column, row = x // 2, y // 2
            if not (x & 1):
                column -= vx < 0
            else:
                row -= vy < 0
            if 0 <= column < width and 0 <= row < height:
                return row * width + column
            return outside

        def describe(state):
            cached = geometry[state]
            if cached is not None:
                return cached
            # Only the first four fields are dimension-dependent. Target
            # bits are attached separately for this puzzle, never shared.
            base = shared_geometry[state]
            if base is not None:
                cached = base + (point_targets.get(state >> 2, 0),)
                geometry[state] = cached
                return cached
            point, direction = ((state >> 2) << 1) | 1, state & 3
            y, x = divmod(point, stride)
            vx, vy = _DIRECTIONS[direction]
            rx, ry = (-vx, vy) if not (x & 1) else (vx, -vy)
            front = cell_at(x, y, vx, vy)
            rear = cell_at(x, y, rx, ry)
            straight = (state_id(x + vx, y + vy, vx, vy)
                        if 0 <= x + vx <= max_x and 0 <= y + vy <= max_y
                        else -1)
            reflected = (state_id(x + rx, y + ry, rx, ry)
                         if 0 <= x + rx <= max_x and 0 <= y + ry <= max_y
                         else -1)
            base = front, rear, straight, reflected
            shared_geometry[state] = base
            cached = base + (point_targets.get(state >> 2, 0),)
            geometry[state] = cached
            return cached

        pending = [state_id(ray.x, ray.y, ray.vx, ray.vy)
                   for ray in reversed(puzzle.lasers)]
        seen = hits = 0
        trail = []

        known_transmit = known_reflect = rear_transmit = unknown_front = unknown_rear = 0
        if bitboard_enabled:
            setup_started = perf_counter()
            try:
                (front_masks, rear_masks, straight_routes, reflected_routes), bitboard_cache_hit = (
                    _TABLES.bitboards(width, height, checkpoint=checkpoint))
                for cell, kind in enumerate(cells):
                    if kind < 0:
                        unknown_front |= front_masks[cell]
                        unknown_rear |= rear_masks[cell]
                    else:
                        if kind in (0, 3):
                            known_transmit |= front_masks[cell]
                            rear_transmit |= rear_masks[cell]
                        if kind in (1, 3):
                            known_reflect |= front_masks[cell]
                target_masks = tuple((15 << state_id(x, y, -1, -1), bit)
                                     for (x, y), bit in targets.items())
                checkpoint()
            finally:
                bitboard_setup_seconds = perf_counter() - setup_started

        def assign(cell, code):
            nonlocal assignment_writes, inventory_mask
            nonlocal known_transmit, known_reflect, rear_transmit, unknown_front, unknown_rear
            if bitboard_enabled:
                front_bits, rear_bits = front_masks[cell], rear_masks[cell]
                unknown_front &= ~front_bits
                unknown_rear &= ~rear_bits
                if code in (0, 3):
                    known_transmit |= front_bits
                    rear_transmit |= rear_bits
                if code in (1, 3):
                    known_reflect |= front_bits
            cells[cell] = code
            remaining[code] -= 1
            if not remaining[code]:
                inventory_mask &= ~(1 << code)
            trail.append(cell)
            assignment_writes += 1

        def undo(length):
            nonlocal inventory_mask
            nonlocal known_transmit, known_reflect, rear_transmit, unknown_front, unknown_rear
            while len(trail) > length:
                cell = trail.pop()
                code = cells[cell]
                if not remaining[code]:
                    inventory_mask |= 1 << code
                remaining[code] += 1
                if bitboard_enabled:
                    front_bits, rear_bits = front_masks[cell], rear_masks[cell]
                    unknown_front |= front_bits
                    unknown_rear |= rear_bits
                    known_transmit &= ~front_bits
                    rear_transmit &= ~rear_bits
                    known_reflect &= ~front_bits
                cells[cell] = -1

        def optimistic(pending, seen, hits):
            nonlocal checks, ray_states, optimistic_epoch, optimistic_batches
            checks += 1
            if bitboard_enabled:
                passing = known_transmit
                reflecting = known_reflect
                pass_rear = rear_transmit
                if inventory_mask & 9:
                    passing |= unknown_front
                    pass_rear |= unknown_rear
                if inventory_mask & 10:
                    reflecting |= unknown_front
                reflecting &= pass_rear
                frontier = 0
                for state in pending:
                    frontier |= 1 << state
                reached = seen
                while frontier:
                    frontier &= ~reached
                    if not frontier:
                        break
                    reached |= frontier
                    # Count states represented in the batch, separately from
                    # Python loop iterations. BFS may discover more states
                    # before the final target than the scalar LIFO walk.
                    ray_states += frontier.bit_count()
                    optimistic_batches += 1
                    checkpoint()
                    for target_states, target_bit in target_masks:
                        if frontier & target_states:
                            hits |= target_bit
                    if hits == all_hits:
                        return True
                    onward, backward = frontier & passing, frontier & reflecting
                    frontier = 0
                    for offset, route in straight_routes:
                        bits = onward & route
                        frontier |= bits << offset if offset >= 0 else bits >> -offset
                    for offset, route in reflected_routes:
                        bits = backward & route
                        frontier |= bits << offset if offset >= 0 else bits >> -offset
                return hits == all_hits
            transmits, reflects = _TRANSMITS[inventory_mask], _REFLECTS[inventory_mask]
            optimistic_epoch += 1
            epoch = optimistic_epoch
            base_seen = seen.to_bytes((state_count + 7) // 8, "little")
            work = list(pending)
            processed = 0
            while work:
                state = work.pop()
                if optimistic_marks[state] == epoch:
                    continue
                optimistic_marks[state] = epoch
                if base_seen[state >> 3] & byte_masks[state & 7]:
                    continue
                front, rear, straight, reflected, target = geometry[state] or describe(state)
                hits |= target
                ray_states += 1
                processed += 1
                if hits == all_hits:
                    return True
                if processed & 255 == 0:
                    checkpoint()
                front_kind = cells[front]
                if straight >= 0 and transmits[front_kind]:
                    work.append(straight)
                if reflected >= 0 and reflects[front_kind]:
                    if transmits[cells[rear]]:
                        work.append(reflected)
            return hits == all_hits

        # Static optimistic reverse graph. These domains never depend on a
        # later assignment, so the distance cache is valid across backtracks.
        original_cells = tuple(cells)
        original_domain = inventory_mask
        reverse_geometry = None
        reverse_tables = {}
        active_goal = None

        def static_mask(cell):
            code = original_cells[cell]
            return original_domain if code < 0 else 1 << code

        def predecessors(state):
            cached = reverse_geometry[state]
            if cached is not None:
                return cached
            cached = _possible_predecessors(
                state, stride, max_x, max_y, cell_at, state_id, static_mask
            )
            reverse_geometry[state] = cached
            return cached

        def choose_goal(frontier, hits):
            nonlocal active_goal
            if active_goal is not None and not hits & targets[active_goal]:
                return
            points = [divmod(((state >> 2) << 1) | 1, stride) for state in frontier]
            # Complementary guided passes handle distant and nearby goals.
            # These are ordering rules, never infeasibility claims.
            choose = max if restarts == 1 else min
            active_goal = choose(
                (point for point, bit in targets.items() if not hits & bit),
                key=lambda point: min(max(abs(point[0] - x), abs(point[1] - y))
                                      for y, x in points),
            )

        def reverse_distances(states):
            nonlocal ray_states, reverse_geometry
            wanted = {state for state in states if state >= 0}
            if not wanted:
                return [1000000000] * len(states)
            if reverse_geometry is None:
                # This graph depends on fixed cells AND the initially
                # available kinds. Targets and laser sources do not affect
                # its edges; distances/queues still belong to this solve.
                graph_key = (width, height, bytes(
                    original_domain if code < 0 else 1 << code
                    for code in original_cells))
                reverse_geometry = _TABLES.predecessors(graph_key, state_count, checkpoint=checkpoint,
                                                       use_database=policy.predecessor_database)
            if active_goal not in reverse_tables:
                x, y = active_goal
                distance = {state_id(x, y, vx, vy): 0 for vx, vy in _DIRECTIONS}
                reverse_tables[active_goal] = distance, deque(distance)
            distance, queue = reverse_tables[active_goal]
            processed = 0
            found = any(state in distance for state in wanted)
            # A single unreachable first candidate must not force a complete
            # traversal when another frontier is close. With unit edge costs,
            # the first discovered member has minimum distance among wanted.
            while not found and queue:
                here = queue.popleft()
                cost = distance[here] + 1
                previous_states = reverse_geometry[here]
                if previous_states is None:
                    previous_states = predecessors(here)
                for previous in previous_states:
                    if previous not in distance:
                        distance[previous] = cost
                        queue.append(previous)
                        if previous in wanted:
                            found = True
                ray_states += 1
                processed += 1
                if processed & 255 == 0:
                    checkpoint()
            # Missing entries only lose this ordering tie; they are never
            # treated as an infeasibility proof or removed from the search.
            return [distance.get(state, 1000000000) for state in states]

        def preferred_order(state, rear, straight, reflected, hits):
            choose_goal([state], hits)
            back_kind = cells[rear]
            reflected_possible = (back_kind in (0, 3)
                                  or back_kind < 0 and (remaining[0] or remaining[3]))
            onward, backward = reverse_distances((straight, reflected if reflected_possible else -1))
            # If both outgoing endpoints are missed targets, retaining both
            # branches is useful even when the active goal favors straight.
            if (restarts == 1 and remaining[3] and reflected_possible and straight >= 0 and reflected >= 0
                    and (geometry[straight] or describe(straight))[4] & ~hits
                    and (geometry[reflected] or describe(reflected))[4] & ~hits):
                return (3, 0, 1, 2)
            # The first guided pass keeps both branches with C. The second
            # tries A first to preserve scarce splitters for another path.
            reflected_order = (3, 1, 0, 2) if restarts == 1 else (1, 3, 0, 2)
            return reflected_order if backward < onward else (0, 3, 1, 2)

        priority_cache = {}

        def frontier_priority(state, hits):
            """Order only: geometric distance never removes a legal branch.

            Direction and obstacles are deliberately absent from this cheap
            tie-breaking heuristic. Cache size is bounded even for puzzles
            with many different subsets of already-hit targets.
            """
            key = state, hits
            cached = priority_cache.get(key)
            if cached is None:
                y, x = divmod(((state >> 2) << 1) | 1, stride)
                cached = min(
                    max(abs(tx - x), abs(ty - y))
                    for (tx, ty), bit in targets.items() if not hits & bit
                )
                if len(priority_cache) >= _PRIORITY_CACHE_LIMIT:
                    priority_cache.clear()
                priority_cache[key] = cached
            return cached

        # Disconnected components disappear before any placement enumeration
        # or complete-layout simulation.
        if not optimistic(pending, seen, hits):
            pruned += 1
            checkpoint()
            return finish("no_solution")

        # frame: cell, remaining choices, next choice, pending, seen, hits,
        # trail length before assigning that cell. No recursive Python calls.
        frames = []
        choice_order = (0, 3, 1, 2)
        sources = tuple(pending)
        stage_nodes = stage_rays = 0
        stage_node_budget = 0 if policy.early_guidance else _RESTART_AFTER
        while True:
            checkpoint()
            nodes += 1
            if restarts < 3 and (
                nodes - stage_nodes > stage_node_budget
                or ray_states - stage_rays > _RESTART_RAY_STATES
            ):
                undo(0)
                pending, seen, hits = list(sources), 0, 0
                frames.clear()
                choice_order = (3, 0, 1, 2)
                restarts += 1
                stage_node_budget = _RESTART_AFTER
                stage_nodes, stage_rays = nodes, ray_states
                if restarts == 2:
                    # Start the alternative goal order from the original
                    # frontier. Static predecessors remain valid; distances
                    # and the selected goal are rebuilt for this pass.
                    active_goal = None
                    reverse_tables.clear()
                if restarts == 3:
                    # The complete fallback no longer uses reverse tables.
                    # Drop only this solve's reference, retaining the shared
                    # table for future puzzles with the same static graph.
                    reverse_geometry = None
                    reverse_tables.clear()
            branch = None
            dead = False
            if nodes > 1 and nodes % optimistic_period == 0:
                if not optimistic(pending, seen, hits):
                    dead = True
            # With one remaining kind, every unresolved cell has that value.
            # Such a node cannot branch again: read the implied value without
            # materializing thousands of forced writes that would need undo.
            available_mask = inventory_mask
            implicit_kind = singleton_kind[available_mask]
            processed = 0
            while pending and not dead:
                if restarts in (1, 2):
                    choose_goal(pending, hits)
                    if len(pending) > 1:
                        distances = reverse_distances(pending)
                        index = min(range(len(pending)), key=distances.__getitem__)
                        state = pending.pop(index)
                    else:
                        state = pending.pop()
                elif restarts == 3 and len(pending) > 1:
                    index = min(
                        range(len(pending)),
                        key=lambda i: frontier_priority(pending[i], hits),
                    )
                    state = pending.pop(index)
                else:
                    state = pending.pop()
                bit = 1 << state
                if seen & bit:
                    continue
                front, rear, straight, reflected, target = geometry[state] or describe(state)
                hits |= target
                if hits == all_hits:
                    break
                kind = cells[front]
                if kind < 0:
                    if implicit_kind >= 0:
                        kind = implicit_kind
                        implicit_reads += 1
                    else:
                        order = (preferred_order(state, rear, straight, reflected, hits)
                                 if restarts in (1, 2) else choice_order)
                        choices = choice_tables[order][available_mask]
                        branch = front
                if branch is None and kind in (1, 3) and reflected >= 0:
                    rear_kind = cells[rear]
                    if rear_kind < 0:
                        if implicit_kind >= 0:
                            rear_kind = implicit_kind
                            implicit_reads += 1
                        else:
                            # Empty and C both transmit here. Try empty first
                            # to preserve splitters for splitting elsewhere.
                            choices = choice_tables[(0, 3, 1, 2)][available_mask]
                            branch = rear
                if branch is not None:
                    pending.append(state)
                    break
                seen |= bit
                ray_states += 1
                processed += 1
                if processed & 255 == 0:
                    checkpoint()
                if kind in (1, 3) and reflected >= 0 and rear_kind in (0, 3):
                    pending.append(reflected)
                if kind in (0, 3) and straight >= 0:
                    pending.append(straight)

            if hits == all_hits and not dead:
                # These hits used explicit choices or the sole remaining
                # kind. Completing inventory respects both, so it cannot
                # remove an established path to an already reached target.
                extra = list(remaining)
                placement = {}
                for index, cell in enumerate(variables):
                    code = cells[cell]
                    if code < 0:
                        code = next(kind for kind in (1, 2, 3, 0) if extra[kind])
                        extra[code] -= 1
                    if code:
                        placement[positions[index]] = _KINDS[code]
                candidates += 1
                result = simulate(puzzle, placement)
                ray_states += result.states_processed
                if not result.is_solution:
                    raise RuntimeError("Frontier search failed independent verification.")
                checkpoint()
                return finish("solved", result)

            if branch is not None and not dead:
                frames.append([
                    branch, choices, 1, tuple(pending), seen, hits, len(trail)
                ])
                assign(branch, choices[0])
                continue

            pruned += 1
            while frames:
                cell, choices, next_choice, saved_pending, saved_seen, saved_hits, length = frames[-1]
                undo(length)
                if next_choice < len(choices):
                    frames[-1][2] += 1
                    assign(cell, choices[next_choice])
                    pending, seen, hits = list(saved_pending), saved_seen, saved_hits
                    break
                frames.pop()
            else:
                checkpoint()
                return finish("no_solution")
    except _Deadline:
        return finish("timeout")
