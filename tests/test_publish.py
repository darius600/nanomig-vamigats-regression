"""Unit tests for hand-verdict parsing, group table, and agreement."""

from pathlib import Path
import unittest

from loader.publish import (
    check_publishable,
    compute_agreement,
    generate_group_markdown,
    generate_test_report_markdown,
    normalize_verdict,
    parse_manual_verdicts,
)


class TestPublish(unittest.TestCase):
    def test_parse_manual_verdicts(self):
        sample_md = """# Agnus_Blitter

| Test Case | Description / Source | Last run | Result | Comment |
| --------- | -------------------- | -------- | ------ | ------- |
| Agnus/Blitter/bbusy/bbusy0 | [Agnus/Blitter/bbusy](https://example.com) | 2024-08-26 | ❌ Different | Differences in timing |
| Agnus/Blitter/bbusy/bbusy1 | [Agnus/Blitter/bbusy](https://example.com) | 2024-08-26 | ✅ Identical | Matches hardware |
| Agnus/Blitter/bbusy/bbusy2 | [Agnus/Blitter/bbusy](https://example.com) | 2024-08-26 |  |  |
| Agnus/Blitter/bbusy/bbusy3 | [Agnus/Blitter/bbusy](https://example.com) | 2024-08-26 | ❌ Different | Note: with | pipe in comment |
"""
        verdicts = parse_manual_verdicts(sample_md)
        self.assertEqual(len(verdicts), 4)
        self.assertEqual(
            verdicts["Agnus/Blitter/bbusy/bbusy0"],
            ("❌ Different", "Differences in timing"),
        )
        self.assertEqual(
            verdicts["Agnus/Blitter/bbusy/bbusy1"],
            ("✅ Identical", "Matches hardware"),
        )
        self.assertEqual(
            verdicts["Agnus/Blitter/bbusy/bbusy2"],
            ("", ""),
        )
        self.assertEqual(
            verdicts["Agnus/Blitter/bbusy/bbusy3"],
            ("❌ Different", "Note: with | pipe in comment"),
        )

    def test_only_single_runs_are_published(self):
        single = (Path("a/result.json"), {"class": "PASS", "nanomig_sha": "0f3d2fd"})
        forked = (Path("b/result.json"), {"class": "PASS", "nanomig_sha": "0f3d2fd", "zygote_fork_at_s": 3.5})
        shortened = (Path("d/result.json"), {"class": "FAIL", "nanomig_sha": "0f3d2fd", "test_wait_s": 1.0})
        dirty = (Path("c/result.json"), {"class": "FAIL", "nanomig_sha": "0f3d2fd-dirty"})
        check_publishable([single])
        for experimental in (forked, shortened):
            with self.assertRaisesRegex(RuntimeError, "experimental"):
                check_publishable([single, experimental])
        with self.assertRaisesRegex(RuntimeError, "uncommitted"):
            check_publishable([single, dirty])

    def test_compute_agreement(self):
        manual_verdicts = {
            "Agnus/Copper/test/t1": ("✅ Identical", ""),
            "Agnus/Copper/test/t2": ("❌ Different", ""),
            "Agnus/Copper/test/t3": ("✅ Identical", "was ok"),
            "Agnus/Copper/test/t4": ("❌ Different", "failed"),
            "Agnus/Copper/test/t5": ("", "no verdict"),
            "Agnus/Copper/test/t6": ("❌ Different", ""),
        }

        variants_by_td = {
            # t1: both identical (auto PASS, manual Identical)
            "Agnus/Copper/test/t1": [
                {"variant": "t1_ocs", "class": "PASS"},
                {"variant": "t1_ecs", "class": "PASS"},
            ],
            # t2: both different (auto FAIL, manual Different)
            "Agnus/Copper/test/t2": [
                {"variant": "t2_ocs", "class": "PASS"},
                {"variant": "t2_ecs", "class": "FAIL"},
            ],
            # t3: hand ident / auto diff (manual Identical, auto FAIL)
            "Agnus/Copper/test/t3": [
                {"variant": "t3_ocs", "class": "FAIL"},
            ],
            # t4: hand diff / auto ident (manual Different, auto PASS)
            "Agnus/Copper/test/t4": [
                {"variant": "t4_ocs", "class": "PASS"},
            ],
            # t5: manual empty -> n/a
            "Agnus/Copper/test/t5": [
                {"variant": "t5_ocs", "class": "PASS"},
            ],
            # t6: auto NOT_CALIBRATED -> n/a
            "Agnus/Copper/test/t6": [
                {"variant": "t6_ocs", "class": "NOT_CALIBRATED"},
            ],
        }

        tally = compute_agreement(manual_verdicts, variants_by_td)
        self.assertEqual(tally["both_identical"], 1)
        self.assertEqual(tally["both_different"], 1)
        self.assertEqual(tally["hand_ident_auto_diff"], 1)
        self.assertEqual(tally["hand_diff_auto_ident"], 1)
        self.assertEqual(tally["na"], 2)

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
                "retrosh_path": "Agnus/Copper/cop2/cop2_fail.retrosh",
            },
        ]
        manual_verdicts = {
            "Agnus/Copper/cop1": ("✅ Identical", "matches"),
            "Agnus/Copper/cop2": ("❌ Different", "broken copper"),
        }

        md = generate_group_markdown(
            group="Agnus/Copper",
            results=results,
            manual_verdicts=manual_verdicts,
            nanomig_sha="0f3d2fd",
            vamigats_sha="e807e32",
        )

        lines = md.splitlines()
        self.assertTrue(lines[0].startswith("# Agnus_Copper"))
        self.assertIn("0f3d2fd", md)
        self.assertIn("e807e32", md)
        self.assertIn("**Compared:** 2 (PASS 1, FAIL 1) | **ERROR:** 0 | **Not compared:** 1", md)
        self.assertIn("NOT_CALIBRATED 1, NO_REFERENCE 0; listed at the end", md)

        # Compared rows first, the not compared ones in their own table at the end
        pass_row = ("| Agnus/Copper/cop1 | cop1_pass | A500 OCS | PASS | 0 |  | ✅ Identical | matches "
                    "| [cop1_pass.png](Agnus/Copper/cop1/cop1_pass.png) |")
        fail_row = ("| Agnus/Copper/cop2 | cop2_fail | A500 ECS | FAIL | 42 |  | ❌ Different | broken copper "
                    "| [cop2_fail.png](Agnus/Copper/cop2/cop2_fail.png) |")
        nc_row = "| Agnus/Copper/cop3 | cop3_nc | A500 ECS | NOT_CALIBRATED | raster 908x524 not calibrated |  |"
        for row in (pass_row, fail_row, nc_row):
            self.assertIn(row, md)
        self.assertLess(md.index(fail_row), md.index("## Not compared"))
        self.assertLess(md.index("## Not compared"), md.index(nc_row))

    def test_group_without_compared_variants(self):
        md = generate_group_markdown(
            group="CPU/68010",
            results=[{"variant": "colorloop_68010", "class": "DEVIATING_CONFIG", "detail": "cpu 68010",
                      "retrosh_path": "CPU/68010/Loopmode/colorloop/colorloop_68010.retrosh"}],
            manual_verdicts={},
            nanomig_sha="0f3d2fd",
            vamigats_sha="e807e32",
        )
        self.assertIn("No variant of this group was compared.", md)
        self.assertIn("| CPU/68010/Loopmode/colorloop | colorloop_68010 |  | DEVIATING_CONFIG | cpu 68010 |  |", md)

    def test_generate_test_report_markdown(self):
        results = [
            {
                "variant": "cop1",
                "class": "PASS",
                "raw_status": "byte-identical",
                "retrosh_path": "Agnus/Copper/cop1/cop1.retrosh",
            },
            {
                "variant": "blit1",
                "class": "FAIL",
                "differing_pixels": 4,
                "machine": "A500_OCS_1MB",
                "retrosh_path": "Agnus/Blitter/blit1/blit1.retrosh",
            },
            {
                "variant": "blit2",
                "class": "FAIL",
                "differing_pixels": 4000,
                "retrosh_path": "Agnus/Blitter/blit2/blit2.retrosh",
            },
            {
                "variant": "loop1",
                "class": "DEVIATING_CONFIG",
                "retrosh_path": "CPU/68010/loop1/loop1.retrosh",
            },
        ]
        agreements = {
            "Agnus/Copper": {
                "both_identical": 1,
                "both_different": 0,
                "hand_ident_auto_diff": 0,
                "hand_diff_auto_ident": 0,
                "na": 0,
            },
            "Agnus/Blitter": {
                "both_identical": 0,
                "both_different": 1,
                "hand_ident_auto_diff": 0,
                "hand_diff_auto_ident": 0,
                "na": 0,
            },
        }

        md = generate_test_report_markdown(
            results=results,
            group_agreements=agreements,
            nanomig_sha="0f3d2fd",
            vamigats_sha="e807e32",
            regression_sha="1bb9f32",
            build_profile="Tang Nano 20k, fx68k",
            date_str="2026-10-12",
            synccpu_fails=1,
        )

        self.assertIn("https://github.com/harbaum/NanoMig/commit/0f3d2fd", md)
        self.assertIn("https://github.com/dirkwhoffmann/vAmigaTS/commit/e807e32", md)
        self.assertIn("nanomig-vamigats-regression/commit/1bb9f32", md)
        self.assertIn("Tang Nano 20k, fx68k", md)
        self.assertIn("[Agnus/Blitter](Agnus_Blitter.md)", md)
        self.assertIn("[Agnus/Copper](Agnus_Copper.md)", md)
        self.assertIn("How to read", md)

        # The overview first (summary with the compared count, groups), then where to start
        self.assertIn("**3 variants compared: 1 identical to the reference (PASS), 2 different (FAIL).** "
                      "The other 1 were not compared", md)
        near_row = "| Agnus/Blitter/blit1 | blit1 | A500 OCS | 4 | [blit1.png](Agnus/Blitter/blit1/blit1.png) |"
        self.assertLess(md.index("## Summary"), md.index("## Results per group"))
        self.assertLess(md.index("## Results per group"), md.index("## Where to start"))
        self.assertLess(md.index("## Where to start"), md.index(near_row))
        self.assertNotIn("| Agnus/Blitter/blit2 | blit2 |", md)
        self.assertIn("1 of the 2 FAILs come from tests whose source calls", md)
        self.assertIn("In all 1 PASS the `.raw` rebuilt", md)

        # Group rows add up: every class has its column
        self.assertIn("| [CPU/68010](CPU_68010.md) | 1 | 0 | 0 | 1 | 0 | 0 | 0 |", md)
        # Agreement is per test directory and leaves out groups without hand verdicts
        self.assertIn("| [Agnus/Copper](Agnus_Copper.md) | 1 | 0 | 0 | 0 |", md)
        agreement = md[md.index("## Agreement"):md.index("## How to read")]
        self.assertNotIn("CPU/68010", agreement)


if __name__ == "__main__":
    unittest.main()
