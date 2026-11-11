# Optional optical-table database

Download `lazor-search-tables.zip` from this repository's GitHub Release and
extract it at the project root. It contains `data/search_tables.sqlite3`.
The standalone project release ZIP already includes this file.

Search also works without the database and computes missing tables lazily.
Use `--algorithm search --no-table-db` to disable the optional disk cache.
The adaptive bitboard cache is computed in memory and needs no download.

To rebuild the 41 geometry and 2019 static predecessor tables:

```powershell
python -m lazor.scaling generate --all-sizes --seed 20261110 --output-dir outputs/scaling_full
python -m lazor.table_database build data/search_tables.sqlite3 --manifest outputs/scaling_full/manifest.json --predecessors all
```

The builder refuses to replace an existing database unless `--overwrite` is
explicitly supplied. The SQLite file is about 101 MiB, so it is distributed as
a Release attachment rather than a normal Git file, following
[GitHub's large-file guidance](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github).

Release `SHA256SUMS.txt` verifies the downloaded archives. SQLite byte hashes
can differ when rebuilt with another SQLite version; table records also have
their own version-bound SHA-256 checksums, verified when loaded by the solver.
