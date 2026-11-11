"""Package the final validation evidence without changing its source files.

The archive is intentionally a whitelist, not a backup of the experiment tree.
Use --verify to check an existing archive without the original experiment files.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = "outputs/adaptive_optimization"
SCRIPTS = (
    "tmp/adaptive_experiments/audit_opening.py",
    "tmp/adaptive_experiments/audit_early_guidance.py",
    "tmp/space_time_experiments/audit_cache.py",
    "tmp/summarize_adaptive.py",
    "tmp/analyze_full_suite.py",
    "tmp/recheck_adaptive.py",
    "tmp/measure_adaptive_cache.py",
    "tmp/adaptive_experiments/final_regression_profile.py",
    "scripts/package_validation.py",
)
SNAPSHOTS = ("adaptive_baseline", "adaptive_release", "optimization_baseline")
META = "validation/PACKAGE_MANIFEST.json"
README = "validation/BUNDLE_README.md"
CHECKSUMS = "validation/SHA256SUMS.txt"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def selected_files(root: Path) -> list[Path]:
    folder = root / EVIDENCE
    files = set(folder.glob("*.json")) | set(folder.glob("*.jsonl"))
    files.add(folder / "unittest_final.log")
    files.update((folder / "profiles").glob("*.json"))
    files.update(p for p in (folder / "fresh_holdout").rglob("*") if p.is_file())
    files.add(root / "outputs/scaling_full/manifest.json")
    files.update((root / "outputs/scaling_full/boards").glob("*.bff"))
    files.update((root / "outputs/scaling_full/witnesses").glob("*.json"))
    for snapshot in SNAPSHOTS:
        paths = list((root / "tmp" / snapshot / "lazor").glob("*.py"))
        if not paths:
            raise FileNotFoundError(f"Missing frozen source: {snapshot}")
        files.update(paths)
    files.update(root / name for name in SCRIPTS)
    for path in files:
        if not path.is_file() or not path.resolve().is_relative_to(root):
            raise ValueError(f"Missing or out-of-workspace source: {path.name}")
    return sorted(files, key=lambda p: p.relative_to(root).as_posix())


class Normalizer:
    """Change machine-specific path strings, leaving result/hash values intact."""

    def __init__(self, root: Path):
        roots = {str(root), root.as_posix()}
        # A relocated copy of the original evidence may still name its old root.
        journal = root / EVIDENCE / "baseline_search_1s.jsonl"
        with journal.open(encoding="utf-8") as stream:
            old_root = json.loads(next(stream))["configuration"]["root"]
        suffix = re.search(r"[/\\]tmp[/\\]adaptive_baseline$", old_root)
        if suffix:
            roots.add(old_root[:suffix.start()])
        self.roots = sorted(roots, key=len, reverse=True)
        homes = {str(Path.home())}
        for value in roots:
            match = re.match(r"^([A-Za-z]:[/\\]Users[/\\][^/\\]+)", value)
            if match:
                homes.add(match.group(1))
        self.homes = sorted(homes, key=len, reverse=True)
        self.root_patterns = tuple(self.pattern(prefix) for prefix in self.roots)
        self.home_patterns = tuple(self.pattern(prefix) for prefix in self.homes)

    @staticmethod
    def pattern(prefix: str) -> re.Pattern:
        parts = re.split(r"[/\\]", prefix.rstrip("/\\"))
        return re.compile(r"[/\\]".join(re.escape(p) for p in parts) + r"(?=$|[/\\])", re.I)

    def string(self, value: str) -> str:
        if "/" not in value and "\\" not in value:
            return value
        changed = False
        for pattern in self.root_patterns:
            value, count = pattern.subn("@PROJECT@", value)
            changed |= bool(count)
        for pattern in self.home_patterns:
            value, count = pattern.subn("external/user-profile", value)
            changed |= bool(count)
        if changed:
            value = value.replace("\\", "/")
            value = value.replace("@PROJECT@/", "").replace("@PROJECT@", ".")
        return value

    def object(self, value):
        if isinstance(value, str):
            return self.string(value)
        if isinstance(value, list):
            return [self.object(item) for item in value]
        if isinstance(value, dict):
            result = {self.string(key): self.object(item) for key, item in value.items()}
            if len(result) != len(value):
                raise ValueError("Path normalization produced duplicate JSON keys")
            return result
        return value

    def data(self, name: str, data: bytes) -> bytes:
        if name.endswith(".bff"):
            return data
        text = data.decode("utf-8-sig")
        if name.endswith(".json"):
            before = json.loads(text)
            after = self.object(before)
            return data if before == after else json_bytes(after)
        if name.endswith(".jsonl"):
            lines = []
            changed = False
            for line in text.splitlines():
                before = json.loads(line)
                after = self.object(before)
                changed |= before != after
                lines.append(json.dumps(after, ensure_ascii=False, separators=(",", ":")))
            return ("\n".join(lines) + "\n").encode("utf-8") if changed else data
        # Source snapshots have no machine paths and remain byte-for-byte intact.
        after = self.string(text)
        return data if text == after else after.encode("utf-8")


def bundle_readme() -> bytes:
    return """# Lazor final validation evidence

Extract this archive into the project checkout root. It preserves project-relative
paths under outputs/, tmp/ and scripts/. No production or original evidence file
was changed while packaging. The archive contains the final 5,997-case matrix,
164 frozen fresh-seed inputs, planted witnesses, all formal adaptive JSON/JSONL
records (including both fresh runs), the final unit-test log, JSON profiles, three
frozen Python source trees, and the scripts required by the two oracle audits and
the paired-results summary. Other historical experiments and binary profiles are
deliberately excluded. The separate clean-checkout log is distributed in the Git
repository's validation directory.

## Integrity and path normalization

PACKAGE_MANIFEST.json records each original file SHA-256 and its packaged SHA-256.
SHA256SUMS.txt additionally covers generated metadata. Existing input_sha256,
code_sha256, manifest_sha256, generator_script_sha256 and other historical hashes
in records are unchanged: they identify the ORIGINAL artifact, not an edited copy.
Use original_sha256 in PACKAGE_MANIFEST.json to resolve a historical hash when a
metadata file was normalized. Frozen Python sources and .bff inputs must retain
their exact original bytes; the packager verifies this condition.

Only real machine path prefixes are normalized, recursively including JSON keys.
Paths inside the project become project-relative paths with forward slashes.
External user-profile paths, if any, become external/user-profile/... metadata
placeholders; these external files are not included or required for the recorded
results. Values, cases, timings, counters, seeds and historical hash strings are
not replaced. This is a redistribution copy, not a new benchmark run.

## Reproduce the checks

Use Python 3.12 or a compatible Python 3. Install the project's requirements for
optional SMT features. Search and independent DFS audits use the standard library.
Run from the project checkout root:

```text
python scripts/package_validation.py --verify validation/lazor-validation.zip
python tmp/summarize_adaptive.py
python tmp/adaptive_experiments/audit_opening.py --candidate-root tmp/adaptive_release --oracle-root tmp/optimization_baseline --output outputs/replayed_oracle_default.json
python tmp/adaptive_experiments/audit_opening.py --candidate-root tmp/adaptive_release --oracle-root tmp/optimization_baseline --force-bitboard --restart-after 0 --output outputs/replayed_oracle_forced.json
python tmp/adaptive_experiments/audit_early_guidance.py --candidate-root tmp/adaptive_release --oracle-root tmp/optimization_baseline --output outputs/replayed_oracle_guidance.json
python -m unittest discover -s tests -v
```

The audit_cache.py file is a fixture helper imported by the two audits; its own
historical main program is not part of the final reproduction workflow. Audit
outputs must be new paths. Unit tests and the current benchmark harness are in the
main Git checkout. Frozen fresh inputs are supplied directly, so recreating older
holdout-exclusion corpora is unnecessary; historical exclusion hashes remain in
their manifest for provenance.

To repeat timing, obtain the separate search_tables.sqlite3 Release asset and put
it at data/search_tables.sqlite3. Its expected SHA-256 is recorded in
outputs/adaptive_optimization/release_integrity.json. The 101 MiB database is not
duplicated in this validation ZIP. Set LAZOR_TABLE_DB to its absolute local path;
for example, in PowerShell:

```powershell
$env:LAZOR_TABLE_DB = (Resolve-Path data/search_tables.sqlite3).Path
python -m lazor.full_benchmark --manifest outputs/scaling_full/manifest.json --root tmp/adaptive_baseline --output outputs/replayed_baseline.jsonl --timeout 1
python -m lazor.full_benchmark --manifest outputs/scaling_full/manifest.json --root tmp/adaptive_release --output outputs/replayed_release.jsonl --timeout 1
python -m lazor.full_benchmark --manifest outputs/adaptive_optimization/fresh_holdout/manifest.json --root tmp/adaptive_baseline --output outputs/replayed_fresh_baseline.jsonl --timeout 1
python -m lazor.full_benchmark --manifest outputs/adaptive_optimization/fresh_holdout/manifest.json --root tmp/adaptive_release --output outputs/replayed_fresh_release.jsonl --timeout 1
```

Run timing commands sequentially on an idle machine. Cache warmness, Python and
hardware affect elapsed time; the original recordings are evidence, not universal
performance guarantees. tmp/recheck_adaptive.py and the JSON profile script are
optional diagnostics. SHA validation alone verifies evidence integrity, not the
correctness of the solver; the independent audits and unit tests provide that
separate check.
""".encode("utf-8")


def zip_write(archive, name: str, data: bytes):
    info = zipfile.ZipInfo(name, date_time=(2026, 11, 11, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)


def verify_archive(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Duplicate ZIP entries")
        for name in names:
            item = PurePosixPath(name)
            if item.is_absolute() or ".." in item.parts or "\\" in name or ":" in name:
                raise ValueError("Unsafe archive path")
        bad = archive.testzip()
        if bad:
            raise ValueError(f"CRC mismatch: {bad}")
        metadata = json.loads(archive.read(META))
        listed = {entry["path"] for entry in metadata["files"]}
        if set(names) != listed | {META, README, CHECKSUMS}:
            raise ValueError("Manifest/ZIP member mismatch")
        for entry in metadata["files"]:
            data = archive.read(entry["path"])
            if digest(data) != entry["packaged_sha256"] or len(data) != entry["packaged_bytes"]:
                raise ValueError(f"Packaged file hash mismatch: {entry['path']}")
            if (entry["path"].endswith(".bff") or any(
                    entry["path"].startswith(f"tmp/{name}/lazor/") for name in SNAPSHOTS)):
                if entry["original_sha256"] != entry["packaged_sha256"]:
                    raise ValueError("Input or frozen source bytes changed")
        checksums = dict(line.split("  ", 1)[::-1]
                         for line in archive.read(CHECKSUMS).decode().splitlines())
        if set(checksums) != set(names) - {CHECKSUMS}:
            raise ValueError("Checksum index/member mismatch")
        for name, expected in checksums.items():
            if digest(archive.read(name)) != expected:
                raise ValueError(f"SHA-256 mismatch: {name}")
        inputs = {}
        for manifest_name, expected in (("outputs/scaling_full/manifest.json", 5997),
                                       (f"{EVIDENCE}/fresh_holdout/manifest.json", 164)):
            manifest = json.loads(archive.read(manifest_name))
            cases = manifest["cases"]
            if len(cases) != expected or manifest["case_count"] != expected:
                raise ValueError("Unexpected input matrix size")
            folder = str(PurePosixPath(manifest_name).parent)
            index = {}
            for case in cases:
                board = archive.read(f"{folder}/{case['board']}")
                if digest(board) != case["input_sha256"]:
                    raise ValueError(f"Input SHA mismatch: {case['id']}")
                witness = json.loads(archive.read(f"{folder}/{case['witness']}"))
                if witness.get("input_sha256") != case["input_sha256"]:
                    raise ValueError(f"Witness input SHA mismatch: {case['id']}")
                index[case["id"]] = case["input_sha256"]
            if len(index) != expected:
                raise ValueError("Duplicate case IDs")
            inputs["fresh" if expected == 164 else "full"] = index
        journals = {}
        for prefix in ("", "fresh_"):
            index = inputs["fresh" if prefix else "full"]
            for version in ("baseline", "release"):
                name = f"{EVIDENCE}/{prefix}{version}_search_1s.jsonl"
                records = [json.loads(line) for line in archive.read(name).splitlines()]
                results = [row for row in records if row.get("type") == "result"]
                if len(results) != len(index) or {r["case_id"] for r in results} != set(index):
                    raise ValueError(f"Journal case set mismatch: {name}")
                if any(r["input_sha256"] != index[r["case_id"]] for r in results):
                    raise ValueError(f"Journal input hash mismatch: {name}")
                if any(r.get("wrong_decision") or r["status"] == "error" for r in results):
                    raise ValueError(f"Wrong decision/error in final evidence: {name}")
                journals[PurePosixPath(name).name] = dict(count=len(results),
                    statuses=dict(Counter(row["status"] for row in results)))
        verification = json.loads(archive.read(f"{EVIDENCE}/verification_summary.json"))
        if not verification["passed"]:
            raise ValueError("Recorded verification did not pass")
        for name, expected in verification["candidate_code_sha256"].items():
            if digest(archive.read(f"tmp/adaptive_release/lazor/{name}")) != expected:
                raise ValueError(f"Frozen candidate SHA mismatch: {name}")
        return dict(verified=True, crc_checked=True, sha256_checked=True,
                    member_count=len(names), source_file_count=len(listed),
                    normalized_files=sum(item["normalized"] for item in metadata["files"]),
                    input_counts={key: len(index) for key, index in inputs.items()},
                    journals=journals,
                    frozen_source_files=sum(name.startswith(tuple(f"tmp/{s}/lazor/" for s in SNAPSHOTS))
                                            for name in names),
                    uncompressed_bytes=sum(item.file_size for item in archive.infolist()))


def package(root: Path, output: Path) -> dict:
    root = root.resolve()
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    normalizer = Normalizer(root)
    entries = []
    sha_lines = []
    copies = {}
    handle, temporary_name = tempfile.mkstemp(prefix=".validation-", suffix=".zip", dir=output.parent)
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w") as archive:
            for path in selected_files(root):
                name = path.relative_to(root).as_posix()
                original = path.read_bytes()
                packaged = normalizer.data(name, original)
                # Only validated text formats enter this whitelist.
                if normalizer.data(name, packaged) != packaged:
                    raise ValueError(f"Normalization is not idempotent: {name}")
                entry = dict(path=name, original_bytes=len(original), packaged_bytes=len(packaged),
                             original_sha256=digest(original), packaged_sha256=digest(packaged),
                             normalized=original != packaged)
                entries.append(entry)
                zip_write(archive, name, packaged)
                sha_lines.append(f"{entry['packaged_sha256']}  {name}")
                if name in (f"{EVIDENCE}/comparison.json", f"{EVIDENCE}/verification_summary.json"):
                    copies[PurePosixPath(name).name] = packaged
            metadata = dict(schema_version=1, scope="Final adaptive optimization validation evidence",
                            path_policy="Project paths become relative; external home paths use external/user-profile.",
                            historical_hash_policy="Original embedded hashes preserved; consult original_sha256 below.",
                            files=entries)
            for name, data in ((META, json_bytes(metadata)), (README, bundle_readme())):
                zip_write(archive, name, data)
                sha_lines.append(f"{digest(data)}  {name}")
            zip_write(archive, CHECKSUMS, ("\n".join(sorted(sha_lines, key=lambda s: s[66:])) + "\n").encode())
        report = verify_archive(temporary)
        report.update(archive=output.name, bytes=temporary.stat().st_size,
                      mib=round(temporary.stat().st_size / 1024**2, 3), sha256=digest(temporary.read_bytes()))
        os.replace(temporary, output)
        for name, data in copies.items():
            (output.parent / name).write_bytes(data)
        (output.parent / "package_report.json").write_bytes(json_bytes(report))
        return report
    finally:
        if temporary.exists():
            temporary.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=ROOT / "validation/lazor-validation.zip")
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    report = verify_archive(args.verify) if args.verify else package(args.root, args.output)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
