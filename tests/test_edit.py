#!/usr/bin/env python3
"""Checks for the adjustment engine. Run: .venv/bin/python tests/test_edit.py"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import edit  # noqa: E402

GREY = np.full((8, 8, 3), 128, dtype=np.uint8)


def patch(b, g, r):
    img = np.zeros((8, 8, 3), dtype=np.uint8)
    img[:, :, 0], img[:, :, 1], img[:, :, 2] = b, g, r
    return img


def test_defaults_change_nothing():
    out = edit.apply_adjustments(GREY, edit.Adjustments())
    assert np.array_equal(out, GREY), "default adjustments altered the image"


def test_srgb_round_trip():
    x = np.linspace(0, 1, 256, dtype=np.float32)
    back = edit.linear_to_srgb(edit.srgb_to_linear(x))
    assert np.max(np.abs(back - x)) < 1e-4, "sRGB conversion does not round-trip"


def test_exposure_one_stop_doubles_the_light():
    """+1 EV must double linear light, not double the stored number."""
    out = edit.apply_adjustments(GREY, edit.Adjustments(exposure=1.0))
    before = edit.srgb_to_linear(np.float32(128 / 255))
    after = edit.srgb_to_linear(np.float32(out[0, 0, 0] / 255))
    assert abs(after / before - 2.0) < 0.05, f"ratio was {after / before:.3f}"


def test_exposure_direction():
    up = edit.apply_adjustments(GREY, edit.Adjustments(exposure=1.0))
    down = edit.apply_adjustments(GREY, edit.Adjustments(exposure=-1.0))
    assert int(up[0, 0, 0]) > 128 > int(down[0, 0, 0])


def test_warm_adds_red_and_removes_blue():
    out = edit.apply_adjustments(GREY, edit.Adjustments(temperature=50))
    b, _, r = (int(v) for v in out[0, 0])
    assert r > 128 and b < 128, f"warm gave b={b} r={r}"
    cool = edit.apply_adjustments(GREY, edit.Adjustments(temperature=-50))
    assert int(cool[0, 0, 2]) < 128 < int(cool[0, 0, 0]), "cool did not go blue"


def test_saturation_minimum_is_greyscale():
    out = edit.apply_adjustments(patch(40, 120, 200), edit.Adjustments(saturation=-100))
    b, g, r = (int(v) for v in out[0, 0])
    assert max(b, g, r) - min(b, g, r) <= 1, f"not grey: {b},{g},{r}"


def test_vibrance_spares_saturated_colours():
    """A dull colour should gain more than an already-vivid one."""
    dull, vivid = patch(120, 130, 145), patch(0, 0, 255)
    spread = lambda im: int(im[0, 0].max()) - int(im[0, 0].min())
    a = edit.Adjustments(vibrance=100)
    dull_gain = spread(edit.apply_adjustments(dull, a)) - spread(dull)
    vivid_gain = spread(edit.apply_adjustments(vivid, a)) - spread(vivid)
    assert dull_gain > vivid_gain, f"dull {dull_gain} vs vivid {vivid_gain}"


def test_contrast_spreads_values_around_mid_grey():
    dark, bright = patch(80, 80, 80), patch(180, 180, 180)
    a = edit.Adjustments(contrast=50)
    assert int(edit.apply_adjustments(dark, a)[0, 0, 0]) < 80
    assert int(edit.apply_adjustments(bright, a)[0, 0, 0]) > 180


def test_shadows_and_highlights_hit_their_own_ends():
    dark, bright = patch(30, 30, 30), patch(225, 225, 225)
    lift = edit.Adjustments(shadows=100)
    assert int(edit.apply_adjustments(dark, lift)[0, 0, 0]) > 30, "shadows did not lift"
    # ...and leave the bright end alone.
    assert abs(int(edit.apply_adjustments(bright, lift)[0, 0, 0]) - 225) <= 2
    pull = edit.Adjustments(highlights=-100)
    assert int(edit.apply_adjustments(bright, pull)[0, 0, 0]) < 225, "highlights did not recover"
    assert abs(int(edit.apply_adjustments(dark, pull)[0, 0, 0]) - 30) <= 2


def test_sharpen_raises_local_contrast_without_moving_flat_areas():
    img = np.full((32, 32, 3), 100, dtype=np.uint8)
    img[:, 16:] = 160                       # one hard edge
    out = edit.apply_adjustments(img, edit.Adjustments(sharpen=80))
    assert int(out[16, 0, 0]) == 100, "flat area moved"
    edge_before = int(img[16, 16, 0]) - int(img[16, 15, 0])
    edge_after = int(out[16, 16, 0]) - int(out[16, 15, 0])
    assert edge_after > edge_before, f"edge {edge_before} -> {edge_after}"


def test_from_dict_ignores_junk():
    a = edit.Adjustments.from_dict(
        {"exposure": "1.5", "nonsense": 9, "contrast": "abc", "sharpen": None})
    assert a.exposure == 1.5 and a.contrast == 0.0 and a.sharpen == 0.0


def test_lut_strength_blends():
    import lut as lut_module
    import tempfile
    size = 2
    rows = []
    for b in range(size):
        for g in range(size):
            for r in range(size):
                rows.append("0 0 0")          # a LUT that crushes everything to black
    body = f"LUT_3D_SIZE {size}\n" + "\n".join(rows) + "\n"
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "black.cube"
        path.write_text(body)
        table = lut_module.load_cube(path)
        full = edit.apply_adjustments(GREY, edit.Adjustments(lut_strength=100), table)
        half = edit.apply_adjustments(GREY, edit.Adjustments(lut_strength=50), table)
        none = edit.apply_adjustments(GREY, edit.Adjustments(lut_strength=0), table)
        assert int(full[0, 0, 0]) < 5, full[0, 0, 0]
        assert 55 < int(half[0, 0, 0]) < 75, half[0, 0, 0]
        assert int(none[0, 0, 0]) == 128, none[0, 0, 0]


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
