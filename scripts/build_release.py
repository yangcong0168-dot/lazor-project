"""Build the final project, optional database and validation release archives."""
import argparse
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = ("lazor", "tests", "boards", "docs", ".github", "scripts", "data")
ROOT_FILES = ("README.md", "requirements.txt", "Launch Editor.cmd", "block_laser.py",
              "VERSION", ".gitignore", ".gitattributes")
TEXT_SUFFIXES = {".py", ".md", ".bff", ".yml", ".yaml"}


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--prepare-database", action="store_true")
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
    names = (f"lazor-project-v{version}.zip", "lazor-search-tables.zip", "lazor-validation.zip")
    if any((out / name).exists() for name in (*names, "SHA256SUMS.txt")):
        raise FileExistsError("Use a fresh output directory; release assets are never overwritten.")
    files = source_files()
    missing = [str(path) for path in files if not path.is_file()]
    if missing:
        raise FileNotFoundError(str(missing))
    prefix = f"lazor-project-v{version}/"
    write_zip(out / names[0], [(path, prefix + path.relative_to(ROOT).as_posix()) for path in files]
              + [(database, prefix + "data/search_tables.sqlite3")])
    write_zip(out / names[1], [(database, "data/search_tables.sqlite3"),
                              (ROOT / "data/README.md", "data/README.md")])
    shutil.copyfile(validation, out / names[2])
    with zipfile.ZipFile(out / names[2]) as archive:
        if archive.testzip():
            raise ValueError("Validation archive checksum failed.")
    checksums = "".join(f"{sha256(out / name)}  {name}\n" for name in names)
    (out / "SHA256SUMS.txt").write_text(checksums, encoding="utf-8")
    print(checksums, end="")
    for name in names:
        print(f"{name}: {(out / name).stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
