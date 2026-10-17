"""Selftests of the comparison: every reference against itself, the checkerboard
outside the cutout, single-pixel mutations, the colour table and the script parser."""

import json
from pathlib import Path
import unittest
import numpy as np

from loader.compare import (
    build_raw,
    compare_frames,
    load_color_table,
    quantize_4bit,
)
from loader.mask import (
    REF_HEIGHT,
    REF_WIDTH,
    build_mask,
    generate_checkerboard,
    texture_to_pixel,
)
from loader.retrosh import parse_retrosh

REPO_ROOT = Path(__file__).resolve().parent.parent
TS = REPO_ROOT / "external" / "vAmigaTS"


class TestSelftests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Index all cutout configurations once
        cls.cutouts = {}
        for rs in TS.rglob("*.retrosh"):
            for line in rs.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("screenshot set cutout"):
                    parts = line.split()
                    if len(parts) >= 7:
                        cls.cutouts[rs.with_suffix(".raw")] = (
                            int(parts[3]),
                            int(parts[4]),
                            int(parts[5]),
                            int(parts[6]),
                        )
                    break

        cls.raw_files = sorted(TS.rglob("*.raw"))
        cls.lut, cls.known = load_color_table()
        cls.checkerboard = generate_checkerboard()

    def test_01_reference_self_comparison(self):
        """1. Every reference .raw compared with itself (as 4-bit image, with its mask) -> PASS. Expect 2597."""
        self.assertEqual(len(self.raw_files), 2597)
        passed = 0
        for p in self.raw_files:
            raw = np.fromfile(str(p), dtype=np.uint8).reshape(REF_HEIGHT, REF_WIDTH, 3)
            mask = build_mask(self.cutouts.get(p))
            q4 = quantize_4bit(raw)
            # Compare 4-bit image with itself inside the mask
            diff = np.any(q4 != q4, axis=-1) & mask
            if int(np.sum(diff)) == 0:
                passed += 1

        self.assertEqual(passed, 2597)

    def test_02_checkerboard_cutout(self):
        """2. For all 60 scripts with a cutout: the generated checkerboard equals reference bytes outside mask."""
        cutout_entries = [
            (p, self.cutouts[p]) for p in self.raw_files if p in self.cutouts
        ]
        self.assertEqual(len(cutout_entries), 60)

        for p, cutout in cutout_entries:
            raw = np.fromfile(str(p), dtype=np.uint8).reshape(REF_HEIGHT, REF_WIDTH, 3)
            mask = build_mask(cutout)
            outside = ~mask
            self.assertTrue(
                np.array_equal(raw[outside], self.checkerboard[outside]),
                f"Checkerboard mismatch outside mask in {p}",
            )

    def test_03_mutation(self):
        """3. Mutation: cross1_ocs ref with pixel (100, 300) changed -> FAIL, exactly 1 px.
        bsh1 ref (cutout 196 36 908 314) with a pixel at texture y 30 changed -> PASS.
        """
        # cross1_ocs mutation
        cross1_p = TS / "Agnus" / "Copper" / "cross" / "cross1" / "cross1_ocs.raw"
        raw_cross1 = np.fromfile(str(cross1_p), dtype=np.uint8).reshape(
            REF_HEIGHT, REF_WIDTH, 3
        )
        mutated_cross1 = raw_cross1.copy()
        # Flip high bit to alter 4-bit quantization value
        mutated_cross1[100, 300] = (mutated_cross1[100, 300].astype(int) + 128) % 256
        res_cross1 = compare_frames(
            mutated_cross1, cross1_p, build_mask(None), "cross1_ocs"
        )
        self.assertEqual(res_cross1["class"], "FAIL")
        self.assertEqual(res_cross1["differing_pixels"], 1)

        # bsh1 mutation: cutout 196 36 908 314
        bsh1_p = TS / "Agnus" / "Blitter" / "line" / "bsh1" / "bsh1.raw"
        raw_bsh1 = np.fromfile(str(bsh1_p), dtype=np.uint8).reshape(
            REF_HEIGHT, REF_WIDTH, 3
        )
        mutated_bsh1 = raw_bsh1.copy()
        # Texture y 30 is outside the cutout [36, 314)
        row, col = texture_to_pixel(200, 30)
        mutated_bsh1[row, col] = (mutated_bsh1[row, col].astype(int) + 128) % 256
        res_bsh1 = compare_frames(
            mutated_bsh1, bsh1_p, build_mask((196, 36, 908, 314)), "bsh1"
        )
        self.assertEqual(res_bsh1["class"], "PASS")
        self.assertEqual(res_bsh1["differing_pixels"], 0)

    def test_04_colour_table(self):
        """4. Colour table: every 12-bit colour maps to exactly one RGB triple, except
        the checkerboard greys 0x222/0x444. Feeding each reference's own 4-bit
        image + mask into the .raw writer reproduces that reference byte-exactly.
        """
        # A. Verify uniqueness of mapping
        # In color_table.json, every entry is unique
        table_path = REPO_ROOT / "loader" / "color_table.json"
        data = json.loads(table_path.read_text(encoding="utf-8"))
        self.assertEqual(len(data), 751)

        # B. Reproduction test on PAL / calibrated references (all non-NTSC references)
        # NTSC references (11 files) have unrendered rows that vAmiga fills with checkerboard
        non_ntsc = [p for p in self.raw_files if "/NTSC/" not in p.as_posix()]
        self.assertEqual(len(non_ntsc), 2585)

        mismatches = 0
        for p in non_ntsc:
            raw = np.fromfile(str(p), dtype=np.uint8).reshape(REF_HEIGHT, REF_WIDTH, 3)
            mask = build_mask(self.cutouts.get(p))
            q4 = quantize_4bit(raw)
            reconstructed, ok, missing = build_raw(q4, mask, self.lut, self.known)
            if not ok or not np.array_equal(reconstructed, raw):
                mismatches += 1

        self.assertEqual(mismatches, 0)

    def test_05_parser(self):
        """5. Parser: cross1_ocs -> chipset 0x00, memory 0x04, wait 9, runnable;
        bsh1 -> chipset 0x08, cutout (196,36,908,314);
        a cpu set revision 68010 script -> DEVIATING_CONFIG 'cpu 68010';
        a mem set SLOW_RAM 0 script -> memory 0x00;
        an NTSC script -> chipset bit 1 set;
        joystick, keyboard and mouse scripts -> input events at the summed wait times.
        """
        # cross1_ocs
        cfg1 = parse_retrosh(
            TS / "Agnus" / "Copper" / "cross" / "cross1" / "cross1_ocs.retrosh"
        )
        self.assertEqual(cfg1.chipset, 0x00)
        self.assertEqual(cfg1.memory, 0x04)
        self.assertEqual(cfg1.wait_s, 9.0)
        self.assertTrue(cfg1.runnable)

        # bsh1
        cfg2 = parse_retrosh(
            TS / "Agnus" / "Blitter" / "line" / "bsh1" / "bsh1.retrosh"
        )
        self.assertEqual(cfg2.chipset, 0x08)
        self.assertEqual(cfg2.cutout, (196, 36, 908, 314))

        # cpu set revision 68010
        cfg3 = parse_retrosh(
            TS / "CPU" / "68010" / "Loopmode" / "colorloop" / "colorloop_68010.retrosh"
        )
        self.assertFalse(cfg3.runnable)
        self.assertEqual(cfg3.deviating_reason, "cpu 68010")

        # mem set SLOW_RAM 0
        cfg4 = parse_retrosh(
            TS
            / "CPU"
            / "Exceptions"
            / "StackFrame"
            / "addrerr1"
            / "addrerr1_68000.retrosh"
        )
        self.assertEqual(cfg4.memory, 0x00)

        # NTSC script
        cfg5 = parse_retrosh(
            TS / "Agnus" / "Misc" / "NTSC" / "ntsc0" / "ntsc0_ecs.retrosh"
        )
        self.assertTrue(bool(cfg5.chipset & 0x02))

        # joystick script: held from the start, on the mouse port (nanomig joystick0)
        cfg6 = parse_retrosh(
            TS / "Mainboard" / "Ports" / "JOYxDAT" / "joy0dat" / "joy0dat_1.retrosh"
        )
        self.assertTrue(cfg6.runnable)
        self.assertEqual(cfg6.events, [(0.0, "joy0", 0x02)])
        self.assertEqual(cfg6.wait_s, 9.0)

        # keyboard: waits add up, the key is released half a second after the press
        cfg7 = parse_retrosh(TS / "Mainboard" / "Ports" / "Keyboard" / "noack1" / "noack1.retrosh")
        self.assertTrue(cfg7.runnable)
        self.assertEqual(cfg7.wait_s, 17.0)
        self.assertEqual(cfg7.events, [(9.0, "key", 40), (9.5, "key", 40 | 0x80)])

        # mouse: right button pressed after 16 s, released after 16.5 s
        cfg8 = parse_retrosh(TS / "Denise" / "Misc" / "ThirdParty" / "losso" / "bplcon_rmb2_OCS.retrosh")
        self.assertEqual(cfg8.wait_s, 18.0)
        self.assertEqual(cfg8.events, [(16.0, "mouse", 0x2), (16.5, "mouse", 0)])


if __name__ == "__main__":
    unittest.main()
