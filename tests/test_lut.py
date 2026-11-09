#!/usr/bin/env python3
"""Checks for the .cube parser, the colour maths, and the URL traversal guard.

Run with:  .venv/bin/python tests/test_lut.py
No test framework on purpose - plain asserts, no extra dependency.
"""

import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lut                      # noqa: E402
from storage import LocalStorage  # noqa: E402


def write_cube(body, folder):
    path = Path(folder) / "t.cube"
    path.write_text(body)
    return path


def cube_text(size, fn, header=""):
    """Build a .cube. Red varies fastest, as the format requires."""
    lines = [f"LUT_3D_SIZE {size}"]
    if header:
        lines.insert(0, header)
    for b in range(size):
        for g in range(size):
            for r in range(size):
                out = fn(r / (size - 1), g / (size - 1), b / (size - 1))
                lines.append("%.6f %.6f %.6f" % out)
    return "\n".join(lines) + "\n"


def test_identity_lut_changes_nothing():
    """The strongest check: a LUT that maps every colour to itself."""
    with tempfile.TemporaryDirectory() as d:
        path = write_cube(cube_text(33, lambda r, g, b: (r, g, b)), d)
        table = lut.load_cube(path)
        rng = np.random.default_rng(0)
        image = rng.integers(0, 256, (40, 60, 3), dtype=np.uint8)
        out = lut.apply_lut(image, table)
        worst = int(np.max(np.abs(out.astype(int) - image.astype(int))))
        assert worst <= 1, f"identity LUT shifted a channel by {worst}"


def test_invert_lut():
    with tempfile.TemporaryDirectory() as d:
        path = write_cube(cube_text(17, lambda r, g, b: (1 - r, 1 - g, 1 - b)), d)
        table = lut.load_cube(path)
        image = np.full((4, 4, 3), 200, dtype=np.uint8)
        out = lut.apply_lut(image, table)
        assert abs(int(out[0, 0, 0]) - 55) <= 1, out[0, 0, 0]


def test_channel_order_is_not_swapped():
    """Catches an R/B mix-up between BGR images and RGB cube files.

    The LUT keeps red and discards green and blue. Pure red must survive;
    pure blue must go black. A swap would do the opposite.
    """
    with tempfile.TemporaryDirectory() as d:
        path = write_cube(cube_text(9, lambda r, g, b: (r, 0.0, 0.0)), d)
        table = lut.load_cube(path)
        red_bgr = np.array([[[0, 0, 255]]], dtype=np.uint8)
        blue_bgr = np.array([[[255, 0, 0]]], dtype=np.uint8)
        assert lut.apply_lut(red_bgr, table)[0, 0, 2] > 250, "red was lost"
        assert lut.apply_lut(blue_bgr, table).max() < 5, "blue leaked through"


def test_parser_handles_comments_title_and_whitespace():
    with tempfile.TemporaryDirectory() as d:
        body = cube_text(2, lambda r, g, b: (r, g, b), header='TITLE "My Look"')
        body = "# a comment\n\n" + body.replace("LUT_3D_SIZE 2", "LUT_3D_SIZE  2  # size")
        table = lut.load_cube(write_cube(body, d))
        assert table.size == 2
        assert table.title == "My Look", table.title


def test_domain_is_respected():
    """DOMAIN_MAX 0.5 means input 0.5 already sits at the top of the cube."""
    with tempfile.TemporaryDirectory() as d:
        body = cube_text(2, lambda r, g, b: (r, g, b))
        body = "DOMAIN_MIN 0.0 0.0 0.0\nDOMAIN_MAX 0.5 0.5 0.5\n" + body
        table = lut.load_cube(write_cube(body, d))
        mid = np.full((1, 1, 3), 128, dtype=np.uint8)   # ~0.5
        assert lut.apply_lut(mid, table).min() > 250, "domain was ignored"


def test_bad_files_are_rejected():
    cases = {
        "no size": "0.0 0.0 0.0\n",
        "wrong row count": "LUT_3D_SIZE 2\n0.0 0.0 0.0\n",
        "1d lut": "LUT_1D_SIZE 16\n",
        "bad number": "LUT_3D_SIZE 2\n" + "x y z\n" * 8,
        "inverted domain": "DOMAIN_MIN 1 1 1\nDOMAIN_MAX 0 0 0\n" + cube_text(2, lambda r, g, b: (r, g, b)),
    }
    with tempfile.TemporaryDirectory() as d:
        for name, body in cases.items():
            try:
                lut.load_cube(write_cube(body, d))
            except lut.CubeError:
                continue
            raise AssertionError(f"{name!r} should have been rejected")


def test_thumbnail_name_cannot_escape_the_photo_folder():
    """The dashboard passes a filename from a URL straight to storage."""
    store = LocalStorage()
    for attack in ("../config.py", "../../etc/passwd", "/etc/passwd", "..%2Fconfig.py"):
        assert store.resolve(attack) is None, f"{attack} resolved"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {name}: {exc}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
