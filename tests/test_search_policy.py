"""Boundaries and exactness for opening policies and bitboard geometry."""

from collections import Counter
from dataclasses import replace
import unittest
from unittest.mock import patch

from lazor.bitboard import MAX_STATES, compile_bitboards
from lazor.dfs_baseline import solve_dfs
from lazor.models import Laser, Puzzle
import lazor.search as search
from lazor.search_policy import BATCH_MAX_STATES, choose_opening, analyze_opening
from lazor.search_tables import SearchTables, configure_search_database
from lazor.simulation import simulate
from lazor.table_database import generate_geometry


def state_count(width, height):
    return 4 * ((2 * width + 1) * (2 * height + 1) // 2)


def bits(mask):
    while mask:
        least = mask & -mask
        yield least.bit_length() - 1
        mask ^= least


class OpeningPolicyTests(unittest.TestCase):
    def test_small_memory_threshold_includes_rectangles(self):
        for width, height, small in ((7, 9, True), (8, 8, True), (5, 13, False),
                                     (9, 7, True), (13, 5, False)):
            with self.subTest(shape=(width, height)):
                policy = choose_opening(width, height, width * height, 4, 2)
                self.assertEqual(policy.geometry_database, not small)
                self.assertEqual(policy.predecessor_database, not small)
                self.assertFalse(policy.use_bitboard)

    def test_batch_area_density_target_and_state_caps(self):
        self.assertEqual(BATCH_MAX_STATES, MAX_STATES)
        for width, height, enabled in ((15, 17, False), (16, 16, True), (1, 257, True),
                                       (17, 15, False), (257, 1, True)):
            with self.subTest(shape=(width, height)):
                self.assertEqual(choose_opening(width, height, width * height, 4, 2).use_bitboard, enabled)
        self.assertTrue(choose_opening(16, 16, 256, 64, 64).use_bitboard)
        self.assertFalse(choose_opening(16, 16, 256, 65, 64).use_bitboard)
        self.assertFalse(choose_opening(16, 16, 256, 64, 65).use_bitboard)
        self.assertTrue(choose_opening(36, 68, 2448, 4).use_bitboard)
        self.assertEqual(choose_opening(36, 68, 2448, 4).state_count, MAX_STATES)
        self.assertFalse(choose_opening(36, 69, 2484, 4).use_bitboard)
        self.assertFalse(choose_opening(69, 36, 2484, 4).use_bitboard)

    def test_large_non_guided_batch_label_distinguishes_constrained_board(self):
        constrained = choose_opening(30, 30, 899, 100)
        self.assertTrue(constrained.use_bitboard)
        self.assertFalse(constrained.early_guidance)
        self.assertEqual(constrained.name, "large_constrained_batch")
        self.assertEqual(choose_opening(30, 30, 900, 100).name, "large_open_batch")

    def test_early_guidance_requires_large_sparse_constrained_board(self):
        examples = ((29, 31, 898, 4, False),  # 899 cells
                    (30, 30, 896, 56, True), # 900 area, density exactly 1/16
                    (30, 30, 895, 56, False),
                    (30, 30, 900, 56, False), # No fixed/forbidden cells
                    (30, 30, 896, 57, False),
                    (25, 36, 896, 56, True),
                    (36, 25, 896, 56, True))
        for width, height, placeable, movable, expected in examples:
            with self.subTest(shape=(width, height), slots=placeable, movable=movable):
                policy = choose_opening(width, height, placeable, movable, 3)
                self.assertEqual(policy.early_guidance, expected)
                self.assertEqual(policy.fixed_or_unplaceable, width * height - placeable)

    def test_zero_inventory_and_invalid_features(self):
        for shape in ((4, 4), (16, 16), (44, 44)):
            policy = choose_opening(*shape, shape[0] * shape[1], 0)
            self.assertFalse(policy.use_bitboard)
            self.assertFalse(policy.early_guidance)
            self.assertFalse(policy.geometry_database)
            self.assertFalse(policy.predecessor_database)
        for arguments in ((0, 4, 0, 0), (4, 4, 17, 1), (4, 4, 2, 3),
                          (4, 4, -1, 0), (4, 4, 16, -1), (4, 4, 16, 1, 0),
                          (True, 4, 4, 1), (4, 4, 16.0, 1)):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                choose_opening(*arguments)

    def test_analysis_counts_b_inventory_fixed_blocks_and_actual_targets(self):
        puzzle = Puzzle((("A", "o", "x"), ("o", "B", "C")),
                        {"A": 0, "B": 1, "C": 1},
                        (Laser(0, 1, 1, 1), Laser(6, 1, -1, 1)),
                        frozenset([(1, 0), (3, 2), (5, 4)]))
        self.assertEqual(analyze_opening(puzzle), choose_opening(3, 2, 2, 2, 3))
        changed_sources = replace(puzzle, lasers=(Laser(2, 1, -1, -1),))
        self.assertEqual(analyze_opening(puzzle), analyze_opening(changed_sources))


class BitboardCompilerTests(unittest.TestCase):
    def test_every_state_matches_scalar_geometry_at_boundaries_and_rectangles(self):
        for width, height in ((1, 1), (2, 3), (3, 2), (8, 8), (15, 17),
                              (16, 16), (17, 15), (1, 44), (44, 1), (44, 44)):
            with self.subTest(shape=(width, height)):
                count = state_count(width, height)
                front, rear, straight, reflected = compile_bitboards(width, height, lambda: None)
                self.assertEqual(len(front), width * height + 1)
                self.assertEqual(len(rear), width * height + 1)
                expected = generate_geometry(width, height)
                decoded = [[None, None, -1, -1] for _ in range(count)]
                for column, masks in enumerate((front, rear)):
                    for cell, mask in enumerate(masks):
                        self.assertGreaterEqual(mask, 0)
                        self.assertLessEqual(mask.bit_length(), count)
                        for state in bits(mask):
                            self.assertIsNone(decoded[state][column], "Incidence masks overlap")
                            decoded[state][column] = cell
                for column, routes in ((2, straight), (3, reflected)):
                    for offset, mask in routes:
                        self.assertLessEqual(mask.bit_length(), count)
                        for state in bits(mask):
                            self.assertEqual(decoded[state][column], -1, "Outgoing route masks overlap")
                            destination = state + offset
                            self.assertTrue(0 <= destination < count)
                            decoded[state][column] = destination
                self.assertEqual([tuple(row) for row in decoded], expected)

    def test_compiler_validates_state_cap_before_checkpoint_or_allocation(self):
        self.assertEqual(state_count(36, 68), MAX_STATES)
        front, rear, _, _ = compile_bitboards(36, 68, lambda: None)
        self.assertEqual(sum(front), (1 << MAX_STATES) - 1)
        self.assertEqual(sum(rear), (1 << MAX_STATES) - 1)
        for width, height in ((0, 1), (1, -1), (True, 1), (1, 1.0), (36, 69)):
            with self.subTest(shape=(width, height)), self.assertRaises(ValueError):
                compile_bitboards(width, height, lambda: self.fail("Invalid input reached allocation"))

    def test_interrupt_during_compilation_propagates_and_retry_is_complete(self):
        class Deadline(Exception):
            pass
        calls = 0
        def checkpoint():
            nonlocal calls
            calls += 1
            if calls == 3:
                raise Deadline("compile interrupted")
        with self.assertRaises(Deadline):
            compile_bitboards(8, 8, checkpoint)
        self.assertEqual(calls, 3)
        masks = compile_bitboards(8, 8, lambda: None)
        full = (1 << state_count(8, 8)) - 1
        self.assertEqual(sum(masks[0]), full)
        self.assertEqual(sum(masks[1]), full)

    def test_interrupted_compilation_is_not_published_to_shared_cache(self):
        tables = SearchTables()
        class Deadline(Exception):
            pass
        calls = 0
        def checkpoint():
            nonlocal calls
            calls += 1
            if calls == 3:
                raise Deadline("compile interrupted")
        with self.assertRaises(Deadline):
            tables.bitboards(8, 8, checkpoint=checkpoint)
        self.assertEqual(tables.info()["bitboards"]["entries"], 0)
        self.assertEqual(tables.info()["bitboards"]["state_slots"], 0)
        compiled, hit = tables.bitboards(8, 8, checkpoint=lambda: None)
        self.assertFalse(hit)
        again, hit = tables.bitboards(8, 8, checkpoint=lambda: None)
        self.assertTrue(hit)
        self.assertIs(again, compiled)
        tables.clear()
        self.assertEqual(len(compiled[0]), 65)
        self.assertEqual(tables.info()["bitboards"]["entries"], 0)

    def test_oversize_cache_entry_does_not_evict_small_active_table(self):
        tables = SearchTables(capacity=state_count(2, 2))
        small, _ = tables.bitboards(2, 2, checkpoint=lambda: None)
        large, hit = tables.bitboards(3, 2, checkpoint=lambda: None)
        self.assertFalse(hit)
        again, hit = tables.bitboards(3, 2, checkpoint=lambda: None)
        self.assertFalse(hit)
        self.assertIsNot(again, large)
        retained, hit = tables.bitboards(2, 2, checkpoint=lambda: None)
        self.assertTrue(hit)
        self.assertIs(retained, small)
        self.assertEqual(tables.info()["bitboards"]["entries"], 1)


class ForcedBitboardSearchTests(unittest.TestCase):
    def setUp(self):
        previous = configure_search_database(None)
        self.addCleanup(configure_search_database, previous)

    def test_forced_tiny_batch_matches_complete_layout_dfs(self):
        original_choose = choose_opening
        def forced(*arguments, **kwargs):
            return replace(original_choose(*arguments, **kwargs), use_bitboard=True,
                           optimistic_period=1, geometry_database=False, predecessor_database=False)
        inventories = ({"A": 1, "B": 1, "C": 1}, {"A": 2, "B": 0, "C": 2},
                       {"A": 0, "B": 2, "C": 2}, {"A": 0, "B": 4, "C": 0},
                       {"A": 0, "B": 0, "C": 4}, {"A": 0, "B": 0, "C": 0})
        for counts in inventories:
            for targets in ([(1, 2), (3, 2)], [(0, 1), (4, 3)],
                            [(1, 0), (3, 4)], [(1, 2), (2, 3), (3, 0)]):
                puzzle = Puzzle((("o", "o"), ("o", "o")), counts.copy(),
                                (Laser(2, 1, 1, 1), Laser(2, 3, -1, -1), Laser(2, 1, 1, 1)),
                                frozenset(targets))
                expected = solve_dfs(puzzle, timeout=None)
                with self.subTest(counts=counts, targets=targets), \
                        patch.object(search, "choose_opening", side_effect=forced), \
                        patch.object(search, "_RESTART_AFTER", 0):
                    actual = search.solve_search(puzzle, timeout=None)
                self.assertEqual(actual.status, expected.status)
                if actual.solved:
                    placed = actual.solution.layout.placements
                    self.assertTrue(simulate(puzzle, placed).is_solution)
                    self.assertEqual(Counter(placed.values()), Counter({k: v for k, v in counts.items() if v}))

    def test_setup_time_includes_interrupted_bitboard_compilation(self):
        puzzle = Puzzle(tuple(tuple("o" * 16) for _ in range(16)),
                        {"A": 1, "B": 0, "C": 0}, (Laser(0, 1, 1, 1),),
                        frozenset([(1, 2)]))
        elapsed = 0.0
        def interrupt(*args, **kwargs):
            nonlocal elapsed
            elapsed = 2.0
            raise search._Deadline
        with patch.object(search._TABLES, "bitboards", side_effect=interrupt), \
                patch.object(search, "perf_counter", side_effect=lambda: elapsed):
            result = search.solve_search(puzzle, timeout=1)
        self.assertEqual(result.status, "timeout")
        self.assertTrue(result.bitboard_enabled)
        self.assertEqual(result.bitboard_setup_seconds, 2.0)


if __name__ == "__main__":
    unittest.main()
