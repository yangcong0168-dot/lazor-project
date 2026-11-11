"""Compare frozen solver versions on a reproducible reference/holdout panel."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
import json
import math
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
from time import perf_counter

from .bff import load_bff, parse_bff
from .scaling import FAMILIES, _bff, _digest, _json_write, make_case
from .simulation import simulate

SCHEMA_VERSION = 1
DEFAULT_HOLDOUT_SEED = 20261112
PACKAGE_ROOT = Path(__file__).resolve().parents[1]

# Bootstrapping with -c imports no lazor package until the selected version's
# path is first in sys.path. A common dependency path supplies the same Z3.
WORKER = r'''
import dataclasses, json, pathlib, sys, time
from copy import deepcopy
root, dependencies, board, method, cutoff, destination = sys.argv[1:]
sys.path.insert(0, root)
sys.path.append(dependencies)
started = time.perf_counter()
record = {"status": "error", "solution_verified": False, "elapsed_seconds": None}
try:
    from lazor.bff import load_bff
    puzzle = load_bff(board)
    original = deepcopy(puzzle)
    if method == "search":
        from lazor.search import solve_search as solve
    else:
        from lazor.solver import solve_constraints as solve
    result = solve(puzzle, timeout=float(cutoff))
    if puzzle != original:
        raise RuntimeError("Solver mutated its input.")
    for field in dataclasses.fields(result):
        value = getattr(result, field.name)
        if value is None or type(value) in (str, int, float, bool):
            record[field.name] = value
    if result.solved:
        if result.solution is None:
            raise RuntimeError("Solved result has no placement.")
        record["placement"] = [[x,y,kind] for (x,y),kind in
                               sorted(result.solution.layout.placements.items())]
except Exception as exc:
    record.update(status="error", error=type(exc).__name__ + ": " + str(exc))
record["worker_elapsed_seconds"] = time.perf_counter() - started
pathlib.Path(destination).write_text(json.dumps(record), encoding="utf-8")
'''


def reference_specs() -> list[tuple[int, int, int, str]]:
    """Two cases at each size; all 49 independent A/C pairs appear."""
    specs = []
    for size_index, size in enumerate(range(4, 45)):
        for slot in range(2):
            index = 2 * size_index + slot
            pair = index % 49
            specs.append((size, 4 + pair // 7, 4 + pair % 7, FAMILIES[index % 3]))
    return specs


def holdout_specs() -> list[tuple[int, int, int, str]]:
    """One fresh-seed case for each inventory pair, covering every size."""
    specs = []
    for pair in range(49):
        size = 4 + (pair * 17) % 41
        a_count, c_count = 4 + pair // 7, 4 + pair % 7
        if a_count + c_count > size * size:
            size = 5
        specs.append((size, a_count, c_count, FAMILIES[(pair + 1) % 3]))
    return specs


def _verify_witness(data: bytes, witness: dict, expected: str) -> None:
    puzzle = parse_bff(data.decode("utf-8"))
    placement = {(x, y): kind for x, y, kind in witness["placements"]}
    core = replace(puzzle, targets=frozenset(map(tuple, witness["core_targets"])))
    if not simulate(core, placement).is_solution:
        raise ValueError("Panel witness failed independent simulation.")
    if expected == "solved" and not simulate(puzzle, placement).is_solution:
        raise ValueError("SAT fixture does not match its witness.")
    if expected == "no_solution" and not any((x + y) % 2 == 0 for x, y in puzzle.targets):
        raise ValueError("UNSAT control lacks its parity certificate.")


def prepare_panel(source: str | Path, destination: str | Path, *,
                  holdout_seed: int = DEFAULT_HOLDOUT_SEED) -> dict:
    """Copy a balanced reference panel and generate new-seed held-out inputs."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    source_data = source.read_bytes()
    source_manifest = json.loads(source_data)
    if holdout_seed == source_manifest["seed"]:
        raise ValueError("Holdout seed must differ from the reference seed.")
    index = {(c["size"], c["A"], c["C"], c["family"]): c
             for c in source_manifest["cases"]}
    if any(spec not in index for spec in reference_specs()):
        raise ValueError("Reference manifest does not contain the required balanced panel.")
    source_hashes = {case["input_sha256"] for case in source_manifest["cases"]}
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "boards").mkdir()
    (destination / "witnesses").mkdir()
    cases = []

    def save(case, data, witness, split):
        _verify_witness(data, witness, case["expected_status"])
        case_id = split + "_" + case["id"]
        board_path = Path("boards") / (case_id + ".bff")
        witness_path = Path("witnesses") / (case_id + ".json")
        (destination / board_path).write_bytes(data)
        _json_write(destination / witness_path, witness)
        cases.append({**case, "id": case_id, "split": split,
                      "board": board_path.as_posix(), "witness": witness_path.as_posix(),
                      "input_sha256": _digest(data)})

    for spec in reference_specs():
        case = index[spec]
        data = (source.parent / case["board"]).read_bytes()
        if _digest(data) != case["input_sha256"]:
            raise ValueError(f"Reference input changed: {case['id']}")
        witness = json.loads((source.parent / case["witness"]).read_text(encoding="utf-8"))
        save(case, data, witness, "reference")
    used_hashes = set(source_hashes)
    for size, a_count, c_count, family in holdout_specs():
        for retry in range(100):
            effective_seed = holdout_seed + retry
            puzzle, witness, metadata = make_case(size, a_count, c_count, family,
                                                  seed=effective_seed)
            data = _bff(puzzle).encode("utf-8")
            if _digest(data) not in used_hashes:
                break
        else:
            raise RuntimeError("Could not create a distinct held-out input.")
        used_hashes.add(_digest(data))
        witness["input_sha256"] = _digest(data)
        case = {"id": f"n{size:02d}_a{a_count:02d}_c{c_count:02d}_{family}",
                "size": size, "A": a_count, "C": c_count, "family": family,
                "generator_seed": effective_seed, **metadata}
        save(case, data, witness, "holdout")
    manifest = {
        "schema_version": SCHEMA_VERSION, "source_manifest_sha256": _digest(source_data),
        "source_seed": source_manifest["seed"], "holdout_seed": holdout_seed,
        "case_count": len(cases), "split_counts": dict(Counter(c["split"] for c in cases)),
        "scope": ("131 synthetic cases: balanced dimensional coverage, not the full 5997-case cross product. "
                  "Held-out seed changes layouts; generator family is shared. Parity controls are easy UNSAT, "
                  "not evidence about hard infeasible instances. Once used for tuning, holdout is no longer unseen."),
        "selection": "Reference: 2 per size 4..44 cycling all49 inventory pairs. Holdout:1 per pair, balanced41 sizes.",
        "cases": cases,
    }
    _json_write(destination / "manifest.json", manifest)
    return manifest


def _package_hashes(root: Path) -> dict:
    package = root / "lazor"
    if not (package / "__init__.py").is_file():
        raise ValueError(f"No lazor package under {root}")
    return {path.name: _digest(path.read_bytes()) for path in sorted(package.glob("*.py"))}


def _score(row: dict, timeout: float) -> float:
    return row["elapsed_seconds"] if row.get("timely_correct") else 2 * timeout


def summarize(records: list[dict], timeout: float, methods) -> dict:
    """Keep censoring and invalid answers visible; compare only matched trials."""
    def group(rows):
        result = {}
        for method in methods:
            method_rows = [r for r in rows if r["method"] == method]
            variants = {}
            for version in ("baseline", "current"):
                runs = [r for r in method_rows if r["version"] == version]
                scores = [_score(r, timeout) for r in runs]
                variants[version] = {
                    "attempted": len(runs), "status_counts": dict(Counter(r["status"] for r in runs)),
                    "correct_decisions": sum(r.get("correct_decision", False) for r in runs),
                    "timely_correct_decisions": sum(r.get("timely_correct", False) for r in runs),
                    "wrong_decisions": sum(r.get("wrong_decision", False) for r in runs),
                    "par2_seconds": statistics.mean(scores) if scores else None,
                    "process_wall_seconds": sum(r.get("process_wall_seconds", 0) for r in runs),
                    "valid": not any(r.get("wrong_decision") or r["status"] == "error" for r in runs),
                }
            pairs = {}
            for row in method_rows:
                pairs.setdefault((row["case_id"], row["trial"]), {})[row["version"]] = row
            ratios = []
            for pair in pairs.values():
                if set(pair) == {"baseline", "current"} and all(r.get("timely_correct") for r in pair.values()):
                    denominator = pair["current"]["elapsed_seconds"]
                    if denominator > 0:
                        ratios.append(pair["baseline"]["elapsed_seconds"] / denominator)
            variants["matched_completions"] = {
                "pairs": len(ratios),
                "median_baseline_over_current": statistics.median(ratios) if ratios else None,
                "geometric_mean_baseline_over_current": statistics.geometric_mean(ratios) if ratios and min(ratios) > 0 else None,
                "note": "Only same-case, same-trial timely correct completions. Ratios>1 favor current; excludes censored runs.",
            }
            result[method] = variants
        return result

    return {"overall": group(records),
            "by_split": {key: group([r for r in records if r["split"] == key])
                         for key in sorted({r["split"] for r in records})},
            "by_family": {key: group([r for r in records if r["family"] == key])
                          for key in sorted({r["family"] for r in records})},
            "by_expected_status": {key: group([r for r in records if r["expected_status"] == key])
                                   for key in sorted({r["expected_status"] for r in records})}}


def compare_versions(manifest_path: str | Path, output: str | Path, *,
                     baseline_root: str | Path, current_root: str | Path = PACKAGE_ROOT,
                     methods=("search",), timeout: float = 0.3, repeats: int = 1,
                     split: str = "all", limit: int | None = None, resume: bool = False) -> dict:
    if (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0
            or type(repeats) is not int or repeats < 1 or not methods
            or len(set(methods)) != len(methods) or any(m not in ("smt", "search") for m in methods)
            or split not in ("all", "reference", "holdout")
            or (limit is not None and (type(limit) is not int or limit < 1))):
        raise ValueError("Invalid comparison parameters.")
    manifest_path, output = Path(manifest_path).resolve(), Path(output).resolve()
    data = manifest_path.read_bytes()
    manifest = json.loads(data)
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported panel schema.")
    cases = [case for case in manifest["cases"] if split == "all" or case["split"] == split]
    if limit is not None:
        cases = cases[:limit]
    if not cases:
        raise ValueError("No cases selected.")
    protected = {manifest_path}
    protected.update((manifest_path.parent / case[key]).resolve()
                     for case in manifest["cases"] for key in ("board", "witness"))
    if output.suffix.lower() != ".json" or output in protected:
        raise ValueError("Use a separate .json output report.")
    roots = {"baseline": Path(baseline_root).resolve(), "current": Path(current_root).resolve()}
    hashes = {version: _package_hashes(root) for version, root in roots.items()}
    for case in cases:
        if _digest((manifest_path.parent / case["board"]).read_bytes()) != case["input_sha256"]:
            raise ValueError(f"Panel input changed: {case['id']}")
    configuration = {
        "manifest_sha256": _digest(data), "package_roots": {k: str(v) for k, v in roots.items()},
        "code_sha256": hashes, "harness_sha256": _digest(Path(__file__).read_bytes()),
        "python": sys.version, "platform": platform.platform(),
        "methods": list(methods), "timeout_seconds": timeout, "repeats": repeats,
        "selected_case_ids": [c["id"] for c in cases],
    }
    if output.exists():
        if not resume:
            raise ValueError("Output exists; use --resume for an identical unfinished comparison.")
        report = json.loads(output.read_text(encoding="utf-8"))
        if report.get("configuration") != configuration:
            raise ValueError("Comparison inputs, code, or configuration changed; start a new report.")
    else:
        report = {"schema_version": SCHEMA_VERSION, "configuration": configuration,
                  "scope": manifest["scope"], "results": [], "completed": False,
                  "metric": "PAR2: mean algorithm elapsed time of timely correct decisions; every other attempt costs2*timeout. Wrong answers/errors invalidate comparison, not merely its score.",
                  "timing_scope": "Algorithm elapsed includes its validation, construction, solving and verification. Worker elapsed adds imports/read; parent independent replay and total process wall are separate. Fresh sequential workers; alternating version order.",
                  "hard_process_limit_seconds": timeout + 2.0}
    done = {(r["case_id"], r["method"], r["trial"], r["version"]) for r in report["results"]}
    _json_write(output, report)
    with tempfile.TemporaryDirectory(prefix="lazor_research_", dir=output.parent) as temporary:
        worker_output = Path(temporary) / "result.json"
        for case_index, case in enumerate(cases):
            board = manifest_path.parent / case["board"]
            for trial in range(1, repeats + 1):
                for method_index, method in enumerate(methods):
                    versions = ("baseline", "current") if (case_index + trial + method_index) % 2 else ("current", "baseline")
                    for version in versions:
                        if (case["id"], method, trial, version) in done:
                            continue
                        if _package_hashes(roots[version]) != hashes[version]:
                            raise ValueError("Solver code changed while comparison was running.")
                        if _digest(board.read_bytes()) != case["input_sha256"]:
                            raise ValueError("Panel input changed while comparison was running.")
                        print(f"{case_index+1}/{len(cases)} {case['id']} {method} {version} trial{trial}", flush=True)
                        command = [sys.executable, "-c", WORKER, str(roots[version]),
                                   str(roots["current"] / ".deps"), str(board), method,
                                   str(timeout), str(worker_output)]
                        worker_output.unlink(missing_ok=True)
                        started = perf_counter()
                        try:
                            process = subprocess.run(command, capture_output=True, text=True, timeout=timeout + 2.0)
                            if process.returncode:
                                raise RuntimeError(process.stderr[-2000:] or "Worker exited unsuccessfully.")
                            row = json.loads(worker_output.read_text(encoding="utf-8"))
                        except subprocess.TimeoutExpired:
                            row = {"status": "timeout", "hard_timeout": True, "elapsed_seconds": None,
                                   "reason": "Parent terminated worker at wall-clock safety limit."}
                        except (OSError, RuntimeError, ValueError) as exc:
                            row = {"status": "error", "error": str(exc), "elapsed_seconds": None}
                        row["process_wall_seconds"] = perf_counter() - started
                        row["solution_verified"] = False
                        row["wrong_decision"] = False
                        if row["status"] == "solved":
                            verification_started = perf_counter()
                            try:
                                placement = {(x, y): kind for x, y, kind in row.pop("placement")}
                                row["solution_verified"] = simulate(load_bff(board), placement).is_solution
                                if not row["solution_verified"]:
                                    raise ValueError("Common independent replay rejected solution.")
                            except (KeyError, TypeError, ValueError) as exc:
                                row.update(status="error", wrong_decision=True, error=str(exc))
                            row["external_verification_seconds"] = perf_counter() - verification_started
                        row.update(case_id=case["id"], split=case["split"], size=case["size"],
                                   A=case["A"], C=case["C"], family=case["family"],
                                   expected_status=case["expected_status"], input_sha256=case["input_sha256"],
                                   method=method, version=version, trial=trial)
                        row["correct_decision"] = row["status"] == case["expected_status"] and (
                            row["status"] != "solved" or row["solution_verified"])
                        row["wrong_decision"] |= row["status"] in ("solved", "no_solution") and not row["correct_decision"]
                        elapsed = row.get("elapsed_seconds")
                        row["timely_correct"] = row["correct_decision"] and elapsed is not None and elapsed <= timeout
                        report["results"].append(row)
                        report["summary"] = summarize(report["results"], timeout, methods)
                        _json_write(output, report)
    report["completed"] = True
    report["summary"] = summarize(report["results"], timeout, methods)
    _json_write(output, report)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("source", type=Path)
    prepare.add_argument("--output-dir", type=Path, required=True)
    prepare.add_argument("--holdout-seed", type=int, default=DEFAULT_HOLDOUT_SEED)
    run = commands.add_parser("run")
    run.add_argument("manifest", type=Path)
    run.add_argument("--baseline-root", type=Path, required=True)
    run.add_argument("--current-root", type=Path, default=PACKAGE_ROOT)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--methods", nargs="+", choices=("search", "smt"), default=("search",))
    run.add_argument("--timeout", type=float, default=0.3)
    run.add_argument("--repeats", type=int, default=1)
    run.add_argument("--split", choices=("all", "reference", "holdout"), default="all")
    run.add_argument("--limit", type=int)
    run.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            manifest = prepare_panel(args.source, args.output_dir, holdout_seed=args.holdout_seed)
            print(f"Prepared {manifest['case_count']} cases: {args.output_dir / 'manifest.json'}")
            return 0
        report = compare_versions(args.manifest, args.output, baseline_root=args.baseline_root,
                                  current_root=args.current_root, methods=args.methods,
                                  timeout=args.timeout, repeats=args.repeats, split=args.split,
                                  limit=args.limit, resume=args.resume)
        print(json.dumps(report["summary"]["overall"], indent=2))
        return int(any(r["status"] == "error" or r.get("wrong_decision") for r in report["results"]))
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(2, f"Error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
