"""Check the physical rules exposed by Block before adding a simulator."""

import unittest

from lazor.models import Block


class BlockTests(unittest.TestCase):
    """Block behavior must preserve the component parallel to a surface."""

    def test_reflection_changes_only_the_normal_component(self):
        block = Block("A")
        cases = [
            ((1, 1), "vertical", ((-1, 1),)),
            ((1, -1), "vertical", ((-1, -1),)),
            ((-1, 1), "horizontal", ((-1, -1),)),
            ((-1, -1), "horizontal", ((-1, 1),)),
        ]
        for direction, face, expected in cases:
            with self.subTest(direction=direction, face=face):
                self.assertEqual(block.interact(direction, face), expected)

    def test_opaque_block_terminates_ray(self):
        for face in ("vertical", "horizontal"):
            with self.subTest(face=face):
                self.assertEqual(Block("B").interact((1, -1), face), ())

    def test_refracting_block_keeps_both_branches(self):
        block = Block("C")
        self.assertEqual(
            block.interact((1, -1), "vertical"), ((1, -1), (-1, -1))
        )
        self.assertEqual(
            block.interact((1, -1), "horizontal"), ((1, -1), (1, 1))
        )

    def test_fixed_and_movable_blocks_have_same_optics(self):
        for kind in ("A", "B", "C"):
            with self.subTest(kind=kind):
                movable = Block(kind)
                fixed = Block(kind, fixed=True)
                self.assertFalse(movable.fixed)
                self.assertTrue(fixed.fixed)
                self.assertEqual(
                    movable.interact((1, 1), "horizontal"),
                    fixed.interact((1, 1), "horizontal"),
                )

    def test_unknown_block_kind_is_rejected(self):
        for kind in ("o", "x", "D", "a", ""):
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError):
                    Block(kind)

    def test_invalid_direction_or_face_is_rejected(self):
        block = Block("A")
        for direction in ((0, 1), (1, 0), (2, -1)):
            with self.subTest(direction=direction):
                with self.assertRaises(ValueError):
                    block.interact(direction, "vertical")
        with self.assertRaises(ValueError):
            block.interact((1, -1), "diagonal")


if __name__ == "__main__":
    unittest.main()

