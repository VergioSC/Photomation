#!/usr/bin/env python3
"""Checks for the adjustment engine. Run: .venv/bin/python tests/test_edit.py"""

import sys
from pathlib import Path

import cv2
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
    # A realistic size on purpose: the sharpening radius is a share of the
    # frame, so on a 32-pixel test square it is correctly sub-pixel and does
    # nothing. See test_detail_sliders_look_the_same_at_any_resolution.
    img = np.full((400, 400, 3), 100, dtype=np.uint8)
    img[:, 200:] = 160                      # one hard edge
    out = edit.apply_adjustments(img, edit.Adjustments(sharpen=80))
    assert int(out[200, 0, 0]) == 100, "flat area moved"
    edge_before = int(img[200, 200, 0]) - int(img[200, 199, 0])
    edge_after = int(out[200, 200, 0]) - int(out[200, 199, 0])
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



def test_contrast_never_throws_tones_away():
    """The old linear contrast flattened everything under 10% into one black."""
    steps = np.array([0, 5, 13, 26, 128, 204, 242, 255], dtype=np.uint8)
    img = np.tile(steps.reshape(1, -1, 1), (2, 1, 3))
    out = edit.apply_adjustments(img, edit.Adjustments(contrast=40))[0, :, 0]
    assert len(set(int(v) for v in out)) == len(steps), f"tones merged: {list(out)}"
    assert int(out[0]) == 0 and int(out[-1]) == 255, "endpoints moved"


def test_contrast_is_monotonic():
    ramp = np.tile(np.arange(256, dtype=np.uint8).reshape(1, -1, 1), (2, 1, 3))
    for amount in (-60, -20, 20, 60):
        out = edit.apply_adjustments(ramp, edit.Adjustments(contrast=amount))[0, :, 0].astype(int)
        assert np.all(np.diff(out) >= 0), f"contrast {amount} is not monotonic"


def test_noise_reduction_cuts_colour_speckle_more_than_detail():
    """Colour noise should go; brightness detail should mostly stay."""
    rng = np.random.default_rng(1)
    base = np.zeros((200, 200, 3), dtype=np.uint8)
    base[:, 100:] = 90                                  # one real edge
    noisy = np.clip(base.astype(np.int16) +
                    rng.normal(0, 9, base.shape), 0, 255).astype(np.uint8)

    def chroma(im):
        lab = cv2.cvtColor(im, cv2.COLOR_BGR2LAB).astype(np.float32)
        return sum(float((lab[:, :, c] -
                          cv2.GaussianBlur(lab[:, :, c], (0, 0), 2.0)).std()) for c in (1, 2))

    cleaned = edit.apply_adjustments(noisy, edit.Adjustments(noise_reduction=60))
    assert chroma(cleaned) < chroma(noisy) * 0.75, (chroma(cleaned), chroma(noisy))
    # the edge is still an edge
    step = int(cleaned[100, 110, 0]) - int(cleaned[100, 90, 0])
    assert step > 60, f"edge washed out: {step}"


def test_noise_reduction_off_by_default():
    img = np.full((20, 20, 3), 100, dtype=np.uint8)
    assert np.array_equal(edit.apply_adjustments(img, edit.Adjustments()), img)



def _hazy():
    """A scene with a grey veil laid over it, as distance haze does."""
    rng = np.random.default_rng(5)
    scene = rng.integers(0, 255, (180, 240, 3)).astype(np.float32)
    veil = 150.0
    t = 0.45                                   # 45% of the scene survives
    return np.clip(scene * t + veil * (1 - t), 0, 255).astype(np.uint8)


def _veil(im):
    f = im.astype(np.float32) / 255.0
    return float(cv2.erode(f.min(axis=2), np.ones((9, 9), np.uint8)).mean() * 255)


def test_dehaze_removes_the_veil():
    hazy = _hazy()
    out = edit.apply_adjustments(hazy, edit.Adjustments(dehaze=70))
    assert _veil(out) < _veil(hazy) * 0.6, (_veil(out), _veil(hazy))


def test_dehaze_leaves_a_clear_photo_nearly_alone():
    rng = np.random.default_rng(6)
    clear = rng.integers(0, 255, (180, 240, 3)).astype(np.uint8)
    out = edit.apply_adjustments(clear, edit.Adjustments(dehaze=70))
    assert float(np.abs(out.astype(int) - clear.astype(int)).mean()) < 30


def test_levels_do_not_clip_what_was_there():
    rng = np.random.default_rng(7)
    img = np.clip(rng.normal(128, 30, (120, 160, 3)), 20, 230).astype(np.uint8)
    out = edit.apply_adjustments(img, edit.Adjustments(black_point=40, white_point=40))
    assert int(out.min()) < int(img.min()), "black point did not deepen"
    assert int(out.max()) > int(img.max()), "white point did not brighten"
    # no wholesale crushing: the spread of distinct values must survive
    assert len(np.unique(out)) > len(np.unique(img)) * 0.7


# ---------------------------------------------------------------- tone curves


def ramp():
    """A row of rising tones, as a 1-pixel-high BGR image."""
    tones = [0.02, 0.05, 0.12, 0.25, 0.4, 0.6, 0.78, 0.9, 0.97]
    img = np.array([[[t, t, t] for t in tones]], dtype=np.float32) * 255.0
    return img.astype(np.uint8)


def test_tone_controls_never_invert_the_order_of_tones():
    """Brighter in must always mean brighter out.

    The first version of these curves failed this: recovering highlights left
    0.97 darker than 0.90, and lifting shadows made the darkest pixel the
    brightest of the three. An inverted gradient cannot be undone later.
    """
    for key, value in (("highlights", 100), ("highlights", -100),
                       ("shadows", 100), ("shadows", -100)):
        out = edit.apply_adjustments(ramp(), edit.Adjustments(**{key: value}))
        row = [int(v) for v in out[0, :, 0]]
        assert all(b >= a for a, b in zip(row, row[1:])), \
            f"{key}={value} inverted the tone order: {row}"


def test_tone_controls_never_fuse_distinct_tones():
    """Pushing a control to its limit must not weld tones into one value.

    The old additive version drove 0.90 and 0.97 both to pure white, and
    0.02/0.05/0.12 all to pure black, destroying the detail in exactly the
    areas the slider was meant to rescue.
    """
    for key, value in (("highlights", 100), ("highlights", -100),
                       ("shadows", 100), ("shadows", -100)):
        out = edit.apply_adjustments(ramp(), edit.Adjustments(**{key: value}))
        row = [int(v) for v in out[0, :, 0]]
        # 9 tones in, at least 8 distinct out: one pair may round together,
        # a whole end of the range may not collapse.
        assert len(set(row)) >= 8, f"{key}={value} fused tones: {row}"


def test_tone_controls_keep_the_ends_of_the_range():
    """Crushing must leave black at black; lifting must not blow out white."""
    out = edit.apply_adjustments(ramp(), edit.Adjustments(shadows=-100))
    assert int(out[0, 0, 0]) == 0, "crushed shadows should reach true black"
    out = edit.apply_adjustments(ramp(), edit.Adjustments(highlights=100))
    assert int(out[0, -1, 0]) < 255, "lifting highlights clipped to pure white"


# ------------------------------------------------- resolution independence


def detail_energy(img):
    grey = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    return float(cv2.Laplacian(grey, cv2.CV_32F).var())


def textured(longest):
    """A repeatable textured image at a given size, for scale comparisons."""
    rng = np.random.default_rng(7)
    small = rng.integers(40, 215, (24, 24, 3), dtype=np.uint8)
    return cv2.resize(small, (longest, longest * 3 // 4),
                      interpolation=cv2.INTER_CUBIC)


def to_long_edge(img, longest):
    height, width = img.shape[:2]
    scale = longest / max(height, width)
    return cv2.resize(img, (max(1, round(width * scale)), max(1, round(height * scale))),
                      interpolation=cv2.INTER_AREA)


def test_detail_sliders_look_the_same_at_any_resolution():
    """A slider must mean the same thing in a preview and in the export.

    Pixel-sized blur radii broke this badly: measured on a real 3120px photo,
    grain at 60 added 10.6x the fine detail in a 1000px preview and 1.7x in
    the export. Anything tuned by eye on the preview came out wrong.

    The two sizes here hold the same content, so the change each slider makes
    is measured at its own resolution and then both are brought to one viewing
    size - which is how a person compares a preview with an export.
    """
    small = textured(500)
    big = textured(2000)
    for key, value in (("grain", 60.0), ("structure", 80.0), ("sharpen", 70.0),
                       ("clarity", 60.0)):
        adj = edit.Adjustments(**{key: value})
        sizes = []
        for base in (small, big):
            change = (edit.apply_adjustments(base, adj).astype(np.float32)
                      - base.astype(np.float32))
            sizes.append(float(to_long_edge(change, 400).std()))
        ratio = max(sizes) / max(min(sizes), 1e-6)
        assert ratio < 2.0, (f"{key}={value} behaves differently by size: "
                             f"change was {sizes[0]:.2f} at 500px and "
                             f"{sizes[1]:.2f} at 2000px, seen at the same size")


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
