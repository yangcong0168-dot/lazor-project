"""Verify spawned editor work, result delivery, cancellation, and reuse."""

import time
import unittest
from unittest.mock import Mock

from lazor.editor_runner import EditorRunner
from lazor.simulation import SimulationResult
from lazor.solver import PlanningResult


BOARD = """GRID START
o o o
o o o
o o o
GRID STOP
C 1
L 1 2 1 1
P 3 4
P 5 6
P 0 5
"""


class EditorRunnerTests(unittest.TestCase):
    """The editor must receive real results without blocking its event loop."""

    def setUp(self):
        self.runner = EditorRunner()
        self.addCleanup(self.runner.cancel)

    def await_result(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = self.runner.poll()
            if result is not None:
                self.assertFalse(self.runner.running)
                self.assertIsNone(self.runner.poll())
                return result
            time.sleep(0.01)
        self.fail("Editor worker did not return within five seconds.")

    def test_simulation_returns_pickled_result_and_preserves_input(self):
        self.runner.start("simulate", BOARD)
        self.assertTrue(self.runner.running)
        mode, result = self.await_result()
        self.assertEqual(mode, "simulate")
        self.assertIsInstance(result, SimulationResult)
        self.assertFalse(result.is_solution)
        self.assertEqual(result.remaining_blocks["C"], 1)
        self.assertEqual(result.layout.puzzle.width, 3)

    def test_search_returns_solution_and_can_be_followed_by_another_run(self):
        self.runner.start("solve", BOARD)
        mode, result = self.await_result()
        self.assertEqual(mode, "solve")
        self.assertIsInstance(result, PlanningResult)
        self.assertTrue(result.solved)
        self.assertTrue(result.solution.is_solution)
        self.assertEqual(result.solution.layout.placements, {(1, 1): "C"})
        self.runner.start("solve", BOARD, timeout=0)
        _, limited = self.await_result()
        self.assertEqual(limited.status, "timeout")

    def test_bad_board_returns_readable_error_instead_of_crashing_parent(self):
        self.runner.start("simulate", "not a valid BFF")
        mode, message = self.await_result()
        self.assertEqual(mode, "error")
        self.assertIn("BFFParseError", message)
        self.assertIn("editor", message)

    def test_start_rejects_overlap_and_cancel_allows_reuse(self):
        self.runner.start("solve", BOARD)
        with self.assertRaises(RuntimeError):
            self.runner.start("simulate", BOARD)
        self.runner.cancel()
        self.assertFalse(self.runner.running)
        self.assertIsNone(self.runner.poll())
        self.runner.cancel()
        self.runner.start("solve", BOARD)
        self.assertEqual(self.await_result()[1].status, "solved")

    def test_invalid_arguments_do_not_launch_process(self):
        cases = [
            {"mode": "unknown"},
            {"text": None},
            {"timeout": -1},
            {"timeout": float("nan")},
            {"timeout": float("inf")},
            {"timeout": True},
        ]
        for changes in cases:
            arguments = {"mode": "solve", "text": BOARD, **changes}
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.runner.start(**arguments)
                self.assertFalse(self.runner.running)

    def test_abnormal_child_exit_is_reported_not_left_pending(self):
        # Model an OS-level crash after startup. There is no actual process to
        # kill here; this checks the GUI-facing failure contract independently.
        from queue import Empty

        process = Mock()
        process.is_alive.return_value = False
        process.exitcode = 9
        results = Mock()
        results.get_nowait.side_effect = Empty
        results.get.side_effect = Empty
        self.runner._process = process
        self.runner._results = results
        mode, message = self.runner.poll()
        self.assertEqual(mode, "error")
        self.assertIn("code 9", message)
        self.assertFalse(self.runner.running)
        process.close.assert_called_once()
        results.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
