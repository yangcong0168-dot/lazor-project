"""Behavioral tests for reading and validating Lazor board files."""

import tempfile
import unittest
from pathlib import Path

from lazor.bff import BFFParseError, load_bff, parse_bff


SIMPLE_BOARD = """GRID START
o o
o o
GRID STOP
A 1
L 0 1 1 1
P 4 3
"""


class ParseBFFTests(unittest.TestCase):
    """Check the public parser contract with small, inspectable boards."""

    def test_reads_grid_inventory_lasers_and_targets(self):
        puzzle = parse_bff(SIMPLE_BOARD)

        self.assertEqual(puzzle.grid, (("o", "o"), ("o", "o")))
        self.assertEqual((puzzle.width, puzzle.height), (2, 2))
        self.assertEqual(puzzle.block_counts, {"A": 1, "B": 0, "C": 0})
        self.assertEqual(len(puzzle.lasers), 1)
        laser = puzzle.lasers[0]
        self.assertEqual((laser.x, laser.y, laser.vx, laser.vy), (0, 1, 1, 1))
        self.assertEqual(puzzle.targets, frozenset({(4, 3)}))

    def test_preserves_fixed_blocks_and_excludes_unplaceable_cells(self):
        puzzle = parse_bff(
            "GRID START\no A x\no B C\nGRID STOP\n"
            "A 1\nC 1\nL 0 1 1 1\nP 6 3\n"
        )

        self.assertEqual((puzzle.width, puzzle.height), (3, 2))
        self.assertEqual(puzzle.placeable_positions, ((0, 0), (0, 1)))
        self.assertEqual(set(puzzle.fixed_blocks), {(1, 0), (1, 1), (2, 1)})
        for position, kind in [((1, 0), "A"), ((1, 1), "B"), ((2, 1), "C")]:
            with self.subTest(position=position):
                self.assertEqual(puzzle.fixed_blocks[position].kind, kind)
                self.assertTrue(puzzle.fixed_blocks[position].fixed)
        # Fixed blocks do not consume the movable inventory below GRID.
        self.assertEqual(puzzle.block_counts, {"A": 1, "B": 0, "C": 1})

    def test_accepts_comments_blank_lines_bom_and_extra_whitespace(self):
        puzzle = parse_bff(
            "\ufeff# A comment before the board\n\n"
            "  GRID START  # begin\n"
            " o\t o # row one\n\n"
            " o  o\n"
            "GRID STOP\n\n"
            " A  1 # movable blocks\n"
            "L 0 1 1 1 # source\n"
            "P 4 3 # target\n"
        )

        self.assertEqual(puzzle.grid, (("o", "o"), ("o", "o")))
        self.assertEqual(puzzle.block_counts["A"], 1)
        self.assertEqual(puzzle.targets, frozenset({(4, 3)}))

    def test_accepts_multiple_lasers_and_deduplicates_targets(self):
        puzzle = parse_bff(SIMPLE_BOARD + "L 4 3 -1 -1\nP 0 1\nP 4 3\n")

        self.assertEqual(len(puzzle.lasers), 2)
        second = puzzle.lasers[1]
        self.assertEqual(
            (second.x, second.y, second.vx, second.vy), (4, 3, -1, -1)
        )
        self.assertEqual(puzzle.targets, frozenset({(0, 1), (4, 3)}))

    def test_accepts_boundaries_without_extra_parity_restrictions(self):
        puzzle = parse_bff(
            "GRID START\no o\no o\nGRID STOP\n"
            "L 0 0 1 1\nL 4 4 -1 -1\nP 0 4\nP 4 0\n"
        )

        self.assertEqual(len(puzzle.lasers), 2)
        self.assertEqual(puzzle.targets, frozenset({(0, 4), (4, 0)}))
        self.assertEqual(puzzle.block_counts, {"A": 0, "B": 0, "C": 0})

    def test_accepts_zero_movable_blocks_and_a_fully_fixed_board(self):
        puzzle = parse_bff(
            "GRID START\nA B C\nGRID STOP\nA 0\nB 0\nC 0\n"
            "L 0 1 1 1\nP 6 1\n"
        )

        self.assertEqual(puzzle.placeable_positions, ())
        self.assertEqual(len(puzzle.fixed_blocks), 3)
        self.assertEqual(sum(puzzle.block_counts.values()), 0)

    def test_rejects_missing_empty_or_repeated_grid_sections(self):
        invalid_boards = {
            "no grid": "L 0 1 1 1\nP 2 1\n",
            "missing start": "o o\no o\nGRID STOP\nL 0 1 1 1\nP 4 3\n",
            "missing stop": SIMPLE_BOARD.replace("GRID STOP\n", ""),
            "empty grid": "GRID START\nGRID STOP\nL 0 1 1 1\nP 2 1\n",
            "nested start": SIMPLE_BOARD.replace(
                "o o\no o", "GRID START\no o"
            ),
            "repeated grid": SIMPLE_BOARD + "GRID START\no o\nGRID STOP\n",
            "extra stop": SIMPLE_BOARD + "GRID STOP\n",
        }
        for label, text in invalid_boards.items():
            with self.subTest(case=label):
                with self.assertRaises(BFFParseError):
                    parse_bff(text)

    def test_rejects_ragged_grid_and_unknown_cells(self):
        for rows in ("o o\no", "o o\no Z", "o o\no 1"):
            with self.subTest(rows=rows):
                with self.assertRaises(BFFParseError):
                    parse_bff(SIMPLE_BOARD.replace("o o\no o", rows))

    def test_rejects_invalid_block_counts(self):
        for declaration in ("A -1", "A 1.5", "A one", "A 5", "A 1\nA 1"):
            with self.subTest(declaration=declaration):
                with self.assertRaises(BFFParseError):
                    parse_bff(SIMPLE_BOARD.replace("A 1", declaration))

    def test_rejects_combined_inventory_larger_than_free_cells(self):
        with self.assertRaises(BFFParseError):
            parse_bff(SIMPLE_BOARD.replace("A 1", "A 3\nB 2"))

    def test_rejects_unknown_directives_and_wrong_argument_counts(self):
        replacements = [
            ("A 1", "D 1"),
            ("A 1", "A"),
            ("A 1", "A 1 2"),
            ("L 0 1 1 1", "L 0 1 1"),
            ("L 0 1 1 1", "L 0 1 1 1 2"),
            ("P 4 3", "P 4"),
            ("P 4 3", "P 4 3 2"),
        ]
        for old, new in replacements:
            with self.subTest(record=new):
                with self.assertRaises(BFFParseError):
                    parse_bff(SIMPLE_BOARD.replace(old, new))

    def test_rejects_coordinates_outside_half_step_board_bounds(self):
        replacements = [
            ("L 0 1 1 1", "L -1 1 1 1"),
            ("L 0 1 1 1", "L 5 1 -1 1"),
            ("L 0 1 1 1", "L 1 -1 1 1"),
            ("L 0 1 1 1", "L 1 5 1 -1"),
            ("P 4 3", "P -1 3"),
            ("P 4 3", "P 5 3"),
            ("P 4 3", "P 3 -1"),
            ("P 4 3", "P 3 5"),
        ]
        for old, new in replacements:
            with self.subTest(record=new):
                with self.assertRaises(BFFParseError):
                    parse_bff(SIMPLE_BOARD.replace(old, new))

    def test_rejects_noninteger_coordinates_and_invalid_directions(self):
        replacements = [
            ("L 0 1 1 1", "L 0.5 1 1 1"),
            ("L 0 1 1 1", "L 0 1 0 1"),
            ("L 0 1 1 1", "L 0 1 1 0"),
            ("L 0 1 1 1", "L 0 1 2 1"),
            ("L 0 1 1 1", "L 0 1 1 -2"),
            ("P 4 3", "P 4 3.5"),
        ]
        for old, new in replacements:
            with self.subTest(record=new):
                with self.assertRaises(BFFParseError):
                    parse_bff(SIMPLE_BOARD.replace(old, new))

    def test_requires_a_laser_and_a_target(self):
        for record in ("L 0 1 1 1\n", "P 4 3\n"):
            with self.subTest(missing=record.strip()):
                with self.assertRaises(BFFParseError):
                    parse_bff(SIMPLE_BOARD.replace(record, ""))

    def test_error_identifies_the_source_and_physical_line_number(self):
        with self.assertRaises(BFFParseError) as caught:
            parse_bff(SIMPLE_BOARD + "Z 1\n", source="broken.bff")

        self.assertIn("broken.bff", str(caught.exception))
        self.assertRegex(str(caught.exception), r"\b8\b")


class LoadBFFTests(unittest.TestCase):
    """Check filesystem behavior independently of the text parser."""

    def test_loads_utf8_bom_file_with_non_ascii_comments(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.bff"
            path.write_text("# 示例关卡\n" + SIMPLE_BOARD, encoding="utf-8-sig")

            from_path = load_bff(path)
            from_string = load_bff(str(path))

        self.assertEqual(from_path.grid, (("o", "o"), ("o", "o")))
        self.assertEqual(from_string.targets, frozenset({(4, 3)}))

    def test_reports_filename_when_file_contents_are_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken.bff"
            path.write_text(SIMPLE_BOARD + "UNKNOWN 1\n", encoding="utf-8")

            with self.assertRaises(BFFParseError) as caught:
                load_bff(path)

        self.assertIn("broken.bff", str(caught.exception))

    def test_missing_file_preserves_file_not_found_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                load_bff(Path(directory) / "missing.bff")


if __name__ == "__main__":
    unittest.main()
