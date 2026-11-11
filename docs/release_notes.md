# Lazor Project v1.0.1 — bundled dependency correction

The v1.0.0 runtime ZIP omitted Z3 and its native library. Calling it a complete
runtime package was incorrect: default SMT solving and the editor's solve
worker could fail on a computer without a separate Z3 installation.

v1.0.1 provides six platform runtime ZIPs. Each includes the project,
`.deps` with Z3 Python bindings and the matching native library, and
`data/search_tables.sqlite3`. These packages do not require `pip install`.
The local Python interpreter and GUI Tk support are still prerequisites.

## Download the matching platform package

Start at the [latest release](https://github.com/yangcong0168-dot/lazor-project/releases/latest).
Choose by the **Python process architecture**, including when Python runs
under emulation. An x86-64 Python on an ARM computer needs the x64 package.

| Asset | Platform requirement |
| --- | --- |
| [lazor-project-v1.0.1-windows-x64.zip](https://github.com/yangcong0168-dot/lazor-project/releases/download/v1.0.1/lazor-project-v1.0.1-windows-x64.zip) | Windows, x86-64 Python |
| [lazor-project-v1.0.1-windows-arm64.zip](https://github.com/yangcong0168-dot/lazor-project/releases/download/v1.0.1/lazor-project-v1.0.1-windows-arm64.zip) | Windows, ARM64 Python |
| [lazor-project-v1.0.1-macos-x64.zip](https://github.com/yangcong0168-dot/lazor-project/releases/download/v1.0.1/lazor-project-v1.0.1-macos-x64.zip) | macOS 13+, x86-64 Python |
| [lazor-project-v1.0.1-macos-arm64.zip](https://github.com/yangcong0168-dot/lazor-project/releases/download/v1.0.1/lazor-project-v1.0.1-macos-arm64.zip) | macOS 13+, ARM64 Python |
| [lazor-project-v1.0.1-linux-x64.zip](https://github.com/yangcong0168-dot/lazor-project/releases/download/v1.0.1/lazor-project-v1.0.1-linux-x64.zip) | Linux, x86-64 Python, glibc 2.27+ |
| [lazor-project-v1.0.1-linux-arm64.zip](https://github.com/yangcong0168-dot/lazor-project/releases/download/v1.0.1/lazor-project-v1.0.1-linux-arm64.zip) | Linux, ARM64 Python, glibc 2.38+ |

[lazor-project-v1.0.1.zip](https://github.com/yangcong0168-dot/lazor-project/releases/download/v1.0.1/lazor-project-v1.0.1.zip)
is a compatibility alias for **Windows x64 only**.
[SHA256SUMS.txt](https://github.com/yangcong0168-dot/lazor-project/releases/download/v1.0.1/SHA256SUMS.txt)
contains the v1.0.1 asset checksums.

GitHub's automatic **Source code (zip)** and **Source code (tar.gz)** files
are source-only downloads without `.deps` or the database. For those archives
or a clone, install `requirements.txt` with the Python interpreter that will
run SMT. They are not platform runtime packages.

## Run

Use Python 3.10+; Python 3.12 is recommended. Extract the whole platform package
and keep its `.deps` directory. For the editor, Python must include
Tkinter/Tcl/Tk. Run from the project root:

```powershell
python -m lazor boards/mad_1.bff --solve --algorithm smt --timeout 110
python -m lazor boards/custom_test.bff --solve --algorithm search --timeout 10
python -m lazor.editor
```

SMT remains the default for `--solve` and for the editor. Select
`--algorithm search` explicitly for the optimized frontier search.
Source-only installations first run:

```powershell
python -m pip install -r requirements.txt
```

See [the Chinese package guide](release_zh.md) for layout and configuration.

## Validation scope

The v1.0.1 packaging checks concern extracted-package dependency loading,
SMT solving and the editor worker. Their actual results and tested platforms
are recorded separately from algorithm benchmarks.

The local Windows x64 candidate passed both fresh `python -I -S` probes:
native Z3 SAT/UNSAT, a real SMT puzzle and independently replayed solution,
plus the editor's separate spawned solve worker. The loaded DLL path was
verified to be inside the extracted ZIP. All 261 local regression tests passed,
including dependency selection and release-gate rejection tests.

Publication requires all six native runners to verify their actual final ZIPs.
Each release includes `verification-<platform>.json` with the archive hash,
runtime platform and isolated probe results. The release gate checks these
against the uploaded ZIPs before publishing `SHA256SUMS.txt`.

The following results belong to the **v1.0.0 frozen algorithm validation**;
they are not claimed as a new 5,997-case run for this packaging correction:

- 5,997 complete cases: 57.8085 → 36.2246 seconds (37.34% reduction).
- 164 post-freeze fresh-seed cases: 2.0098 → 1.3599 seconds (32.34% reduction).
- No errors or timeouts in either set, with a one-second per-case budget.
- 238 unit tests and 740 independent complete-layout oracle trials passed
  during that algorithm release.
- Some cases regress; see [the algorithm report](adaptive_search_zh.md).

The unchanged [database-only archive](https://github.com/yangcong0168-dot/lazor-project/releases/download/v1.0.0/lazor-search-tables.zip)
remains optional for source users. The [v1.0.0 validation archive](https://github.com/yangcong0168-dot/lazor-project/releases/download/v1.0.0/lazor-validation.zip)
contains the historical records, input boards and frozen comparison versions.
