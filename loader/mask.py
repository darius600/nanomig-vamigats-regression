"""Mask and checkerboard generation for vAmiga comparison."""

from typing import Optional, Tuple
import numpy as np

# Texture coordinates of the dump area
ORIGIN_X = 196
ORIGIN_Y = 26
REF_WIDTH = 716
REF_HEIGHT = 285


def get_texture_grids() -> Tuple[np.ndarray, np.ndarray]:
    """Returns 2D grids (tex_y, tex_x) of texture coordinates."""
    yy, xx = np.mgrid[0:REF_HEIGHT, 0:REF_WIDTH]
    return yy + ORIGIN_Y, xx + ORIGIN_X


def generate_checkerboard() -> np.ndarray:
    """Generates the 716x285x3 RGB checkerboard background.
    
    Outside cutout is vAmiga's checkerboard:
    0x22 if ((y >> 3) & 1) == ((x >> 3) & 1) else 0x44
    for all three channels in texture coordinates.
    """
    tex_y, tex_x = get_texture_grids()
    pattern = np.where(
        ((tex_y >> 3) & 1) == ((tex_x >> 3) & 1),
        0x22,
        0x44,
    ).astype(np.uint8)
    return np.repeat(pattern[:, :, np.newaxis], 3, axis=2)


# An NTSC frame reaches texture rows 26..262, i.e. the first 237 rows of the dump area.
# Below it vAmiga writes a checkerboard with fields 4 rows high (8 elsewhere); fitted
# exactly to the 11 NTSC references at the pinned vAmigaTS commit.
NTSC_BEAM_ROWS = 237


def generate_ntsc_background() -> np.ndarray:
    """vAmiga's background for an NTSC frame: the usual checkerboard in the rows the
    beam reaches, 4-row fields below them."""
    tex_y, tex_x = get_texture_grids()
    below = np.where(
        (((tex_y + 4) >> 2) & 1) == ((tex_x >> 3) & 1),
        0x22,
        0x44,
    ).astype(np.uint8)
    board = generate_checkerboard()
    board[NTSC_BEAM_ROWS:] = np.repeat(below[NTSC_BEAM_ROWS:, :, np.newaxis], 3, axis=2)
    return board


def is_ntsc_frame(ref_img: np.ndarray) -> bool:
    """Whether a reference shows an NTSC frame: everything below the rows an NTSC
    beam reaches is vAmiga's NTSC background."""
    return bool(np.array_equal(ref_img[NTSC_BEAM_ROWS:], generate_ntsc_background()[NTSC_BEAM_ROWS:]))


def build_mask(cutout: Optional[Tuple[int, int, int, int]] = None) -> np.ndarray:
    """Builds a boolean mask (shape 285x716) for pixels to compare.
    
    True inside the cutout / dump area, False outside.
    Cutout tuple: (x1, y1, x2, y2) in texture coordinates (x1<=x<x2, y1<=y<y2).
    """
    if cutout is None:
        return np.ones((REF_HEIGHT, REF_WIDTH), dtype=bool)

    x1, y1, x2, y2 = cutout
    tex_y, tex_x = get_texture_grids()
    return (tex_x >= x1) & (tex_x < x2) & (tex_y >= y1) & (tex_y < y2)


def texture_to_pixel(tx: int, ty: int) -> Tuple[int, int]:
    """Converts texture (tx, ty) to image (row, col)."""
    return ty - ORIGIN_Y, tx - ORIGIN_X


def pixel_to_texture(row: int, col: int) -> Tuple[int, int]:
    """Converts image (row, col) to texture (tx, ty)."""
    return ORIGIN_X + col, ORIGIN_Y + row
