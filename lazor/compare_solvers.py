"""Reproducible timing of reconstructed DFS versus the current SMT planner.

Each trial runs in a fresh process, sequentially, against one frozen input.
The DFS baseline is not a recovered copy of the historical solver.
"""

import argparse
import hashlib
import json
import math
import platform
import statistics
import subprocess
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

from .bff import load_bff, parse_bff
from .output import write_bff, write_report, write_svg
from .simulation import simulate


def write_json(path, value):
    """Write a benchmark artifact in the already-created run directory."""
    Path(path).write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def run_trial(method, board, timeout, result_path, trial):
    """Time input reading, solving, and independent solution verification."""
    if method == "dfs":
        from .dfs_baseline import solve_dfs
    else:
        from .solver import solve_constraints

    def progress(record):
        print(json.dumps({
            "event": "progress", "method": method, "trial": trial,
            **record,
        }), flush=True)

    record = {"method": method, "trial": trial, "status": "error"}
    started = perf_counter()
    try:
        puzzle = load_bff(board)
        original = deepcopy(puzzle)
        if method == "dfs":
            plan = solve_dfs(puzzle, timeout=timeout, progress=progress)
            fields = (
                "candidates_tested", "states_expanded",
                "ray_states_processed",
            )
        else:
            plan = solve_constraints(puzzle, timeout=timeout)
            fields = (
                "build_seconds", "solver_seconds", "verification_seconds",
                "state_count", "transition_count", "variable_count",
                "constraint_count", "solver_version", "solver_checks",
            )
        record.update({field: getattr(plan, field) for field in fields})
        record.update({
            "status": plan.status, "reason": plan.reason,
            "solver_call_seconds": plan.elapsed_seconds,
            "target_count": len(original.targets),
            "solution_verified": False,
        })
        if puzzle != original:
            raise RuntimeError("The solver modified its input puzzle.")
        if plan.status == "solved":
            solution = plan.solution
            if solution is None:
                raise RuntimeError("Solved status without a solution.")
            replay = simulate(original, solution.layout.placements)
            if (not solution.is_solution or not replay.is_solution
                    or solution.layout.grid != replay.layout.grid
                    or set(solution.segments) != set(replay.segments)):
                raise RuntimeError("Independent solution verification failed.")
            record.update({
                "solution_verified": True,
                "hit_count": len(replay.hit_targets),
                "placements": [
                    {"column": column, "row": row, "kind": kind}
                    for (column, row), kind
                    in sorted(solution.layout.placements.items())
                ],
            })
        record["elapsed_seconds"] = perf_counter() - started
        # Export after stopping the clock, so rendering is not benchmarked.
        if plan.status == "solved" and trial == 1:
            stem = result_path.parent / f"{method}_solution"
            options = {"search": plan} if method == "smt" else {}
            write_report(solution, stem.with_suffix(".txt"), **options)
            write_svg(solution, stem.with_suffix(".svg"), **options)
            write_bff(solution, stem.with_suffix(".bff"), **options)
    except Exception as exc:
        record.update({
            "status": "error", "error": f"{type(exc).__name__}: {exc}",
            "elapsed_seconds": perf_counter() - started,
        })
    write_json(result_path, record)
    print(json.dumps({"event": "finished", **record}), flush=True)
    return 1 if record["status"] == "error" else 0


def summarize(records, timeout):
    """Keep censored timeout measurements separate from successful times."""
    summary = {}
    for method in ("dfs", "smt"):
        runs = [r for r in records if r["method"] == method]
        successes = [r for r in runs if r["status"] == "solved"
                     and r.get("solution_verified")]
        times = [r["elapsed_seconds"] for r in successes]
        summary[method] = {
            "trials": len(runs), "solved": len(successes),
            "timeouts": sum(r["status"] == "timeout" for r in runs),
            "successful_times_seconds": times,
            "median_success_seconds": statistics.median(times)
            if times else None,
        }
    dfs, smt = summary["dfs"], summary["smt"]
    if (dfs["trials"] and dfs["solved"] == dfs["trials"]
            and smt["trials"] and smt["solved"] == smt["trials"]):
        summary["median_time_ratio_dfs_over_smt"] = (
            dfs["median_success_seconds"] / smt["median_success_seconds"]
        )
    elif (dfs["trials"] and dfs["timeouts"] == dfs["trials"]
          and smt["trials"] and smt["solved"] == smt["trials"]):
        summary["conservative_time_ratio_lower_bound"] = (
            timeout / max(smt["successful_times_seconds"])
        )
        summary["ratio_note"] = (
            "DFS did not solve before the cutoff. This is a lower bound "
            "using the slowest measured successful SMT run, not an exact "
            "speedup or an extrapolated DFS completion time."
        )
    return summary


def compare(source, destination, timeout, repeats):
    """Alternate trial order while keeping CPU-intensive runs sequential."""
    data = source.read_bytes()
    puzzle = parse_bff(data.decode("utf-8-sig"))
    destination.mkdir(parents=True, exist_ok=False)
    snapshot = destination / "input.bff"
    snapshot.write_bytes(data)
    report_path = destination / "comparison.json"
    available = len(puzzle.placeable_positions)
    layouts = 1
    for kind in ("A", "B", "C"):
        count = puzzle.block_counts.get(kind, 0)
        layouts *= math.comb(available, count)
        available -= count
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": str(source.resolve()),
        "input_sha256": hashlib.sha256(data).hexdigest(),
        "input_snapshot": str(snapshot.resolve()),
        "width": puzzle.width, "height": puzzle.height,
        "movable_inventory": puzzle.block_counts,
        "placeable_cells": len(puzzle.placeable_positions),
        "laser_count": len(puzzle.lasers),
        "target_count": len(puzzle.targets),
        "distinct_complete_layouts": layouts,
        "python": sys.version, "platform": platform.platform(),
        "code_sha256": {
            name: hashlib.sha256(
                Path(__file__).with_name(name).read_bytes()
            ).hexdigest()
            for name in (
                "dfs_baseline.py", "solver.py", "simulation.py",
                "compare_solvers.py",
            )
        },
        "solver_timeout_seconds": timeout, "repetitions": repeats,
        "protocol": (
            "Sequential fresh processes; alternating method order; same "
            "frozen input. Timing includes input reading, solving (including "
            "Z3 loading/model construction), and independent replay. "
            "Process startup/common imports and artifact export excluded."
        ),
        "dfs_provenance": (
            "Reconstructed deduplicated combination DFS, not recovered "
            "historical source. A/B/C kind order, row-major cell order; "
            "complete layouts simulated; no SMT hints or optical pruning."
        ),
        "results": [], "completed": False,
    }
    write_json(report_path, report)
    for trial in range(1, repeats + 1):
        order = ("smt", "dfs") if trial % 2 else ("dfs", "smt")
        for method in order:
            result_path = destination / f"{method}_trial_{trial}.json"
            print(f"Starting {method}, trial {trial}/{repeats}", flush=True)
            command = [
                sys.executable, "-u", "-m", "lazor.compare_solvers",
                str(snapshot), "--worker", method,
                "--result", str(result_path), "--trial", str(trial),
                "--timeout", str(timeout),
            ]
            try:
                completed = subprocess.run(command, timeout=timeout + 20)
                if completed.returncode or not result_path.is_file():
                    raise RuntimeError(
                        f"Worker failed with exit code {completed.returncode}"
                    )
                record = json.loads(result_path.read_text(encoding="utf-8"))
            except (subprocess.TimeoutExpired, RuntimeError) as exc:
                record = {
                    "method": method, "trial": trial,
                    "status": "worker_error", "error": str(exc),
                }
            report["results"].append(record)
            report["summary"] = summarize(report["results"], timeout)
            write_json(report_path, report)
    report["completed"] = True
    report["source_unchanged"] = source.read_bytes() == data
    write_json(report_path, report)
    print(json.dumps(report["summary"], indent=2), flush=True)
    print(f"Report: {report_path.resolve()}", flush=True)
    return int(any(
        record["status"] in ("error", "worker_error")
        for record in report["results"]
    ))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--timeout", type=float, default=110.0)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--worker", choices=("dfs", "smt"))
    parser.add_argument("--result", type=Path)
    parser.add_argument("--trial", type=int, default=1)
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("timeout must be finite and positive")
    if args.repeats < 1:
        parser.error("repeats must be positive")
    if args.worker:
        if args.result is None:
            parser.error("worker requires --result")
        return run_trial(
            args.worker, args.source, args.timeout, args.result, args.trial
        )
    if args.output_dir is None:
        parser.error("--output-dir is required and must not exist yet")
    return compare(
        args.source, args.output_dir, args.timeout, args.repeats
    )


if __name__ == "__main__":
    raise SystemExit(main())
