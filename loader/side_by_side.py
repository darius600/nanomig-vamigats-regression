#!/usr/bin/env python3
"""Side-by-side images for visual review: NanoMig | vAmigaTS reference | differences.

    python3 -m loader.side_by_side <capture> [<capture> ...] --out DIR

A capture is a <variant>.frame.vga or <variant>.frame.vga.gz written by the
harness, in a run directory or in the results repository. The variant's
.retrosh and reference .raw are found through the result file next to it, or
else by name in external/vAmigaTS. The NanoMig panel shows the real captured pixels (cropped
and de-scandoubled like the comparison does, no checkerboard). In the
difference panel the reference is dimmed, pixels outside the compared area
(the cutout) are tinted blue and differing pixels are red, compared at
4 bits per channel exactly like loader.compare.
"""
import argparse
import gzip
import json
from pathlib import Path

import cv2
import numpy as np

from loader.capture import CROP_TABLE, NTSC_GEOMETRIES, NotCalibratedError, crop_vga_raster
from loader.compare import compared_area
from loader.mask import build_mask
from loader.retrosh import parse_retrosh

ROOT = Path(__file__).resolve().parent.parent
VAMIGATS = ROOT / "external" / "vAmigaTS"
H, W, X0, Y0 = 285, 716, 196, 26
_written = {}   # (out_dir, lower-case stem) -> .retrosh rendered under that stem in this process
LABEL_H, GAP = 24, 8


def read_vga(path):
    data = gzip.open(path, "rb").read() if path.suffix == ".gz" else path.read_bytes()
    w, h = np.frombuffer(data[:8], np.uint32)
    return np.frombuffer(data[8:8 + int(w) * int(h) * 3], np.uint8).reshape(int(h), int(w), 3)


def q4(img):
    return np.minimum(15, np.round(img.astype(np.float32) / 16.0)).astype(np.int16)


def panel(img, label):
    out = np.zeros((LABEL_H + H, W, 3), np.uint8)
    out[LABEL_H:] = img
    cv2.putText(out, label, (4, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
    return out


def render(capture, out_dir, old_dir=None, save_original=False):
    variant = capture.name.split(".frame.vga")[0]
    result = capture.parent / "result.json"   # a run's layout
    if not result.is_file():
        result = capture.parent / f"{variant}.result.json"   # the results repository's
    if result.is_file():
        # names like timing1 exist in several groups - use the run's own path
        data = json.loads(result.read_text())
        retrosh = VAMIGATS / data["retrosh_path"]
        # the variant is the script's screenshot name, which can differ from the
        # script's file name (timing11l3.retrosh saves timing13l3)
        variant = data.get("variant") or retrosh.stem
    else:
        hits = list(VAMIGATS.rglob(f"{variant}.retrosh"))
        if len(hits) != 1:
            return f"{variant}: {len(hits)} .retrosh files found, skipped"
        retrosh = hits[0]
        variant = retrosh.stem
    if not retrosh.with_suffix(".raw").is_file():
        return f"{variant}: no reference .raw in vAmigaTS, skipped"
    ref = np.fromfile(retrosh.with_suffix(".raw"), np.uint8).reshape(H, W, 3)
    stem = image_stem(out_dir, retrosh, variant)
    raster = read_vga(capture)
    geom = (raster.shape[1], raster.shape[0])
    try:
        nano = crop_vga_raster(raster, *geom)
    except NotCalibratedError:
        return f"{variant}: raster {geom[0]}x{geom[1]} not calibrated, skipped"

    # the same compared area as loader.compare (cutout, NTSC rows)
    area, _ = compared_area(build_mask(parse_retrosh(retrosh).cutout), ref, geom in NTSC_GEOMETRIES)
    diff_panel, verdict = differences(nano, ref, area, "differences")
    sep = np.full((LABEL_H + H, GAP, 3), 64, np.uint8)
    top = [panel(nano, f"NanoMig  {variant}"), panel(ref, "vAmigaTS reference")]

    old = old_screenshot(variant, old_dir, retrosh) if old_dir else None
    out_dir.mkdir(parents=True, exist_ok=True)
    if old is None:
        img = np.hstack([top[0], sep, top[1], sep, diff_panel])
        cv2.imwrite(str(out_dir / f"{stem}.png"), cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
        return f"{stem}: {verdict}"

    old_path, old_img = old
    if save_original:
        (out_dir / "original").mkdir(exist_ok=True)
        (out_dir / "original" / old_path.name).write_bytes(old_path.read_bytes())
    old_ref, old_verdict = differences(old_img, ref, area, "old vs reference")
    _, new_verdict = differences(nano, ref, area, "new vs reference")
    old_new, change = differences(nano, old_img, area, "new vs old NanoMig")
    row1 = np.hstack([panel(old_img, "old NanoMig (Results repo, compared by hand)"), sep, top[0], sep, top[1]])
    row2 = np.hstack([old_ref, sep, diff_panel, sep, old_new])
    img = np.vstack([row1, np.full((GAP, row1.shape[1], 3), 64, np.uint8), row2])
    cv2.imwrite(str(out_dir / f"{stem}.png"), cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
    return f"{stem}: new {new_verdict}, old {old_verdict}, new vs old {change}"


def image_stem(out_dir, retrosh, name=None):
    """A file name in out_dir, based on name (default: the script's file name),
    that no other test has taken in this process.

    Two tests may share a variant name (overscan2) or differ only in case
    (CPU/IPL/.../MOVE1b_68000, CPU/Timing/.../MOVE1B_68000), which NTFS treats
    as the same file. The later one gets the directories above its test
    directory in the name, as many as it takes to be unique.
    """
    base = name or retrosh.stem
    above = retrosh.relative_to(VAMIGATS).parent.parent.parts
    for n in range(len(above) + 1):
        stem = base if n == 0 else f"{base}__{'_'.join(above[-n:])}"
        if _written.setdefault((out_dir.resolve(), stem.lower()), retrosh) == retrosh:
            return stem
    raise ValueError(f"no unique image name for {retrosh}")


def differences(img, ref, area, title):
    """Dimmed ref, blue outside the compared area, red where img differs (4 bit)."""
    differ = np.any(q4(img) != q4(ref), axis=-1) & area
    out = (ref.astype(np.float32) * 0.35).astype(np.uint8)
    out[~area] = (out[~area] * 0.5 + np.array([0, 0, 110])).astype(np.uint8)
    out[differ] = (255, 0, 0)
    n = int(differ.sum())
    if n == 0:
        return panel(out, f"{title}: identical"), "identical"
    ys, xs = np.nonzero(differ)
    if n <= 200:
        # single pixels are invisible at 1:1 - ring each one
        for y, x in zip(ys, xs):
            cv2.circle(out, (int(x), int(y)), 6, (255, 0, 0), 1)
    verdict = (f"{n} px differ (x {xs.min() + X0}-{xs.max() + X0}, "
               f"y {ys.min() + Y0}-{ys.max() + Y0})")
    return panel(out, f"{title}: {verdict}"), verdict


def old_screenshot(variant, old_dir, retrosh):
    """The NanoMig screenshot in the Results repo, cropped like the comparison.

    The Results repo keeps a test's screenshots one level above its test
    directory (Agnus/Copper/cross/cross1_ocs.png); search there first.
    """
    near = old_dir / retrosh.relative_to(VAMIGATS).parent.parent
    hits = [p for p in near.glob("*.png") if p.stem.lower() == variant.lower()]
    if not hits:
        hits = [p for p in old_dir.rglob("*.png") if p.stem.lower() == variant.lower()]
    if len(hits) != 1:
        return None
    raster = cv2.cvtColor(cv2.imread(str(hits[0])), cv2.COLOR_BGR2RGB)
    geom = (raster.shape[1], raster.shape[0])
    if geom not in CROP_TABLE:
        return None
    cx, cy = CROP_TABLE[geom]
    return hits[0], raster[cy:cy + 2 * H, cx:cx + W][1::2][:H]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("captures", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--old", type=Path,
                    help="Results repo with older NanoMig screenshots; adds them as extra panels")
    args = ap.parse_args()
    for c in args.captures:
        try:
            print(render(c, args.out, args.old))
        except Exception as e:  # one bad capture must not stop the others
            print(f"{c.name}: ERROR {type(e).__name__}: {e}, skipped")


if __name__ == "__main__":
    main()
