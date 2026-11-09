#!/usr/bin/env python3
"""Read .cube colour lookup tables and apply them to images.

A 3D LUT is a cube of colours. Split each of red, green and blue into N steps
and you get N*N*N grid points; the file gives the output colour at each one.
A real pixel almost never lands exactly on a grid point, so its colour is mixed
from the eight grid points around it. That mixing is trilinear interpolation.

OpenCV's cv2.LUT only handles 1D 256-entry tables, so the 3D case is done here
with numpy.
"""

import re
from dataclasses import dataclass

import numpy as np

# A 33x33x33 cube is the common size; 64 is the practical maximum in the wild.
MAX_SIZE = 256

# Pixels are processed in blocks of this many. A full-resolution photo expanded
# to its eight surrounding grid points at once would need gigabytes: a 24MP
# image is 24e6 px * 8 corners * 3 channels * 4 bytes = about 2.3 GB.
CHUNK_PIXELS = 1_000_000


class CubeError(Exception):
    """The .cube file could not be read."""


@dataclass(frozen=True)
class Lut:
    """A parsed 3D LUT.

    table is indexed [r, g, b] and holds RGB floats, normally in 0..1.
    """

    size: int
    table: np.ndarray
    domain_min: np.ndarray
    domain_max: np.ndarray
    title: str = ""


def load_cube(path):
    """Parse a .cube file into a Lut.

    Handles the keywords the format defines (TITLE, LUT_3D_SIZE, DOMAIN_MIN,
    DOMAIN_MAX), '#' comments, blank lines and any run of spaces or tabs.
    """
    size = None
    title = ""
    domain_min = [0.0, 0.0, 0.0]
    domain_max = [1.0, 1.0, 1.0]
    rows = []

    try:
        text = open(path, "r", encoding="utf-8", errors="replace").read()
    except OSError as exc:
        raise CubeError(f"cannot read {path}: {exc}") from exc

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        upper = line.upper()

        if upper.startswith("TITLE"):
            match = re.search(r'"(.*)"', line)
            title = match.group(1) if match else line[5:].strip()
        elif upper.startswith("LUT_3D_SIZE"):
            size = _int_after(line, "LUT_3D_SIZE")
        elif upper.startswith("LUT_1D_SIZE"):
            # shortcut: 1D .cube files are rejected rather than supported.
            # Nearly every grading LUT is 3D. Add per-channel interpolation if
            # a real 1D file turns up.
            raise CubeError("this is a 1D .cube; only 3D LUTs are supported")
        elif upper.startswith("DOMAIN_MIN"):
            domain_min = _floats_after(line, "DOMAIN_MIN", 3)
        elif upper.startswith("DOMAIN_MAX"):
            domain_max = _floats_after(line, "DOMAIN_MAX", 3)
        else:
            parts = line.split()
            if len(parts) != 3:
                raise CubeError(f"expected 3 numbers per row, got: {line!r}")
            try:
                rows.append([float(p) for p in parts])
            except ValueError as exc:
                raise CubeError(f"bad number in row: {line!r}") from exc

    if size is None:
        raise CubeError("no LUT_3D_SIZE line; is this a .cube file?")
    if not 2 <= size <= MAX_SIZE:
        raise CubeError(f"LUT_3D_SIZE {size} out of range 2..{MAX_SIZE}")
    if len(rows) != size ** 3:
        raise CubeError(f"expected {size ** 3} rows for size {size}, found {len(rows)}")

    lo = np.asarray(domain_min, dtype=np.float32)
    hi = np.asarray(domain_max, dtype=np.float32)
    if np.any(hi <= lo):
        raise CubeError(f"DOMAIN_MAX {hi.tolist()} must exceed DOMAIN_MIN {lo.tolist()}")

    # In a .cube the red index changes fastest, then green, then blue. Read in
    # file order that is [b][g][r]; move it to [r][g][b] so lookups read naturally.
    table = np.asarray(rows, dtype=np.float32).reshape(size, size, size, 3)
    table = np.transpose(table, (2, 1, 0, 3))
    return Lut(size=size, table=np.ascontiguousarray(table),
               domain_min=lo, domain_max=hi, title=title)


def _int_after(line, keyword):
    rest = line[len(keyword):].strip()
    try:
        return int(float(rest.split()[0]))
    except (IndexError, ValueError) as exc:
        raise CubeError(f"bad {keyword} line: {line!r}") from exc


def _floats_after(line, keyword, count):
    parts = line[len(keyword):].split()
    if len(parts) != count:
        raise CubeError(f"{keyword} needs {count} numbers: {line!r}")
    try:
        return [float(p) for p in parts]
    except ValueError as exc:
        raise CubeError(f"bad number in {keyword}: {line!r}") from exc


def apply_lut(image_bgr, lut):
    """Apply `lut` to a BGR uint8 image and return a new BGR uint8 image.

    OpenCV gives images as BGR, while a .cube is RGB, so channels are swapped
    on the way in and back on the way out.
    """
    if image_bgr.dtype != np.uint8:
        raise CubeError(f"expected an 8-bit image, got {image_bgr.dtype}")

    height, width = image_bgr.shape[:2]
    rgb = image_bgr[:, :, ::-1].astype(np.float32) / 255.0
    flat = rgb.reshape(-1, 3)
    out = np.empty_like(flat)

    for start in range(0, flat.shape[0], CHUNK_PIXELS):
        chunk = flat[start:start + CHUNK_PIXELS]
        out[start:start + CHUNK_PIXELS] = _interpolate(chunk, lut)

    out = (np.clip(out, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    return out.reshape(height, width, 3)[:, :, ::-1]


def _interpolate(pixels, lut):
    """Trilinear lookup for an (n, 3) block of RGB floats."""
    size = lut.size
    scaled = (pixels - lut.domain_min) / (lut.domain_max - lut.domain_min)
    coords = np.clip(scaled, 0.0, 1.0) * (size - 1)

    low = np.floor(coords).astype(np.intp)
    low = np.minimum(low, size - 2) if size > 1 else low
    high = np.minimum(low + 1, size - 1)
    frac = coords - low

    r0, g0, b0 = low[:, 0], low[:, 1], low[:, 2]
    r1, g1, b1 = high[:, 0], high[:, 1], high[:, 2]
    fr = frac[:, 0:1]
    fg = frac[:, 1:2]
    fb = frac[:, 2:3]

    t = lut.table
    # Mix along red, then green, then blue.
    c00 = t[r0, g0, b0] * (1 - fr) + t[r1, g0, b0] * fr
    c01 = t[r0, g0, b1] * (1 - fr) + t[r1, g0, b1] * fr
    c10 = t[r0, g1, b0] * (1 - fr) + t[r1, g1, b0] * fr
    c11 = t[r0, g1, b1] * (1 - fr) + t[r1, g1, b1] * fr

    c0 = c00 * (1 - fg) + c10 * fg
    c1 = c01 * (1 - fg) + c11 * fg
    return c0 * (1 - fb) + c1 * fb
