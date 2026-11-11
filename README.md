# Lazor Project

The final optimized version (`v1.0.0`) reads Lazor `.bff` puzzles, solves block
placements, independently verifies the optical paths, and exports text, SVG,
and replayable `.bff` files. Its optimized exact search selects strategies
from the opening board and uses cached bit masks for batched reachability.

**Select `--algorithm search` to use the final optimized search.** Running
`--solve` without that option still uses Z3 SMT; the desktop editor also uses
SMT. Both solvers verify their answers with the independent simulator.

```powershell
python -m lazor boards/custom_test.bff --solve --algorithm search --timeout 10
```

Final validation recorded **zero errors and zero timeouts** on the complete
5,997-case matrix and 164 fresh-seed cases, with a one-second budget per case:

| Evaluation | Previous version | Final version | Time reduction |
| --- | ---: | ---: | ---: |
| 5,997 cases, square sizes 4–44 and A/C inventories 4–10 | 57.8085 s | 36.2246 s | 37.34% |
| 164 cases generated after the code was frozen | 2.0098 s | 1.3599 s | 32.34% |

These are same-machine measurements of generated fixtures, not a guarantee
for every puzzle. See [the final results, limits, and reproduction protocol](docs/adaptive_search_zh.md).

## Release downloads

Choose the named assets from this repository's release:

| Asset | Purpose |
| --- | --- |
| [lazor-project-v1.0.0.zip](https://github.com/yangcong0168-dot/lazor-project/releases/download/v1.0.0/lazor-project-v1.0.0.zip) | Complete release bundle for running the project |
| [lazor-search-tables.zip](https://github.com/yangcong0168-dot/lazor-project/releases/download/v1.0.0/lazor-search-tables.zip) | Optional precomputed SQLite tables; extract into the project root so the file is `data/search_tables.sqlite3` |
| [lazor-validation.zip](https://github.com/yangcong0168-dot/lazor-project/releases/download/v1.0.0/lazor-validation.zip) | Validation records accompanying this release |

[Release v1.0.0](https://github.com/yangcong0168-dot/lazor-project/releases/tag/v1.0.0) · [Checksums](https://github.com/yangcong0168-dot/lazor-project/releases/download/v1.0.0/SHA256SUMS.txt)

**The source works without the database.** Missing or incompatible tables
fall back to ordinary computation. Historical `tmp/` and `outputs/` directories
are not part of the source checkout. See [发布包与运行说明](docs/release_zh.md)
for the package layout and setup.

## Requirements and setup

- Python 3.10 or newer.
- Exact search, reading, simulation, and SVG output use the standard library.
- SMT planning, including the editor's solve action, requires
  `z3-solver==5.3.3.0` from `requirements.txt`.
- The desktop editor requires Python's Tkinter/Tcl/Tk support.
- Run commands from the project root.

Install the planner dependency into your selected Python environment:

```powershell
python -m pip install -r requirements.txt
```

Alternatively, keep the dependency local to this project:

```powershell
python -m pip install --only-binary=:all: --no-deps --target .deps -r requirements.txt
```

The dependency loader also checks the local `.deps` folder, which is ignored
by Git. Install the dependency when using SMT on a fresh checkout; the
`search` algorithm does not require it. Use a Python 3.10+ environment and
check the selected interpreter with `python --version`.

## Quick start

```powershell
# Inspect an input without solving.
python -m lazor boards/mad_1.bff

# Run the final optimized search and export the independently verified result.
python -m lazor boards/custom_test.bff --solve --algorithm search --timeout 10 --svg outputs/search_solution.svg --save-board outputs/search_solution.bff

# Use SMT and export its constraint model; SMT remains the default algorithm.
python -m lazor boards/mad_1.bff --solve --model outputs/mad_1.smt2 --output outputs/mad_1_planning.txt --svg outputs/mad_1_planning.svg --save-board outputs/mad_1_planning.bff

# Simulate fixed blocks, including a mirror loop.
python -m lazor boards/loop_demo.bff --simulate

# Inspect a manually supplied movable placement.
python -m lazor boards/placement_demo.bff --place C:1,1 --check --svg outputs/manual.svg

# Run tests and benchmark exactly the supplied boards.
python -m unittest discover -s tests -v
python -m lazor.benchmark boards --output outputs/planning_benchmark.json --timeout 110
```

## Generate tests and use optional tables

Generate the size/inventory matrix locally, then run a bounded sample:

```powershell
python -m lazor.scaling generate --all-sizes --output-dir outputs/scaling_full
python -m lazor.scaling run outputs/scaling_full/manifest.json --output outputs/scaling_smoke.json --methods smt search --timeout 2 --profile smoke
```

The full suite covers every square size 4 through 44 and independent A/C
counts 4 through 10. It contains 5,997 cases across planted open boards,
obstacles/multiple sources, and analytically impossible parity controls.
Ten over-capacity 4x4 inventory pairs are recorded as skipped. The current
results are documented in [the final adaptive-search report](docs/adaptive_search_zh.md).
Full-matrix runs use a version-isolated persistent worker, independent solution
replay, and append-only JSONL checkpoints:

```powershell
python -m lazor.full_benchmark --manifest outputs/scaling_full/manifest.json --root . --output outputs/my_full_run.jsonl --method search --timeout 1
```

Use a new output path for a changed solver or configuration. `--resume` accepts
only the same code, input hashes and run parameters.

For optional database acceleration, extract `lazor-search-tables.zip` into
the project root. Search checks `data/search_tables.sqlite3` and loads tables
only when its opening policy uses them. Small boards and eligible bitboard
boards skip full geometry-table decoding; unused tables stay on disk.
Missing, incompatible or damaged records fall back to normal computation.

Use `--table-db PATH` with `--solve --algorithm search` to select another
database, or `--no-table-db` to disable disk tables. `LAZOR_TABLE_DB=PATH`
(or `off`) provides the same selection for Python callers and benchmark
workers. For example:

```powershell
python -m lazor boards/custom_test.bff --solve --algorithm search --no-table-db
```

To build tables yourself after generating the matrix above:

```powershell
python -m lazor.table_database build data/search_tables.sqlite3 --manifest outputs/scaling_full/manifest.json --predecessors all
```

The build command refuses to replace an existing file unless `--overwrite`
is explicitly supplied. Rebuilding is unnecessary for ordinary starts.

Inspect the selected opening policy with
`lazor.search_policy.analyze_opening(puzzle)`. Python callers can use
`lazor.search.clear_search_caches()` to release retained tables and
`lazor.search.search_cache_info()` to inspect the three bounded caches.
Targets, dynamic assignments, and solutions are kept local to each solve.

## Optimization history

Read [the final adaptive-search report](docs/adaptive_search_zh.md) for this
version's results. The following documents are retained in development order;
each describes its own frozen baseline and measurements. Earlier timeout
counts and timings do not describe the final release.

| Stage | Documentation |
| --- | --- |
| Initial planner and fixture design | [Optimization measurements](docs/optimization_zh.md), [test matrix and protocol](docs/scaling_tests_zh.md) |
| Research and solver construction | [Research-backed changes](docs/research_optimization_zh.md), [131-case paired evaluation](docs/research_benchmark_zh.md) |
| Full-suite search changes | [Full-suite optimization](docs/full_suite_optimization_zh.md) |
| Search-loop work reduction | [Hot-path optimization](docs/hotpath_optimization_zh.md) |
| Reusable in-memory tables | [Space-for-time optimization](docs/space_time_optimization_zh.md) |
| Optional persistent tables | [SQLite database and loading costs](docs/table_database_zh.md) |
| Final opening policy and bitboards | [Final 5,997/164 results](docs/adaptive_search_zh.md) |

Historical paths under `tmp/` and `outputs/` refer to development artifacts.
They are not promised by a source checkout. The release's
`lazor-validation.zip` supplies the accompanying validation records; generating
the matrix above creates a new local `outputs/` directory for your own runs.

## Compare SMT with enumeration

To compare the current planner with a reconstructed, deduplicated DFS
enumeration baseline on the same board:

```powershell
python -m lazor.compare_solvers boards/custom_test.bff --output-dir outputs/comparison_new --timeout 110 --repeats 3
```

The output directory must be new. Trials run sequentially in fresh processes
against an input snapshot, with alternating method order. The JSON report
includes individual times, median times, candidate counts, code/input hashes,
and independent solution verification. Timeouts are recorded separately.
The baseline in `lazor/dfs_baseline.py` was reconstructed for this comparison;
it is not the original historical solver. The editor continues to use SMT.

Existing outputs require `--force` before replacement. The input cannot be
an output; output paths cannot collide with each other or act as another
output's parent directory. Saved files use UTF-8.

## Desktop test editor

Double-click `Launch Editor.cmd`, or run `python -m lazor.editor`. The optional
argument `boards/mad_1.bff` opens an existing puzzle. Tkinter/Tcl/Tk must be
installed with Python.

Click to place fixed A/B/C blocks, laser sources, and targets. Specify movable
inventory separately, save the original design as `.bff`, and use **约束规划求解**
to compute a placement. Runs occur in a cancellable child process. Results
are overlays and do not overwrite the original design. See the
[Chinese editor guide](docs/editor_zh.md).

Already-open editor processes use the code loaded when they started. Save
any edited puzzle before closing and reopening the editor after an update.

## Command line

| Argument | Behavior |
| --- | --- |
| `BOARD` only | Read, validate, and display the input |
| `--solve` | Solve with the selected algorithm; SMT is the default |
| `--algorithm smt\|search` | Choose SMT (default) or exact ray-frontier search; requires `--solve` |
| `--table-db PATH` | Select a read-only table database; requires `--solve --algorithm search` |
| `--no-table-db` | Compute tables without SQLite; requires `--solve --algorithm search` |
| `--model PATH` | Export the SMT-LIB model; requires `--solve` |
| `--timeout SECONDS` | Planning time budget, default 110 seconds |
| `--simulate` | Trace the supplied layout |
| `--place KIND:COL,ROW` | Add a manual movable block; repeat as needed |
| `--output PATH` | Save a report, or a failed/incomplete planning summary |
| `--svg PATH` | Save a self-contained optical diagram |
| `--save-board PATH` | Save the evaluated layout as a `.bff` snapshot |
| `--check` | Require the supplied layout to satisfy all solution conditions |
| `--force` | Allow replacing existing output files |

`--solve` cannot be combined with `--simulate` or `--place`. A manual placement
uses zero-based whole-cell `(column, row)`, not half-block laser coordinates.
Without `--solve`, placement/output/check options imply simulation.
`--model` requires the SMT algorithm. The editor continues to use the optimized
SMT planner; the search algorithm is available through the CLI and Python API.

Exit status is 0 on success, 1 for a failed check or an unsolved planning
result, and 2 for input, parameter, or file errors. A manual simulation may
exit 0 with missed targets because tracing succeeded; add `--check` to require
a solution. Unsuccessful planning does not write an SVG or BFF solution.
A completed model may still be exported for an unsatisfiable or incomplete run;
a timeout before model construction finishes may leave no model file.

## Constraint formulation

For each originally available `o` cell, one integer variable selects empty,
A, B, or C. Exact inventory equations constrain the number of each movable
block. Fixed blocks and `x` cells are constants.

The optical state is `(x, y, vx, vy)` at a face midpoint. Conditional directed
edges encode straight motion, reflection, absorption, and both C branches.
Boolean reachability variables and integer witness ranks constrain which
states can actually be reached from a source. Every target must have at least
one reachable directional state at its coordinates.

Forward closure includes every enabled branch. A reachable non-source state
must have a lower-rank reachable predecessor. This rules out isolated cycles
that would otherwise claim to illuminate themselves, while allowing genuine
source-connected mirror loops. Rank ordering applies to a witness predecessor,
not to every edge of the physical cycle.

This is a feasibility problem (equivalently, minimize a constant zero), with
no shortest-path or minimum-block objective. Z3 performs internal reasoning
and search; this is not a closed-form formula. The application does not
explicitly enumerate block placements using BFS. The simulator's work queue
only traces a fixed returned layout for independent verification.

See [the mathematical model and proof](docs/planning_model_zh.md) and
[the Chinese code walkthrough](docs/walkthrough_zh.md).

## Python API and result states

Use the final optimized search without a Z3 dependency:

```python
from lazor import load_bff
from lazor.search import solve_search

puzzle = load_bff("boards/custom_test.bff")
result = solve_search(puzzle, timeout=10)
if result.solved:
    print(result.solution.layout.placements)
else:
    print(result.status, result.reason)
```

For SMT planning and model export:

```python
from lazor import load_bff
from lazor.solver import solve_constraints
from lazor.output import write_svg

puzzle = load_bff("boards/mad_1.bff")
plan = solve_constraints(puzzle, timeout=110, model_path="outputs/model.smt2")
if plan.solved:
    print(plan.solution.layout.placements)
    write_svg(plan.solution, "outputs/solution.svg", search=plan)
else:
    print(plan.status, plan.reason)
```

| Status | Meaning |
| --- | --- |
| `solved` | A placement was found and passed independent simulation |
| `no_solution` | The selected solver proved that no valid placement exists under these optical rules |
| `timeout` | The planning budget expired; no conclusion of unsatisfiability |
| `unknown` | SMT could not decide; inspect `reason` |

`PlanningResult` reports build, solver, verification, and total times, plus
state/transition/variable/constraint counts and the Z3 version. These are
model statistics, not counts of enumerated candidate layouts. `search=` in
output functions accepts the exact successful result associated with the
supplied `SimulationResult`.

## File format and coordinates

One rectangular `GRID START` / `GRID STOP` section uses these cell symbols:

| Symbol | Meaning |
| --- | --- |
| `o` | Empty cell where a movable block may be placed |
| `x` | Empty cell that forbids block placement; light still passes |
| `A` | Fixed reflecting block |
| `B` | Fixed opaque block |
| `C` | Fixed refracting/splitting block |

Outside the grid, `A 2` declares two movable A blocks; fixed blocks do not use
that inventory. Omitted counts default to zero. `L x y vx vy` defines a source;
`P x y` defines a target. Multiple sources are supported, repeated targets are
deduplicated, and blank lines and `#` comments are accepted.

Cell indexing is `grid[row][column]`. Laser/target coordinates use half-block
units, with origin at the top left, x rightward, and y downward. A cell center
is `(2*column+1, 2*row+1)`. For width W and height H, coordinate bounds are
inclusive `0..2W` and `0..2H`.

Sources must be at face midpoints (one odd and one even coordinate), with
direction components each equal to +/-1. Corner/center sources are rejected.
Other legal integer targets remain valid but may be unreachable. Sources,
absorbing surfaces, and boundary endpoints count as target hits. Repeated
full optical states stop duplicate tracing without an arbitrary step limit.

## Outputs and validation scope

Text and SVG outputs identify the placement, target status, and planning
statistics. SVG generation needs no plotting library. PNG previews can be
converted separately; there is no `--png` flag.

BFF result exports are layout snapshots: currently placed blocks become fixed
cells and remaining inventory is retained. Comments preserve original versus
new block provenance. These snapshots reproduce the evaluated light paths;
keep the original puzzle to solve other placements.

The benchmark includes reading, model construction, solving, and independent
replay, but excludes process startup and output rendering. It records the
5/60/120 second thresholds for each listed input. Only `mad_1.bff` is copied
from the assignment example; the other included boards are teaching/regression
fixtures. The full official and hidden board sets have not been provided,
so their correctness and timing are not established by these local results.

The development machine's historical `outputs/` and `tmp/` directories are
ignored by Git and are not included in the source checkout. Older documents
may reference artifacts from earlier solver versions. Use the release's
`lazor-validation.zip` for its accompanying validation records,
`lazor-project-v1.0.0.zip` for the complete release bundle, and
`lazor-search-tables.zip` for the optional database. New output files can be
generated locally using the commands above.
