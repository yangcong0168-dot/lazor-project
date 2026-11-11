"""Boolean/integer planning with a symbolic optical transition relation.

There is no placement BFS. One SMT formula describes block decisions and the
least source-reachable set of ray states. Strictly descending parent witnesses
exclude unsupported cycles, while forward closure preserves real ray loops.
"""

import importlib
import sys
from dataclasses import dataclass
from math import ceil, isfinite
from pathlib import Path
from time import perf_counter
from typing import Any, Literal

from .models import Puzzle
from .simulation import SimulationResult, State, simulate

PlanStatus = Literal["solved", "no_solution", "timeout", "unknown"]
EMPTY, REFLECT, OPAQUE, SPLIT = 0, 1, 2, 3
KIND_CODES = {"A": REFLECT, "B": OPAQUE, "C": SPLIT}
DIRECTIONS = ((-1, -1), (-1, 1), (1, -1), (1, 1))


def _load_z3():
    """Use installed Z3 or the project's explicitly installed .deps wheel."""
    try:
        module = importlib.import_module("z3")
    except ModuleNotFoundError as exc:
        if exc.name != "z3":
            raise
        local = Path(__file__).resolve().parents[1] / ".deps"
        if not (local / "z3" / "__init__.py").is_file():
            raise RuntimeError(
                "Constraint solving requires z3-solver. Install it with "
                "python -m pip install -r requirements.txt "
                "(or add --target .deps for a project-local install)."
            ) from exc
        sys.path.insert(0, str(local))
        module = importlib.import_module("z3")
    if not hasattr(module, "Solver"):
        raise RuntimeError("Install z3-solver, not the unrelated z3 package.")
    return module


@dataclass(frozen=True)
class Transition:
    """A possible directed optical edge and its block-dependent guard."""

    source: State
    target: State
    condition: Any


@dataclass(frozen=True)
class PlanningModel:
    """Inspectable SMT model; Z3 objects stay out of worker result messages."""

    puzzle: Puzzle
    solver: Any
    cell_vars: dict[tuple[int, int], Any]
    states: tuple[State, ...]
    reachable: dict[State, Any]
    ranks: dict[State, Any]
    edges: tuple[Transition, ...]

    @property
    def variable_count(self) -> int:
        """Count block decisions, reachability Booleans, and witness ranks."""
        return len(self.cell_vars) + 2 * len(self.states)

    @property
    def constraint_count(self) -> int:
        """Number of top-level assertions in the feasibility model."""
        return len(self.solver.assertions())

    def to_smt2(self) -> str:
        """Export a standalone SMT-LIB formula for inspection or replay."""
        state_map = "".join(
            f"; reach_{i}, rank_{i}: state {state}\n"
            for i, state in enumerate(self.states)
        )
        return (
            "; Lazor state-transition feasibility planning\n"
            "; Cells: 0=empty, 1=A, 2=B, 3=C\n"
            "; reach states use half-grid coordinates and direction.\n"
            "; rank is a source witness, not a bound on physical looping.\n"
            + state_map
            + self.solver.to_smt2()
        )


@dataclass(frozen=True)
class PlanningResult:
    """Outcome, model size, timing, and a simulator-verified solution.

    Only no_solution represents a solver proof of infeasibility. Timeout and
    unknown retain the solver reason and make no claim about feasibility.
    """

    status: PlanStatus
    solution: SimulationResult | None
    elapsed_seconds: float
    build_seconds: float
    solver_seconds: float
    verification_seconds: float
    state_count: int
    transition_count: int
    variable_count: int
    constraint_count: int
    solver_version: str
    reason: str
    solver_checks: int
    reachable_states: frozenset[State]

    @property
    def solved(self) -> bool:
        """Whether a model and its complete optical result were verified."""
        return self.status == "solved"


def _forward_cell(state: State) -> tuple[int, int]:
    """Identify the cell entered from a half-grid face midpoint."""
    x, y, vx, vy = state
    if x % 2 == 0:
        return x // 2 - int(vx < 0), y // 2
    return x // 2, y // 2 - int(vy < 0)


class _BuildTimeout(TimeoutError):
    """The Python model builder exhausted the caller's shared time budget."""


def _encode_planning_graph(
    puzzle, states, raw_edges, positions, movable_domain, counts, sources,
    checkpoint,
) -> PlanningModel:
    """Batch the exact formula through Z3's native SMT-LIB parser.

    Constructing every implication and witness with Z3Py incurs many native
    calls. The names below contain only validated integer coordinates/indexes;
    no input text is interpolated. Cell/ray maps and cached optical guards
    remain Z3 expressions for inspection and independent model verification.
    The formula still uses bounded integer ranks and exact inventory counts.
    """
    z3 = _load_z3()
    solver = z3.Solver()
    solver.set(random_seed=0)
    declarations, assertions = [], []
    cells, tests = {}, {}
    codes = tuple(code for code in range(4) if movable_domain & (1 << code))

    def conjunction(terms):
        if not terms:
            return "true"
        return terms[0] if len(terms) == 1 else "(and " + " ".join(terms) + ")"

    def disjunction(terms):
        if not terms:
            return "false"
        return terms[0] if len(terms) == 1 else "(or " + " ".join(terms) + ")"

    for index, position in enumerate(positions):
        if index % 128 == 0:
            checkpoint()
        name = f"block_{position[0]}_{position[1]}"
        cells[position] = z3.Int(name)
        declarations.append(f"(declare-fun {name} () Int)")
        tests[position] = {code: f"(= {name} {code})" for code in codes}
        assertions.append(disjunction(list(tests[position].values())))
    weights = " ".join("1" for _ in positions)
    for kind, code in KIND_CODES.items():
        if counts.get(kind, 0):
            terms = " ".join(tests[position][code] for position in positions)
            # Z3's native pseudo-Boolean equality, also used by its exporter.
            assertions.append(f"((_ pbeq {counts[kind]} {weights}) {terms})")

    reach_names, rank_names = {}, {}
    reachable, ranks = {}, {}
    for index, state in enumerate(states):
        if index % 128 == 0:
            checkpoint()
        rname, dname = f"reach_{index}", f"rank_{index}"
        reach_names[state], rank_names[state] = rname, dname
        reachable[state], ranks[state] = z3.Bool(rname), z3.Int(dname)
        declarations.extend((f"(declare-fun {rname} () Bool)",
                             f"(declare-fun {dname} () Int)"))

    incoming = {state: [] for state in states}
    edges = []
    guard_cache = {(): (z3.BoolVal(True), "true")}
    predicate_cache = {}
    for index, (source, target, requirements) in enumerate(raw_edges):
        if index % 128 == 0:
            checkpoint()
        if requirements not in guard_cache:
            predicates, terms = [], []
            for position, mask in requirements:
                key = position, mask
                if key not in predicate_cache:
                    allowed = [code for code in codes if mask & (1 << code)]
                    text = disjunction([tests[position][code] for code in allowed])
                    expressions = [cells[position] == code for code in allowed]
                    expression = (
                        expressions[0] if len(expressions) == 1
                        else z3.Or(expressions)
                    )
                    predicate_cache[key] = expression, text
                expression, text = predicate_cache[key]
                predicates.append(expression)
                terms.append(text)
            expression = (
                predicates[0] if len(predicates) == 1 else z3.And(predicates)
            )
            guard_cache[requirements] = expression, conjunction(terms)
        guard, text = guard_cache[requirements]
        edges.append(Transition(source, target, guard))
        incoming[target].append((source, text))
        premise = (
            reach_names[source] if not requirements
            else conjunction([reach_names[source], text])
        )
        assertions.append(f"(=> {premise} {reach_names[target]})")

    for index, state in enumerate(states):
        if index % 128 == 0:
            checkpoint()
        active, rank = reach_names[state], rank_names[state]
        if state in sources:
            assertions.extend((active, f"(= {rank} 0)"))
            continue
        assertions.append(
            f"(ite {active} (and (>= {rank} 1) (< {rank} {len(states)})) "
            f"(= {rank} (- 1)))"
        )
        witnesses = [conjunction([
            reach_names[source], guard, f"(< {rank_names[source]} {rank})",
        ]) for source, guard in incoming[state]]
        assertions.append(f"(=> {active} {disjunction(witnesses)})")
    for x, y in puzzle.targets:
        assertions.append(disjunction([
            reach_names[(x, y, vx, vy)] for vx, vy in DIRECTIONS
            if (x, y, vx, vy) in reach_names
        ]))
    checkpoint()
    text = "\n".join(
        declarations + [f"(assert {assertion})" for assertion in assertions]
    )
    # The native parse is one operation; check the shared budget on both sides.
    solver.from_string(text)
    checkpoint()
    return PlanningModel(
        puzzle, solver, cells, states, reachable, ranks, tuple(edges)
    )


def build_planning_model(
    puzzle: Puzzle, *, deadline: float | None = None,
) -> PlanningModel:
    """Build an exact finite reachability model, including cycle witnesses.

    For every enabled edge u -> v, reach[u] implies reach[v]. Conversely each
    reachable non-source v needs at least ONE enabled reachable predecessor u
    with rank[u] < rank[v]. Not every edge must increase rank: cycles remain
    valid when a source-supported spanning witness tree exists.
    """
    def checkpoint():
        if deadline is not None and perf_counter() >= deadline:
            raise _BuildTimeout("Budget exhausted while modeling.")

    checkpoint()
    z3 = _load_z3()
    # Validate input geometry through the established independent simulator.
    simulate(puzzle)
    counts = puzzle.block_counts
    if any(type(counts.get(kind, 0)) is not int
           or counts.get(kind, 0) < 0 for kind in KIND_CODES):
        raise ValueError(
            "Movable inventory must contain nonnegative integers."
        )
    positions = puzzle.placeable_positions
    block_total = sum(counts.values())
    if block_total > len(positions):
        raise ValueError("Movable inventory exceeds available cells.")
    if not puzzle.lasers or not puzzle.targets:
        raise ValueError("At least one laser and target are required.")

    checkpoint()
    sources = {(ray.x, ray.y, ray.vx, ray.vy) for ray in puzzle.lasers}

    def contradictory_model(states):
        """Keep the inspection/export API, but do not encode a known UNSAT graph.

        Every actual ray state is at an odd-parity face midpoint and inside
        the optimistic source graph. Failure of either necessary condition
        is already a complete proof, irrespective of inventory placement.
        Variable maps remain available to callers that inspect cell names;
        no domain, cardinality, or witness constraints are needed with False.
        """
        solver = z3.Solver()
        solver.add(z3.BoolVal(False))
        cells = {}
        for index, position in enumerate(positions):
            if index % 128 == 0:
                checkpoint()
            cells[position] = z3.Int(f"block_{position[0]}_{position[1]}")
        reachable = {
            state: z3.Bool(f"reach_{i}") for i, state in enumerate(states)
        }
        ranks = {state: z3.Int(f"rank_{i}") for i, state in enumerate(states)}
        checkpoint()
        return PlanningModel(puzzle, solver, cells, states, reachable, ranks, ())

    # No diagonal half-block step changes x+y parity. Do this only after
    # input validation so a malformed laser/inventory still raises an error.
    if any((x + y) % 2 != 1 for x, y in puzzle.targets):
        return contradictory_model(tuple(sorted(sources)))

    # Compute the over-approximation in plain Python BEFORE creating any Z3
    # ray variables. Every physical path is in this graph; an absent edge
    # cannot be enabled by any legal placement. Inventory-zero block types
    # are excluded already, including empty when every cell must be filled.
    movable_domain = sum(
        1 << code for kind, code in KIND_CODES.items() if counts.get(kind, 0)
    ) | (1 if block_total < len(positions) else 0)
    domains = dict.fromkeys(positions, movable_domain)
    width, height = puzzle.width, puzzle.height
    max_x, max_y = 2 * width, 2 * height
    transparent = (1 << EMPTY) | (1 << SPLIT)
    reflective = (1 << REFLECT) | (1 << SPLIT)

    def domain_at(position):
        if position in domains:
            return domains[position]
        column, row = position
        code = KIND_CODES.get(puzzle.grid[row][column], EMPTY) if (
            0 <= column < width and 0 <= row < height
        ) else EMPTY
        return 1 << code

    pending = sorted(sources)
    discovered = set(sources)
    raw_edges = []
    for index, state in enumerate(pending):
        if index % 128 == 0:
            checkpoint()
        x, y, vx, vy = state
        front = _forward_cell(state)
        reflected = (-vx, vy) if x % 2 == 0 else (vx, -vy)
        for direction, allowed in (((vx, vy), transparent),
                                   (reflected, reflective)):
            next_vx, next_vy = direction
            next_x, next_y = x + next_vx, y + next_vy
            if not (0 <= next_x <= max_x and 0 <= next_y <= max_y):
                continue
            front_domain = domain_at(front)
            if not front_domain & allowed:
                continue
            requirements = []
            if front_domain & allowed != front_domain:
                requirements.append((front, front_domain & allowed))
            if direction == reflected:
                # Reflections must not cross an A/B behind the source.
                rear = _forward_cell((x, y, next_vx, next_vy))
                rear_domain = domain_at(rear)
                if not rear_domain & transparent:
                    continue
                if rear_domain & transparent != rear_domain:
                    requirements.append((rear, rear_domain & transparent))
            target = (next_x, next_y, next_vx, next_vy)
            raw_edges.append((state, target, tuple(requirements)))
            if target not in discovered:
                discovered.add(target)
                pending.append(target)

    states = tuple(sorted(discovered))
    checkpoint()
    possible_points = {state[:2] for state in states}
    if not puzzle.targets <= possible_points:
        return contradictory_model(states)
    return _encode_planning_graph(
        puzzle, states, raw_edges, positions, movable_domain,
        counts, sources, checkpoint,
    )


def solve_constraints(
    puzzle: Puzzle,
    *,
    timeout: float | None = 110.0,
    model_path: str | Path | None = None,
) -> PlanningResult:
    """Solve one feasibility formula and independently replay its answer.

    The budget includes building, SMT checking, and verification. Z3 gets the
    remaining budget, not the original budget again. Model construction also
    checks the deadline in bounded batches, as does symbolic verification.
    SMT internally performs logical search; this is not a closed-form formula.
    """
    if timeout is not None and (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not isfinite(timeout) or timeout < 0
    ):
        raise ValueError("timeout must be finite and nonnegative, or None.")
    started = perf_counter()
    deadline = None if timeout is None else started + timeout
    model = None
    build_time = solve_time = verify_time = 0.0
    version = "not loaded"
    checks = 0

    def expired():
        return deadline is not None and perf_counter() >= deadline

    def finish(status, solution=None, reason="", reached=frozenset()):
        return PlanningResult(
            status=status, solution=solution,
            elapsed_seconds=perf_counter() - started,
            build_seconds=build_time, solver_seconds=solve_time,
            verification_seconds=verify_time,
            state_count=len(model.states) if model else 0,
            transition_count=len(model.edges) if model else 0,
            variable_count=model.variable_count if model else 0,
            constraint_count=model.constraint_count if model else 0,
            solver_version=version, reason=reason, solver_checks=checks,
            reachable_states=reached,
        )

    if expired():
        return finish("timeout", reason="Budget exhausted before modeling.")
    build_started = perf_counter()
    try:
        model = build_planning_model(puzzle, deadline=deadline)
    except _BuildTimeout as exc:
        build_time = perf_counter() - build_started
        return finish("timeout", reason=str(exc))
    z3 = _load_z3()
    version = z3.get_version_string()
    if model_path is not None:
        path = Path(model_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(model.to_smt2(), encoding="utf-8")
    build_time = perf_counter() - build_started
    if expired():
        return finish("timeout", reason="Budget exhausted while modeling.")
    if deadline is not None:
        remaining_ms = max(1, ceil((deadline - perf_counter()) * 1000))
        model.solver.set(timeout=remaining_ms)

    solve_started = perf_counter()
    answer = model.solver.check()
    solve_time = perf_counter() - solve_started
    checks = 1
    if answer == z3.unknown:
        reason = model.solver.reason_unknown()
        status = (
            "timeout"
            if "timeout" in reason.lower() or expired()
            else "unknown"
        )
        return finish(status, reason=reason)
    if expired():
        return finish("timeout", reason="Budget exhausted during SMT check.")
    if answer == z3.unsat:
        return finish("no_solution", reason="The planning formula is UNSAT.")

    verification_started = perf_counter()

    def verification_timeout():
        nonlocal verify_time
        verify_time = perf_counter() - verification_started
        return finish("timeout", reason="Budget exhausted during verification.")

    assignment = model.solver.model()
    reverse_codes = {value: key for key, value in KIND_CODES.items()}
    placement = {}
    for index, (position, variable) in enumerate(model.cell_vars.items()):
        if index % 128 == 0 and expired():
            return verification_timeout()
        code = assignment.eval(variable, model_completion=True).as_long()
        if code != EMPTY:
            placement[position] = reverse_codes[code]
    reached_set = set()
    for index, state in enumerate(model.states):
        if index % 128 == 0 and expired():
            return verification_timeout()
        if z3.is_true(assignment.eval(model.reachable[state])):
            reached_set.add(state)
    reached = frozenset(reached_set)
    result = simulate(puzzle, placement)
    replay_states = {
        (ray.x, ray.y, ray.vx, ray.vy) for ray in puzzle.lasers
    }
    replay_states.update(
        (end[0], end[1], end[0] - start[0], end[1] - start[1])
        for start, end in result.segments
    )
    projected_points = frozenset((x, y) for x, y, _, _ in reached)
    projected_segments = set()
    enabled_guards = {}
    for index, edge in enumerate(model.edges):
        if index % 128 == 0 and expired():
            return verification_timeout()
        if edge.source not in reached:
            continue
        # Many edges share the same cell predicate (or the constant True).
        # Crossing into Z3 once per distinct guard avoids repeated evaluation.
        guard_id = edge.condition.get_id()
        if guard_id not in enabled_guards:
            enabled_guards[guard_id] = z3.is_true(assignment.eval(edge.condition))
        if enabled_guards[guard_id]:
            projected_segments.add((edge.source[:2], edge.target[:2]))
    if (not result.is_solution or reached != replay_states
            or projected_points != result.visited_points
            or projected_segments != set(result.segments)):
        raise RuntimeError(
            "Constraint model failed independent ray verification."
        )
    verify_time = perf_counter() - verification_started
    if expired():
        return finish(
            "timeout", reason="Budget exhausted during verification."
        )
    return finish("solved", result, reached=reached)
