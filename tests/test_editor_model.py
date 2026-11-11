"""Test authoring and BFF export independently of the graphical toolkit."""

import tempfile
import unittest
from pathlib import Path

from lazor.bff import BFFParseError, load_bff
from lazor.editor_model import EditorDocument, save_document
from lazor.models import Laser
from lazor.simulation import simulate


def complete_document():
    """A fixed C with two reachable branches and separate movable inventory."""
    document = EditorDocument.new(3, 3)
    document.set_cell(1, 1, "C")
    document.add_laser(1, 2, 1, 1)
    document.toggle_target(0, 5)
    document.toggle_target(5, 6)
    return document


class EditorDocumentTests(unittest.TestCase):
    """Draft editing must not silently change puzzle semantics."""

    def test_new_draft_can_be_previewed_but_not_run(self):
        document = EditorDocument.new(2, 3)
        self.assertEqual((document.width, document.height), (2, 3))
        self.assertIn(
            "GRID START\no o\no o\no o\nGRID STOP", document.to_bff()
        )
        self.assertEqual(document.block_counts, {"A": 0, "B": 0, "C": 0})
        with self.assertRaises(BFFParseError):
            document.to_puzzle()
        with self.assertRaises(BFFParseError):
            document.to_bff(validate=True)

    def test_round_trip_keeps_fixed_blocks_and_inventory_separate(self):
        document = complete_document()
        document.set_cell(0, 0, "A")
        document.set_cell(2, 2, "x")
        document.set_inventory("A", 2)
        document.set_inventory("B", 1)
        restored = EditorDocument.from_text(document.to_bff(validate=True))
        self.assertEqual(restored, document)
        puzzle = restored.to_puzzle()
        self.assertEqual(puzzle.block_counts, {"A": 2, "B": 1, "C": 0})
        self.assertEqual(puzzle.fixed_blocks[(1, 1)].kind, "C")
        self.assertEqual(puzzle.fixed_blocks[(0, 0)].kind, "A")
        self.assertTrue(puzzle.fixed_blocks[(0, 0)].fixed)

    def test_loaded_puzzle_and_cloned_history_are_independent(self):
        document = complete_document()
        original_puzzle = document.to_puzzle()
        loaded = EditorDocument.from_puzzle(original_puzzle)
        clone = loaded.clone()
        clone.set_cell(0, 0, "B")
        clone.set_inventory("A", 1)
        clone.add_laser(0, 1, 1, 1)
        clone.toggle_target(1, 0)
        self.assertEqual(loaded, document)
        self.assertEqual(original_puzzle.grid[0][0], "o")
        self.assertEqual(original_puzzle.block_counts["A"], 0)
        self.assertEqual(len(original_puzzle.lasers), 1)
        self.assertNotIn((1, 0), original_puzzle.targets)

    def test_lasers_deduplicate_exact_matches_but_keep_other_directions(self):
        document = EditorDocument.new()
        document.add_laser(2, 3, 1, 1)
        document.add_laser(2, 3, 1, 1)
        document.add_laser(2, 3, -1, 1)
        self.assertEqual(
            document.lasers, [Laser(2, 3, 1, 1), Laser(2, 3, -1, 1)]
        )

    def test_target_toggle_and_point_eraser(self):
        document = EditorDocument.new()
        document.toggle_target(2, 3)
        document.toggle_target(2, 3)
        self.assertEqual(document.targets, set())
        document.toggle_target(2, 3)
        document.toggle_target(4, 5)
        document.add_laser(2, 3, 1, 1)
        document.add_laser(2, 3, -1, -1)
        document.add_laser(4, 5, 1, -1)
        document.remove_point(2, 3)
        self.assertEqual(document.targets, {(4, 5)})
        self.assertEqual(document.lasers, [Laser(4, 5, 1, -1)])

    def test_temporary_excess_inventory_is_rejected_only_when_validating(self):
        document = complete_document()
        document.set_inventory("A", 9)
        self.assertIn("A 9", document.to_bff())
        with self.assertRaises(BFFParseError):
            document.to_puzzle()
        document.set_inventory("A", 8)
        self.assertEqual(document.to_puzzle().block_counts["A"], 8)

    def test_dimension_cell_and_inventory_errors(self):
        for width, height in ((0, 3), (3, -1), (1.5, 2), (True, 2)):
            with self.subTest(size=(width, height)):
                with self.assertRaises(ValueError):
                    EditorDocument.new(width, height)
        document = EditorDocument.new(2, 2)
        for arguments in ((2, 0, "A"), (-1, 0, "A"), (0, 0, "Z"),
                          (0.5, 0, "A"), (True, 0, "A")):
            with self.subTest(cell=arguments):
                with self.assertRaises(ValueError):
                    document.set_cell(*arguments)
        for kind, count in (("A", -1), ("A", 0.5), ("A", True), ("D", 1)):
            with self.subTest(inventory=(kind, count)):
                with self.assertRaises(ValueError):
                    document.set_inventory(kind, count)

    def test_invalid_source_geometry_and_direction(self):
        document = EditorDocument.new(2, 2)
        cases = [
            (0, 0, 1, 1), (1, 1, 1, 1), (-1, 0, 1, 1),
            (5, 0, -1, 1), (0.5, 1, 1, 1), (0, 1, 0, 1),
            (0, 1, 1, 2), (0, 1, True, 1),
        ]
        for arguments in cases:
            with self.subTest(laser=arguments):
                with self.assertRaises(ValueError):
                    document.add_laser(*arguments)

    def test_boundary_points_are_valid_and_noninteger_targets_are_not(self):
        document = complete_document()
        document.toggle_target(6, 0)
        document.add_laser(6, 1, -1, 1)
        self.assertIn((6, 0), document.to_puzzle().targets)
        for point in ((7, 0), (0, -1), (0.5, 1), (True, 1)):
            with self.subTest(point=point):
                with self.assertRaises(ValueError):
                    document.toggle_target(*point)

    def test_imported_corner_source_can_be_edited_but_not_exported(self):
        document = EditorDocument.from_text(
            "GRID START\no o\nGRID STOP\nL 0 0 1 1\nP 1 2\n"
        )
        with self.assertRaises(ValueError):
            document.to_bff(validate=True)
        document.remove_point(0, 0)
        document.add_laser(0, 1, 1, 1)
        self.assertTrue(simulate(document.to_puzzle()).all_targets_hit)


class SaveDocumentTests(unittest.TestCase):
    """Only validated BFF documents should reach the filesystem."""

    def test_saved_case_reloads_and_runs_without_changing_inventory(self):
        document = complete_document()
        document.set_inventory("A", 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases" / "sample.bff"
            self.assertEqual(save_document(document, path), path)
            loaded = load_bff(path)
        self.assertEqual(loaded.block_counts, {"A": 1, "B": 0, "C": 0})
        self.assertTrue(simulate(loaded).all_targets_hit)
        self.assertFalse(simulate(loaded).is_solution)

    def test_invalid_draft_cannot_replace_an_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "keep.bff"
            path.write_text("keep this content", encoding="utf-8")
            with self.assertRaises(BFFParseError):
                save_document(EditorDocument.new(), path)
            self.assertEqual(path.read_text("utf-8"), "keep this content")

    def test_wrong_extension_is_rejected_before_creating_any_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "sample.bif"
            with self.assertRaises(ValueError):
                save_document(complete_document(), path)
            self.assertFalse(path.parent.exists())


if __name__ == "__main__":
    unittest.main()
