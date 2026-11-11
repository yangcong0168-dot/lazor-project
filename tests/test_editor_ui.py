"""Tk widget integration tests, using an isolated withdrawn test window.

These tests never send OS input to the user's live editor. File dialogs are
stubbed to temporary paths; only model/widget callbacks in this test instance
are exercised. They skip cleanly when Tk cannot initialize in a sandbox.
"""

import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lazor import load_bff, simulate
from lazor.editor import EditorApp
from lazor.editor_model import EditorDocument
from lazor.models import Laser


class EditorWidgetTests(unittest.TestCase):
    """Check that canvas editing, files, undo, and worker results connect."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
            cls.root.withdraw()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"Tk display unavailable: {exc}")

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        for widget in self.root.winfo_children():
            widget.destroy()
        self.app = EditorApp(self.root)
        self.root.update_idletasks()
        self.app.geometry = (40, 40, 80)

    def tearDown(self):
        self.app.cancel()
        self.app.runner.cancel()
        # Remove scheduled polls before the next test owns this root.
        for callback in self.root.tk.call("after", "info"):
            self.root.after_cancel(callback)

    def click(self, tool, point):
        self.app.tool.set(tool)
        x, y = self.app._pixel(point)
        self.app.click_canvas(SimpleNamespace(x=x, y=y))

    def make_loop(self):
        for point in ((3, 1), (1, 3), (5, 3), (3, 5)):
            self.click("A", point)
        self.app.direction.set("右上 ↗")
        self.click("L", (3, 2))
        for point in ((3, 2), (4, 3), (3, 4), (2, 3)):
            self.click("P", point)

    def test_canvas_edits_export_to_bff_and_trace_a_closed_loop(self):
        self.make_loop()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "loop.bff"
            with patch("lazor.editor.filedialog.asksaveasfilename",
                       return_value=str(path)):
                self.assertTrue(self.app.save())
            puzzle = load_bff(path)
        result = simulate(puzzle)
        self.assertTrue(result.is_solution)
        self.assertGreater(result.repeated_states, 0)
        self.assertFalse(self.app.dirty)
        self.assertIn("L 3 2 1 -1", self.app.preview.get("1.0", "end"))

    def test_undo_and_redo_restore_canvas_document(self):
        self.click("C", (3, 3))
        self.assertEqual(self.app.document.grid[1][1], "C")
        self.app.undo()
        self.assertEqual(self.app.document.grid[1][1], "o")
        self.app.redo()
        self.assertEqual(self.app.document.grid[1][1], "C")

    def test_same_source_can_have_two_directions(self):
        self.click("L", (2, 3))
        self.app.direction.set("左上 ↖")
        self.click("L", (2, 3))
        self.assertEqual(len(self.app.document.lasers), 2)

    def test_eraser_repairs_imported_non_face_points(self):
        self.app.document.targets.add((1, 1))
        self.app.document.lasers.append(Laser(1, 1, 1, 1))
        x, y = self.app._pixel((1, 1))
        self.app.erase_canvas(SimpleNamespace(x=x, y=y))
        self.assertNotIn((1, 1), self.app.document.targets)
        self.assertFalse(self.app.document.lasers)

    def test_inventory_changes_do_not_paint_fixed_blocks(self):
        self.app.count_vars["A"].set("2")
        self.assertEqual(self.app.document.block_counts["A"], 2)
        self.assertTrue(all(cell == "o" for row in self.app.document.grid
                            for cell in row))
        self.app.undo()
        self.assertEqual(self.app.document.block_counts["A"], 0)

    def test_invalid_inventory_prevents_save(self):
        self.app.count_vars["A"].set("wrong")
        with patch("lazor.editor.messagebox.showerror") as error:
            self.assertFalse(self.app.save())
        error.assert_called_once()

    def test_cancelling_unsaved_new_board_preserves_draft(self):
        self.click("A", (1, 1))
        before = self.app.document.clone()
        self.app.width_var.set("6")
        with patch(
            "lazor.editor.messagebox.askyesnocancel", return_value=None
        ):
            self.app.new_board()
        self.assertEqual(self.app.document, before)

    def test_background_solve_overlay_preserves_original_puzzle(self):
        self.app.document = EditorDocument.from_text(
            "GRID START\no o o\no o o\no o o\nGRID STOP\n"
            "C 1\nL 1 2 1 1\nP 0 5\nP 5 6\n"
        )
        self.app.refresh()
        original = self.app.document.clone()
        self.app.run("solve")
        self.assertTrue(self.app.busy)
        deadline = time.monotonic() + 8
        while self.app.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertFalse(self.app.busy)
        self.assertIsNotNone(self.app.result)
        self.assertTrue(self.app.result.is_solution)
        self.assertEqual(self.app.document, original)
        with tempfile.TemporaryDirectory() as directory:
            with patch("lazor.editor.filedialog.askdirectory",
                       return_value=directory):
                self.app.export_result()
            files = {path.suffix for path in Path(directory).iterdir()}
            self.assertEqual(files, {".bff", ".svg", ".txt"})
        self.click("x", (1, 1))
        self.assertIsNone(self.app.result)

    def test_point_snapping_stays_on_board_face_midpoints(self):
        for point in ((0, 0), (1, 1), (8, 8), (4.1, 3.4)):
            snapped = self.app._point_at(*self.app._pixel(point))
            self.assertEqual(sum(snapped) % 2, 1)
            self.assertTrue(all(0 <= value <= 8 for value in snapped))

    def test_unknown_planning_status_does_not_claim_infeasibility(self):
        result = SimpleNamespace(
            solved=False, status="unknown", state_count=28,
            constraint_count=110,
        )
        self.app._set_busy(True)
        with patch.object(self.app.runner, "poll", return_value=(
            "solve", result
        )):
            self.app._poll()
        self.assertFalse(self.app.busy)
        self.assertIsNone(self.app.result)
        self.assertIn("暂未确定", self.app.summary_var.get())
        self.assertNotIn("关卡无解", self.app.summary_var.get())
        self.assertIn("28 个状态", self.app.summary_var.get())
        self.assertIn("110 条约束", self.app.summary_var.get())


if __name__ == "__main__":
    unittest.main()
