"""VGA frame capture reading and cropping for NanoMig regression runs."""

import gzip
from pathlib import Path
from typing import Tuple
import numpy as np

# Where vAmiga's 716 x 285 dump area lies in NanoMig's scan-doubled VGA frame:
# raster geometry (width, height) -> (x, y) of its top left corner. The offset was
# found by searching for the position at which the captures of four OCS tests
# (Agnus/Copper/cross/cross1, Agnus/Copper/Wait/copwait4, Agnus/DDF/DDF/oldhwstop1,
# Agnus/DIW/DIWV/diwv1) match their references pixel for pixel; a periodic picture
# alone is not enough, cross1 also matches 8 colour clocks off. 908 x 626 is a PAL
# long frame, 908 x 624 a short frame (tests that clear LOF, e.g.
# Agnus/Registers/VPOS/lof2), which starts one VGA line lower.
# NTSC uses the same offsets: 908 x 524 (262 lines, the ECS Agnus) is the unique best
# offset in all five Agnus/Misc/NTSC _ecs tests; 908 x 526 (263 lines, the OCS Agnus)
# follows the PAL long frame and is not confirmed by a picture, because every NTSC
# _ocs reference at the pinned commit is black where the beam reaches. Every other
# geometry (interlace, ERSY) is NOT_CALIBRATED until it has been checked.
CROP_TABLE = {
    (908, 626): (78, 42),
    (908, 624): (78, 43),
    (908, 526): (78, 42),
    (908, 524): (78, 43),
}
NTSC_GEOMETRIES = {(908, 526), (908, 524)}

REF_WIDTH = 716
REF_HEIGHT = 285


class NotCalibratedError(Exception):
    """Raised when a VGA frame has an uncalibrated geometry."""

    def __init__(self, width: int, height: int):
        super().__init__(f"{width}x{height}")
        self.width = width
        self.height = height
        self.geometry = f"{width}x{height}"


def read_vga_file(path: Path | str) -> Tuple[int, int, np.ndarray]:
    """Reads a .frame.vga file.
    
    Header: 2 x uint32 (width, height).
    Payload: width * height * 3 bytes (RGB).
    Returns (width, height, raster) with raster shape (height, width, 3).
    """
    path = Path(path)
    header = np.fromfile(str(path), dtype=np.uint32, count=2)
    if len(header) < 2:
        raise ValueError(f"File {path} too short for VGA header")
    width, height = int(header[0]), int(header[1])
    payload = np.fromfile(str(path), dtype=np.uint8, offset=8)
    expected_bytes = width * height * 3
    if len(payload) < expected_bytes:
        raise ValueError(
            f"VGA payload in {path} has {len(payload)} bytes, expected {expected_bytes}"
        )
    raster = payload[:expected_bytes].reshape(height, width, 3)
    return width, height, raster


def crop_vga_raster(raster: np.ndarray, width: int, height: int) -> np.ndarray:
    """Crops a raster to 285x716 active display area.
    
    Uses raster[cy:cy+570, cx:cx+716][1::2]. Rows an NTSC raster does not reach are
    black: nothing is drawn there.
    Raises NotCalibratedError if (width, height) is not in CROP_TABLE.
    """
    geom = (width, height)
    if geom not in CROP_TABLE:
        raise NotCalibratedError(width, height)
    cx, cy = CROP_TABLE[geom]
    cropped = raster[cy : cy + 570, cx : cx + REF_WIDTH][1::2]
    if geom in NTSC_GEOMETRIES and cropped.shape[0] < REF_HEIGHT:
        pad = np.zeros((REF_HEIGHT - cropped.shape[0], REF_WIDTH, 3), dtype=cropped.dtype)
        cropped = np.concatenate([cropped, pad])
    if cropped.shape != (REF_HEIGHT, REF_WIDTH, 3):
        raise ValueError(
            f"Cropped image shape {cropped.shape} does not match ({REF_HEIGHT}, {REF_WIDTH}, 3)"
        )
    return cropped


def capture_from_vga(path: Path | str) -> np.ndarray:
    """Reads and crops a .frame.vga file directly."""
    return read_capture(path)[0]


def read_capture(path: Path | str) -> Tuple[np.ndarray, bool]:
    """Reads and crops a .frame.vga (or .frame.vga.gz) file; returns the cropped
    frame and whether the raster is an NTSC one."""
    path = Path(path)
    if path.suffix == ".gz":
        data = gzip.open(path, "rb").read()
        width, height = (int(v) for v in np.frombuffer(data[:8], np.uint32))
        raster = np.frombuffer(data[8:8 + width * height * 3], np.uint8).reshape(height, width, 3)
    else:
        width, height, raster = read_vga_file(path)
    return crop_vga_raster(raster, width, height), (width, height) in NTSC_GEOMETRIES
