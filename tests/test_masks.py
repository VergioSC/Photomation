#!/usr/bin/env python3
"""Checks for region masks and selective editing.
Run: .venv/bin/python tests/test_masks.py"""

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auto     # noqa: E402
import edit     # noqa: E402
import masks    # noqa: E402


def _landscape():
    """Bright smooth sky on top, dark textured ground below."""
    rng = np.random.default_rng(3)
    img = np.zeros((240, 360, 3), dtype=np.uint8)
    img[:110] = (205, 180, 150)                       # BGR: a cool bright sky
    img[:110] = np.clip(img[:110] + rng.normal(0, 2, img[:110].shape), 0, 255)
    ground = rng.integers(20, 90, (130, 360, 3))      # dark and busy
    img[110:] = ground
    return img


def _interior():
    rng = np.random.default_rng(4)
    return rng.integers(10, 120, (240, 360, 3)).astype(np.uint8)


def test_sky_is_found_at_the_top_and_not_the_bottom():
    m = masks.analyse(_landscape())
    assert m.has_sky, "no sky found in an obvious landscape"
    assert m.sky[:90].mean() > 0.6, m.sky[:90].mean()
    assert m.sky[150:].mean() < 0.1, m.sky[150:].mean()


def test_foreground_is_the_complement_of_sky():
    m = masks.analyse(_landscape())
    assert np.allclose(m.sky + m.foreground, 1.0, atol=1e-5)


def test_no_sky_indoors():
    """Bright smooth patches indoors must not be mistaken for sky."""
    m = masks.analyse(_interior())
    assert not m.has_sky, f"found {m.sky.mean() * 100:.0f}% sky indoors"


def test_bright_ground_without_a_top_connection_is_not_sky():
    """Sky has to reach the top of the frame; a bright floor does not."""
    img = _landscape()
    img[200:] = 235                                  # a bright patch at the bottom
    m = masks.analyse(img)
    assert m.sky[200:].mean() < 0.15, m.sky[200:].mean()


def test_selective_moves_sky_and_ground_in_opposite_directions():
    """The whole point: one slider cannot do this."""
    img = _landscape()
    scene = masks.analyse(img)
    adj = edit.Adjustments(sky_exposure=-40, foreground_exposure=60)
    out = edit.apply_adjustments(img, adj, scene=scene)
    sky_before, sky_after = img[:90].mean(), out[:90].mean()
    gnd_before, gnd_after = img[150:].mean(), out[150:].mean()
    assert sky_after < sky_before - 3, (sky_before, sky_after)
    assert gnd_after > gnd_before + 3, (gnd_before, gnd_after)


def test_selective_does_nothing_without_masks():
    img = _landscape()
    adj = edit.Adjustments(sky_exposure=-40, foreground_exposure=60)
    assert np.array_equal(edit.apply_adjustments(img, adj), img)


def test_auto_splits_a_landscape_but_not_an_interior():
    land = auto.analyse(_landscape())[0]
    inside = auto.analyse(_interior())[0]
    assert land.foreground_exposure > 0 or land.sky_exposure < 0
    assert inside.foreground_exposure == 0 and inside.sky_exposure == 0


def test_regional_bands_give_the_same_answer_as_one_pass():
    """Blending a band at a time must not change the result, only the memory.

    Done in one go it held the photo, an adjusted copy and the blend weights
    as full-resolution float32 at once - over 650MB on a 24MP frame, which was
    enough to get the process killed.
    """
    rng = np.random.default_rng(4)
    img = cv2.GaussianBlur(rng.integers(40, 200, (700, 1100, 3), dtype=np.uint8), (0, 0), 2)
    img[:240] = np.clip(img[:240].astype(int) + 55, 0, 255).astype(np.uint8)
    scene = masks.analyse(img)
    adj = edit.Adjustments(sky_exposure=-40, foreground_exposure=30, sky_contrast=15)

    got = edit.apply_regional(img, adj, scene)

    # The same thing written the obvious way, on the whole frame at once.
    want = img
    for amount, mask, extra in ((adj.sky_exposure, scene.sky, {"contrast": adj.sky_contrast}),
                                (adj.foreground_exposure, scene.foreground, {})):
        layer = edit.apply_adjustments(want, edit.Adjustments(exposure=amount / 50.0, **extra))
        weight = mask[:, :, None]
        want = np.clip(want.astype(np.float32) * (1.0 - weight)
                       + layer.astype(np.float32) * weight, 0, 255).astype(np.uint8)

    assert np.array_equal(got, want), (
        f"banding changed the result by up to "
        f"{int(np.abs(got.astype(int) - want.astype(int)).max())}")


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
