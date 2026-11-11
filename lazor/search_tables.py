"""Bounded, reusable optical tables for frontier search.

Geometry depends only on board dimensions and contains no target bits.
Static predecessors depend on dimensions and each original cell's optical
domain. Their key includes the initially available inventory kinds, fixed
cells and the outside cell, but no sources or targets. These tables never
store a search node's assignments, remaining inventory or visited states.

Each cache retains at most 65,536 state slots, rather than imposing a hard
process-RSS limit. Slots fill lazily with immutable tuples. A table larger
than the capacity is constructed for its caller without being retained.
Active searches can hold references after eviction or cache clearing, so
their live memory is additional to the retained LRU capacity.
"""

from collections import OrderedDict
import os
from pathlib import Path
from threading import RLock


class WeightedLRU:
    """Keep complete tables under a capacity measured in state slots.

    Cache metadata and table allocation are synchronized. Callers may fill
    independent table slots with deterministic immutable tuples; concurrent
    duplicate calculations have the same value and do not change metadata.
    """

    def __init__(self, capacity: int = 65536):
        if type(capacity) is not int or capacity < 0:
            raise ValueError("Cache capacity must be a nonnegative integer.")
        self.capacity = capacity
        self._entries = OrderedDict()
        self._weight = 0
        self._hits = self._misses = self._evictions = 0
        # A timed database factory may invoke the caller's progress hook;
        # allow that same thread to inspect or clear cache metadata.
        self._lock = RLock()

    def get(self, key, weight: int, factory):
        """Get one table; build an uncached value if it exceeds capacity."""
        if type(weight) is not int or weight < 0:
            raise ValueError("Cache weight must be a nonnegative integer.")
        with self._lock:
            entry = self._entries.pop(key, None)
            if entry is not None:
                self._hits += 1
                self._entries[key] = entry
                return entry[1]
            self._misses += 1
            value = factory()
            # Progress callbacks may reenter the cache and finish this key
            # while its outer factory is running. Reuse that publication.
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
                return entry[1]
            if weight <= self.capacity:
                while self._weight + weight > self.capacity:
                    _, (old_weight, _) = self._entries.popitem(last=False)
                    self._weight -= old_weight
                    self._evictions += 1
                self._entries[key] = weight, value
                self._weight += weight
            return value

    def clear(self):
        """Drop retained tables and reset counters; active users remain valid."""
        with self._lock:
            self._entries.clear()
            self._weight = 0
            self._hits = self._misses = self._evictions = 0

    def stats(self):
        """Return a synchronized copy of cache metadata."""
        with self._lock:
            return {
                "capacity": self.capacity,
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
                "state_slots": self._weight,
                "entries": len(self._entries),
            }


class SearchTables:
    """Separate caches for geometry, static predecessors and dimension masks."""

    def __init__(self, capacity: int = 65536, *, database=None):
        self._geometries = WeightedLRU(capacity)
        self._predecessors = WeightedLRU(capacity)
        self._bitboards = WeightedLRU(capacity)
        self._configuration_lock = RLock()
        self._database_path = Path(database).resolve() if database is not None else None
        self._database = None

    def _load(self, kind, key, state_count, checkpoint, use_database=True):
        if not use_database or self._database_path is None:
            return [None] * state_count
        if checkpoint is not None:
            checkpoint()
        table = None
        if self._database_path is not None:
            if self._database is None:
                # Loading SQLite is deferred until a solver actually needs
                # a persistent table. Importing lazor never scans the DB.
                from .table_database import TableDatabase
                self._database = TableDatabase(self._database_path)
            if kind == "geometry":
                table = self._database.geometry(*key, state_count=state_count, checkpoint=checkpoint)
            else:
                table = self._database.predecessors(key, state_count=state_count, checkpoint=checkpoint)
        if checkpoint is not None:
            checkpoint()
        return table if table is not None else [None] * state_count

    def geometry(self, width: int, height: int, state_count: int, *, checkpoint=None, use_database=True):
        """Return lazy (front, rear, transmitted, reflected) state entries."""
        with self._configuration_lock:
            return self._geometries.get(
                (width, height), state_count,
                lambda: self._load("geometry", (width, height), state_count, checkpoint, use_database)
            )

    def predecessors(self, graph_key, state_count: int, *, checkpoint=None, use_database=True):
        """Return lazy predecessor tuples for (width, height, domain_bytes)."""
        with self._configuration_lock:
            return self._predecessors.get(
                graph_key, state_count,
                lambda: self._load("predecessors", graph_key, state_count, checkpoint, use_database)
            )

    def bitboards(self, width, height, *, checkpoint):
        """Cache immutable dimension masks, with a hard per-table state cap."""
        from .bitboard import MAX_STATES, compile_bitboards
        if (type(width) is not int or type(height) is not int
                or min(width, height) < 1):
            raise ValueError("Invalid or oversized bitboard dimensions.")
        count = 4 * ((2 * width + 1) * (2 * height + 1) // 2)
        if count > MAX_STATES:
            raise ValueError("Invalid or oversized bitboard dimensions.")
        built = False

        def build():
            nonlocal built
            built = True
            return compile_bitboards(width, height, checkpoint)

        with self._configuration_lock:
            table = self._bitboards.get((width, height), count, build)
        return table, not built

    def clear(self):
        with self._configuration_lock:
            self._geometries.clear()
            self._predecessors.clear()
            self._bitboards.clear()

    def configure_database(self, path):
        """Switch persistent storage and clear RAM tables; return the old path.

        None disables disk storage. Reconfiguring the same path also closes
        the reader, which is useful for a fresh-read benchmark or replacement.
        Existing solve-local references remain valid immutable table entries.
        """
        with self._configuration_lock:
            old_path = self._database_path
            if self._database is not None:
                self._database.close()
                self._database = None
            self._database_path = Path(path).resolve() if path is not None else None
            self.clear()
            return old_path

    def database_info(self):
        with self._configuration_lock:
            return {"path": str(self._database_path) if self._database_path is not None else None,
                    "opened": self._database is not None,
                    "reader": self._database.info() if self._database is not None else None}

    def info(self):
        return {
            "geometry": self._geometries.stats(),
            "predecessors": self._predecessors.stats(),
            "bitboards": self._bitboards.stats(),
        }


DEFAULT_DATABASE = Path(__file__).resolve().parents[1] / "data" / "search_tables.sqlite3"
_database_setting = os.environ.get("LAZOR_TABLE_DB")
TABLES = SearchTables(database=(None if _database_setting == "off" else
                               _database_setting or DEFAULT_DATABASE))


def clear_search_caches():
    """Clear tables retained between solves, for cold runs or memory release."""
    TABLES.clear()


def search_cache_info():
    """Return retained state-slot counts and LRU hit/miss statistics."""
    return TABLES.info()


def configure_search_database(path):
    """Select a read-only table database, or None to compute without disk tables."""
    return TABLES.configure_database(path)


def search_database_info():
    """Inspect persistent-table loading separately from in-memory LRU hits."""
    return TABLES.database_info()
