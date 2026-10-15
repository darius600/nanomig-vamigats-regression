"""Unit tests for hand-verdict parsing, group table, and agreement."""

from pathlib import Path
import unittest

from loader.publish import (
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
                "variant": "cop1_pass",
                "class": "PASS",
                "differing_pixels": 0,
                "retrosh_path": "Agnus/Copper/cop1/cop1_pass.retrosh",
            },
            {
                "variant": "cop2_fail",
                "class": "FAIL",
                "differing_pixels": 42,
                "retrosh_path": "Agnus/Copper/cop2/cop2_fail.retrosh",
            },
            {
                "variant": "cop3_nc",
                "class": "NOT_CALIBRATED",
                "geometry": "908x524",
                "retrosh_path": "Agnus/Copper/cop3/cop3_nc.retrosh",
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
        self.assertIn("Total:** 3", md)
        self.assertIn("PASS:** 1", md)
        self.assertIn("FAIL:** 1", md)
        self.assertIn("NOT_CALIBRATED:** 1", md)

        # Table rows
        self.assertIn(
            "| Agnus/Copper/cop1 | cop1_pass | PASS | 0 |  | ✅ Identical | matches | [cop1_pass.png](Agnus/Copper/cop1/cop1_pass.png) |",
            md,
        )
        self.assertIn(
            "| Agnus/Copper/cop2 | cop2_fail | FAIL | 42 |  | ❌ Different | broken copper | [cop2_fail.png](Agnus/Copper/cop2/cop2_fail.png) |",
            md,
        )
        self.assertIn(
            "| Agnus/Copper/cop3 | cop3_nc | NOT_CALIBRATED | 908x524 |  |  |  | — |",
            md,
        )

    def test_generate_test_report_markdown(self):
        results = [
            {
                "variant": "cop1",
                "class": "PASS",
                "retrosh_path": "Agnus/Copper/cop1/cop1.retrosh",
            },
            {
                "variant": "blit1",
                "class": "FAIL",
                "retrosh_path": "Agnus/Blitter/blit1/blit1.retrosh",
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
        )

        self.assertIn("https://github.com/harbaum/NanoMig/commit/0f3d2fd", md)
        self.assertIn("https://github.com/dirkwhoffmann/vAmigaTS/commit/e807e32", md)
        self.assertIn("1bb9f32", md)
        self.assertIn("Tang Nano 20k, fx68k", md)
        self.assertIn("[Agnus/Blitter](Agnus_Blitter.md)", md)
        self.assertIn("[Agnus/Copper](Agnus_Copper.md)", md)
        self.assertIn("How to read", md)


if __name__ == "__main__":
    unittest.main()
