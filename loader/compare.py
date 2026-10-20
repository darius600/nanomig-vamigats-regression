#!/usr/bin/env python3
"""Image comparison and .raw reconstruction for NanoMig regression tests."""

import json
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Tuple
import cv2
import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from loader.mask import (
    NTSC_BEAM_ROWS,
    REF_HEIGHT,
    REF_WIDTH,
    generate_checkerboard,
    generate_ntsc_background,
    is_ntsc_frame,
)

COLOR_TABLE_PATH = Path(__file__).resolve().parent / "color_table.json"


def quantize_4bit(img: np.ndarray) -> np.ndarray:
    """Quantizes an image to 4 bits per channel: min(15, round(v / 16))."""
    return np.minimum(15, np.round(img.astype(float) / 16.0)).astype(np.uint8)


def load_color_table(
    table_path: Optional[Path | str] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    """Loads the 12-bit -> RGB lookup table.

    color_table.json holds, for each of the 751 Amiga colours (4 bits per
    channel) that occur in the vAmigaTS references at the pinned commit, the RGB
    bytes vAmiga wrote for it. It was collected from the references themselves;
    tests/test_selftests.py checks that every colour has exactly one RGB value
    (the checkerboard greys aside) and that every PAL reference is rebuilt from
    its own 4-bit image byte for
    byte. A colour missing from the table is reported as raw_status
    "missing colours" (the frame is still compared at 4 bits).

    Returns (lut, known):
      lut: shape (16, 16, 16, 3) uint8 RGB
      known: shape (16, 16, 16) bool
    """
    path = Path(table_path) if table_path else COLOR_TABLE_PATH
    if not path.is_file():
        raise FileNotFoundError(f"Color table not found at {path}")

    data = json.loads(path.read_text(encoding="utf-8"))
    lut = np.zeros((16, 16, 16, 3), dtype=np.uint8)
    known = np.zeros((16, 16, 16), dtype=bool)

    for k_str, rgb in data.items():
        r, g, b = map(int, k_str.split(","))
        lut[r, g, b] = rgb
        known[r, g, b] = True

    return lut, known


def compared_area(
    mask: np.ndarray, ref_img: np.ndarray, ntsc_capture: bool
) -> Tuple[np.ndarray, np.ndarray]:
    """The pixels to compare and vAmiga's background outside them.

    mask is the script's cutout. When both the reference and NanoMig's raster are
    NTSC frames, the rows below the NTSC beam are not compared: neither side draws
    there. When only one of them is NTSC, every row is compared, so the missing or
    extra lines count as differences.
    """
    if ntsc_capture and is_ntsc_frame(ref_img):
        area = mask.copy()
        area[NTSC_BEAM_ROWS:] = False
        return area, generate_ntsc_background()
    return mask, generate_checkerboard()


def build_raw(
    image_4b: np.ndarray,
    mask: np.ndarray,
    lut: Optional[np.ndarray] = None,
    known: Optional[np.ndarray] = None,
    background: Optional[np.ndarray] = None,
) -> Tuple[np.ndarray, bool, List[Tuple[int, int, int]]]:
    """Builds a vAmiga format .raw image (716x285x3 RGB).

    Inside mask: maps 12-bit color to RGB via color table.
    Outside mask: vAmiga's background (default: its checkerboard).

    Returns:
      (raw_img, all_colors_known, missing_colors_list)
    """
    if lut is None or known is None:
        lut, known = load_color_table()

    checkerboard = generate_checkerboard() if background is None else background
    raw_img = np.empty((REF_HEIGHT, REF_WIDTH, 3), dtype=np.uint8)
    raw_img[~mask] = checkerboard[~mask]

    # Check for missing colors inside the mask
    r_idx = image_4b[..., 0]
    g_idx = image_4b[..., 1]
    b_idx = image_4b[..., 2]

    is_known = known[r_idx, g_idx, b_idx]
    missing_mask = (~is_known) & mask

    missing_colors: List[Tuple[int, int, int]] = []
    if np.any(missing_mask):
        missing_vals = image_4b[missing_mask]
        unq = np.unique(missing_vals, axis=0)
        missing_colors = [tuple(c.tolist()) for c in unq]

    # Map inside mask
    raw_img[mask] = lut[r_idx[mask], g_idx[mask], b_idx[mask]]

    return raw_img, len(missing_colors) == 0, missing_colors


def write_diff_image(
    diff_path: Path | str,
    captured: np.ndarray,
    ref: np.ndarray,
    area: np.ndarray,
    variant: str,
) -> None:
    """Writes the image of a FAIL as the published ones look: NanoMig | vAmigaTS
    reference | differences, with titles, the vAmiga coordinates of the differing
    pixels and rings around single pixels (drawn by loader.side_by_side)."""
    # side_by_side imports this module, so import it here
    from loader.side_by_side import GAP, H, LABEL_H, differences, panel

    diff_panel, _ = differences(captured, ref, area, "differences")
    sep = np.full((LABEL_H + H, GAP, 3), 64, np.uint8)
    img = np.hstack([panel(captured, f"NanoMig  {variant}"), sep, panel(ref, "vAmigaTS reference"), sep, diff_panel])
    diff_path = Path(diff_path)
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(diff_path), cv2.cvtColor(img, cv2.COLOR_RGB2BGR))


def compare_frames(
    captured_img: np.ndarray,
    ref_raw_path: Path | str,
    mask: np.ndarray,
    variant: str,
    out_dir: Optional[Path | str] = None,
    ntsc_capture: bool = False,
) -> Dict[str, Any]:
    """Compares a 285x716 captured frame against the reference .raw file.

    mask is the script's cutout; ntsc_capture says whether NanoMig's raster was an
    NTSC one (see compared_area).
    Returns a result dict with comparison outcome and details.
    """
    ref_path = Path(ref_raw_path)
    if not ref_path.is_file():
        return {
            "class": "ERROR",
            "detail": f"Reference file {ref_path} not found",
            "differing_pixels": -1,
            "raw_byte_identical": False,
        }

    ref_raw = np.fromfile(str(ref_path), dtype=np.uint8)
    expected_size = REF_HEIGHT * REF_WIDTH * 3
    if len(ref_raw) < expected_size:
        return {
            "class": "ERROR",
            "detail": f"Reference file {ref_path} size {len(ref_raw)} < {expected_size}",
            "differing_pixels": -1,
            "raw_byte_identical": False,
        }
    ref_img = ref_raw[:expected_size].reshape(REF_HEIGHT, REF_WIDTH, 3)
    mask, background = compared_area(mask, ref_img, ntsc_capture)

    # 4-bit comparison inside mask only
    cap_4b = quantize_4bit(captured_img)
    ref_4b = quantize_4bit(ref_img)

    diff_mask = np.any(cap_4b != ref_4b, axis=-1) & mask
    diff_count = int(np.sum(diff_mask))

    out_path = Path(out_dir) if out_dir else None
    if out_path:
        out_path.mkdir(parents=True, exist_ok=True)

    # Determine PASS / FAIL
    if diff_count == 0:
        res_class = "PASS"
        detail = "identical"
    else:
        res_class = "FAIL"
        detail = f"{diff_count} differing pixels"
        if out_path:
            diff_png_path = out_path / f"{variant}_diff.png"
            write_diff_image(diff_png_path, captured_img, ref_img, mask, variant)

    # Reconstruct .raw in vAmiga format
    lut, known = load_color_table()
    gen_raw, colors_ok, missing_colors = build_raw(cap_4b, mask, lut, known, background)

    raw_identical = False
    raw_status = ""
    if not colors_ok:
        raw_status = f"missing colours: {missing_colors}"
    else:
        raw_identical = bool(gen_raw.tobytes() == ref_raw[:expected_size].tobytes())
        raw_status = "byte-identical" if raw_identical else "bytes differ"

    diff_image_path = None
    if out_path:
        gen_raw_path = out_path / f"{variant}.raw"
        gen_raw.tofile(str(gen_raw_path))
        if diff_count > 0:
            diff_image_path = str(out_path / f"{variant}_diff.png")

    return {
        "class": res_class,
        "detail": detail,
        "differing_pixels": diff_count,
        "raw_byte_identical": raw_identical,
        "raw_status": raw_status,
        "missing_colors": missing_colors,
        "diff_image": diff_image_path,
    }
