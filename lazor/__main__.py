"""Inspect, simulate, or solve with SMT constraints or exact pruned search."""

import argparse
from pathlib import Path

from .bff import load_bff
from .models import Puzzle
from .output import format_report, write_bff, write_report, write_svg
from .simulation import simulate
from .search import SearchResult, configure_search_database, solve_search
from .solver import PlanningResult, solve_constraints


def _placement(text: str) -> tuple[tuple[int, int], str]:
    """Parse KIND:COLUMN,ROW using whole-cell grid indices."""
    try:
        kind, coordinates = text.split(":")
        column, row = (int(value) for value in coordinates.split(","))
        kind = kind.upper()
        if kind not in ("A", "B", "C"):
            raise ValueError
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "Use A:0,1, B:0,1, or C:0,1 (zero-based column,row)."
        ) from exc
    return (column, row), kind


def _print_input(puzzle: Puzzle) -> None:
    """Display input data without tracing or changing the board."""
    print(f"Board: {puzzle.width} columns x {puzzle.height} rows")
    for row in puzzle.grid:
        print(" ".join(row))
    inventory = ", ".join(
        f"{kind}={count}" for kind, count in puzzle.block_counts.items()
    )
    print(f"Movable blocks: {inventory}")
    print(f"Placeable cells: {len(puzzle.placeable_positions)}")
    print(f"Fixed blocks: {len(puzzle.fixed_blocks)}")
    for index, laser in enumerate(puzzle.lasers, 1):
        print(
            f"Laser {index}: position=({laser.x}, {laser.y}), "
            f"direction=({laser.vx}, {laser.vy})"
        )
    print(f"Targets: {sorted(puzzle.targets)}")
    print("Input validated. Use --simulate to trace this layout.")


def _check_output_paths(args: argparse.Namespace) -> None:
    """Reject input overwrite, output collisions, and accidental overwrite."""
    source = Path(args.board).resolve()
    outputs: set[Path] = set()
    for filename in (args.output, args.svg, args.save_board, args.model):
        if filename is None:
            continue
        path = Path(filename).resolve()
        if path == source or (path.exists() and path.samefile(source)):
            raise ValueError("Output must not overwrite the input .bff file.")
        same_existing_file = path.exists() and any(
            other.exists() and path.samefile(other) for other in outputs
        )
        if path in outputs or same_existing_file:
            raise ValueError("Each output must have a different file path.")
        if any(
            path in other.parents or other in path.parents
            for other in outputs
        ):
            raise ValueError("An output cannot be another output's folder.")
        if path.exists() and (not args.force or path.is_dir()):
            raise ValueError(
                f"Output already exists: {path}. Use --force for files."
            )
        outputs.add(path)


def _search_summary(search: PlanningResult | SearchResult) -> str:
    """Describe planner outcomes without treating unknown as infeasible."""
    if isinstance(search, SearchResult):
        return "\n".join([
            "LAZOR PRUNED SEARCH", f"Status: {search.status.upper()}",
            f"Search time: {search.elapsed_seconds:.6f} seconds",
            f"Search nodes: {search.states_expanded}",
            f"Branches pruned: {search.branches_pruned}",
            f"Reason: {search.reason}", "",
        ])
    reasons = {
        "no_solution": "The state-transition constraints are unsatisfiable.",
        "timeout": "Time limit reached; solvability is still unknown.",
        "unknown": "The constraint solver could not determine feasibility.",
    }
    return "\n".join([
        "LAZOR STATE-TRANSITION PLANNING",
        f"Status: {search.status.upper()}",
        reasons[search.status],
        f"Planning time: {search.elapsed_seconds:.6f} seconds",
        f"Model states: {search.state_count}",
        f"Model transitions: {search.transition_count}",
        f"Model constraints: {search.constraint_count}",
        f"Solver checks: {search.solver_checks}",
        f"Reason: {search.reason}",
        "",
    ])


def main(argv: list[str] | None = None) -> int:
    """Inspect or trace; exit 0 normally, 1 on failed check, 2 on error."""
    parser = argparse.ArgumentParser(
        description="Inspect, simulate, or solve Lazor with SMT or pruned search."
    )
    parser.add_argument("board", help="Path to a UTF-8 .bff file")
    parser.add_argument(
        "--simulate", action="store_true", help="Trace the supplied layout"
    )
    parser.add_argument(
        "--solve", action="store_true",
        help="Solve state-transition constraints",
    )
    parser.add_argument(
        "--algorithm", choices=("smt", "search"), default=None,
        help="Solve with SMT (default) or exact ray-frontier search",
    )
    parser.add_argument(
        "--timeout", type=float, default=110.0, metavar="SECONDS",
        help="Planning time budget (default: 110 seconds)",
    )
    database_options = parser.add_mutually_exclusive_group()
    database_options.add_argument(
        "--table-db", metavar="PATH",
        help="Read precomputed optical tables from this SQLite file; requires --algorithm search",
    )
    database_options.add_argument(
        "--no-table-db", action="store_true",
        help="Compute optical tables without a database; requires --algorithm search",
    )
    parser.add_argument(
        "--model", metavar="PATH",
        help="Export the SMT-LIB constraint model; requires --solve",
    )
    parser.add_argument(
        "--place", type=_placement, action="append", default=[],
        metavar="KIND:COL,ROW", help="Place a movable block; repeat as needed",
    )
    parser.add_argument("--output", help="Write a readable text report")
    parser.add_argument("--svg", help="Write a board and ray-path SVG")
    parser.add_argument(
        "--save-board", help="Write a replayable .bff layout snapshot"
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Simulate; exit 1 unless all inventory is placed and targets hit",
    )
    parser.add_argument(
        "--force", action="store_true", help="Allow replacing output files"
    )
    args = parser.parse_args(argv)
    if args.solve and (args.simulate or args.place):
        parser.error("--solve cannot be combined with --simulate or --place.")
    if args.model and not args.solve:
        parser.error("--model requires --solve.")
    if args.algorithm is not None and not args.solve:
        parser.error("--algorithm requires --solve.")
    if args.model and args.algorithm == "search":
        parser.error("--model requires --algorithm smt.")
    if (args.table_db or args.no_table_db) and (not args.solve or args.algorithm != "search"):
        parser.error("--table-db/--no-table-db requires --solve --algorithm search.")

    try:
        puzzle = load_bff(args.board)
        placements = {}
        for position, kind in args.place:
            if position in placements:
                raise ValueError(f"Duplicate placement at {position}.")
            placements[position] = kind
        _check_output_paths(args)
        should_simulate = any((
            args.simulate, args.place, args.output, args.svg,
            args.save_board, args.check, args.solve,
        ))
        if not should_simulate:
            _print_input(puzzle)
            return 0

        search = None
        if args.solve:
            if args.algorithm == "search":
                if args.no_table_db:
                    configure_search_database(None)
                elif args.table_db:
                    configure_search_database(args.table_db)
                search = solve_search(puzzle, timeout=args.timeout)
            else:
                search = solve_constraints(
                    puzzle, timeout=args.timeout, model_path=args.model
                )
            if not search.solved:
                summary = _search_summary(search)
                print(summary, end="")
                if args.output is not None:
                    path = Path(args.output)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(summary, encoding="utf-8")
                    print(f"Saved: {path}")
                return 1
            result = search.solution
        else:
            result = simulate(puzzle, placements)
        print(format_report(result, search=search), end="")
        for path, writer in (
            (args.output, write_report),
            (args.svg, write_svg),
            (args.save_board, write_bff),
        ):
            if path is not None:
                written = writer(result, path, search=search)
                print(f"Saved: {written}")
        if args.check and not result.is_solution:
            return 1
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(2, f"Error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

