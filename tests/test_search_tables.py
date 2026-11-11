"""Cache isolation and finite inventory-table contracts for frontier search."""

from collections import Counter
from dataclasses import replace
import itertools
from threading import Thread
import unittest
from unittest.mock import patch

from lazor.models import Laser, Puzzle
import lazor.search as search
from lazor.search_tables import (SearchTables, WeightedLRU, TABLES,
                                 clear_search_caches, search_cache_info,
                                 configure_search_database)
from lazor.simulation import simulate


COUNTERS = ("candidates_tested", "states_expanded", "ray_states_processed",
            "branches_pruned", "optimistic_checks", "forced_assignments",
            "restarts", "assignment_writes", "implicit_cell_reads")


def board(rows, counts=None, lasers=None, targets=None):
    return Puzzle(tuple(tuple(row) for row in rows),
                  {kind: (counts or {}).get(kind, 0) for kind in "ABC"},
                  tuple(Laser(*ray) for ray in (lasers or [(2, 1, 1, 1), (2, 3, -1, -1)])),
                  frozenset(targets or [(1, 0), (3, 4)]))


def signature(result):
    placements = None if not result.solved else result.solution.layout.placements
    return result.status, placements, tuple(getattr(result, name) for name in COUNTERS)


def exhaustive_exists(puzzle):
    positions = puzzle.placeable_positions
    for labels in itertools.product("oABC", repeat=len(positions)):
        counts = Counter(labels)
        if any(counts[kind] != puzzle.block_counts[kind] for kind in "ABC"):
            continue
        placements = {position: label for position, label in zip(positions, labels)
                      if label != "o"}
        if simulate(puzzle, placements).is_solution:
            return True
    return False


class WeightedLRUTests(unittest.TestCase):
    def test_reentrant_factory_does_not_double_charge_same_key(self):
        cache = WeightedLRU(8)
        value = cache.get("same", 3, lambda: cache.get("same", 3, lambda: [None] * 3))
        self.assertIs(cache.get("same", 3, lambda: self.fail("lost inner result")), value)
        self.assertEqual(cache.stats()["state_slots"], 3)
        self.assertEqual(cache.stats()["entries"], 1)

    def test_factory_checkpoint_can_read_and_clear_cache_without_deadlock(self):
        cache = WeightedLRU(4)
        completed = []
        def factory():
            entries = cache.stats()["entries"]
            cache.clear()
            return [entries, None]
        def work():
            value = cache.get("key", 2, factory)
            completed.append(value)
        worker = Thread(target=work, daemon=True)
        worker.start()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive(), "A load checkpoint deadlocked while reading cache statistics")
        self.assertEqual(completed, [[0, None]])
        self.assertEqual(cache.stats()["entries"], 1)
        self.assertEqual(cache.stats()["state_slots"], 2)

    def test_hits_refresh_lru_and_eviction_preserves_active_reference(self):
        cache = WeightedLRU(4)
        first = cache.get("first", 2, lambda: [None, None])
        second = cache.get("second", 2, lambda: [None, None])
        self.assertIs(cache.get("first", 2, lambda: self.fail("cache miss")), first)
        third = cache.get("third", 2, lambda: [None, None])
        self.assertEqual(cache.stats()["evictions"], 1)
        second[0] = (1, 2)
        self.assertEqual(second, [(1, 2), None])
        self.assertIsNot(cache.get("second", 2, lambda: [None, None]), second)
        self.assertEqual(cache.stats()["state_slots"], 4)
        self.assertEqual(cache.stats()["entries"], 2)
        self.assertEqual(len(third), 2)

    def test_oversize_and_zero_capacity_do_not_retain_or_evict(self):
        cache = WeightedLRU(2)
        retained = cache.get("small", 2, lambda: [None, None])
        large = cache.get("large", 3, lambda: [None] * 3)
        self.assertIsNot(cache.get("large", 3, lambda: [None] * 3), large)
        self.assertIs(cache.get("small", 2, lambda: self.fail("evicted small table")), retained)
        self.assertEqual(cache.stats()["entries"], 1)
        self.assertEqual(cache.stats()["evictions"], 0)
        disabled = WeightedLRU(0)
        self.assertIsNot(disabled.get("x", 1, list), disabled.get("x", 1, list))
        self.assertEqual(disabled.stats()["state_slots"], 0)
        self.assertEqual(disabled.stats()["entries"], 0)

    def test_failed_factory_and_clear_do_not_publish_corrupt_table(self):
        cache = WeightedLRU(4)
        live = cache.get("live", 2, lambda: [(1,), None])
        def fail():
            raise RuntimeError("interrupted table allocation")
        with self.assertRaises(RuntimeError):
            cache.get("failed", 2, fail)
        self.assertEqual(cache.stats()["entries"], 1)
        self.assertEqual(cache.stats()["state_slots"], 2)
        good = cache.get("failed", 2, lambda: [None, None])
        cache.clear()
        self.assertEqual(cache.stats(), {"capacity": 4, "hits": 0, "misses": 0,
                                         "evictions": 0, "state_slots": 0, "entries": 0})
        self.assertEqual(live, [(1,), None])
        self.assertEqual(good, [None, None])

    def test_invalid_capacity_and_weight_are_rejected(self):
        for value in (-1, True, 1.5, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                WeightedLRU(value)
            with self.subTest(weight=value), self.assertRaises(ValueError):
                WeightedLRU().get("key", value, list)


class SearchTableTests(unittest.TestCase):
    def setUp(self):
        self.previous_database = configure_search_database(None)

    def tearDown(self):
        configure_search_database(self.previous_database)

    def assert_valid(self, puzzle, result):
        self.assertEqual(result.status, "solved" if exhaustive_exists(puzzle) else "no_solution")
        if result.solved:
            placements = result.solution.layout.placements
            self.assertTrue(simulate(puzzle, placements).is_solution)
            self.assertEqual(Counter(placements.values()),
                             Counter({kind: count for kind, count in puzzle.block_counts.items() if count}))

    def test_inventory_tables_exhaust_all_sixteen_domains_and_choice_orders(self):
        for mask in range(16):
            kinds = [code for code in range(4) if mask & (1 << code)]
            expected_singleton = kinds[0] if len(kinds) == 1 else -1
            with self.subTest(mask=mask):
                self.assertEqual(search._SINGLETON_KIND[mask], expected_singleton)
                for order, table in search._CHOICE_TABLES.items():
                    self.assertEqual(sorted(order), list(range(4)))
                    self.assertEqual(table[mask], tuple(code for code in order if code in kinds))
                for code in (-1, 0, 1, 2, 3):
                    domain = mask if code < 0 else 1 << code
                    self.assertEqual(search._TRANSMITS[mask][code], bool(domain & 9))
                    self.assertEqual(search._REFLECTS[mask][code], bool(domain & 10))
        self.assertEqual(search._BYTE_MASKS, tuple(1 << index for index in range(8)))

    def test_rectangular_geometry_and_static_domains_have_distinct_keys(self):
        tables = SearchTables(1000)
        horizontal = tables.geometry(3, 2, 68)
        vertical = tables.geometry(2, 3, 68)
        self.assertIsNot(horizontal, vertical)
        horizontal[0] = (1, 2, 3, 4)
        self.assertIs(tables.geometry(3, 2, 68), horizontal)
        self.assertIsNone(vertical[0])
        keys = [(2, 2, bytes(domain)) for domain in (
            (15, 15, 15, 15, 1), (14, 14, 14, 14, 1),
            (9, 9, 9, 9, 1), (9, 2, 9, 9, 1), (9, 4, 9, 9, 1))]
        refs = [tables.predecessors(key, 48) for key in keys]
        refs[0][0] = ()
        for key, table in zip(keys, refs):
            self.assertIs(tables.predecessors(key, 48), table)
        self.assertEqual(len({id(value) for value in refs}), len(keys))
        self.assertEqual(refs[0][0], ())
        self.assertIsNone(refs[1][0])

    def test_global_clear_resets_metadata_without_mutating_active_tables(self):
        geometry = TABLES.geometry(2, 2, 48)
        predecessor = TABLES.predecessors((2, 2, bytes([15] * 4 + [1])), 48)
        geometry[0], predecessor[0] = (0, 4, 1, -1), ()
        clear_search_caches()
        for stats in search_cache_info().values():
            self.assertEqual(stats["entries"], 0)
            self.assertEqual(stats["state_slots"], 0)
        self.assertEqual(geometry[0], (0, 4, 1, -1))
        self.assertEqual(predecessor[0], ())

    def test_changed_targets_inventory_grid_and_sources_match_cold_runs(self):
        initial = board(["oo", "oo"], {"A": 1, "C": 1})
        puzzles = [initial,
                   replace(initial, targets=frozenset([(1, 2), (3, 2), (3, 0)])),
                   replace(initial, block_counts={"A": 2, "B": 0, "C": 2}),
                   replace(initial, block_counts={"A": 0, "B": 0, "C": 4}),
                   replace(initial, block_counts={"A": 1, "B": 1, "C": 0}),
                   replace(initial, grid=(("A", "o"), ("o", "o"))),
                   replace(initial, grid=(("B", "o"), ("o", "o"))),
                   replace(initial, grid=(("x", "o"), ("o", "o"))),
                   replace(initial, lasers=(Laser(0, 1, 1, 1), Laser(4, 3, -1, -1))),
                   replace(initial, targets=frozenset([(1, 1)]))]
        cold = []
        with patch.object(search, "_RESTART_AFTER", 0):
            for puzzle in puzzles:
                clear_search_caches()
                result = search.solve_search(puzzle, timeout=None)
                self.assert_valid(puzzle, result)
                cold.append(signature(result))
            clear_search_caches()
            for index in list(range(len(puzzles))) + list(reversed(range(len(puzzles)))) + [0]:
                with self.subTest(case=index):
                    self.assertEqual(signature(search.solve_search(puzzles[index], timeout=None)), cold[index])
        self.assertGreater(search_cache_info()["geometry"]["hits"], 0)

    def test_complete_fallback_detaches_shared_predecessor_table(self):
        captured = []
        original_get = TABLES.predecessors
        def remember(key, count, **kwargs):
            table = original_get(key, count, **kwargs)
            captured.append((table, count))
            return table
        reached_fallback = False
        with patch.object(TABLES, "predecessors", side_effect=remember), \
                patch.object(search, "_RESTART_AFTER", 0):
            for targets in ([(1, 0), (3, 4)], [(1, 2), (3, 2)],
                            [(0, 1), (4, 3)], [(1, 2), (2, 3), (3, 0)]):
                result = search.solve_search(board(["oo", "oo"], {"A": 1, "C": 1}, targets=targets), timeout=None)
                reached_fallback |= result.restarts == 3
        self.assertTrue(reached_fallback)
        self.assertTrue(captured)
        self.assertTrue(any(any(value is not None for value in table) for table, _ in captured))
        for table, count in captured:
            self.assertEqual(len(table), count, "Fallback cleared a shared predecessor table")

    def test_deadline_after_lazy_geometry_population_leaves_valid_warm_table(self):
        puzzle = board(["oo", "oo"], {"A": 1, "C": 1})
        captured = []
        original_get = TABLES.geometry
        def remember(width, height, count, **kwargs):
            table = original_get(width, height, count, **kwargs)
            captured.append(table)
            return table
        def interrupted_clock():
            return 2.0 if any(any(value is not None for value in table) for table in captured) else 0.0
        with patch.object(TABLES, "geometry", side_effect=remember), \
                patch.object(search, "perf_counter", side_effect=interrupted_clock):
            interrupted = search.solve_search(puzzle, timeout=1)
        self.assertEqual(interrupted.status, "timeout")
        self.assertTrue(captured)
        self.assertTrue(any(value is not None for value in captured[0]))
        warm = search.solve_search(puzzle, timeout=None)
        clear_search_caches()
        cold = search.solve_search(puzzle, timeout=None)
        self.assertEqual(signature(warm), signature(cold))
        self.assert_valid(puzzle, warm)

    def test_deadline_after_lazy_predecessor_population_can_retry(self):
        puzzle = board(["oo", "oo"], {"A": 1, "C": 1}, targets=[(1, 2), (3, 2)])
        captured = []
        original_get = TABLES.predecessors
        def remember(key, count, **kwargs):
            table = original_get(key, count, **kwargs)
            captured.append(table)
            return table
        def interrupted_clock():
            return 2.0 if any(any(value is not None for value in table) for table in captured) else 0.0
        with patch.object(search, "_RESTART_AFTER", 0):
            with patch.object(TABLES, "predecessors", side_effect=remember), \
                    patch.object(search, "perf_counter", side_effect=interrupted_clock):
                interrupted = search.solve_search(puzzle, timeout=1)
            self.assertEqual(interrupted.status, "timeout")
            self.assertTrue(captured)
            self.assertTrue(any(value is not None for value in captured[0]))
            warm = search.solve_search(puzzle, timeout=None)
            clear_search_caches()
            cold = search.solve_search(puzzle, timeout=None)
        self.assertEqual(signature(warm), signature(cold))
        self.assert_valid(puzzle, warm)

    def test_mutable_inventory_is_keyed_by_values_not_puzzle_identity(self):
        puzzle = board(["oo", "oo"], {"A": 1, "C": 1}, targets=[(1, 2), (3, 2)])
        inventories = ({"A": 1, "B": 0, "C": 1}, {"A": 1, "B": 0, "C": 3},
                       {"A": 0, "B": 1, "C": 2})
        cold = []
        with patch.object(search, "_RESTART_AFTER", 0):
            for counts in inventories:
                clear_search_caches()
                puzzle.block_counts.update(counts)
                cold.append(signature(search.solve_search(puzzle, timeout=None)))
            clear_search_caches()
            for index in (0, 1, 2, 1, 0):
                puzzle.block_counts.update(inventories[index])
                actual = search.solve_search(puzzle, timeout=None)
                self.assertEqual(signature(actual), cold[index])
                self.assert_valid(puzzle, actual)


if __name__ == "__main__":
    unittest.main()
