"""Time constraint planning and independently verify the returned layouts."""

import argparse
import json
import math
import platform
import sys
from copy import deepcopy
from pathlib import Path
from time import perf_counter

from .bff import load_bff
from .simulation import simulate
from .solver import solve_constraints


def collect_boards(source: str | Path) -> list[Path]:
    """Return sorted BFF files from one directory or one explicit BFF file."""
    path = Path(source)
    if path.is_dir():
        boards = sorted(
            (item for item in path.iterdir()
             if item.is_file() and item.suffix.lower() == ".bff"),
            key=lambda item: item.name.lower(),
        )
        if not boards:
            raise ValueError(f"No .bff files found in {path}.")
        return boards
    if not path.is_file():
        raise FileNotFoundError(f"Board source does not exist: {path}")
    if path.suffix.lower() != ".bff":
        raise ValueError("An explicit board file must have a .bff suffix.")
    return [path]


def benchmark_files(
    paths: list[Path], *, timeout: float = 110.0,
) -> dict:
    """Benchmark exactly these files; no claim about absent course boards.

    Per-board wall time includes reading, planning, and verification. Image
    export and process startup are excluded. Verification reruns the returned
    placement against a separate copy of the original puzzle.
    """
    if not paths:
        raise ValueError("At least one board file is required.")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout < 0
    ):
        raise ValueError("Timeout must be a finite nonnegative number.")

    records = []
    for filename in paths:
        path = Path(filename)
        record = {
            "board": path.name,
            "path": str(path.resolve()),
            "status": "error",
            "error": None,
            "elapsed_seconds": 0.0,
            "planning_seconds": None,
            "build_seconds": None,
            "solver_seconds": None,
            "verification_seconds": None,
            "state_count": 0,
            "transition_count": 0,
            "variable_count": 0,
            "constraint_count": 0,
            "solver_checks": 0,
            "solver_version": None,
            "reason": "",
            "target_count": None,
            "hit_count": 0,
            "solution_verified": False,
        }
        started = perf_counter()
        try:
            puzzle = load_bff(path)
            original = deepcopy(puzzle)
            record["target_count"] = len(original.targets)
            search = solve_constraints(puzzle, timeout=timeout)
            record.update({
                "status": search.status,
                "planning_seconds": search.elapsed_seconds,
                "build_seconds": search.build_seconds,
                "solver_seconds": search.solver_seconds,
                "verification_seconds": search.verification_seconds,
                "state_count": search.state_count,
                "transition_count": search.transition_count,
                "variable_count": search.variable_count,
                "constraint_count": search.constraint_count,
                "solver_checks": search.solver_checks,
                "solver_version": search.solver_version,
                "reason": search.reason,
            })
            if puzzle != original:
                raise ValueError("Solver changed the original puzzle.")
            if search.solved:
                solution = search.solution
                if solution is None:
                    raise ValueError("Solved result has no solution data.")
                replay = simulate(original, solution.layout.placements)
                if (
                    not solution.is_solution
                    or not replay.is_solution
                    or solution.layout.grid != replay.layout.grid
                    or solution.hit_targets != replay.hit_targets
                    or set(solution.segments) != set(replay.segments)
                ):
                    raise ValueError("Returned solution failed verification.")
                record["hit_count"] = len(replay.hit_targets)
                record["solution_verified"] = True
        except (OSError, ValueError, RuntimeError) as exc:
            record["status"] = "error"
            record["error"] = f"{type(exc).__name__}: {exc}"
        elapsed = perf_counter() - started
        record["elapsed_seconds"] = elapsed
        for seconds in (5, 60, 120):
            record[f"under_{seconds}_seconds"] = elapsed < seconds
        record["passed"] = (
            record["status"] == "solved"
            and record["solution_verified"]
            and record["under_120_seconds"]
        )
        records.append(record)

    return {
        "algorithm": "SMT state-transition planning (Z3)",
        "scope": (
            "Only the listed input files were tested. This is not proof "
            "that all official or hidden course boards meet the time limits."
        ),
        "timing_scope": (
            "Per board: input reading, planning, and solution verification; "
            "excludes process startup and image/file export."
        ),
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "timeout_seconds": timeout,
        "board_count": len(records),
        "all_passed": all(record["passed"] for record in records),
        "results": records,
    }


def _timeout(text: str) -> float:
    """Accept finite nonnegative seconds as an argparse value."""
    try:
        value = float(text)
        if not math.isfinite(value) or value < 0:
            raise ValueError
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "Timeout must be finite and nonnegative."
        ) from exc
    return value


def main(argv: list[str] | None = None) -> int:
    """Write a JSON benchmark report; return 0 if every board passes."""
    parser = argparse.ArgumentParser(
        description="Benchmark constraint planning and verify BFF answers."
    )
    parser.add_argument("source", help="A .bff file or directory")
    parser.add_argument(
        "--output", required=True, help="Destination .json file"
    )
    parser.add_argument("--timeout", type=_timeout, default=110.0)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        boards = collect_boards(args.source)
        destination = Path(args.output).resolve()
        if destination.suffix.lower() != ".json":
            raise ValueError("Benchmark output must use a .json suffix.")
        if any(
            destination == board.resolve()
            or (destination.exists() and destination.samefile(board))
            for board in boards
        ):
            raise ValueError("Benchmark output must not replace an input.")
        if destination.exists() and (
            not args.force or not destination.is_file()
        ):
            raise ValueError("Output exists; use --force to replace a file.")
        report = benchmark_files(boards, timeout=args.timeout)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(2, f"Error: {exc}\n")

    for result in report["results"]:
        print(
            f"{result['board']}: {result['status']}, "
            f"{result['elapsed_seconds']:.6f} s, "
            f"verified={result['solution_verified']}"
        )
    print(f"Saved: {destination}")
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
