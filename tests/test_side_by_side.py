"""Unit tests for side-by-side image names (names that clash, also only in case)."""

from pathlib import Path
import unittest

from loader import side_by_side
from loader.side_by_side import VAMIGATS, image_stem


class TestImageStem(unittest.TestCase):
    def setUp(self):
        side_by_side._written.clear()
        self.out = Path("/tmp/side-by-side-test")

    def test_same_test_keeps_its_name(self):
        r = VAMIGATS / "CPU/IPL/MOVE1/MOVE1b/MOVE1b_68000.retrosh"
        self.assertEqual(image_stem(self.out, r), "MOVE1b_68000")
        self.assertEqual(image_stem(self.out, r), "MOVE1b_68000")

    def test_names_differing_only_in_case(self):
        ipl = VAMIGATS / "CPU/IPL/MOVE1/MOVE1b/MOVE1b_68000.retrosh"
        timing = VAMIGATS / "CPU/Timing/MOVE1/MOVE1B/MOVE1B_68000.retrosh"
        self.assertEqual(image_stem(self.out, ipl), "MOVE1b_68000")
        self.assertEqual(image_stem(self.out, timing), "MOVE1B_68000__MOVE1")

    def test_clash_also_one_level_up(self):
        ipl = VAMIGATS / "CPU/IPL/AND/ANDCCR/ANDCCR_68000.retrosh"
        timing = VAMIGATS / "CPU/Timing/AND/ANDCCR/ANDCCR_68000.retrosh"
        timing2 = VAMIGATS / "CPU/Timing2/AND/ANDCCR/ANDCCR_68000.retrosh"
        self.assertEqual(image_stem(self.out, ipl), "ANDCCR_68000")
        self.assertEqual(image_stem(self.out, timing), "ANDCCR_68000__AND")
        self.assertEqual(image_stem(self.out, ipl), "ANDCCR_68000")
        self.assertEqual(image_stem(self.out, timing2), "ANDCCR_68000__Timing2_AND")

    def test_other_out_dir_is_independent(self):
        ipl = VAMIGATS / "CPU/IPL/MOVE1/MOVE1b/MOVE1b_68000.retrosh"
        timing = VAMIGATS / "CPU/Timing/MOVE1/MOVE1B/MOVE1B_68000.retrosh"
        image_stem(self.out, ipl)
        self.assertEqual(image_stem(Path("/tmp/other"), timing), "MOVE1B_68000")


if __name__ == "__main__":
    unittest.main()
