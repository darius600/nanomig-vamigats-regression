"""Unit tests for the group pages, the test report, labels and what may be published."""

from pathlib import Path
import unittest

from loader.publish import (
    VAMIGATS,
    check_publishable,
    generate_group_markdown,
    generate_test_report_markdown,
    reference_uniform,
    where_from_render,
)
from loader.retrosh import parse_retrosh

TREE = "https://github.com/dirkwhoffmann/vAmigaTS/tree/e807e32"
REGRESSION = "https://github.com/darius600/nanomig-vamigats-regression/commit/"


class TestPublish(unittest.TestCase):
    def test_only_single_runs_are_published(self):
        single = (Path("a/result.json"), {"class": "PASS", "nanomig_sha": "0f3d2fd"})
        forked = (Path("b/result.json"), {"class": "PASS", "nanomig_sha": "0f3d2fd", "zygote_fork_at_s": 3.5})
        shortened = (Path("d/result.json"), {"class": "FAIL", "nanomig_sha": "0f3d2fd", "test_wait_s": 1.0})
        dirty = (Path("c/result.json"), {"class": "FAIL", "nanomig_sha": "0f3d2fd-dirty"})
        fpga = (Path("e/result.json"), {"class": "PASS", "nanomig_sha": "0f3d2fd", "init": "fpga (experimental)"})
        check_publishable([single])
        for experimental in (forked, shortened, fpga):
            with self.assertRaisesRegex(RuntimeError, "experimental"):
                check_publishable([single, experimental])
        with self.assertRaisesRegex(RuntimeError, "uncommitted"):
            check_publishable([single, dirty])

    def test_generate_group_markdown(self):
        results = [
            {
                "variant": "cop3_nc",
                "class": "NOT_CALIBRATED",
                "geometry": "908x524",
                "machine": "A500_ECS_1MB",
                "retrosh_path": "Agnus/Copper/cop3/cop3_nc.retrosh",
            },
            {
                "variant": "cop1_pass",
                "class": "PASS",
                "differing_pixels": 0,
                "machine": "A500_OCS_1MB",
                "retrosh_path": "Agnus/Copper/cop1/cop1_pass.retrosh",
            },
            {
                "variant": "cop2_fail",
                "class": "FAIL",
                "differing_pixels": 42,
                "machine": "A500_ECS_1MB",
                "labels": ["synccpu"],
                "retrosh_path": "Agnus/Copper/cop2/cop2_fail.retrosh",
            },
        ]

        md = generate_group_markdown(
            group="Agnus/Copper",
            results=results,
            nanomig_sha="0f3d2fd",
            vamigats_sha="e807e32",
        )

        lines = md.splitlines()
        self.assertEqual(lines[0], "# Agnus/Copper")
        self.assertIn("[TestReport.md](TestReport.md#how-to-read)", md)
        self.assertIn("https://github.com/MiSTle-Dev/NanoMig/commit/0f3d2fd", md)
        self.assertIn("e807e32", md)
        self.assertIn("**Compared:** 2 (PASS 1, FAIL 1) | **ERROR:** 0 | **Not compared:** 1", md)
        self.assertIn("NOT_CALIBRATED 1, NO_REFERENCE 0; listed at the end", md)
        self.assertNotIn("Hand", md)

        # Compared rows first, the not compared ones in their own table at the end; the
        # test cell links the test in vAmigaTS at the pinned commit
        pass_row = (f"| [Agnus/Copper/cop1]({TREE}/Agnus/Copper/cop1) | cop1_pass | A500 OCS | PASS | 0 |  "
                    "| [cop1_pass.png](Agnus/Copper/cop1/cop1_pass.png) |")
        fail_row = (f"| [Agnus/Copper/cop2]({TREE}/Agnus/Copper/cop2) | cop2_fail | A500 ECS | FAIL | 42 "
                    "| `synccpu` | [cop2_fail.png](Agnus/Copper/cop2/cop2_fail.png) |")
        nc_row = (f"| [Agnus/Copper/cop3]({TREE}/Agnus/Copper/cop3) | cop3_nc | A500 ECS | NOT_CALIBRATED "
                  "| raster 908x524 not calibrated |  |")
        for row in (pass_row, fail_row, nc_row):
            self.assertIn(row, md)
        self.assertLess(md.index(fail_row), md.index("## Not compared"))
        self.assertLess(md.index("## Not compared"), md.index(nc_row))

    def test_group_without_compared_variants(self):
        md = generate_group_markdown(
            group="CPU/68010",
            results=[{"variant": "colorloop_68010", "class": "DEVIATING_CONFIG", "detail": "cpu 68010",
                      "retrosh_path": "CPU/68010/Loopmode/colorloop/colorloop_68010.retrosh"}],
            nanomig_sha="0f3d2fd",
            vamigats_sha="e807e32",
        )
        self.assertIn("No variant of this group was compared.", md)
        self.assertIn(f"| [CPU/68010/Loopmode/colorloop]({TREE}/CPU/68010/Loopmode/colorloop) | colorloop_68010 "
                      "|  | DEVIATING_CONFIG | cpu 68010 |  |", md)

    def test_generate_test_report_markdown(self):
        old, new = "52cfceb" + "0" * 33, "f2ae07b" + "0" * 33
        results = [
            {
                "variant": "cop1",
                "class": "PASS",
                "raw_status": "byte-identical",
                "regression_sha": old,
                "retrosh_path": "Agnus/Copper/cop1/cop1.retrosh",
            },
            {
                "variant": "blit1",
                "class": "FAIL",
                "differing_pixels": 4,
                "machine": "A500_OCS_1MB",
                "simulated_on": "CPU A, 8 cores",
                "regression_sha": old,
                "retrosh_path": "Agnus/Blitter/blit1/blit1.retrosh",
            },
            {
                "variant": "blit2",
                "class": "FAIL",
                "differing_pixels": 4000,
                "simulated_on": "CPU B, 11 cores",
                "regression_sha": new,
                "retrosh_path": "Agnus/Blitter/blit2/blit2.retrosh",
            },
            {
                "variant": "loop1",
                "class": "DEVIATING_CONFIG",
                "retrosh_path": "CPU/68010/loop1/loop1.retrosh",
            },
        ]

        md = generate_test_report_markdown(
            results=results,
            nanomig_sha="0f3d2fd",
            vamigats_sha="e807e32",
            regression_sha="1bb9f32",
            build_profile="Tang Nano 20k, fx68k",
            date_str="2026-10-12",
            synccpu_fails=1,
            where={"Agnus/Blitter/blit1/blit1": "x 196–199, y 178"},
        )

        self.assertIn("https://github.com/MiSTle-Dev/NanoMig/commit/0f3d2fd", md)
        self.assertIn("https://github.com/dirkwhoffmann/vAmigaTS/commit/e807e32", md)
        self.assertIn("Tang Nano 20k, fx68k", md)
        self.assertIn("[Agnus/Blitter](Agnus_Blitter.md)", md)
        self.assertIn("[Agnus/Copper](Agnus_Copper.md)", md)
        self.assertIn("How to read", md)
        # every regression commit that made results, and the one that published
        self.assertIn(f"results made with [`52cfceb`]({REGRESSION}{old}) (2 variants), "
                      f"[`f2ae07b`]({REGRESSION}{new}) (1 variants); published with [`1bb9f32`]({REGRESSION}1bb9f32)",
                      md)

        # The overview first (summary with the compared count, groups), then where to start
        self.assertIn("**3 variants compared: 1 identical to the reference (PASS), 2 different (FAIL).** "
                      "The other 1 were not compared", md)
        near_row = (f"| [Agnus/Blitter/blit1]({TREE}/Agnus/Blitter/blit1) | blit1 | A500 OCS | 4 | x 196–199, y 178 "
                    "| [blit1.png](Agnus/Blitter/blit1/blit1.png) |")
        self.assertLess(md.index("## Summary"), md.index("## Results per group"))
        self.assertLess(md.index("## Results per group"), md.index("## Where to start"))
        self.assertLess(md.index("## Where to start"), md.index(near_row))
        self.assertNotIn("| blit2 |", md)
        self.assertIn("1 of the 2 FAILs come from tests whose source calls", md)
        self.assertIn("In all 1 PASS the `.raw` rebuilt", md)
        self.assertNotIn("## Agreement", md)

        # Which CPUs simulated, and the note on several machines
        self.assertIn("- **Simulated on:** CPU A, 8 cores (1 variants); CPU B, 11 cores (1 variants)", md)
        self.assertIn("**Several machines** simulated this run", md)

        # Group rows add up: every class has its column
        self.assertIn("| [CPU/68010](CPU_68010.md) | 1 | 0 | 0 | 1 | 0 | 0 | 0 |", md)

    def test_where_from_render(self):
        self.assertEqual(where_from_render("waitblt1_ocs: 4 px differ (x 196-199, y 178-178)"), "x 196–199, y 178")
        self.assertEqual(where_from_render("stop1: 2 px differ (x 256-257, y 40-41)"), "x 256–257, y 40–41")
        self.assertEqual(where_from_render("cross1_ocs: identical"), "")

    def test_reference_uniform(self):
        """The NTSC _ocs references are black where they are compared; cross1's is not."""
        ntsc = VAMIGATS / "Agnus/Misc/NTSC/ntsc3/ntsc3_ocs.retrosh"
        cross = VAMIGATS / "Agnus/Copper/cross/cross1/cross1_ocs.retrosh"
        self.assertTrue(reference_uniform(ntsc, parse_retrosh(ntsc)))
        self.assertFalse(reference_uniform(cross, parse_retrosh(cross)))


if __name__ == "__main__":
    unittest.main()
