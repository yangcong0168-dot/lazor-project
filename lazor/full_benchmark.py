"""Sequential full-suite timing with isolated persistent workers and JSONL checkpoints."""

from __future__ import annotations

import argparse
from collections import Counter, deque
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import queue
import subprocess
import sys
import threading
from time import perf_counter

from .bff import load_bff
from .simulation import simulate

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1

WORKER = r'''
import dataclasses, hashlib, json, pathlib, sys, time
from copy import deepcopy
root, dependencies, method = sys.argv[1:]
sys.path.insert(0, root)
sys.path.append(dependencies)
import lazor
from lazor.bff import parse_bff
if method == "search":
    from lazor.search import solve_search as solve
    solver_version = None
else:
    from lazor.solver import solve_constraints as solve, _load_z3
    solver_version = _load_z3().get_version_string()
hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
          for p in sorted((pathlib.Path(root) / "lazor").glob("*.py"))}
print(json.dumps({"type":"ready", "package":str(pathlib.Path(lazor.__file__).resolve()),
                  "code_sha256":hashes, "solver_version":solver_version}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    started = time.perf_counter()
    record = {"type":"result", "request_id":request["request_id"], "status":"error",
              "elapsed_seconds":None, "solver_version":solver_version}
    try:
        data = pathlib.Path(request["board"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != request["input_sha256"]:
            raise RuntimeError("Input hash changed before worker read.")
        puzzle = parse_bff(data.decode("utf-8-sig"))
        original = deepcopy(puzzle)
        result = solve(puzzle, timeout=request["timeout"])
        if puzzle != original:
            raise RuntimeError("Solver mutated the input puzzle.")
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
    print(json.dumps(record, allow_nan=False), flush=True)
'''


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def package_hashes(root: Path) -> dict:
    if not (root / "lazor" / "__init__.py").is_file():
        raise ValueError(f"No lazor package under {root}")
    return {p.name: digest(p.read_bytes()) for p in sorted((root / "lazor").glob("*.py"))}


@dataclass
class PersistentWorker:
    root: Path
    dependencies: Path
    method: str
    expected_hashes: dict

    def __post_init__(self):
        self.process = None
        self.messages = queue.Queue()
        self.errors = deque(maxlen=20)
        self.generation = 0
        self.startup_seconds = 0.0

    @staticmethod
    def _read_stdout(stream, messages):
        try:
            for line in stream:
                messages.put(line)
        finally:
            messages.put(None)

    @staticmethod
    def _read_stderr(stream, errors):
        for line in stream:
            errors.append(line[-2000:])

    def start(self):
        self.close()
        self.messages = queue.Queue()
        self.errors = deque(maxlen=20)
        started = perf_counter()
        self.process = subprocess.Popen(
            [sys.executable, "-u", "-c", WORKER, str(self.root), str(self.dependencies), self.method],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1,
        )
        threading.Thread(target=self._read_stdout, args=(self.process.stdout, self.messages), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(self.process.stderr, self.errors), daemon=True).start()
        try:
            ready = self.receive(30)
            if (ready.get("type") != "ready" or ready.get("code_sha256") != self.expected_hashes
                    or Path(ready.get("package", "")).resolve() != (self.root / "lazor" / "__init__.py").resolve()):
                raise RuntimeError("Worker package identity or code hash does not match the frozen version.")
        except Exception:
            self.close()
            raise
        self.generation += 1
        self.startup_seconds = perf_counter() - started
        return ready

    def receive(self, timeout):
        line = self.messages.get(timeout=timeout)
        if line is None:
            raise RuntimeError("Worker exited: " + "".join(self.errors)[-4000:])
        try:
            return json.loads(line)
        except ValueError as exc:
            raise RuntimeError("Worker emitted invalid JSON: " + line[:500]) from exc

    def request(self, request: dict, hard_timeout: float) -> dict:
        if self.process is None:
            self.start()
        started = perf_counter()
        try:
            self.process.stdin.write(json.dumps(request) + "\n")
            self.process.stdin.flush()
            row = self.receive(hard_timeout)
            if row.get("type") != "result" or row.get("request_id") != request["request_id"]:
                raise RuntimeError("Worker returned the wrong request identity.")
        except queue.Empty:
            row = {"status": "timeout", "hard_timeout": True, "elapsed_seconds": None,
                   "reason": "External watchdog terminated the over-budget worker."}
            self.close()
        except (OSError, RuntimeError) as exc:
            row = {"status": "error", "elapsed_seconds": None, "error": str(exc)}
            self.close()
        row["process_wall_seconds"] = perf_counter() - started
        row["worker_generation"] = self.generation
        return row

    def close(self):
        process, self.process = self.process, None
        if process is None:
            return
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=2)
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


def read_checkpoint(path: Path, configuration: dict) -> tuple[list[dict], int]:
    """Validate JSONL and recover only an incomplete last write on resume."""
    rows = []
    good_offset = 0
    truncated = 0
    with path.open("rb") as stream:
        first = stream.readline()
        # A complete JSON object without its line terminator is still an
        # interrupted header write. Never append another record onto it.
        if not first.endswith(b"\n"):
            raise ValueError("Invalid or incomplete checkpoint header.")
        try:
            header = json.loads(first)
        except ValueError as exc:
            raise ValueError("Invalid or incomplete checkpoint header.") from exc
        if header.get("type") != "header" or header.get("configuration") != configuration:
            raise ValueError("Resume inputs, solver code, verifier, harness or parameters differ.")
        good_offset = stream.tell()
        seen = set()
        for line in stream:
            if not line.endswith(b"\n"):
                truncated = len(line)
                break
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise ValueError("Corrupted complete JSONL record; refusing automatic repair.") from exc
            if row.get("type") == "result":
                key = row["case_id"], row["trial"]
                if key in seen:
                    raise ValueError("Duplicate trial in checkpoint.")
                seen.add(key)
                rows.append(row)
            good_offset = stream.tell()
    if truncated:
        with path.open("r+b") as stream:
            stream.truncate(good_offset)
    return rows, truncated


def summary(rows: list[dict], timeout: float) -> dict:
    def group(selected):
        count = len(selected)
        scores = [r["elapsed_seconds"] if r.get("timely_correct") else 2 * timeout for r in selected]
        return {"attempted": count, "status_counts": dict(Counter(r["status"] for r in selected)),
                "correct_decisions": sum(r.get("correct_decision", False) for r in selected),
                "timely_correct": sum(r.get("timely_correct", False) for r in selected),
                "wrong_decisions": sum(r.get("wrong_decision", False) for r in selected),
                "hard_timeouts": sum(r.get("hard_timeout", False) for r in selected),
                "par2_seconds": sum(scores) / count if count else None,
                "algorithm_seconds_observed": sum(r.get("elapsed_seconds") or 0 for r in selected),
                "process_wall_seconds": sum(r.get("process_wall_seconds", 0) for r in selected),
                "valid": not any(r["status"] == "error" or r.get("wrong_decision") for r in selected)}
    return {"overall": group(rows),
            "by_family": {family: group([r for r in rows if r["family"] == family])
                          for family in sorted({r["family"] for r in rows})},
            "by_size": {str(size): group([r for r in rows if r["size"] == size])
                        for size in sorted({r["size"] for r in rows})}}


def _load_case_ids(path: Path | None):
    if path is None:
        return None
    text = path.read_text(encoding="utf-8-sig")
    values = json.loads(text) if text.lstrip().startswith("[") else [s.strip() for s in text.splitlines() if s.strip()]
    if (not isinstance(values, list) or not values or not all(isinstance(v, str) for v in values)
            or len(set(values)) != len(values)):
        raise ValueError("Case IDs must be a nonempty list of unique strings.")
    return set(values)


def run_full(manifest: str | Path, root: str | Path, output: str | Path, *,
             timeout: float = 1.0, method: str = "search", grace: float = 1.0,
             limit: int | None = None, repeats: int = 1, case_ids: str | Path | None = None,
             resume: bool = False, dependencies: str | Path | None = None) -> dict:
    if (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0
            or type(grace) not in (int, float) or not math.isfinite(grace) or grace <= 0
            or method not in ("search", "smt") or type(repeats) is not int or repeats < 1
            or (limit is not None and (type(limit) is not int or limit < 1))):
        raise ValueError("Invalid method, cutoff, watchdog grace, repeat count or limit.")
    manifest, root, output = Path(manifest).resolve(), Path(root).resolve(), Path(output).resolve()
    dependencies = Path(dependencies).resolve() if dependencies else ROOT / ".deps"
    manifest_data = manifest.read_bytes()
    suite = json.loads(manifest_data)
    wanted = _load_case_ids(Path(case_ids)) if case_ids is not None else None
    all_cases = suite["cases"]
    if len({c["id"] for c in all_cases}) != len(all_cases):
        raise ValueError("Manifest case IDs are not unique.")
    if wanted is not None and not wanted <= {c["id"] for c in all_cases}:
        raise ValueError("Some requested case IDs are absent from the manifest.")
    cases = [c for c in all_cases if wanted is None or c["id"] in wanted]
    if limit is not None:
        cases = cases[:limit]
    if not cases:
        raise ValueError("No cases selected.")
    protected = {manifest}
    protected.update((manifest.parent / c[key]).resolve() for c in all_cases
                     for key in ("board", "witness") if key in c)
    summary_path = output.with_name(output.name + ".summary.json")
    if output.suffix.lower() != ".jsonl" or output in protected or summary_path in protected:
        raise ValueError("Use a separate .jsonl report path.")
    code = package_hashes(root)
    for case in cases:
        if case["expected_status"] not in ("solved", "no_solution"):
            raise ValueError("Each case needs a known solved/no_solution expected status.")
        if digest((manifest.parent / case["board"]).read_bytes()) != case["input_sha256"]:
            raise ValueError(f"Input changed: {case['id']}")
    configuration = {
        "manifest_sha256": digest(manifest_data), "root": str(root), "code_sha256": code,
        "harness_sha256": digest(Path(__file__).read_bytes()),
        "verifier_sha256": {name: digest((ROOT / "lazor" / name).read_bytes())
                            for name in ("bff.py", "simulation.py", "models.py", "layout.py")},
        "dependencies": str(dependencies), "python": sys.version, "platform": platform.platform(),
        "method": method, "timeout_seconds": timeout, "watchdog_grace_seconds": grace,
        "repeats": repeats, "case_ids": [c["id"] for c in cases],
        "protocol": "Sequential persistent isolated worker. Imports/Z3 loading warm before cases. Per-case solver timing; watchdog kills/restarts worker. Common independent exact-inventory replay. Not comparable to cold-process timing without accounting for protocol.",
    }
    if method == "search":
        database_setting = os.environ.get("LAZOR_TABLE_DB")
        database_path = (None if database_setting == "off" else
                         Path(database_setting).resolve() if database_setting else
                         root / "data" / "search_tables.sqlite3")
        present = database_path is not None and database_path.is_file()
        configuration["table_database"] = {
            "setting": database_setting if database_setting is not None else "auto",
            "path": str(database_path) if database_path is not None else None,
            "size_bytes": database_path.stat().st_size if present else None,
            "sha256": digest(database_path.read_bytes()) if present else None,
        }
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        if not resume:
            raise ValueError("Output exists; use --resume with the identical configuration.")
        rows, truncated = read_checkpoint(output, configuration)
    else:
        rows, truncated = [], 0
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps({"type": "header", "schema_version": SCHEMA_VERSION,
                                     "configuration": configuration}, ensure_ascii=False) + "\n")
    done = {(r["case_id"], r["trial"]) for r in rows}
    expected = {(c["id"], trial) for c in cases for trial in range(1, repeats + 1)}
    if not done <= expected:
        raise ValueError("Checkpoint contains trials outside the selected run.")
    worker = PersistentWorker(root, dependencies, method, code)
    total = len(expected)
    run_started = perf_counter()

    def save_summary(completed):
        data = {"completed": completed, "expected_trials": total, "report": str(output),
                "configuration": configuration, "summary": summary(rows, timeout)}
        temporary = summary_path.with_name(summary_path.name + ".tmp")
        temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(summary_path)
        return data

    try:
        with output.open("a", encoding="utf-8", newline="\n") as stream:
            def append(record):
                stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                stream.flush()

            if resume:
                append({"type": "resume", "completed_trials": len(done), "truncated_tail_bytes": truncated})
            for case in cases:
                for trial in range(1, repeats + 1):
                    if (case["id"], trial) in done:
                        continue
                    if len(rows) % 100 == 0 and package_hashes(root) != code:
                        raise ValueError("Frozen solver files changed while running.")
                    if worker.process is None:
                        ready = worker.start()
                        append({**ready, "type": "worker_start", "generation": worker.generation,
                                "startup_seconds": worker.startup_seconds})
                    request = {"request_id": f"{case['id']}:{trial}",
                               "board": str((manifest.parent / case["board"]).resolve()),
                               "input_sha256": case["input_sha256"], "timeout": timeout}
                    row = worker.request(request, timeout + grace)
                    row.update(type="result", case_id=case["id"], trial=trial, size=case["size"],
                               A=case["A"], C=case["C"], family=case["family"],
                               input_sha256=case["input_sha256"], expected_status=case["expected_status"],
                               solution_verified=False, wrong_decision=False)
                    if row["status"] == "solved":
                        started = perf_counter()
                        try:
                            placements = {(x, y): kind for x, y, kind in row["placement"]}
                            puzzle = load_bff(manifest.parent / case["board"])
                            replay = simulate(puzzle, placements)
                            if not replay.is_solution:
                                raise ValueError("Independent replay failed targets or exact inventory.")
                            row["solution_verified"] = True
                        except (KeyError, TypeError, ValueError) as exc:
                            row.update(status="error", wrong_decision=True, error=str(exc))
                        row["external_verification_seconds"] = perf_counter() - started
                    row["correct_decision"] = row["status"] == case["expected_status"] and (
                        row["status"] != "solved" or row["solution_verified"])
                    row["wrong_decision"] |= row["status"] in ("solved", "no_solution") and not row["correct_decision"]
                    elapsed = row.get("elapsed_seconds")
                    row["timely_correct"] = bool(row["correct_decision"] and elapsed is not None and elapsed <= timeout)
                    append(row)
                    rows.append(row)
                    if len(rows) % 100 == 0 or len(rows) == total:
                        os.fsync(stream.fileno())
                        current = save_summary(False)["summary"]["overall"]
                        print(f"{len(rows)}/{total}: {dict(current['status_counts'])}; {perf_counter()-run_started:.1f}s this session", flush=True)
            if package_hashes(root) != code:
                raise ValueError("Frozen solver files changed before run completed.")
            append({"type": "complete", "trials": len(rows)})
            os.fsync(stream.fileno())
    finally:
        worker.close()
    return save_summary(True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=1.0)
    parser.add_argument("--method", choices=("search", "smt"), default="search")
    parser.add_argument("--grace", type=float, default=1.0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--case-ids", type=Path)
    parser.add_argument("--deps", type=Path)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run_full(args.manifest, args.root, args.output, timeout=args.timeout,
                          method=args.method, grace=args.grace, limit=args.limit,
                          repeats=args.repeats, case_ids=args.case_ids, resume=args.resume,
                          dependencies=args.deps)
    except (OSError, ValueError, RuntimeError, queue.Empty) as exc:
        parser.exit(2, f"Error: {exc}\n")
    print(json.dumps(report["summary"]["overall"], indent=2))
    return 0 if report["summary"]["overall"]["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
