#!/usr/bin/env python3
"""Checks for scoring, near-duplicates and exposure.
Run: .venv/bin/python tests/test_cull.py"""

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cull  # noqa: E402


def scene(seed, size=600):
    """A repeatable photo-like image: smooth shapes, not pure noise."""
    rng = np.random.default_rng(seed)
    small = rng.integers(20, 235, (12, 16, 3), dtype=np.uint8)
    return cv2.GaussianBlur(
        cv2.resize(small, (size, size * 3 // 4), interpolation=cv2.INTER_CUBIC), (0, 0), 3)


def test_the_same_photo_has_the_same_signature():
    img = scene(1)
    assert cull.signature_distance(cull.signature_of(img), cull.signature_of(img)) == 0


def test_another_frame_of_the_same_shot_is_close():
    """The next frame of a burst differs by a nudge of the camera and noise.

    If that reads as a different scene, burst grouping finds nothing. On a
    real shoot five frames of one scene sat 2-8 bits apart out of 64.
    """
    img = scene(2)
    rng = np.random.default_rng(9)
    nudged = np.roll(img, 4, axis=1).astype(np.int16)
    nudged += rng.integers(-6, 7, nudged.shape, dtype=np.int16)
    nudged = np.clip(nudged, 0, 255).astype(np.uint8)
    apart = cull.signature_distance(cull.signature_of(img), cull.signature_of(nudged))
    assert apart <= 10, f"the next frame of the same shot read as {apart} bits different"


def test_a_different_scene_is_far_away():
    """And the separation has to be wide, or grouping merges real photographs."""
    worst = 64
    for a in range(1, 6):
        for b in range(a + 1, 7):
            worst = min(worst, cull.signature_distance(
                cull.signature_of(scene(a)), cull.signature_of(scene(b))))
    assert worst > 10, f"two different scenes were only {worst} bits apart"


def test_exposure_finds_a_frame_that_is_simply_black():
    """Sharpness cannot catch these: a clipped edge is still a hard edge."""
    black = np.zeros((400, 600, 3), dtype=np.uint8)
    white = np.full((400, 600, 3), 255, dtype=np.uint8)
    normal = scene(3)

    assert cull.exposure_of(black)[0] > 0.9, "an all-black frame did not read as black"
    assert cull.exposure_of(white)[1] > 0.9, "an all-white frame did not read as blown"
    dark, blown = cull.exposure_of(normal)
    assert dark < 0.55 and blown < 0.25, (
        f"an ordinary photo was flagged: {dark:.2f} black, {blown:.2f} blown")


def test_scoring_shrinks_but_never_enlarges():
    """Enlarging interpolates new pixels, which smooths real edges away: a
    sharp 600px photo measured ~958 natively and ~94 blown up to 1000px."""
    # Hard edges on purpose: a soft image scores near zero either way and the
    # comparison proves nothing.
    small = np.zeros((450, 600, 3), dtype=np.uint8)
    small[::6] = 255
    small[:, ::6] = 255
    native = cull.blur_score_of(small)
    enlarged = cull.blur_score_of(
        cv2.resize(small, (1000, 750), interpolation=cv2.INTER_CUBIC))
    assert native > enlarged, f"enlarging raised the score: {native:.0f} vs {enlarged:.0f}"


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
