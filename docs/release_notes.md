# Lazor Project v1.0.0

Final exact-search and SMT puzzle solver, independent optical simulator,
Tk editor, CLI, examples and regression tests.

The optimized search selects its opening strategy from board dimensions,
inventory density and target count. It batches optimistic reachability with
cached bitboards, reuses static SQLite tables when useful and enters target
guidance earlier for large sparse constrained boards.

Local frozen-version validation:

- 5997 complete cases: 57.8085 → 36.2246 seconds (37.34% reduction).
- 164 post-freeze fresh seeds: 2.0098 → 1.3599 seconds (32.34% reduction).
- No errors or timeouts in either set, with the same 1-second per-case budget.
- 238 unit tests and 740 independent complete-layout oracle trials passed.
- Some cases regress; see `docs/adaptive_search_zh.md` and the full comparison.

Assets:

- `lazor-project-v1.0.0.zip`: final source, tests, docs, examples and database.
- `lazor-search-tables.zip`: optional database only, plus setup instructions.
- `lazor-validation.zip`: normalized validation records, input boards and
  frozen comparison versions; extracted paths match the report instructions.
- `SHA256SUMS.txt`: checksums for the three archives.

Python 3.10+ is required; Python 3.12 is validated. Install `requirements.txt`
for SMT. Exact search and simulation use the standard library. The desktop
editor additionally requires a Python installation with Tk.

```powershell
python -m pip install -r requirements.txt
python -m lazor boards/custom_test.bff --solve --algorithm search --timeout 10
python -m lazor.editor
```
