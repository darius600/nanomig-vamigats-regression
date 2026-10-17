"""NTSC frames: vAmiga's background below the NTSC beam, the compared rows and the crop."""

from pathlib import Path
import unittest

import numpy as np

from loader.capture import crop_vga_raster
from loader.compare import build_raw, compare_frames, compared_area, load_color_table, quantize_4bit
from loader.mask import NTSC_BEAM_ROWS, build_mask, generate_ntsc_background, is_ntsc_frame

REPO_ROOT = Path(__file__).resolve().parent.parent
NTSC = REPO_ROOT / "external" / "vAmigaTS" / "Agnus" / "Misc" / "NTSC"


def load(p):
    return np.fromfile(str(p), dtype=np.uint8).reshape(285, 716, 3)


class TestNtsc(unittest.TestCase):
    def test_ntsc_references_detected(self):
        """The 11 NTSC-raster references end in vAmiga's NTSC background; ntsc3_ecs switches
        to PAL and a PAL test is no NTSC frame either."""
        refs = sorted(NTSC.glob("ntsc*/ntsc*_*.raw"))
        ntsc = [p.name for p in refs if is_ntsc_frame(load(p))]
        self.assertEqual(len(ntsc), 11)
        self.assertNotIn("ntsc3_ecs.raw", ntsc)
        pal = REPO_ROOT / "external/vAmigaTS/Agnus/Copper/cross/cross1/cross1_ocs.raw"
        self.assertFalse(is_ntsc_frame(load(pal)))

    def test_ntsc_references_rebuilt_byte_exact(self):
        """Each NTSC reference's own 4-bit image, with the NTSC rows and background,
        rebuilds that reference byte for byte."""
        lut, known = load_color_table()
        for p in sorted(NTSC.glob("ntsc*/ntsc*_*.raw")):
            ref = load(p)
            if not is_ntsc_frame(ref):
                continue
            area, background = compared_area(build_mask(None), ref, ntsc_capture=True)
            raw, ok, _ = build_raw(quantize_4bit(ref), area, lut, known, background)
            self.assertTrue(ok, p.name)
            self.assertTrue(np.array_equal(raw, ref), p.name)

    def test_rows_below_the_beam(self):
        """Below the NTSC beam nothing is compared when both rasters are NTSC; when
        NanoMig's raster is PAL, those rows count."""
        ref_path = NTSC / "ntsc0" / "ntsc0_ocs.raw"
        frame = load(ref_path).copy()
        frame[NTSC_BEAM_ROWS + 3, 100] = (255, 255, 255)
        both_ntsc = compare_frames(frame, ref_path, build_mask(None), "ntsc0_ocs", ntsc_capture=True)
        pal_capture = compare_frames(frame, ref_path, build_mask(None), "ntsc0_ocs", ntsc_capture=False)
        self.assertEqual(both_ntsc["class"], "PASS")
        self.assertEqual(both_ntsc["raw_status"], "byte-identical")
        self.assertEqual(pal_capture["class"], "FAIL")
        self.assertEqual(pal_capture["differing_pixels"], 1)

    def test_crop_pads_ntsc_raster(self):
        """A 908 x 524 raster yields 240 lines; the rest of the dump area is black."""
        raster = np.full((524, 908, 3), 200, np.uint8)
        crop = crop_vga_raster(raster, 908, 524)
        self.assertEqual(crop.shape, (285, 716, 3))
        self.assertTrue((crop[:240] == 200).all())
        self.assertTrue((crop[240:] == 0).all())

    def test_background_rows(self):
        bg = generate_ntsc_background()
        self.assertEqual(bg.shape, (285, 716, 3))
        # texture y 263 (row 237), x 196..199: 0x22; x 200..207: 0x44
        self.assertEqual(bg[237, 0, 0], 0x22)
        self.assertEqual(bg[237, 4, 0], 0x44)


if __name__ == "__main__":
    unittest.main()
