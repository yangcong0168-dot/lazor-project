"""Build and read versioned SQLite optical tables without executable data.

The database stores target-free dimension geometry and exact static-domain
predecessors. Rows use fixed-width little-endian signed integers, never
pickle. Checksums bind payloads to their version and complete lookup key.
Opening a reader checks metadata only; each requested row is validated and
decoded on demand. Missing, incompatible or damaged rows return ``None`` so
the caller can compute its normal table. Caller checkpoints still propagate.

Example: ``python -m lazor.table_database build data/search_tables.sqlite3
--manifest outputs/scaling_full/manifest.json --predecessors all``.
"""

import hashlib
from pathlib import Path
import sqlite3
import struct
from threading import RLock
from time import perf_counter


SCHEMA_VERSION = 1
OPTICS_VERSION = "face-midpoint-diagonal-v1"
ENCODING = "i32le"
_DIRECTIONS = ((-1, -1), (-1, 1), (1, -1), (1, 1))
_GEOMETRY_ROW = struct.Struct("<4i")
_PREDECESSOR_ROW = struct.Struct("<2i")


class InvalidTable(ValueError):
    """A table's metadata, checksum or decoded bounds are incompatible."""


def _state_count(width, height):
    if (type(width) is not int or type(height) is not int
            or width < 1 or height < 1):
        raise InvalidTable("Board dimensions must be positive integers.")
    count = 4 * ((2 * width + 1) * (2 * height + 1) // 2)
    if count > 2147483647:
        raise InvalidTable("Board states exceed the signed-int32 encoding.")
    return count


def _domain(width, height, domain):
    _state_count(width, height)
    if (not isinstance(domain, bytes) or len(domain) != width * height + 1
            or domain[-1] != 1 or any(mask < 1 or mask > 15 for mask in domain)):
        raise InvalidTable("Static domains must include every cell and empty outside.")
    return domain


def row_checksum(kind, width, height, domain, payload):
    """Hash a row's version, optical key and bytes, not merely its payload."""
    if kind not in ("geometry", "predecessors"):
        raise ValueError("Unknown optical table kind.")
    digest = hashlib.sha256()
    digest.update(f"lazor:{SCHEMA_VERSION}:{OPTICS_VERSION}:{ENCODING}:{kind}\0".encode("ascii"))
    digest.update(struct.pack("<III", width, height, len(domain)))
    digest.update(domain)
    digest.update(payload)
    return digest.hexdigest()


def _cell_at(x, y, vx, vy, width, height):
    column, row = x // 2, y // 2
    if not x & 1:
        column -= vx < 0
    else:
        row -= vy < 0
    return row * width + column if 0 <= column < width and 0 <= row < height else width * height


def _state_id(x, y, vx, vy, stride):
    return (((y * stride + x) >> 1) << 2) | ((vx > 0) << 1) | (vy > 0)


def _geometry_rows(width, height):
    count = _state_count(width, height)
    max_x, max_y, stride = 2 * width, 2 * height, 2 * width + 1
    for state in range(count):
        y, x = divmod(((state >> 2) << 1) | 1, stride)
        vx, vy = _DIRECTIONS[state & 3]
        rx, ry = (-vx, vy) if not x & 1 else (vx, -vy)
        front = _cell_at(x, y, vx, vy, width, height)
        rear = _cell_at(x, y, rx, ry, width, height)
        straight = (_state_id(x + vx, y + vy, vx, vy, stride)
                    if 0 <= x + vx <= max_x and 0 <= y + vy <= max_y else -1)
        reflected = (_state_id(x + rx, y + ry, rx, ry, stride)
                     if 0 <= x + rx <= max_x and 0 <= y + ry <= max_y else -1)
        yield front, rear, straight, reflected


def generate_geometry(width, height):
    """Generate one complete geometry table, for comparisons or small builds."""
    return list(_geometry_rows(width, height))


def _reverse_routes(width, height):
    """Precompute dimension-only inverse routes once for all domains of a size."""
    count = _state_count(width, height)
    max_x, max_y, stride = 2 * width, 2 * height, 2 * width + 1
    routes = []
    for state in range(count):
        y, x = divmod(((state >> 2) << 1) | 1, stride)
        vx, vy = _DIRECTIONS[state & 3]
        px, py = x - vx, y - vy
        if 0 <= px <= max_x and 0 <= py <= max_y:
            travel = _cell_at(px, py, vx, vy, width, height)
            first = _state_id(px, py, vx, vy, stride)
            rx, ry = (-vx, vy) if not px & 1 else (vx, -vy)
            front = _cell_at(px, py, rx, ry, width, height)
            second = _state_id(px, py, rx, ry, stride)
            routes.append((travel, first, front, second))
        else:
            routes.append((0, -1, 0, -1))
    return routes


def _predecessor_pairs(routes, domain):
    for travel, first, front, second in routes:
        if first >= 0 and domain[travel] & 9:
            yield first, second if domain[front] & 10 else -1
        else:
            yield -1, -1


def generate_predecessors(width, height, domain):
    """Generate one complete static predecessor table for an exact domain."""
    _domain(width, height, domain)
    return [tuple(state for state in pair if state >= 0)
            for pair in _predecessor_pairs(_reverse_routes(width, height), domain)]


def _encode(rows):
    from array import array
    import sys

    values = array("i")
    if values.itemsize != 4:
        raise RuntimeError("This builder requires 32-bit C int arrays.")
    for row in rows:
        values.extend(row)
    if sys.byteorder != "little":
        values.byteswap()
    return values.tobytes()


def graph_key_for_puzzle(puzzle):
    """Match search's original static domains, including empty inventory."""
    counts = puzzle.block_counts
    if any(type(counts.get(kind, 0)) is not int or counts.get(kind, 0) < 0
           for kind in ("A", "B", "C")):
        raise ValueError("Movable inventory must contain nonnegative integers.")
    available = len(puzzle.placeable_positions)
    total = sum(counts.values())
    if total > available:
        raise ValueError("Movable inventory exceeds available cells.")
    remaining = [available - total] + [counts.get(kind, 0) for kind in ("A", "B", "C")]
    mask = sum(1 << code for code, count in enumerate(remaining) if count)
    fixed = {"x": 1, "A": 2, "B": 4, "C": 8}
    domain = bytes(mask if kind == "o" else fixed[kind]
                   for row in puzzle.grid for kind in row) + b"\x01"
    return puzzle.width, puzzle.height, _domain(puzzle.width, puzzle.height, domain)


def collect_manifest_graph_keys(manifest):
    """Deduplicate exact static domains from a verified generated manifest."""
    import json

    from .bff import parse_bff

    path = Path(manifest)
    suite = json.loads(path.read_text(encoding="utf-8"))
    keys = set()
    for case in suite["cases"]:
        board_path = path.parent / case["board"]
        data = board_path.read_bytes()
        expected = case.get("input_sha256")
        if expected is not None and hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f"Manifest board checksum differs: {board_path}")
        puzzle = parse_bff(data.decode("utf-8-sig"), source=str(board_path))
        keys.add(graph_key_for_puzzle(puzzle))
    return sorted(keys)


def build_database(path, *, dimensions=None, graph_keys=(), overwrite=False, progress=None):
    """Build beside the destination, then atomically publish a complete file.

    A new destination is published with a no-overwrite hard link, preventing
    a check/publish race. Explicit replacement uses ``os.replace``. Failure
    before publishing removes the temporary file and preserves any old DB.
    """
    from datetime import datetime, timezone
    import os
    import tempfile

    started = perf_counter()
    destination = Path(path).absolute()
    if destination.exists() and not overwrite:
        raise FileExistsError(f"Destination already exists: {destination}")
    dimensions = sorted(set((n, n) for n in range(4, 45)) if dimensions is None
                        else set(tuple(pair) for pair in dimensions))
    keys = sorted(set((width, height, _domain(width, height, domain))
                      for width, height, domain in graph_keys))
    for width, height in dimensions:
        _state_count(width, height)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp",
                                           dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temp_name)
    connection = None
    geometry_bytes = predecessor_bytes = 0
    try:
        connection = sqlite3.connect(temporary)
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        connection.executescript("""
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
            CREATE TABLE geometry (
                width INTEGER NOT NULL, height INTEGER NOT NULL,
                state_count INTEGER NOT NULL, payload BLOB NOT NULL,
                checksum TEXT NOT NULL, PRIMARY KEY(width, height)
            ) WITHOUT ROWID;
            CREATE TABLE predecessors (
                width INTEGER NOT NULL, height INTEGER NOT NULL, domain BLOB NOT NULL,
                state_count INTEGER NOT NULL, payload BLOB NOT NULL,
                checksum TEXT NOT NULL, PRIMARY KEY(width, height, domain)
            ) WITHOUT ROWID;
        """)
        for index, (width, height) in enumerate(dimensions, 1):
            payload = _encode(_geometry_rows(width, height))
            connection.execute("INSERT INTO geometry VALUES(?,?,?,?,?)",
                (width, height, _state_count(width, height), payload,
                 row_checksum("geometry", width, height, b"", payload)))
            geometry_bytes += len(payload)
            if progress is not None:
                progress({"kind": "geometry", "completed": index, "total": len(dimensions)})
        previous_size, routes = None, None
        for index, (width, height, domain) in enumerate(keys, 1):
            if previous_size != (width, height):
                routes = _reverse_routes(width, height)
                previous_size = width, height
            payload = _encode(_predecessor_pairs(routes, domain))
            connection.execute("INSERT INTO predecessors VALUES(?,?,?,?,?,?)",
                (width, height, domain, _state_count(width, height), payload,
                 row_checksum("predecessors", width, height, domain, payload)))
            predecessor_bytes += len(payload)
            if progress is not None:
                progress({"kind": "predecessors", "completed": index, "total": len(keys)})
        metadata = {
            "schema_version": str(SCHEMA_VERSION), "optics_version": OPTICS_VERSION,
            "encoding": ENCODING, "complete": "1",
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "geometry_tables": str(len(dimensions)), "predecessor_tables": str(len(keys)),
        }
        connection.executemany("INSERT INTO metadata VALUES(?,?)", metadata.items())
        connection.commit()
        connection.close()
        connection = None
        # Windows' CRT _commit (os.fsync) requires a writable descriptor.
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, destination)
        else:
            os.link(temporary, destination)
            temporary.unlink()
        return {"path": str(destination), "geometry_tables": len(dimensions),
                "predecessor_tables": len(keys), "geometry_payload_bytes": geometry_bytes,
                "predecessor_payload_bytes": predecessor_bytes,
                "database_bytes": destination.stat().st_size,
                "elapsed_seconds": perf_counter() - started}
    finally:
        if connection is not None:
            connection.close()
        temporary.unlink(missing_ok=True)
        Path(str(temporary) + "-journal").unlink(missing_ok=True)


class TableDatabase:
    """Read-only, metadata-light reader with safe per-table fallback."""

    def __init__(self, path):
        started = perf_counter()
        self.path = Path(path).absolute()
        self._lock = RLock()
        self._connection = None
        self.metadata = {}
        self._stats = {"read_seconds": 0.0, "tables_loaded": 0, "misses": 0,
                       "failures": 0, "geometry_loaded": 0, "predecessors_loaded": 0,
                       "bytes_read": 0, "last_error": ""}
        connection = None
        try:
            # This optional cache must not spend SQLite's default five-second
            # busy wait blocking a short solver deadline behind another writer.
            connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True,
                                         timeout=0.0, check_same_thread=False)
            connection.execute("PRAGMA query_only=ON")
            metadata = dict(connection.execute("SELECT key,value FROM metadata"))
            expected = {"schema_version": str(SCHEMA_VERSION), "optics_version": OPTICS_VERSION,
                        "encoding": ENCODING, "complete": "1"}
            if any(metadata.get(key) != value for key, value in expected.items()):
                raise InvalidTable("Database schema, optics or encoding version is incompatible.")
            if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise InvalidTable("SQLite schema version is incompatible.")
            self.metadata = metadata
            self._connection = connection
        except (sqlite3.Error, OSError, InvalidTable) as error:
            if connection is not None:
                connection.close()
            self._stats["failures"] += 1
            self._stats["last_error"] = str(error)
        self._open_seconds = perf_counter() - started

    @property
    def available(self):
        with self._lock:
            return self._connection is not None

    def close(self):
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def info(self):
        with self._lock:
            return {"path": str(self.path), "available": self._connection is not None,
                    "open_seconds": self._open_seconds, **self._stats}

    def _load(self, kind, width, height, domain, state_count, checkpoint):
        started = perf_counter()
        with self._lock:
            try:
                if checkpoint is not None:
                    checkpoint()
                expected = _state_count(width, height)
                if state_count is not None and (type(state_count) is not int or state_count != expected):
                    raise InvalidTable("Requested state count does not match dimensions.")
                if kind == "predecessors":
                    _domain(width, height, domain)
                if self._connection is None:
                    self._stats["misses"] += 1
                    return None
                if kind == "geometry":
                    row = self._connection.execute(
                        "SELECT state_count,payload,checksum FROM geometry WHERE width=? AND height=?",
                        (width, height)).fetchone()
                    decoder = _GEOMETRY_ROW
                else:
                    row = self._connection.execute(
                        "SELECT state_count,payload,checksum FROM predecessors WHERE width=? AND height=? AND domain=?",
                        (width, height, domain)).fetchone()
                    decoder = _PREDECESSOR_ROW
                if row is None:
                    self._stats["misses"] += 1
                    return None
                count, payload, checksum = row
                if type(count) is not int or count != expected:
                    raise InvalidTable("Stored state count does not match dimensions.")
                if not isinstance(payload, bytes) or len(payload) != expected * decoder.size:
                    raise InvalidTable("Stored payload has an incorrect binary length.")
                self._stats["bytes_read"] += len(payload)
                if checksum != row_checksum(kind, width, height, domain, payload):
                    raise InvalidTable("Stored optical table checksum differs.")
                result = []
                for index, values in enumerate(decoder.iter_unpack(payload)):
                    if checkpoint is not None and index & 255 == 0:
                        checkpoint()
                    if kind == "geometry":
                        front, rear, straight, reflected = values
                        if not (0 <= front <= width * height and 0 <= rear <= width * height
                                and -1 <= straight < count and -1 <= reflected < count):
                            raise InvalidTable("Geometry contains an out-of-bounds cell or state.")
                        result.append(values)
                    else:
                        first, second = values
                        if not (-1 <= first < count and -1 <= second < count
                                and (first >= 0 or second < 0)):
                            raise InvalidTable("Predecessors contain an invalid state or padding.")
                        result.append(() if first < 0 else (first,) if second < 0 else values)
                if checkpoint is not None:
                    checkpoint()
                self._stats["tables_loaded"] += 1
                self._stats[kind + "_loaded"] += 1
                return result
            except (sqlite3.Error, InvalidTable) as error:
                self._stats["failures"] += 1
                self._stats["last_error"] = str(error)
                return None
            finally:
                self._stats["read_seconds"] += perf_counter() - started

    def geometry(self, width, height, state_count=None, *, checkpoint=None):
        return self._load("geometry", width, height, b"", state_count, checkpoint)

    def predecessors(self, graph_key, state_count=None, *, checkpoint=None):
        width, height, domain = graph_key
        return self._load("predecessors", width, height, domain, state_count, checkpoint)

    def _keys(self, kind):
        with self._lock:
            if self._connection is None:
                return []
            try:
                if kind == "geometry":
                    keys = self._connection.execute("SELECT width,height FROM geometry ORDER BY width,height").fetchall()
                    for width, height in keys:
                        _state_count(width, height)
                else:
                    keys = self._connection.execute("SELECT width,height,domain FROM predecessors ORDER BY width,height,domain").fetchall()
                    for width, height, domain in keys:
                        _domain(width, height, domain)
                return keys
            except (sqlite3.Error, InvalidTable) as error:
                self._stats["failures"] += 1
                self._stats["last_error"] = str(error)
                return []

    def geometry_keys(self):
        return self._keys("geometry")

    def predecessor_keys(self):
        return self._keys("predecessors")


def main(argv=None):
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="Build versioned optical tables atomically.")
    build.add_argument("path", type=Path)
    build.add_argument("--min-size", type=int, default=4)
    build.add_argument("--max-size", type=int, default=44)
    build.add_argument("--manifest", type=Path)
    build.add_argument("--predecessors", choices=("none", "all"), default="none")
    build.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    if args.min_size < 1 or args.max_size < args.min_size:
        parser.error("Require 1 <= min-size <= max-size.")
    if args.predecessors == "all" and args.manifest is None:
        parser.error("--predecessors all requires --manifest.")
    started = perf_counter()
    graph_keys = collect_manifest_graph_keys(args.manifest) if args.predecessors == "all" else ()
    def progress(update):
        if (update["kind"] == "geometry" and update["completed"] == update["total"]
                or update["kind"] == "predecessors"
                and (update["completed"] % 100 == 0 or update["completed"] == update["total"])):
            print(json.dumps(update), file=sys.stderr, flush=True)
    report = build_database(args.path,
        dimensions=[(size, size) for size in range(args.min_size, args.max_size + 1)],
        graph_keys=graph_keys, overwrite=args.overwrite, progress=progress)
    report["command_elapsed_seconds"] = perf_counter() - started
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
