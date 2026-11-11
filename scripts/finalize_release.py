"""Gate a six-platform release on the exact ZIPs tested by isolated probes."""

import argparse
import hashlib
import json
from pathlib import Path

PLATFORMS = tuple(f"{system}-{arch}" for system in ("windows", "macos", "linux")
                  for arch in ("x64", "arm64"))


def sha256(path):
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def finalize(directory, version):
    files = []
    for platform in PLATFORMS:
        archive = directory / f"lazor-project-v{version}-{platform}.zip"
        report_path = directory / f"verification-{platform}.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        probes = {probe["mode"]: probe for probe in report["probes"]}
        if (report.get("passed") is not True or len(report["probes"]) != 2
                or set(probes) != {"direct", "editor"}
                or not all(probe.get("passed") is True and probe.get("isolated") is True
                           and probe.get("no_site") is True
                           and probe.get("runtime_platform") == platform
                           and type(probe.get("process_exit_code")) is int
                           and probe["process_exit_code"] == 0 for probe in probes.values())
                or report.get("archive_sha256") != sha256(archive)):
            raise ValueError(f"Missing or mismatched isolated verification: {platform}")
        files.extend((archive, report_path))
    alias = directory / f"lazor-project-v{version}.zip"
    if sha256(alias) != sha256(directory / f"lazor-project-v{version}-windows-x64.zip"):
        raise ValueError("The default ZIP must be identical to the verified Windows x64 package")
    files.extend((alias, directory / "lazor-search-tables.zip", directory / "lazor-validation.zip"))
    expected = {path.name for path in files} | {"SHA256SUMS.txt"}
    unexpected = {path.name for path in directory.iterdir()} - expected
    if unexpected:
        raise ValueError(f"Unexpected release assets: {sorted(unexpected)}")
    checksums = "".join(f"{sha256(path)}  {path.name}\n" for path in sorted(files))
    (directory / "SHA256SUMS.txt").write_text(checksums, encoding="utf-8")
    print(checksums, end="")
    print("All six native packages passed isolated SMT and editor worker verification.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    version = (Path(__file__).resolve().parents[1] / "VERSION").read_text().strip()
    finalize(args.directory.resolve(), version)
