"""Reproducible cross-product fixtures and isolated, resumable timing runs.

Planted witnesses establish satisfiability but are never given to a solver.
Parity controls test fast rejection; they are explicitly not hard UNSAT cases.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import platform
import random
import statistics
import subprocess
import sys
import tempfile
from time import perf_counter

from .bff import load_bff
from .models import Laser, Puzzle
from .simulation import simulate

SCHEMA_VERSION = 1
DEFAULT_SEED = 20261110
REPRESENTATIVE_SIZES = tuple(range(4, 45, 4))
FAMILIES = ("open", "obstacles", "parity_unsat")
METHODS = ("smt", "search", "dfs")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_write(path: Path, value: dict) -> None:
    """Atomically checkpoint a report without a partially written JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    temporary.replace(path)


def _bff(puzzle: Puzzle) -> str:
    records = ["GRID START", *(" ".join(row) for row in puzzle.grid), "GRID STOP"]
    records.extend(f"{kind} {puzzle.block_counts[kind]}" for kind in "ABC")
    records.extend(f"L {ray.x} {ray.y} {ray.vx} {ray.vy}" for ray in puzzle.lasers)
    records.extend(f"P {x} {y}" for x, y in sorted(puzzle.targets))
    return "\n".join(records) + "\n"


def make_case(size: int, a_count: int, c_count: int, family: str, *,
              seed: int = DEFAULT_SEED) -> tuple[Puzzle, dict, dict]:
    """Return a fixture, independent planted witness, and difficulty metadata.

    Two adjacent C blocks at an interior source guarantee both initial
    branches, even for completely occupied 4x4 boards. The other blocks are
    seeded samples, not a placement-order hint. Targets include both branches
    and a point absent from the unplaced trace whenever one exists.
    """
    if type(size) is not int or not 4 <= size <= 44:
        raise ValueError("Size must be an integer from 4 through 44.")
    if any(type(count) is not int or not 4 <= count <= 10
           for count in (a_count, c_count)):
        raise ValueError("A and C inventories must each be from 4 through 10.")
    if family not in FAMILIES:
        raise ValueError(f"Unknown fixture family: {family}")
    if a_count + c_count > size * size:
        raise ValueError("Inventory exceeds board capacity.")
    # The parity control shares its satisfiable core with the open fixture.
    variant = "open" if family == "parity_unsat" else family
    case_seed = int.from_bytes(hashlib.sha256(
        f"{seed}:{size}:{a_count}:{c_count}:{variant}".encode()
    ).digest()[:8], "big")
    randomizer = random.Random(case_seed)
    center = size // 2
    source = Laser(2 * center, 2 * center + 1, 1, 1)
    placement = {(center, center): "C", (center - 1, center): "C"}
    remaining_cells = [(x, y) for y in range(size) for x in range(size)
                       if (x, y) not in placement]
    randomizer.shuffle(remaining_cells)
    kinds = ["A"] * a_count + ["C"] * (c_count - 2)
    randomizer.shuffle(kinds)
    placement.update(zip(remaining_cells, kinds))
    grid = [["o"] * size for _ in range(size)]
    lasers = [source]
    if family == "obstacles":
        unused = [cell for cell in remaining_cells if cell not in placement]
        # Fixed optics and forbidden cells add geometry without consuming the
        # movable inventory. A full board legitimately has no spare obstacles.
        obstacle_count = min(len(unused), max(1, size * size // 10))
        for index, (x, y) in enumerate(unused[:obstacle_count]):
            grid[y][x] = ("x", "x", "B", "A", "C")[index % 5]
        lasers.append(Laser(2 * size, 2 * size - 1, -1, -1))
    puzzle = Puzzle(tuple(tuple(row) for row in grid),
                    {"A": a_count, "B": 0, "C": c_count}, tuple(lasers),
                    frozenset({(source.x, source.y)}))
    trace = simulate(puzzle, placement)
    unplaced = simulate(puzzle)
    source_points = {(ray.x, ray.y) for ray in lasers}
    candidates = set(trace.visited_points) - source_points
    distance = lambda point: abs(point[0] - source.x) + abs(point[1] - source.y)
    ordered = sorted(candidates, key=lambda point: (-distance(point), point))
    targets = set()
    # Select far points from each side so the anchor's split is exercised.
    for predicate in (lambda p: p[0] < source.x, lambda p: p[0] > source.x):
        side = [point for point in ordered if predicate(point)]
        if side:
            targets.add(side[0])
    novel = [point for point in ordered if point not in unplaced.visited_points]
    if novel:
        targets.add(novel[0])
    target_limit = min(6, max(2, size // 8))
    for point in ordered:
        if len(targets) >= target_limit:
            break
        targets.add(point)
    if not targets:
        raise RuntimeError("Fixture construction failed to produce a non-source target.")
    core = replace(puzzle, targets=frozenset(targets))
    verified = simulate(core, placement)
    if not verified.is_solution:
        raise RuntimeError("Planted witness did not independently verify.")
    if family == "parity_unsat":
        # Diagonal half-grid motion preserves odd x+y, so this center cannot
        # be visited under ANY placement. This is an easy analytic control.
        targets.add((2 * center + 1, 2 * center + 1))
    puzzle = replace(core, targets=frozenset(targets))
    # Segment order follows the tracer's breadth-first discovery order.
    point_depth = {point: 0 for point in source_points}
    for start, end in verified.segments:
        point_depth[end] = min(point_depth.get(end, math.inf),
                               point_depth.get(start, 0) + 1)
    split_points = set()
    reflection_segments = 0
    outgoing = {}
    for start, end in verified.segments:
        outgoing.setdefault(start, set()).add((end[0] - start[0], end[1] - start[1]))
    for point, directions in outgoing.items():
        if len(directions) > 1:
            split_points.add(point)
    # Count physical reflection witnesses by checking the planted layout and
    # incoming states, including sources. This is metadata, never a constraint.
    arrivals = {(ray.x, ray.y, ray.vx, ray.vy) for ray in lasers}
    arrivals.update((end[0], end[1], end[0] - start[0], end[1] - start[1])
                    for start, end in verified.segments)
    segments = set(verified.segments)
    for x, y, vx, vy in arrivals:
        col, row = ((x // 2 - (vx < 0), y // 2) if x % 2 == 0
                    else (x // 2, y // 2 - (vy < 0)))
        if not (0 <= col < size and 0 <= row < size
                and verified.layout.grid[row][col] in ("A", "C")):
            continue
        reflected = (-vx, vy) if x % 2 == 0 else (vx, -vy)
        if ((x, y), (x + reflected[0], y + reflected[1])) in segments:
            reflection_segments += 1
    witness = {
        "role": "satisfiable_core" if family == "parity_unsat" else "solution",
        "placements": [[x, y, kind] for (x, y), kind in sorted(placement.items())],
        "core_targets": [list(point) for point in sorted(core.targets)],
        "verified": True,
    }
    metadata = {
        "case_seed": case_seed,
        "expected_status": "no_solution" if family == "parity_unsat" else "solved",
        "proof": ("even-parity target cannot be reached by face-midpoint rays"
                  if family == "parity_unsat" else "independently simulated planted witness"),
        "difficulty_class": ("easy_unsat_control" if family == "parity_unsat"
                             else "planted_general" if novel else "planted_degenerate"),
        "target_count": len(puzzle.targets),
        "non_source_targets": len(core.targets - source_points),
        "targets_not_hit_without_placement": len(core.targets - unplaced.visited_points),
        "laser_count": len(lasers),
        "fixed_or_forbidden_cells": sum(cell != "o" for row in grid for cell in row),
        "witness_states": verified.states_processed,
        "witness_segments": len(verified.segments),
        "witness_max_point_depth": max(point_depth.values(), default=0),
        "witness_branch_points": len(split_points),
        "witness_reflection_transitions": reflection_segments,
        "witness_repeated_states": verified.repeated_states,
    }
    return puzzle, witness, metadata


def generate_suite(destination: str | Path, *, sizes=REPRESENTATIVE_SIZES,
                   inventories=tuple(range(4, 11)), families=FAMILIES,
                   seed: int = DEFAULT_SEED) -> dict:
    """Materialize a Cartesian suite; impossible inventory pairs are recorded."""
    sizes, inventories, families = tuple(sizes), tuple(inventories), tuple(families)
    if (not sizes or not inventories or not families
            or any(type(n) is not int or not 4 <= n <= 44 for n in sizes)
            or any(type(n) is not int or not 4 <= n <= 10 for n in inventories)
            or any(f not in FAMILIES for f in families)
            or any(len(set(values)) != len(values) for values in (sizes, inventories, families))):
        raise ValueError("Use distinct valid sizes, inventory values, and families.")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "boards").mkdir()
    (destination / "witnesses").mkdir()
    cases, skipped = [], []
    for size in sizes:
        for a_count in inventories:
            for c_count in inventories:
                if a_count + c_count > size * size:
                    skipped.append({"size": size, "A": a_count, "C": c_count,
                                    "families": list(families),
                                    "reason": "A+C exceeds available square-board cells"})
                    continue
                for family in families:
                    case_id = f"n{size:02d}_a{a_count:02d}_c{c_count:02d}_{family}"
                    puzzle, witness, metadata = make_case(size, a_count, c_count, family, seed=seed)
                    board_path = Path("boards") / (case_id + ".bff")
                    witness_path = Path("witnesses") / (case_id + ".json")
                    data = _bff(puzzle).encode("utf-8")
                    (destination / board_path).write_bytes(data)
                    witness["input_sha256"] = _digest(data)
                    _json_write(destination / witness_path, witness)
                    cases.append({"id": case_id, "size": size, "A": a_count,
                                  "C": c_count, "family": family,
                                  "board": board_path.as_posix(),
                                  "witness": witness_path.as_posix(),
                                  "input_sha256": _digest(data), **metadata})
    manifest = {
        "schema_version": SCHEMA_VERSION, "seed": seed, "sizes": list(sizes),
        "A_values": list(inventories), "C_values": list(inventories),
        "families": list(families), "case_count": len(cases),
        "requested_inventory_pairs": len(sizes) * len(inventories) ** 2,
        "skipped_inventory_pairs": skipped,
        "scope": ("Synthetic planted cases, plus easy parity UNSAT controls. "
                  "Witnesses establish SAT only; no claim about uniform difficulty, "
                  "hard UNSAT, official puzzles, or guaranteed solving time."),
        "cases": cases,
    }
    _json_write(destination / "manifest.json", manifest)
    return manifest


def _code_hashes() -> dict:
    root = Path(__file__).parent
    return {path.name: _digest(path.read_bytes()) for path in sorted(root.glob("*.py"))}


def _worker(board: Path, method: str, timeout: float) -> dict:
    """Read only the puzzle; neither the witness nor manifest enters a solver."""
    started = perf_counter()
    record = {"status": "error", "solution_verified": False}
    try:
        puzzle = load_bff(board)
        original = deepcopy(puzzle)
        if method == "smt":
            from .solver import solve_constraints
            solve = solve_constraints
        elif method == "search":
            from .search import solve_search
            solve = solve_search
        elif method == "dfs":
            from .dfs_baseline import solve_dfs
            solve = solve_dfs
        else:
            raise ValueError(f"Unknown method: {method}")
        result = solve(puzzle, timeout=timeout)
        if puzzle != original:
            raise RuntimeError("Solver mutated its input puzzle.")
        record.update(status=result.status, reason=result.reason,
                      algorithm_seconds=result.elapsed_seconds)
        for name in ("build_seconds", "solver_seconds", "verification_seconds",
                     "state_count", "transition_count", "variable_count", "constraint_count",
                     "solver_checks", "solver_version", "candidates_tested", "states_expanded",
                     "ray_states_processed", "pruned_nodes", "reachability_prunes",
                     "inventory_prunes", "branches_pruned", "optimistic_checks",
                     "forced_assignments", "restarts", "assignment_writes",
                     "implicit_cell_reads", "opening_strategy", "optimistic_period",
                     "bitboard_enabled", "optimistic_batches", "bitboard_setup_seconds",
                     "bitboard_cache_hit"):
            if hasattr(result, name):
                record[name] = getattr(result, name)
        if result.solved:
            if result.solution is None:
                raise RuntimeError("Claimed solution has no placement.")
            verify_started = perf_counter()
            replay = simulate(original, result.solution.layout.placements)
            if not replay.is_solution:
                raise RuntimeError("Independent solution replay failed.")
            record["solution_verified"] = True
            record["external_verification_seconds"] = perf_counter() - verify_started
    except Exception as exc:
        record.update(status="error", error=f"{type(exc).__name__}: {exc}")
    record["elapsed_seconds"] = perf_counter() - started
    return record


def _select_cases(manifest: dict, profile: str, sizes=None, limit=None) -> list[dict]:
    cases = manifest["cases"]
    if sizes is not None:
        cases = [case for case in cases if case["size"] in sizes]
    if profile == "smoke":
        available = sorted({case["size"] for case in cases})
        selected_sizes = {available[i] for i in (0, len(available) // 2, -1)} if available else set()
        inventory = manifest["A_values"]
        selected_counts = {min(inventory), max(inventory)}
        cases = [case for case in cases if case["size"] in selected_sizes
                 and case["A"] == case["C"] and case["A"] in selected_counts]
    return cases[:limit] if limit is not None else cases


def _summary(records: list[dict], methods) -> dict:
    result = {}
    for method in methods:
        rows = [row for row in records if row["method"] == method]
        completed = [row["elapsed_seconds"] for row in rows if row.get("correct_decision")]
        timed = [row["elapsed_seconds"] for row in rows if row["elapsed_seconds"] is not None]
        wall = [row["process_wall_seconds"] for row in rows]
        result[method] = {
            "attempted": len(rows), "status_counts": dict(Counter(row["status"] for row in rows)),
            "correct_decisions": sum(row.get("correct_decision", False) for row in rows),
            "wrong_decisions": sum(row.get("wrong_decision", False) for row in rows),
            "median_correct_decision_seconds": statistics.median(completed) if completed else None,
            "median_all_timed_attempts_seconds": statistics.median(timed) if timed else None,
            "timed_attempts": len(timed),
            "median_process_wall_seconds": statistics.median(wall) if wall else None,
            "note": "Correct-decision median excludes timeouts; all-timed median includes timeouts with worker timing. Process-wall median includes every attempt, including killed workers.",
        }
    return result


def run_suite(manifest_path: str | Path, output: str | Path, *, methods=("smt", "search"),
              timeout: float = 5.0, repeats: int = 1, profile: str = "smoke",
              sizes=None, limit: int | None = None, resume: bool = False) -> dict:
    """Run sequential fresh processes, checkpointing each completed trial."""
    if (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0
            or type(repeats) is not int or repeats < 1
            or profile not in ("smoke", "full") or not methods
            or any(method not in METHODS for method in methods)
            or len(set(methods)) != len(methods)
            or (limit is not None and (type(limit) is not int or limit < 1))):
        raise ValueError("Invalid run parameters.")
    manifest_path, output = Path(manifest_path).resolve(), Path(output).resolve()
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported manifest schema.")
    cases = _select_cases(manifest, profile, sizes, limit)
    if not cases:
        raise ValueError("No cases selected.")
    input_paths = {(manifest_path.parent / case["board"]).resolve() for case in cases}
    protected_paths = input_paths | {manifest_path}
    protected_paths.update((manifest_path.parent / case["witness"]).resolve() for case in manifest["cases"])
    if output in protected_paths or output.suffix.lower() != ".json":
        raise ValueError("Output must be a separate .json report, not a suite input.")
    # Verify inputs before any report mutation, also when resuming.
    for case in cases:
        board = manifest_path.parent / case["board"]
        if _digest(board.read_bytes()) != case["input_sha256"]:
            raise ValueError(f"Input changed: {case['id']}")
    configuration = {
        "manifest_sha256": _digest(manifest_bytes), "code_sha256": _code_hashes(),
        "python": sys.version, "platform": platform.platform(),
        "methods": list(methods), "timeout_seconds": timeout, "repeats": repeats,
        "profile": profile, "selected_case_ids": [case["id"] for case in cases],
    }
    if output.exists():
        if not resume:
            raise ValueError("Report exists; use --resume to continue its identical configuration.")
        report = json.loads(output.read_text(encoding="utf-8"))
        if report.get("configuration") != configuration:
            raise ValueError("Resume configuration, code, or inputs differ; use a new report.")
    else:
        report = {"schema_version": SCHEMA_VERSION, "configuration": configuration,
                  "manifest": str(manifest_path), "completed": False, "results": [],
                  "timing_scope": "Input reading, solver and independent replay; process startup excluded from elapsed_seconds and included in process_wall_seconds.",
                  "hard_process_limit_seconds": timeout + 2.0,
                  "scope": manifest["scope"]}
    done = {(row["case_id"], row["method"], row["trial"]) for row in report["results"]}
    _json_write(output, report)
    with tempfile.TemporaryDirectory(prefix="lazor_trials_", dir=output.parent) as temporary:
        result_path = Path(temporary) / "trial.json"
        for case in cases:
            for trial in range(1, repeats + 1):
                order = list(methods) if trial % 2 else list(reversed(methods))
                for method in order:
                    if (case["id"], method, trial) in done:
                        continue
                    print(f"{case['id']} {method} trial {trial}/{repeats}", flush=True)
                    board = manifest_path.parent / case["board"]
                    command = [sys.executable, "-m", "lazor.scaling", "worker", str(board),
                               "--method", method, "--timeout", str(timeout), "--output", str(result_path)]
                    process_started = perf_counter()
                    try:
                        process = subprocess.run(command, capture_output=True, text=True,
                                                 timeout=timeout + 2.0)
                        if process.returncode:
                            raise RuntimeError(process.stderr[-2000:] or "Worker exited unsuccessfully.")
                        record = json.loads(result_path.read_text(encoding="utf-8"))
                    except subprocess.TimeoutExpired:
                        record = {"status": "timeout", "hard_timeout": True,
                                  "elapsed_seconds": None, "solution_verified": False,
                                  "reason": "Parent terminated worker at wall-time safety limit."}
                    except (OSError, ValueError, RuntimeError) as exc:
                        record = {"status": "error", "error": str(exc),
                                  "elapsed_seconds": None, "solution_verified": False}
                    record.update(case_id=case["id"], method=method, trial=trial,
                                  size=case["size"], A=case["A"], C=case["C"], family=case["family"],
                                  expected_status=case["expected_status"],
                                  input_sha256=case["input_sha256"],
                                  process_wall_seconds=perf_counter() - process_started)
                    record["correct_decision"] = (record["status"] == case["expected_status"]
                        and (record["status"] != "solved" or record["solution_verified"]))
                    record["wrong_decision"] = (record["status"] in ("solved", "no_solution")
                        and not record["correct_decision"])
                    report["results"].append(record)
                    report["summary"] = _summary(report["results"], methods)
                    _json_write(output, report)
    report["completed"] = True
    report["summary"] = _summary(report["results"], methods)
    _json_write(output, report)
    return report


def _numbers(text: str) -> tuple[int, ...]:
    try:
        return tuple(int(value) for value in text.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use comma-separated integers.") from exc


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate", help="Write reproducible fixture files and manifest")
    generate.add_argument("--output-dir", type=Path, required=True)
    group = generate.add_mutually_exclusive_group()
    group.add_argument("--sizes", type=_numbers, default=REPRESENTATIVE_SIZES)
    group.add_argument("--all-sizes", action="store_true", help="Every square size 4 through 44")
    generate.add_argument("--inventory", type=_numbers, default=tuple(range(4, 11)))
    generate.add_argument("--families", nargs="+", choices=FAMILIES, default=FAMILIES)
    generate.add_argument("--seed", type=int, default=DEFAULT_SEED)
    run = commands.add_parser("run", help="Run isolated trials with durable checkpoints")
    run.add_argument("manifest", type=Path)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--methods", nargs="+", choices=METHODS, default=("smt", "search"))
    run.add_argument("--timeout", type=float, default=5.0)
    run.add_argument("--repeats", type=int, default=1)
    run.add_argument("--profile", choices=("smoke", "full"), default="smoke")
    run.add_argument("--sizes", type=_numbers)
    run.add_argument("--limit", type=int)
    run.add_argument("--resume", action="store_true")
    worker = commands.add_parser("worker", help=argparse.SUPPRESS)
    worker.add_argument("board", type=Path)
    worker.add_argument("--method", choices=METHODS, required=True)
    worker.add_argument("--timeout", type=float, required=True)
    worker.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            report = generate_suite(args.output_dir, sizes=range(4, 45) if args.all_sizes else args.sizes,
                                    inventories=args.inventory, families=args.families, seed=args.seed)
            print(f"Generated {report['case_count']} cases; skipped {len(report['skipped_inventory_pairs'])} impossible inventory pairs.")
            print(f"Manifest: {(args.output_dir / 'manifest.json').resolve()}")
        elif args.command == "worker":
            _json_write(args.output, _worker(args.board, args.method, args.timeout))
        else:
            report = run_suite(args.manifest, args.output, methods=args.methods, timeout=args.timeout,
                               repeats=args.repeats, profile=args.profile, sizes=args.sizes,
                               limit=args.limit, resume=args.resume)
            print(json.dumps(report["summary"], indent=2))
            return int(any(row["status"] == "error" or row.get("wrong_decision") for row in report["results"]))
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(2, f"Error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
