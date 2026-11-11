"""Verify an actual release ZIP without installed Python packages or a GUI.

Usage: python scripts/verify_release.py dist/lazor-project-v1.0.1.zip
       --output dist/release-verification.json

The direct SMT and editor-worker probes run in separate ``python -I -S``
processes after extraction to a fresh directory containing spaces and Chinese
characters. Only the extracted project root is added to their import paths.
No dependency is installed, downloaded, or borrowed from the working tree.
"""

import argparse
from collections import Counter
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import tempfile
import time
import traceback
import zipfile


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def extract_release(archive_path, destination):
    """Extract one unambiguous project root without archive path traversal."""
    destination = Path(destination).resolve()
    with zipfile.ZipFile(archive_path) as archive:
        seen = set()
        for info in archive.infolist():
            name = PurePosixPath(info.filename)
            if (name.is_absolute() or "\\" in info.filename
                    or any(part in ("..", ".") or ":" in part for part in name.parts)
                    or stat.S_ISLNK(info.external_attr >> 16)):
                raise ValueError(f"Unsafe ZIP entry: {info.filename}")
            relative = name.as_posix().rstrip("/")
            key = relative.casefold() if os.name == "nt" else relative
            if key in seen:
                raise ValueError(f"Duplicate ZIP entry: {info.filename}")
            seen.add(key)
            (destination / name).resolve().relative_to(destination)
        archive.extractall(destination)
    roots = [path.parent for path in destination.rglob("lazor")
             if path.is_dir() and (path / "solver.py").is_file()
             and (path.parent / "boards/mad_1.bff").is_file()]
    if len(roots) != 1:
        raise ValueError("Expected exactly one release root containing lazor/ and boards/mad_1.bff")
    return roots[0].resolve()


def _inside(path, root, description):
    path = Path(path).resolve(strict=True)
    try:
        path.relative_to(root)
    except ValueError as error:
        raise RuntimeError(f"{description} came from outside the extracted release: {path}") from error
    return path


def _isolation(project):
    if not (sys.flags.isolated and sys.flags.no_site and sys.flags.no_user_site
            and sys.flags.ignore_environment):
        raise RuntimeError("Release probes require python -I -S")
    if any("site-packages" in str(path).lower() or "dist-packages" in str(path).lower()
           for path in sys.path):
        raise RuntimeError("Installed package directory leaked into isolated sys.path")
    if "lazor" in sys.modules or "z3" in sys.modules:
        raise RuntimeError("Probe was not started with a clean interpreter")
    sys.path.insert(0, str(project))
    import lazor
    from lazor.dependencies import runtime_platform
    _inside(lazor.__file__, project, "Lazor package")
    return {"isolated": bool(sys.flags.isolated), "no_site": bool(sys.flags.no_site),
            "no_user_site": bool(sys.flags.no_user_site),
            "python_version": sys.version, "platform": sys.platform,
            "runtime_platform": runtime_platform(),
            "project_root": str(project), "lazor_module": str(Path(lazor.__file__).resolve())}


def _native_library(z3):
    library = getattr(z3.z3core, "_lib", None)
    if isinstance(library, ctypes.CDLL):
        return library
    # Recent generated bindings delete _lib after defining their wrappers.
    # ctypes retains the owning CDLL in each bound function's _objects, so use
    # that actual loaded object rather than loading or guessing another DLL.
    defaults = getattr(z3.z3core.Z3_get_version, "__defaults__", ()) or ()
    libraries = {}
    for elementary in defaults:
        function = getattr(elementary, "f", None)
        for owner in (getattr(function, "_objects", None) or {}).values():
            if isinstance(owner, ctypes.CDLL):
                libraries[id(owner)] = owner
    if len(libraries) != 1:
        raise RuntimeError("Cannot identify the native library bound to Z3_get_version")
    return next(iter(libraries.values()))


def _native_path(z3):
    library = _native_library(z3)
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        get_name = kernel.GetModuleFileNameW
        get_name.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint)
        get_name.restype = ctypes.c_uint
        buffer = ctypes.create_unicode_buffer(32768)
        length = get_name(ctypes.c_void_p(library._handle), buffer, len(buffer))
        if not length:
            raise ctypes.WinError(ctypes.get_last_error())
        if length >= len(buffer):
            raise RuntimeError("Loaded Z3 DLL path was truncated")
        return Path(buffer.value)
    path = Path(os.fsdecode(library._name))
    if not path.is_absolute():
        raise RuntimeError(f"Cannot verify an absolute native Z3 library path: {library._name!r}")
    return path


def _verify_solution(puzzle, planning):
    from lazor.simulation import simulate
    if planning.status != "solved" or planning.solution is None:
        raise RuntimeError(f"Expected a real SMT solution, received {planning.status!r}")
    placements = planning.solution.layout.placements
    required = Counter({kind: count for kind, count in puzzle.block_counts.items() if count})
    if Counter(placements.values()) != required:
        raise RuntimeError("SMT solution does not use the exact movable inventory")
    replay = simulate(puzzle, placements)
    if not replay.is_solution:
        raise RuntimeError("Independent optical replay rejected the SMT solution")
    return {"status": planning.status, "solution_verified": True,
            "placements": [[x, y, kind] for (x, y), kind in sorted(placements.items())]}


def _direct_probe(project, timeout):
    from dataclasses import replace
    from lazor.bff import load_bff
    from lazor.solver import _load_z3, solve_constraints
    z3 = _load_z3()
    dependency_root = (project / ".deps").resolve(strict=True)
    module = _inside(z3.__file__, dependency_root, "Z3 Python module")
    native = _inside(_native_path(z3), dependency_root, "Loaded native Z3 library")
    # Both calls cross the native C API; import success alone is insufficient.
    value = z3.Int("release_native_probe")
    solver = z3.Solver()
    solver.add(value == 1)
    native_sat = str(solver.check())
    solver.add(value == 2)
    native_unsat = str(solver.check())
    if (native_sat, native_unsat) != ("sat", "unsat"):
        raise RuntimeError("Native Z3 SAT/UNSAT checks returned unexpected results")
    puzzle = load_bff(project / "boards/mad_1.bff")
    result = solve_constraints(puzzle, timeout=timeout / 3)
    verification = _verify_solution(puzzle, result)
    impossible = replace(puzzle, targets=puzzle.targets | {(0, 0)})
    control = solve_constraints(impossible, timeout=timeout / 3)
    if control.status != "no_solution":
        raise RuntimeError(f"Parity UNSAT control returned {control.status!r}")
    return {"z3_version": z3.get_version_string(), "z3_module": str(module),
            "z3_module_sha256": _sha256(module), "native_library": str(native),
            "native_library_sha256": _sha256(native), "native_sat": native_sat,
            "native_unsat": native_unsat, "board": "boards/mad_1.bff", **verification,
            "unsat_control": {"kind": "unreachable even-parity target", "status": control.status}}


def _editor_probe(project, timeout):
    from lazor.bff import parse_bff
    from lazor.editor_runner import EditorRunner
    if any(name == "z3" or name.startswith("z3.") for name in sys.modules):
        raise RuntimeError("Editor probe parent loaded Z3 before spawning its worker")
    text = (project / "boards/mad_1.bff").read_text(encoding="utf-8-sig")
    runner = EditorRunner()

    def solve(board_text):
        runner.start("solve", board_text, timeout=timeout / 3)
        deadline = time.monotonic() + timeout / 3 + 2
        while time.monotonic() < deadline:
            message = runner.poll()
            if message is None:
                time.sleep(.01)
                continue
            mode, result = message
            if mode != "solve":
                raise RuntimeError(f"Editor worker failed: {result}")
            return result
        raise TimeoutError("Editor background SMT worker did not return within its budget")

    try:
        result = solve(text)
        verification = _verify_solution(parse_bff(text), result)
        control = solve(text.rstrip() + "\nP 0 0\n")
        if control.status != "no_solution":
            raise RuntimeError(f"Editor UNSAT control returned {control.status!r}")
        return {"board": "boards/mad_1.bff", "worker_start_method": "spawn",
                "parent_z3_imported_before_start": False, "ui_opened": False,
                **verification, "unsat_control": {"kind": "unreachable even-parity target", "status": control.status}}
    finally:
        runner.cancel()


def _probe(mode, project, output, timeout):
    started = time.perf_counter()
    report = {"mode": mode, "passed": False}
    try:
        project = Path(project).resolve(strict=True)
        report.update(_isolation(project))
        report.update((_direct_probe if mode == "direct" else _editor_probe)(project, timeout))
        report["passed"] = True
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        report["traceback"] = traceback.format_exc()
    report["elapsed_seconds"] = time.perf_counter() - started
    _write_json(output, report)
    return 0 if report["passed"] else 1


def verify_release(archive, *, python=None, timeout=30.0):
    """Return a machine-readable result; any failed probe makes passed false."""
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    archive = Path(archive).resolve(strict=True)
    executable = str(Path(python or sys.executable).resolve(strict=True))
    report = {"passed": False, "archive": str(archive), "archive_sha256": _sha256(archive),
              "verifier_sha256": _sha256(__file__), "python": executable,
              "probe_flags": ["-I", "-S"], "probes": []}
    started = time.perf_counter()
    try:
        with tempfile.TemporaryDirectory(prefix="lazor release 中文 ") as directory:
            project = extract_release(archive, directory)
            report["extraction_path_contains_space_and_chinese"] = " " in str(project) and "中文" in str(project)
            environment = {key: value for key, value in os.environ.items()
                           if not key.upper().startswith("PYTHON") and key.upper() != "Z3_LIBRARY_PATH"}
            environment["LAZOR_TABLE_DB"] = "off"
            for mode in ("direct", "editor"):
                result_path = Path(directory) / f"{mode}-probe.json"
                command = [executable, "-I", "-S", str(Path(__file__).resolve()),
                           "--_probe", mode, "--project-root", str(project),
                           "--output", str(result_path), "--timeout", str(timeout)]
                try:
                    completed = subprocess.run(command, cwd=project, env=environment,
                                               capture_output=True, text=True, encoding="utf-8",
                                               errors="replace", timeout=timeout)
                    probe = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else {
                        "mode": mode, "passed": False, "error": "Isolated probe produced no result"}
                    probe["process_exit_code"] = completed.returncode
                    if completed.returncode != 0:
                        probe["passed"] = False
                    if completed.stdout:
                        probe["stdout"] = completed.stdout[-8000:]
                    if completed.stderr:
                        probe["stderr"] = completed.stderr[-8000:]
                except subprocess.TimeoutExpired:
                    probe = {"mode": mode, "passed": False, "error": "Isolated probe exceeded its external timeout"}
                report["probes"].append(probe)
            report["passed"] = all(probe["passed"] for probe in report["probes"])
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
    report["elapsed_seconds"] = time.perf_counter() - started
    return report


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, nargs="?")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--python", type=Path)
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="External time limit in seconds for each isolated probe")
    parser.add_argument("--_probe", choices=("direct", "editor"), help=argparse.SUPPRESS)
    parser.add_argument("--project-root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be finite and positive")
    if args._probe:
        if args.project_root is None or args.output is None:
            parser.error("Internal probes require a project root and output path")
        return _probe(args._probe, args.project_root, args.output, args.timeout)
    if args.archive is None:
        parser.error("Supply the final project ZIP")
    try:
        report = verify_release(args.archive, python=args.python, timeout=args.timeout)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        report = {"passed": False, "error": f"{type(error).__name__}: {error}"}
    if args.output is not None:
        _write_json(args.output, report)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
