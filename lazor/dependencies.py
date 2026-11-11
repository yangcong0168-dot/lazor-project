"""Load the SMT dependency from an installed environment or a platform bundle."""

import importlib
import json
from pathlib import Path
importplatform
import shlex
import struct
import subprocess
import sys
import sysconfig
from threading import RLock


Z3_VERSION = "5.3.3.0"
_IMPORT_LOCK = RLock()


def runtime_platform() -> str:
    """Identify the supported OS and architecture of this Python process."""
    system = {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}.get(
        platform.system(), "unsupported"
    )
    machine = platform.machine().lower()
    bits = struct.calcsize("P") * 8
    if system == "windows" and bits == 64:
        # Windows may report the ARM host CPU to an emulated x64 Python.
        # The Python build tag identifies the DLL architecture we can load.
        machine = {"win-amd64": "amd64", "win-arm64": "arm64"}.get(
            sysconfig.get_platform(), machine
        )
    if bits == 64 and machine in ("amd64", "x86_64"):
        architecture = "x64"
    elif bits == 64 and machine in ("arm64", "aarch64"):
        architecture = "arm64"
    else:
        architecture = f"unsupported-{machine or 'unknown'}-{bits}bit"
    return f"{system}-{architecture}"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _z3_modules() -> dict:
    return {name: module for name, module in sys.modules.copy().items()
            if name == "z3" or name.startswith("z3.")}


def _inside_package(module, directory: Path) -> bool:
    filename = getattr(module, "__file__", None)
    return bool(filename and Path(filename).resolve().is_relative_to(directory / "z3"))


def _import_z3(directory: Path | None = None, *, expected_version: str | None = None):
    """Restore import state after a failed attempt, including partial submodules."""
    previous_modules = _z3_modules()
    previous_path = sys.path[:]
    try:
        if directory is not None:
            if not _inside_package(sys.modules.get("z3"), directory):
                for name in previous_modules:
                    sys.modules.pop(name, None)
            sys.path.insert(0, str(directory))
            importlib.invalidate_caches()
        module = importlib.import_module("z3")
        if not callable(getattr(module, "Solver", None)):
            raise RuntimeError("Install z3-solver, not the unrelated z3 package.")
        if directory is not None and not _inside_package(module, directory):
            raise RuntimeError("The project-local Z3 package was not loaded from .deps.")
        if expected_version is not None:
            version = ".".join(str(part) for part in module.get_version())
            if version != expected_version:
                raise RuntimeError(f"Bundled Z3 version is {version}; expected {expected_version}.")
        return module
    except BaseException:
        for name in _z3_modules():
            sys.modules.pop(name, None)
        sys.modules.update(previous_modules)
        raise
    finally:
        sys.path[:] = previous_path


def _bundle_platform(directory: Path) -> str:
    marker = json.loads((directory / "lazor_bundle.json").read_text(encoding="utf-8"))
    if not isinstance(marker, dict) or (
        type(marker.get("schema_version")) is not int or marker["schema_version"] != 1
        or marker.get("distribution") != "z3-solver" or marker.get("version") != Z3_VERSION
    ):
        raise ValueError("Invalid .deps/lazor_bundle.json; download a complete release archive.")
    target = marker.get("platform")
    supported = {f"{system}-{arch}" for system in ("windows", "macos", "linux")
                 for arch in ("x64", "arm64")}
    if target not in supported:
        raise ValueError("Invalid platform in .deps/lazor_bundle.json.")
    current = runtime_platform()
    if target != current:
        raise ValueError(f"This bundle targets {target}, but this Python process is {current}.")
    return target


def load_z3():
    """Load Z3 without changing algorithms or masking a broken platform bundle.

    Marked release dependencies take priority over globally installed packages.
    Source checkouts retain installed-first loading and an optional manual .deps
    installation. Failed imports restore the previous Z3 modules and Python path.
    """
    root = _project_root()
    local = root / ".deps"
    with _IMPORT_LOCK:
        try:
            if (local / "lazor_bundle.json").exists():
                _bundle_platform(local)
                if not (local / "z3" / "__init__.py").is_file():
                    raise RuntimeError("The release archive is missing .deps/z3/__init__.py.")
                return _import_z3(local, expected_version=Z3_VERSION)
            try:
                return _import_z3()
            except ModuleNotFoundError as exc:
                if exc.name != "z3" or not (local / "z3" / "__init__.py").is_file():
                    raise
                return _import_z3(local)
        except Exception as exc:
            command = [sys.executable, "-m", "pip", "install", "-r", str(root / "requirements.txt")]
            install = subprocess.list2cmdline(command) if sys.platform == "win32" else shlex.join(command)
            raise RuntimeError(
                f"Cannot load z3-solver for SMT solving ({runtime_platform()}). {exc}\n"
                "For a release ZIP, download and fully extract the package matching this Python process. "
                "For a source checkout, install the dependency with the same Python interpreter:\n"
                f"{install}"
            ) from exc
