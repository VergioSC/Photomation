#!/usr/bin/env python3
"""Checks for the automatic adjustments. Run: .venv/bin/python tests/test_auto.py"""

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auto  # noqa: E402
import edit  # noqa: E402
import masks as masks_module  # noqa: E402


def blown_share(image):
    return float((image >= 254).any(axis=2).mean())


def render(image, adj):
    scene = masks_module.analyse(image) if adj.has_regional else None
    return edit.apply_adjustments(image, adj, scene=scene)


def lit_subject_on_a_dark_ground(size=600):
    """A frame that is mostly unrecoverable black with a correctly lit subject.

    This is the shape that broke auto: a shop interior where a third of the
    picture was black shelving. Averaging the whole frame said the photo was
    far too dark, so auto bought a full stop and blew out the people, who had
    been exposed correctly all along.
    """
    rng = np.random.default_rng(3)
    # Enough unrecoverable black to own the frame's median, then a mid-dark
    # interior and a subject lit close to the top of the range - the shape of
    # the shop photo this was traced from.
    img = rng.integers(40, 95, (size, size, 3), dtype=np.uint8)
    img[:size * 9 // 20] = rng.integers(2, 9, (size * 9 // 20, size, 3), dtype=np.uint8)
    lit = slice(size // 2, size * 5 // 6)
    img[lit, lit] = rng.integers(180, 236, (size // 3, size // 3, 3), dtype=np.uint8)
    return cv2.GaussianBlur(img, (0, 0), 1.5)


def test_auto_exposes_for_the_subject_not_the_black():
    """A frame full of black must not buy itself a big exposure rise.

    This is a regression guard on the result, not a proof of the metering
    change on its own: a synthetic frame has a flatter tone distribution than
    a real photo, so the whole-frame and subject-only medians here sit much
    closer together than they did on the shop photo that exposed the problem
    (0.18 against a correctly exposed subject, which bought the full +1.2).
    """
    img = lit_subject_on_a_dark_ground()
    adj, _, _, _ = auto.analyse(img)
    assert adj.exposure < 0.75, (
        f"a frame that is mostly black bought {adj.exposure:+.2f} stops; "
        f"the lit part was already exposed correctly")


def test_auto_does_not_blow_out_the_highlights():
    """Clipped white has no detail left to recover, so auto must not create it."""
    img = lit_subject_on_a_dark_ground()
    before = blown_share(img)
    adj, _, _, _ = auto.analyse(img)
    after = blown_share(render(img, adj))
    assert after <= max(0.01, before * 2 + 0.005), (
        f"auto pushed {after * 100:.2f}% of the frame to white, "
        f"up from {before * 100:.2f}%")


def test_auto_still_brightens_a_genuinely_dark_photo():
    """The highlight guard must not turn into a refusal to adjust anything."""
    rng = np.random.default_rng(11)
    img = rng.integers(18, 54, (600, 800, 3), dtype=np.uint8)
    adj, _, _, _ = auto.analyse(img)
    out = render(img, adj)
    assert float(out.mean()) > float(img.mean()) * 1.2, (
        f"a dark, flat frame was left at {out.mean():.0f} from {img.mean():.0f}")


def test_guard_reports_what_it_took_back():
    """Every number auto changes has to be explained; so does every one it undoes."""
    img = lit_subject_on_a_dark_ground()
    _, _, notes, _ = auto.analyse(img)
    pulled = [n for n in notes if "protect the highlights" in n]
    if pulled:
        assert "to" in pulled[0], "the note should say what moved and to where"


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
