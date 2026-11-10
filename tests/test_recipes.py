#!/usr/bin/env python3
"""Checks for recipe translation, the importer, and auto analysis.
Run: .venv/bin/python tests/test_recipes.py"""

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auto      # noqa: E402
import edit      # noqa: E402
import recipes   # noqa: E402

KODACHROME = """
Film Simulation: Classic Chrome
Dynamic Range: DR200
Highlight: 0
Shadow: +2
Color: +2
Sharpening: +1
Clarity: +3
Grain Effect: Weak, Small
Color Chrome Effect: Strong
White Balance: Daylight, +2 Red & -5 Blue
Exposure Compensation: +2/3
"""


def test_importer_reads_a_real_recipe():
    parsed = recipes.parse_recipe_text(KODACHROME, "Kodachrome")
    s = parsed["settings"]
    assert s["film_simulation"] == "Classic Chrome"
    assert s["shadow"] == 2 and s["color"] == 2
    assert s["white_balance"] == {"mode": "Daylight", "red": 2.0, "blue": -5.0}
    assert abs(s["exposure_compensation"] - 2 / 3) < 1e-6, s["exposure_compensation"]


def test_importer_rejects_junk():
    try:
        recipes.parse_recipe_text("the quick brown fox\nno settings here")
    except recipes.RecipeError:
        return
    raise AssertionError("junk text should have been rejected")


def test_camera_shadow_sign_is_flipped():
    """Camera 'Shadow +2' DARKENS shadows; our slider LIFTS them."""
    adj = recipes.to_adjustments({"shadow": 2})
    assert adj.shadows < 0, adj.shadows
    assert recipes.to_adjustments({"shadow": -2}).shadows > 0


def test_white_balance_shift_becomes_warmth():
    warm = recipes.to_adjustments({"white_balance": {"mode": "Daylight", "red": 4, "blue": -4}})
    cool = recipes.to_adjustments({"white_balance": {"mode": "Daylight", "red": -4, "blue": 4}})
    assert warm.temperature > 0 > cool.temperature


def test_dynamic_range_protects_highlights():
    assert recipes.to_adjustments({"dynamic_range": 400}).highlights < \
           recipes.to_adjustments({"dynamic_range": 100}).highlights


def test_grain_text_is_understood():
    weak = recipes.to_adjustments({"grain": "Weak, Small"})
    strong = recipes.to_adjustments({"grain": "Strong, Large"})
    off = recipes.to_adjustments({"grain": "Off"})
    assert off.grain == 0
    assert 0 < weak.grain < strong.grain
    assert strong.grain_size > weak.grain_size


def test_every_shipped_recipe_loads_and_maps_to_a_real_look():
    found, problems = recipes.load_all()
    assert not problems, problems
    assert len(found) >= 10, len(found)
    for r in found:
        film = r["adjustments"].film
        assert film in edit.FILM_LOOKS, f"{r['id']} -> unknown look {film!r}"


def test_recipes_actually_change_a_photo():
    img = np.full((40, 60, 3), 120, dtype=np.uint8)
    img[:, 30:] = (60, 140, 200)
    base = img.astype(int)
    for r in recipes.load_all()[0]:
        out = edit.apply_adjustments(img, r["adjustments"]).astype(int)
        assert np.abs(out - base).mean() > 1.0, f"{r['id']} did nothing"


# ----------------------------------------------------------------- auto


def _scene(mean, cast=(1.0, 1.0, 1.0), spread=40):
    rng = np.random.default_rng(4)
    base = rng.normal(mean, spread, (160, 240, 3))
    for i, m in enumerate(cast):
        base[:, :, i] *= m
    return np.clip(base, 0, 255).astype(np.uint8)


def test_auto_brightens_a_dark_photo_and_darkens_a_bright_one():
    dark = auto.analyse(_scene(45))[0]
    bright = auto.analyse(_scene(215))[0]
    assert dark.exposure > 0.1, dark.exposure
    assert bright.exposure < -0.1, bright.exposure


def test_auto_leaves_a_correct_photo_alone():
    adj = auto.analyse(_scene(120))[0]
    assert abs(adj.exposure) < 0.25, adj.exposure


def test_auto_cools_a_warm_cast():
    """A neutral scene tinted orange should be cooled, not warmed."""
    adj = auto.analyse(_scene(120, cast=(0.78, 1.0, 1.25)))[0]
    assert adj.temperature < -2, adj.temperature


def test_auto_does_not_fight_a_genuinely_blue_subject():
    """A saturated blue scene is a subject, not a cast: leave it be.

    Plain grey-world fails this, turning a blue sea brown.
    """
    sea = np.zeros((160, 240, 3), dtype=np.uint8)
    sea[:, :, 0], sea[:, :, 1], sea[:, :, 2] = 190, 110, 45   # strongly blue
    adj = auto.analyse(sea)[0]
    assert abs(adj.temperature) < 12, adj.temperature


def test_auto_adds_contrast_only_to_a_flat_photo():
    flat = auto.analyse(_scene(128, spread=9))[0]
    full = auto.analyse(_scene(128, spread=85))[0]
    assert flat.contrast > full.contrast, (flat.contrast, full.contrast)


def test_auto_crop_keeps_most_of_the_frame_and_fits_inside_it():
    img = _scene(120)
    img[30:70, 150:210] = 255                     # a detailed corner
    _, rect, _, _ = auto.analyse(img, want_crop=True)
    x, y, w, h = rect
    assert 0 <= x and 0 <= y and x + w <= 1.0001 and y + h <= 1.0001, rect
    assert w * h > 0.6, f"crop threw away too much: {w * h:.2f}"


def test_auto_crop_respects_a_requested_aspect():
    img = _scene(120)
    _, rect, _, _ = auto.analyse(img, want_crop=True, aspect=1.0)
    _, _, w, h = rect
    got = (w * img.shape[1]) / (h * img.shape[0])
    assert abs(got - 1.0) < 0.02, f"asked for 1:1, got {got:.3f}"


def test_crop_image_returns_the_requested_region():
    img = np.zeros((100, 200, 3), dtype=np.uint8)
    out = edit.crop_image(img, (0.25, 0.5, 0.5, 0.5))
    assert out.shape[:2] == (50, 100), out.shape



# --------------------------------------------------- photographic judgement


def test_high_iso_limits_shadow_lifting():
    """A noisy capture must not be pushed like a clean one."""
    import exif
    dark = _scene(35)
    clean = auto.judge(dark, exif.Shot(iso=200, shutter=1/250, aperture=8, focal_35mm=35))
    noisy = auto.judge(dark, exif.Shot(iso=12800, shutter=1/250, aperture=8, focal_35mm=35))
    assert noisy.max_shadow_lift < clean.max_shadow_lift, \
        (noisy.max_shadow_lift, clean.max_shadow_lift)


def test_blown_highlights_stop_recovery_being_attempted():
    """White with nothing under it cannot be recovered, only greyed."""
    blown = np.full((120, 160, 3), 120, dtype=np.uint8)
    blown[:, :60] = 255                       # a big, fully clipped area
    limits = auto.judge(blown)
    assert limits.max_highlight_pull < 25, limits.max_highlight_pull
    assert any("blown" in v for v in limits.verdicts), limits.verdicts


def test_shake_risk_holds_sharpening_back():
    """Sharpening motion blur makes it crunchy, not sharp."""
    import exif
    img = _scene(120)
    steady = auto.judge(img, exif.Shot(iso=200, shutter=1/500, focal_35mm=50))
    shaky = auto.judge(img, exif.Shot(iso=200, shutter=1/15, focal_35mm=50))
    assert shaky.max_sharpen < steady.max_sharpen, (shaky.max_sharpen, steady.max_sharpen)
    assert any("shake" in v for v in shaky.verdicts), shaky.verdicts


def test_exposure_compensation_is_respected():
    """A photographer who dialled -1 stop meant the photo to be dark."""
    import exif
    dark = _scene(45)
    plain = auto.analyse(dark, shot=exif.Shot(iso=200))[0]
    chosen = auto.analyse(dark, shot=exif.Shot(iso=200, exposure_bias=-1.0))[0]
    assert abs(chosen.exposure) < abs(plain.exposure), (chosen.exposure, plain.exposure)


def test_limits_actually_bind_the_result():
    import exif
    dark = _scene(30)
    adj, _, _, limits = auto.analyse(dark, shot=exif.Shot(iso=25600))
    assert adj.shadows <= limits.max_shadow_lift + 0.01, (adj.shadows, limits.max_shadow_lift)
    assert limits.exposure_floor <= adj.exposure <= limits.exposure_ceiling


def test_exif_reads_a_real_photo():
    import exif
    from pathlib import Path as _P
    photos = sorted(_P("input").glob("*.JPG")) if _P("input").is_dir() else []
    if not photos:
        return                      # nothing to read; not a failure
    shot = exif.read(photos[0])
    assert shot.known, "no exposure data found in a camera JPEG"
    assert shot.iso > 0 and shot.shutter > 0


def test_photographer_presets_load_and_differ():
    found, problems = recipes.load_all()
    assert not problems, problems
    shots = [r for r in found if r["group"] == "Photographer"]
    assert len(shots) >= 15, len(shots)

    img = np.full((60, 90, 3), 120, dtype=np.uint8)
    img[:, 45:] = (60, 140, 200)
    seen = {}
    for r in shots:
        out = edit.apply_adjustments(img, r["adjustments"])
        key = out.tobytes()
        assert key not in seen, f"{r['id']} renders identically to {seen.get(key)}"
        seen[key] = r["id"]


def test_a_recipe_may_use_editor_parameters_directly():
    data = {"name": "x", "adjustments": {"film": "acros", "contrast": 30}}
    adj = recipes.build_adjustments(data)
    assert adj.film == "acros" and adj.contrast == 30


def test_adjustments_override_camera_settings():
    data = {"name": "x",
            "settings": {"film_simulation": "Velvia", "color": 4},
            "adjustments": {"saturation": -50}}
    adj = recipes.build_adjustments(data)
    assert adj.film == "velvia", adj.film        # kept from settings
    assert adj.saturation == -50, adj.saturation  # overridden


def test_solarized_look_reverses_the_highlights():
    """Man Ray's solarization must bend back down, not just add contrast."""
    ramp = np.tile(np.linspace(0, 255, 256, dtype=np.uint8), (4, 1))
    ramp = cv2.merge([ramp, ramp, ramp])
    out = edit.apply_film_look(ramp, "solarized")[0, :, 0]
    peak = int(np.argmax(out))
    assert 100 < peak < 220, f"peak at {peak}"
    assert int(out[255]) < int(out[peak]) - 60, "highlights did not reverse"


def test_auto_does_not_crush_a_dusk_scene():
    """A dark, low-contrast frame is dusk, not a fault. Half contrast at most."""
    rng = np.random.default_rng(2)
    dusk = np.clip(rng.normal(55, 16, (160, 240, 3)), 0, 255).astype(np.uint8)
    bright_flat = np.clip(rng.normal(150, 16, (160, 240, 3)), 0, 255).astype(np.uint8)
    dusk_adj = auto.analyse(dusk)[0]
    flat_adj = auto.analyse(bright_flat)[0]
    assert dusk_adj.contrast < flat_adj.contrast, (dusk_adj.contrast, flat_adj.contrast)


def test_auto_sets_noise_reduction_from_iso():
    import exif
    img = _scene(120)
    clean = auto.analyse(img, shot=exif.Shot(iso=200))[0]
    dirty = auto.analyse(img, shot=exif.Shot(iso=12800))[0]
    assert clean.noise_reduction == 0, clean.noise_reduction
    assert dirty.noise_reduction >= 40, dirty.noise_reduction


def test_auto_withholds_vibrance_and_sharpening_on_noisy_files():
    import exif
    img = _scene(120)
    dirty = auto.analyse(img, shot=exif.Shot(iso=25600, shutter=1/8, focal_35mm=85))[0]
    assert dirty.vibrance == 0, dirty.vibrance
    assert dirty.sharpen == 0, dirty.sharpen


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
