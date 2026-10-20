"""Unit tests for comparing a run with the published results."""

import gzip
import json
from pathlib import Path
import tempfile
import unittest

from loader.vs_published import compare_run, verdict


def res(cls, px=None):
    return {"class": cls, "differing_pixels": px}


class TestVerdict(unittest.TestCase):
    def test_compared(self):
        self.assertEqual(verdict(res("PASS", 0), res("FAIL", 40)), "better")
        self.assertEqual(verdict(res("FAIL", 10), res("FAIL", 40)), "better")
        self.assertEqual(verdict(res("FAIL", 40), res("PASS", 0)), "worse")
        self.assertEqual(verdict(res("FAIL", 41), res("FAIL", 40)), "worse")
        self.assertEqual(verdict(res("FAIL", 40), res("FAIL", 40)), "unchanged")
        self.assertEqual(verdict(res("PASS", 0), res("PASS", 0)), "unchanged")

    def test_not_compared_and_errors(self):
        self.assertEqual(verdict(res("DEVIATING_CONFIG"), res("DEVIATING_CONFIG")), "not compared")
        self.assertEqual(verdict(res("NOT_CALIBRATED"), res("FAIL", 3)), "not compared")
        self.assertEqual(verdict(res("ERROR"), res("PASS", 0)), "worse")
        self.assertEqual(verdict(res("ERROR"), res("ERROR")), "unchanged")


class TestCompareRun(unittest.TestCase):
    def test_layouts(self):
        """A run keeps <test dir>/<variant>/result.json, the results repository
        <test dir>/<variant>.result.json."""
        with tempfile.TemporaryDirectory() as tmp:
            run, pub = Path(tmp) / "run", Path(tmp) / "pub"
            test_dir = "Agnus/Copper/Wait/waitblt1"
            for variant, new, old in (("waitblt1_ocs", res("PASS", 0), res("FAIL", 4)),
                                      ("waitblt1_ecs", res("FAIL", 9), res("FAIL", 4))):
                common = {"variant": variant, "retrosh_path": f"{test_dir}/{variant}.retrosh",
                          "nanomig_sha": "a", "vamigats_sha": "v"}
                (run / test_dir / variant).mkdir(parents=True)
                (run / test_dir / variant / "result.json").write_text(json.dumps({**common, **new}))
                (pub / test_dir).mkdir(parents=True, exist_ok=True)
                (pub / test_dir / f"{variant}.result.json").write_text(json.dumps({**common, **old}))
            counts, changed, _ = compare_run(run, pub)
            self.assertEqual((counts["better"], counts["worse"]), (1, 1))
            self.assertIn(("worse", test_dir, "waitblt1_ecs", "FAIL 4 px -> FAIL 9 px", 5), changed)

    def test_moved_difference(self):
        """A FAIL with the same pixel count but another capture is reported as moved."""
        with tempfile.TemporaryDirectory() as tmp:
            run, pub = Path(tmp) / "run", Path(tmp) / "pub"
            test_dir = "Agnus/Copper/Wait/waitblt4"
            for variant, mine, theirs in (("waitblt4_ocs", b"frame A", b"frame B"),
                                          ("waitblt4_ecs", b"frame C", b"frame C")):
                r = {"variant": variant, "retrosh_path": f"{test_dir}/{variant}.retrosh", "class": "FAIL",
                     "differing_pixels": 4, "nanomig_sha": "a", "vamigats_sha": "v"}
                (run / test_dir / variant).mkdir(parents=True)
                (run / test_dir / variant / "result.json").write_text(json.dumps(r))
                (run / test_dir / variant / f"{variant}.frame.vga.gz").write_bytes(gzip.compress(mine))
                (pub / test_dir).mkdir(parents=True, exist_ok=True)
                (pub / test_dir / f"{variant}.result.json").write_text(json.dumps(r))
                (pub / test_dir / f"{variant}.frame.vga.gz").write_bytes(gzip.compress(theirs))
            counts, changed, _ = compare_run(run, pub)
            self.assertEqual((counts["moved"], counts["unchanged"], counts["worse"]), (1, 1, 0))
            self.assertIn(("moved", test_dir, "waitblt4_ocs", "FAIL 4 px -> FAIL 4 px", 0), changed)


if __name__ == "__main__":
    unittest.main()
