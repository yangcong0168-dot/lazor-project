"""Build the final project, optional database and validation release archives."""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = ("lazor", "tests", "boards", "docs", ".github", "scripts", "data", "third_party")
ROOT_FILES = ("README.md", "requirements.txt", "Launch Editor.cmd", "block_laser.py",
              "VERSION", ".gitignore", ".gitattributes", "THIRD_PARTY_NOTICES.md")
TEXT_SUFFIXES = {".py", ".md", ".bff", ".yml", ".yaml", ".txt"}


def source_files():
    files = [ROOT / name for name in ROOT_FILES]
    for directory in SOURCE_DIRS:
        files.extend(path for path in (ROOT / directory).rglob("*")
                     if path.is_file() and path.suffix in TEXT_SUFFIXES
                     and "__pycache__" not in path.parts)
    return sorted(set(files))


def sha256(path):
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def write_zip(destination, entries):
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6) as archive:
        for source, name in entries:
            info = zipfile.ZipInfo(name.replace("\\", "/"), date_time=(2026, 11, 11, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            with source.open("rb") as reader, archive.open(info, "w", force_zip64=True) as writer:
                shutil.copyfileobj(reader, writer)
    with zipfile.ZipFile(destination) as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f"Corrupt archive entry: {bad}")


def prepare_database(database):
    if database.exists():
        return
    manifest = ROOT / "outputs/scaling_full/manifest.json"
    if not manifest.exists():
        subprocess.run([sys.executable, "-m", "lazor.scaling", "generate", "--all-sizes",
                        "--seed", "20261110", "--output-dir", str(manifest.parent)],
                       cwd=ROOT, check=True)
    database.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, "-m", "lazor.table_database", "build", str(database),
                    "--manifest", str(manifest), "--predecessors", "all"], cwd=ROOT, check=True)


def vendor_dependencies(directory, target):
    """Install the locked native wheel into a clean release-only directory."""
    from lazor.dependencies import Z3_VERSION
    subprocess.run([sys.executable, "-m", "pip", "--isolated", "install",
                    "--index-url", "https://pypi.org/simple", "--only-binary=:all:",
                    "--no-compile", "--target", str(directory),
                    "-r", str(ROOT / "requirements.txt")], check=True, cwd=ROOT)
    distributions = {dist.metadata["Name"]: dist.version
                     for dist in importlib.metadata.distributions(path=[str(directory)])}
    if distributions.get("z3-solver") != Z3_VERSION:
        raise RuntimeError(f"Wrong dependency version installed: {distributions}")
    native_name = {"windows": "libz3.dll", "macos": "libz3.dylib", "linux": "libz3.so"}[target.split("-")[0]]
    if not (directory / "z3/lib" / native_name).is_file():
        raise RuntimeError("The dependency wheel is missing its native Z3 library.")
    (directory / "lazor_bundle.json").write_text(json.dumps({
        "schema_version": 1, "distribution": "z3-solver", "version": Z3_VERSION,
        "platform": target, "distributions": distributions,
    }, indent=2) + "\n", encoding="utf-8")
    # Preserve the entire wheel installation, including DLLs, metadata and notices.
    return sorted(path for path in directory.rglob("*") if path.is_file()
                  and "__pycache__" not in path.parts and path.suffix != ".pyc")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--prepare-database", action="store_true")
    parser.add_argument("--project-only", action="store_true",
                        help="Omit the shared database and validation download archives")
    parser.add_argument("--expected-platform", help="Fail if Python's architecture differs from the CI target")
    args = parser.parse_args()
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("VERSION must be a numeric major.minor.patch version.")
    database = ROOT / "data/search_tables.sqlite3"
    validation = ROOT / "validation/lazor-validation.zip"
    if not validation.is_file():
        raise FileNotFoundError("The curated validation/lazor-validation.zip is required.")
    if args.prepare_database:
        prepare_database(database)
    if not database.is_file():
        raise FileNotFoundError("Install the optional database or use --prepare-database.")
    sys.path.insert(0, str(ROOT))
    from lazor.dependencies import runtime_platform
    target = runtime_platform()
    if args.expected_platform and target != args.expected_platform:
        raise RuntimeError(f"Expected {args.expected_platform}, but this Python process targets {target}")
    if target not in {f"{system}-{arch}" for system in ("windows", "macos", "linux")
                      for arch in ("x64", "arm64")}:
        raise RuntimeError(f"Unsupported native release platform: {target}")
    from lazor.table_database import TableDatabase
    reader = TableDatabase(database)
    try:
        predecessor_keys = reader.predecessor_keys()
        if len(reader.geometry_keys()) != 41 or len(predecessor_keys) != 2019:
            raise ValueError("Release database does not match the full 4..44 test matrix.")
        if reader.geometry(44, 44) is None:
            raise ValueError("Release database failed its geometry checksum.")
        if reader.predecessors(predecessor_keys[-1]) is None:
            raise ValueError("Release database failed its predecessor checksum.")
    finally:
        reader.close()
    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    project_name = f"lazor-project-v{version}-{target}.zip"
    names = [project_name]
    if target == "windows-x64":
        names.append(f"lazor-project-v{version}.zip")
    if not args.project_only:
        names.extend(("lazor-search-tables.zip", "lazor-validation.zip"))
    if any((out / name).exists() for name in (*names, "SHA256SUMS.txt")):
        raise FileExistsError("Use a fresh output directory; release assets are never overwritten.")
    files = source_files()
    missing = [str(path) for path in files if not path.is_file()]
    if missing:
        raise FileNotFoundError(str(missing))
    prefix = f"lazor-project-v{version}/"
    with tempfile.TemporaryDirectory(prefix="lazor-dependencies-", dir=out.parent) as staging:
        dependencies = Path(staging) / ".deps"
        dependencies.mkdir()
        vendored = vendor_dependencies(dependencies, target)
        write_zip(out / project_name,
                  [(path, prefix + path.relative_to(ROOT).as_posix()) for path in files]
                  + [(database, prefix + "data/search_tables.sqlite3")]
                  + [(path, prefix + ".deps/" + path.relative_to(dependencies).as_posix())
                     for path in vendored])
    if target == "windows-x64":
        shutil.copyfile(out / project_name, out / f"lazor-project-v{version}.zip")
    if not args.project_only:
        write_zip(out / "lazor-search-tables.zip", [(database, "data/search_tables.sqlite3"),
                                                  (ROOT / "data/README.md", "data/README.md")])
        shutil.copyfile(validation, out / "lazor-validation.zip")
        with zipfile.ZipFile(out / "lazor-validation.zip") as archive:
            if archive.testzip():
                raise ValueError("Validation archive checksum failed.")
    checksums = "".join(f"{sha256(out / name)}  {name}\n" for name in names)
    (out / "SHA256SUMS.txt").write_text(checksums, encoding="utf-8")
    print(checksums, end="")
    for name in names:
        print(f"{name}: {(out / name).stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
