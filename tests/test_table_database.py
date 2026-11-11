"""Persistent optical-table compatibility, isolation and safe fallback tests."""

from collections import Counter
from contextlib import closing
from dataclasses import replace
import hashlib
from pathlib import Path
import sqlite3
import struct
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

from lazor.models import Laser, Puzzle
import lazor.search as search
import lazor.search_tables as tables
from lazor.simulation import simulate
from lazor.table_database import TableDatabase, build_database, row_checksum


COUNTERS = ("candidates_tested", "states_expanded", "ray_states_processed",
            "branches_pruned", "optimistic_checks", "forced_assignments",
            "restarts", "assignment_writes", "implicit_cell_reads")


def state_count(width, height):
    return 4 * (((2 * width + 1) * (2 * height + 1)) // 2)


def graph_key(puzzle):
    codes = {"o": -1, "x": 0, "A": 1, "B": 2, "C": 3}
    counts = [len(puzzle.placeable_positions) - sum(puzzle.block_counts.values())]
    counts += [puzzle.block_counts[kind] for kind in "ABC"]
    domain = sum(1 << code for code, count in enumerate(counts) if count)
    return puzzle.width, puzzle.height, bytes(
        [domain if kind == "o" else 1 << codes[kind]
         for row in puzzle.grid for kind in row] + [1])


def puzzle_variants():
    initial = Puzzle((("o", "o"), ("o", "o")), {"A": 1, "B": 0, "C": 1},
                     (Laser(2, 1, 1, 1), Laser(2, 3, -1, -1)),
                     frozenset([(1, 2), (3, 2)]))
    return [initial,
            replace(initial, targets=frozenset([(1, 0), (3, 4)])),
            replace(initial, targets=frozenset([(0, 1), (4, 3)])),
            replace(initial, block_counts={"A": 2, "B": 0, "C": 2}),
            replace(initial, block_counts={"A": 0, "B": 1, "C": 2}),
            replace(initial, grid=(("A", "o"), ("o", "o"))),
            replace(initial, grid=(("B", "o"), ("o", "o"))),
            replace(initial, grid=(("x", "o"), ("o", "o"))),
            replace(initial, lasers=(Laser(0, 1, 1, 1), Laser(4, 3, -1, -1))),
            replace(initial, grid=(("o", "o", "x"), ("o", "o", "C")),
                    targets=frozenset([(0, 1), (6, 3)])),
            replace(initial, grid=(("o", "o"), ("x", "C"), ("o", "o")),
                    targets=frozenset([(0, 1), (4, 5)])),
            replace(initial, block_counts={"A": 0, "B": 4, "C": 0})]


def signature(result):
    placement = None if not result.solved else result.solution.layout.placements
    return result.status, placement, tuple(getattr(result, name) for name in COUNTERS)


class TableDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "tables.sqlite3"
        previous = tables.configure_search_database(None)
        self.addCleanup(tables.configure_search_database, previous)
        # Exercise the persistence layer even though the default opening
        # policy deliberately avoids disk reads for these tiny test boards.
        original_choose = search.choose_opening
        def database_policy(*arguments, **kwargs):
            return replace(original_choose(*arguments, **kwargs), use_bitboard=False,
                           geometry_database=True, predecessor_database=True)
        policy_patch = patch.object(search, "choose_opening", side_effect=database_policy)
        policy_patch.start()
        self.addCleanup(policy_patch.stop)

    def build(self, path=None, puzzles=None, **kwargs):
        puzzles = puzzle_variants() if puzzles is None else puzzles
        return build_database(path or self.path,
                              dimensions=sorted({(p.width, p.height) for p in puzzles}),
                              graph_keys=sorted({graph_key(p) for p in puzzles}), **kwargs)

    def open(self, path=None):
        database = TableDatabase(path or self.path)
        self.addCleanup(database.close)
        return database

    def test_missing_database_and_missing_record_are_safe_misses(self):
        missing = self.open()
        self.assertIsNone(missing.geometry(2, 2))
        self.assertIsNone(missing.predecessors(graph_key(puzzle_variants()[0])))
        self.assertFalse(self.path.exists(), "A read must not create a SQLite file")
        self.build(puzzles=puzzle_variants()[:1])
        database = self.open()
        self.assertIsNone(database.geometry(3, 2))
        self.assertIsNone(database.predecessors((2, 2, bytes([14] * 4 + [1]))))

    def test_non_sqlite_file_is_a_safe_miss(self):
        payload = b"This is not a SQLite optical database."
        self.path.write_bytes(payload)
        database = self.open()
        self.assertIsNone(database.geometry(2, 2))
        self.assertEqual(self.path.read_bytes(), payload)

    def test_exclusive_writer_lock_falls_back_without_default_five_second_wait(self):
        self.build(puzzles=puzzle_variants()[:1])
        completed = []
        def read():
            with TableDatabase(self.path) as database:
                completed.append(database.geometry(2, 2))
        with closing(sqlite3.connect(self.path)) as writer:
            writer.execute("BEGIN EXCLUSIVE")
            worker = Thread(target=read, daemon=True)
            worker.start()
            worker.join(timeout=1)
            waiting = worker.is_alive()
            writer.rollback()
        worker.join(timeout=1)
        self.assertFalse(waiting, "Optional database reader waited on a writer lock")
        self.assertEqual(completed, [None])

    def test_incompatible_or_incomplete_metadata_is_rejected(self):
        for key, value in (("schema_version", "999"), ("optics_version", "unknown-optics"),
                           ("encoding", "i64be"), ("complete", "0")):
            with self.subTest(key=key):
                self.build(puzzles=puzzle_variants()[:1], overwrite=self.path.exists())
                with closing(sqlite3.connect(self.path)) as connection, connection:
                    connection.execute("UPDATE metadata SET value=? WHERE key=?", (value, key))
                with TableDatabase(self.path) as database:
                    self.assertFalse(database.available)
                    self.assertIsNone(database.geometry(2, 2))
                    self.assertIsNone(database.predecessors(graph_key(puzzle_variants()[0])))

    def test_corrupt_rows_are_misses_and_never_poison_search_cache(self):
        puzzle = puzzle_variants()[0]
        with patch.object(search, "_RESTART_AFTER", 0):
            expected = signature(search.solve_search(puzzle, timeout=None))
        # A wrong checksum covers ordinary file damage. Recomputed checksums
        # deliberately reach the independent shape and numeric-range checks.
        damages = ("checksum", "length", "cell_index", "successor_index",
                   "state_count", "predecessor_index", "predecessor_padding")
        for damage in damages:
            with self.subTest(damage=damage):
                tables.configure_search_database(None)
                self.build(puzzles=[puzzle], overwrite=self.path.exists())
                predecessor = damage.startswith("predecessor")
                kind = "predecessors" if predecessor else "geometry"
                domain = graph_key(puzzle)[2] if predecessor else b""
                with closing(sqlite3.connect(self.path)) as connection, connection:
                    payload = connection.execute(f"SELECT payload FROM {kind} WHERE width=2 AND height=2").fetchone()[0]
                    payload = bytearray(payload)
                    if damage == "length":
                        payload.pop()
                    elif damage == "cell_index":
                        struct.pack_into("<i", payload, 0, 5)
                    elif damage == "successor_index":
                        struct.pack_into("<i", payload, 8, state_count(2, 2))
                    elif damage == "predecessor_index":
                        struct.pack_into("<i", payload, 0, state_count(2, 2))
                    elif damage == "predecessor_padding":
                        struct.pack_into("<ii", payload, 0, -1, 0)
                    checksum = ("0" * 64 if damage == "checksum"
                                else row_checksum(kind, 2, 2, domain, bytes(payload)))
                    connection.execute(f"UPDATE {kind} SET payload=?, checksum=?", (bytes(payload), checksum))
                    if damage == "state_count":
                        connection.execute("UPDATE geometry SET state_count=state_count+1")
                with TableDatabase(self.path) as database:
                    result = (database.predecessors(graph_key(puzzle)) if predecessor
                              else database.geometry(2, 2))
                    self.assertIsNone(result)
                tables.configure_search_database(self.path)
                with patch.object(search, "_RESTART_AFTER", 0):
                    actual = search.solve_search(puzzle, timeout=None)
                    self.assertEqual(signature(actual), expected)
                    self.assertEqual(signature(search.solve_search(puzzle, timeout=None)), expected)
        tables.configure_search_database(None)

    def test_rectangular_dimensions_and_static_domains_are_isolated(self):
        self.build()
        database = self.open()
        horizontal = database.geometry(3, 2)
        vertical = database.geometry(2, 3)
        self.assertEqual(len(horizontal), state_count(3, 2))
        self.assertEqual(len(vertical), state_count(2, 3))
        self.assertNotEqual(horizontal, vertical)
        self.assertIsNone(database.geometry(2, 2, state_count(2, 2) + 1))
        puzzles = puzzle_variants()
        first = database.predecessors(graph_key(puzzles[0]))
        self.assertEqual(first, database.predecessors(graph_key(puzzles[1])))
        self.assertNotEqual(first, database.predecessors(graph_key(puzzles[-1])))
        self.assertNotEqual(first, database.predecessors(graph_key(puzzles[5])))

    def test_checkpoint_exception_is_not_swallowed_as_corruption(self):
        self.build(puzzles=puzzle_variants()[:1])
        database = self.open()
        class Interrupted(Exception):
            pass
        def checkpoint():
            raise Interrupted("deadline")
        with self.assertRaises(Interrupted):
            database.geometry(2, 2, checkpoint=checkpoint)
        with self.assertRaises(Interrupted):
            database.predecessors(graph_key(puzzle_variants()[0]), checkpoint=checkpoint)
        self.assertIsNotNone(database.geometry(2, 2))

    def test_interrupted_decode_is_not_published_to_memory_cache(self):
        self.build(puzzles=puzzle_variants()[:1])
        tables.configure_search_database(self.path)
        class Interrupted(Exception):
            pass
        calls = 0
        def checkpoint():
            nonlocal calls
            calls += 1
            if calls >= 2:
                raise Interrupted("decode interrupted")
        with self.assertRaises(Interrupted):
            tables.TABLES.geometry(2, 2, state_count(2, 2), checkpoint=checkpoint)
        self.assertEqual(tables.search_cache_info()["geometry"]["entries"], 0)
        loaded = tables.TABLES.geometry(2, 2, state_count(2, 2))
        self.assertTrue(all(value is not None for value in loaded))

    def test_decode_checkpoint_can_read_stats_clear_and_disable_database(self):
        self.build(puzzles=puzzle_variants()[:1])
        tables.configure_search_database(self.path)
        calls = 0
        def checkpoint():
            nonlocal calls
            calls += 1
            tables.search_cache_info()
            if calls == 3:  # Outer load, reader entry, then first decode row.
                tables.clear_search_caches()
                tables.configure_search_database(None)
        loaded = tables.TABLES.geometry(2, 2, state_count(2, 2), checkpoint=checkpoint)
        self.assertGreaterEqual(calls, 3)
        self.assertTrue(all(value is not None for value in loaded))
        self.assertIsNone(tables.search_database_info()["path"])
        puzzle = puzzle_variants()[0]
        warm = search.solve_search(puzzle, timeout=None)
        tables.clear_search_caches()
        cold = search.solve_search(puzzle, timeout=None)
        self.assertEqual(signature(warm), signature(cold))

    def test_failed_atomic_publish_preserves_previous_database(self):
        self.build(puzzles=puzzle_variants()[:1])
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with patch("os.replace", side_effect=OSError("publish failed")):
            with self.assertRaises(OSError):
                self.build(overwrite=True)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)
        self.assertIsNotNone(self.open().geometry(2, 2))

    def test_database_and_no_database_preserve_results_and_all_counters(self):
        puzzles = puzzle_variants()
        self.build()
        expected = []
        with patch.object(search, "_RESTART_AFTER", 0):
            for puzzle in puzzles:
                tables.clear_search_caches()
                expected.append(signature(search.solve_search(puzzle, timeout=None)))
            tables.configure_search_database(self.path)
            for index in list(range(len(puzzles))) + list(reversed(range(len(puzzles)))):
                with self.subTest(index=index):
                    result = search.solve_search(puzzles[index], timeout=None)
                    self.assertEqual(signature(result), expected[index])
                    if result.solved:
                        placement = result.solution.layout.placements
                        self.assertTrue(simulate(puzzles[index], placement).is_solution)
                        self.assertEqual(Counter(placement.values()), Counter(
                            {kind: count for kind, count in puzzles[index].block_counts.items() if count}))
        self.assertGreater(tables.search_database_info()["reader"]["tables_loaded"], 0)

    def test_database_load_time_is_inside_search_deadline(self):
        self.build(puzzles=puzzle_variants()[:1])
        tables.configure_search_database(self.path)
        loaded = False
        original = TableDatabase.geometry
        def load_then_expire(database, *args, **kwargs):
            nonlocal loaded
            value = original(database, *args, **kwargs)
            loaded = True
            return value
        with patch.object(TableDatabase, "geometry", load_then_expire), \
                patch.object(search, "perf_counter", side_effect=lambda: 2.0 if loaded else 0.0):
            result = search.solve_search(puzzle_variants()[0], timeout=1)
        self.assertTrue(loaded)
        self.assertEqual(result.status, "timeout")
        retry = search.solve_search(puzzle_variants()[0], timeout=None)
        tables.configure_search_database(None)
        cold = search.solve_search(puzzle_variants()[0], timeout=None)
        self.assertEqual(signature(retry), signature(cold))


if __name__ == "__main__":
    unittest.main()
