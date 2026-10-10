"""Unit tests for report aggregation on synthetic result.json files."""

import json
from pathlib import Path
import tempfile
import unittest

from loader.report import (
    build_report_data,
    generate_report_markdown,
    get_vamigats_group,
    to_repo_rel,
    write_reports,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestReportAggregation(unittest.TestCase):
    def setUp(self):
        self.synthetic_results = [
            # Agnus/Copper group
            {
                "variant": "cop1_pass",
                "class": "PASS",
                "detail": "identical",
                "labels": ["denise-ecs-sensitive"],
                "nanomig_sha": "0f3d2fd",
                "vamigats_sha": "e807e32",
                "build_profile": "tang_nano_20k",
                "retrosh_path": "Agnus/Copper/test1/cop1_pass.retrosh",
                "harness_args": ["roms/kick13.rom", "cop1_pass.frame"],
                "harness_exit_code": 0,
                "start_time": "2026-10-10T08:00:00Z",
                "end_time": "2026-10-10T08:01:00Z",
                "wall_time": 60.0,
                "differing_pixels": 0,
                "raw_status": "byte-identical",
            },
            {
                "variant": "cop2_fail",
                "class": "FAIL",
                "detail": "42 differing pixels",
                "labels": ["denise-ecs-sensitive"],
                "nanomig_sha": "0f3d2fd",
                "vamigats_sha": "e807e32",
                "build_profile": "tang_nano_20k",
                "retrosh_path": "Agnus/Copper/test2/cop2_fail.retrosh",
                "harness_args": ["roms/kick13.rom", "cop2_fail.frame"],
                "harness_exit_code": 0,
                "start_time": "2026-10-10T08:01:00Z",
                "end_time": "2026-10-10T08:02:00Z",
                "wall_time": 60.0,
                "differing_pixels": 42,
                "raw_status": "bytes differ",
                "diff_image": "runs/test/Agnus/Copper/test2/cop2_fail/cop2_fail_diff.png",
            },
            # Agnus/Blitter group (alphabetically before Copper)
            {
                "variant": "blit1_fail",
                "class": "FAIL",
                "detail": "5 differing pixels",
                "labels": [],
                "nanomig_sha": "0f3d2fd",
                "vamigats_sha": "e807e32",
                "build_profile": "tang_nano_20k",
                "retrosh_path": "Agnus/Blitter/line/blit1_fail.retrosh",
                "harness_args": ["roms/kick13.rom", "blit1_fail.frame"],
                "harness_exit_code": 0,
                "start_time": "2026-10-10T08:02:00Z",
                "end_time": "2026-10-10T08:03:00Z",
                "wall_time": 60.0,
                "differing_pixels": 5,
                "raw_status": "bytes differ",
                "diff_image": "runs/test/Agnus/Blitter/line/blit1_fail/blit1_fail_diff.png",
            },
            # Agnus/Misc group (NOT_CALIBRATED)
            {
                "variant": "ntsc1",
                "class": "NOT_CALIBRATED",
                "detail": "uncalibrated geometry: 908x524",
                "geometry": "908x524",
                "labels": [],
                "nanomig_sha": "0f3d2fd",
                "vamigats_sha": "e807e32",
                "build_profile": "tang_nano_20k",
                "retrosh_path": "Agnus/Misc/NTSC/ntsc1/ntsc1.retrosh",
                "harness_args": ["roms/kick13.rom", "ntsc1.frame"],
                "harness_exit_code": 0,
                "start_time": "2026-10-10T08:03:00Z",
                "end_time": "2026-10-10T08:04:00Z",
                "wall_time": 60.0,
                "differing_pixels": None,
                "raw_status": None,
            },
            # CPU/68010 group (DEVIATING_CONFIG)
            {
                "variant": "loop1_68010",
                "class": "DEVIATING_CONFIG",
                "detail": "cpu 68010",
                "labels": [],
                "nanomig_sha": "0f3d2fd",
                "vamigats_sha": "e807e32",
                "build_profile": "tang_nano_20k",
                "retrosh_path": "CPU/68010/Loopmode/loop1/loop1_68010.retrosh",
                "harness_args": None,
                "harness_exit_code": None,
                "start_time": "2026-10-10T08:04:00Z",
                "end_time": "2026-10-10T08:04:00Z",
                "wall_time": 0.0,
                "differing_pixels": None,
                "raw_status": None,
            },
            # Custom/Group (ERROR)
            {
                "variant": "err1",
                "class": "ERROR",
                "detail": "Harness timed out after 60s",
                "labels": [],
                "nanomig_sha": "0f3d2fd",
                "vamigats_sha": "e807e32",
                "build_profile": "tang_nano_20k",
                "retrosh_path": "Custom/Group/err1/err1.retrosh",
                "harness_args": None,
                "harness_exit_code": None,
                "start_time": "2026-10-10T08:05:00Z",
                "end_time": "2026-10-10T08:06:00Z",
                "wall_time": 60.0,
                "differing_pixels": None,
                "raw_status": None,
            },
        ]

    def test_get_vamigats_group(self):
        self.assertEqual(
            get_vamigats_group("Agnus/Copper/cross/cross1/cross1_ocs.retrosh"),
            "Agnus/Copper",
        )
        self.assertEqual(
            get_vamigats_group("CPU/68010/Loopmode/colorloop/colorloop_68010.retrosh"),
            "CPU/68010",
        )
        self.assertEqual(
            get_vamigats_group("single.retrosh"),
            "single.retrosh",
        )

    def test_build_report_data_counts(self):
        data = build_report_data(
            results=self.synthetic_results,
            subtrees=["Agnus/Copper", "Agnus/Blitter", "Agnus/Misc", "CPU/68010", "Custom/Group"],
            nanomig_sha="0f3d2fd",
            vamigats_sha="e807e32",
            build_profile="tang_nano_20k",
            wall_time=240.5,
            report_date="2026-10-10T08:10:00Z",
        )

        # Overall summary
        summary = data["summary"]
        self.assertEqual(summary["total"], 6)
        self.assertEqual(summary["PASS"], 1)
        self.assertEqual(summary["FAIL"], 2)
        self.assertEqual(summary["NOT_CALIBRATED"], 1)
        self.assertEqual(summary["DEVIATING_CONFIG"], 1)
        self.assertEqual(summary["ERROR"], 1)

        # Denise ECS Sensitivity: 1 PASS, 1 FAIL
        ecs = data["denise_ecs_sensitive"]
        self.assertEqual(ecs["PASS"], 1)
        self.assertEqual(ecs["FAIL"], 1)

        # Counts by group
        by_group = data["by_group"]
        self.assertEqual(by_group["Agnus/Copper"]["total"], 2)
        self.assertEqual(by_group["Agnus/Copper"]["PASS"], 1)
        self.assertEqual(by_group["Agnus/Copper"]["FAIL"], 1)

        self.assertEqual(by_group["Agnus/Blitter"]["total"], 1)
        self.assertEqual(by_group["Agnus/Blitter"]["FAIL"], 1)

        self.assertEqual(by_group["Agnus/Misc"]["total"], 1)
        self.assertEqual(by_group["Agnus/Misc"]["NOT_CALIBRATED"], 1)

        self.assertEqual(by_group["CPU/68010"]["total"], 1)
        self.assertEqual(by_group["CPU/68010"]["DEVIATING_CONFIG"], 1)

        self.assertEqual(by_group["Custom/Group"]["total"], 1)
        self.assertEqual(by_group["Custom/Group"]["ERROR"], 1)

        # Failures list must be sorted by group: Agnus/Blitter before Agnus/Copper
        failures = data["failures"]
        self.assertEqual(len(failures), 2)
        self.assertEqual(failures[0]["group"], "Agnus/Blitter")
        self.assertEqual(failures[0]["variant"], "blit1_fail")
        self.assertEqual(failures[0]["differing_pixels"], 5)
        self.assertEqual(failures[1]["group"], "Agnus/Copper")
        self.assertEqual(failures[1]["variant"], "cop2_fail")
        self.assertEqual(failures[1]["differing_pixels"], 42)

        # Deviating config list
        dev = data["deviating_config"]
        self.assertEqual(len(dev), 1)
        self.assertEqual(dev[0]["variant"], "loop1_68010")
        self.assertEqual(dev[0]["reason"], "cpu 68010")

        # Not calibrated list
        not_cal = data["not_calibrated"]
        self.assertEqual(len(not_cal), 1)
        self.assertEqual(not_cal[0]["variant"], "ntsc1")
        self.assertEqual(not_cal[0]["geometry"], "908x524")

    def test_markdown_formatting_and_no_absolute_paths(self):
        data = build_report_data(
            results=self.synthetic_results,
            subtrees=["Agnus/Copper", "Agnus/Blitter", "Agnus/Misc", "CPU/68010", "Custom/Group"],
            nanomig_sha="0f3d2fd",
            vamigats_sha="e807e32",
            build_profile="tang_nano_20k",
            wall_time=240.5,
            report_date="2026-10-10T08:10:00Z",
        )
        md = generate_report_markdown(data)

        # Basic markdown structure
        self.assertIn("# NanoMig vAmigaTS Regression Report", md)
        self.assertIn("| Agnus/Copper | 2 | 1 | 1 | 0 | 0 | 0 |", md)
        self.assertIn("| Agnus/Blitter | 1 | 0 | 1 | 0 | 0 | 0 |", md)
        self.assertIn("cop2_fail", md)
        self.assertIn("blit1_fail", md)
        self.assertIn("loop1_68010", md)
        self.assertIn("908x524", md)
        self.assertIn("- **PASS:** 1", md)
        self.assertIn("- **FAIL:** 1", md)

        # No absolute local paths
        for forbidden in ["C:\\", "/mnt/", "/home/", "/Users/"]:
            self.assertNotIn(forbidden, md)
            self.assertNotIn(forbidden, json.dumps(data))

    def test_write_reports_file_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            md_path, json_path = write_reports(
                out_dir=tmp_path,
                results=self.synthetic_results,
                subtrees=["Agnus/Copper"],
                nanomig_sha="0f3d2fd",
                vamigats_sha="e807e32",
                build_profile="tang_nano_20k",
                wall_time=12.34,
            )

            self.assertTrue(md_path.is_file())
            self.assertTrue(json_path.is_file())

            json_content = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(json_content["summary"]["total"], 6)
            self.assertEqual(json_content["summary"]["PASS"], 1)

            md_text = md_path.read_text(encoding="utf-8")
            self.assertIn("# NanoMig vAmigaTS Regression Report", md_text)


if __name__ == "__main__":
    unittest.main()
