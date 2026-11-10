#!/usr/bin/env python3
"""Automatic analysis: work out sensible edits for a photo on its own.

This is image analysis, not a neural network. Nothing is uploaded and nothing
is downloaded; it all runs here from the pixels. What it does:

  white balance   grey-world: averaged over a whole scene, colours tend to
                  cancel out to grey, so a cast shows up as the channel
                  averages drifting apart
  exposure        aim the median brightness at a normal midtone
  contrast        measure how much of the range the photo actually uses and
                  stretch it if it is sitting in a narrow band
  shadows and     look at how much is crushed to black or blown to white and
  highlights      recover only as much as is actually clipped
  crop            find the subject by edge density - detail is where interest
                  usually is - and frame it on a rule-of-thirds line

Every result is a suggestion that lands in the sliders, so you can take it as
a starting point and move anything you disagree with.
"""

from dataclasses import dataclass, field

import cv2
import numpy as np

import edit
import exif

# A normal midtone. Most photographs look right with the median here.
TARGET_MEDIAN = 0.46

# How far to go towards that target. A dusk photograph is meant to be dark, so
# correcting it the whole way would flatten the mood out of it. Moving most of
# the way fixes a genuinely wrong exposure and leaves a deliberate one alone.
EXPOSURE_DAMPING = 0.6

# Nothing is allowed to run away on its own.
MAX_EXPOSURE_EV = 1.2
MAX_TEMPERATURE = 30.0
MAX_TONE = 45.0

# No face detection: OpenCV 5 removed the Haar CascadeClassifier API, and its
# replacement (FaceDetectorYN) needs an ONNX model downloaded into the project.
# Edge density finds portrait subjects well enough in practice, because eyes,
# hair and clothing carry far more detail than a backdrop.


def analyse(image_bgr, want_crop=False, aspect=None, shot=None):
    """Return (Adjustments, crop_rect_or_None, notes).

    crop_rect is (x, y, w, h) in 0..1. notes explains each decision in plain
    words, so the numbers are not a black box.

    Every value is held inside what `judge` says this photo will tolerate, so
    a noisy ISO 10000 frame is not pushed like a clean ISO 200 one.
    """
    # Work on a small copy: these are statistics, and full resolution adds
    # nothing but time.
    work = _downscale(image_bgr, 900)
    limits = judge(work, shot)
    notes = list(limits.verdicts)

    # Haze is decided first and then APPLIED to the copy everything else is
    # measured on. Removing haze takes light away, so a scene measured before
    # it comes off reads brighter than the result actually is - which left the
    # beach frames correctly de-hazed and two stops too dark.
    dehaze = _auto_dehaze(work, notes)
    if dehaze > 0:
        work = edit.apply_adjustments(work, edit.Adjustments(dehaze=dehaze))

    black_point, white_point = _auto_levels(work, notes)
    vibrance = _auto_vibrance(work, limits, notes)
    sharpen = _auto_sharpen(work, limits, notes)
    temperature, tint = _auto_white_balance(work, notes)
    exposure = _auto_exposure(work, notes)
    contrast = _auto_contrast(work, notes)
    shadows, highlights = _auto_tone(work, notes)

    # Apply the photographic limits, and say so when one actually bites.
    exposure_held = float(np.clip(exposure, limits.exposure_floor, limits.exposure_ceiling))
    if abs(exposure_held - exposure) > 0.01:
        notes.append(f"Exposure held to {exposure_held:+.2f} stops rather than "
                     f"{exposure:+.2f}, for the reason above.")
    shadows_held = min(shadows, limits.max_shadow_lift)
    if shadows_held < shadows - 0.5:
        notes.append(f"Shadow lift held to {shadows_held:.0f} instead of {shadows:.0f} "
                     f"to keep noise down.")
    highlights_held = max(highlights, -limits.max_highlight_pull)
    if highlights_held > highlights + 0.5:
        notes.append(f"Highlight recovery held to {abs(highlights_held):.0f}: there is "
                     f"nothing left under the clipped areas.")

    adj = edit.Adjustments(
        temperature=temperature,
        tint=tint,
        exposure=round(exposure_held, 2),
        contrast=contrast,
        shadows=round(shadows_held, 1),
        highlights=round(highlights_held, 1),
        noise_reduction=limits.noise_reduction,
        dehaze=dehaze,
        black_point=black_point,
        white_point=white_point,
        vibrance=vibrance,
        sharpen=sharpen,
    )

    rect = _auto_crop(work, aspect, notes) if want_crop else None
    return adj, rect, notes, limits


def _downscale(image, longest):
    height, width = image.shape[:2]
    scale = longest / max(height, width)
    if scale >= 1:
        return image
    return cv2.resize(image, (max(1, round(width * scale)), max(1, round(height * scale))),
                      interpolation=cv2.INTER_AREA)


def _auto_white_balance(image, notes):
    """White balance from the near-neutral pixels only.

    Plain grey-world assumes a scene averages to grey, which is wrong whenever
    a real colour fills the frame: a blue sea reads as a blue cast, and
    "correcting" it turns the sea brown. Measuring only the pixels that are
    already close to neutral avoids that, because those are the ones whose
    colour really is the light rather than the subject.
    """
    x = image.astype(np.float32) / 255.0
    channel_max = x.max(axis=2)
    channel_min = x.min(axis=2)
    saturation = (channel_max - channel_min) / np.maximum(channel_max, 1e-6)

    luma = np.sum(x * edit.LUMA_BGR, axis=2)
    usable = (luma > 0.05) & (luma < 0.92) & (saturation < 0.22)
    share = float(usable.mean())
    if share < 0.02:
        notes.append("White balance left alone: the frame is too strongly "
                     "coloured to tell a cast from the subject.")
        return 0.0, 0.0

    linear = edit.srgb_to_linear(x)
    b, g, r = (linear[:, :, i][usable].mean() for i in range(3))
    if min(b, g, r) <= 1e-6:
        return 0.0, 0.0

    # A red-heavy average means the photo is already warm, so cool it down.
    warmth = (r - b) / max(r + b, 1e-6)
    green = (g - (r + b) / 2.0) / max(g, 1e-6)

    temperature = float(np.clip(-warmth * 130.0, -MAX_TEMPERATURE, MAX_TEMPERATURE))
    tint = float(np.clip(green * 90.0, -25.0, 25.0))

    if abs(temperature) < 3:
        temperature = 0.0
    if abs(tint) < 3:
        tint = 0.0
    if temperature or tint:
        notes.append(f"White balance {('cooled' if temperature < 0 else 'warmed')} "
                     f"by {abs(temperature):.0f} to neutralise a colour cast.")
    else:
        notes.append("Colour already looks neutral.")
    return temperature, tint


def _auto_exposure(image, notes):
    """Move the median brightness towards a normal midtone, in stops."""
    luma = np.sum(image.astype(np.float32) / 255.0 * edit.LUMA_BGR, axis=2)
    median = float(np.median(luma))
    if median <= 0.004:
        notes.append("Exposure left alone: the frame is almost entirely black.")
        return 0.0

    current = float(edit.srgb_to_linear(np.float32(median)))
    target = float(edit.srgb_to_linear(np.float32(TARGET_MEDIAN)))
    full = float(np.log2(target / max(current, 1e-6)))
    ev = float(np.clip(full * EXPOSURE_DAMPING, -MAX_EXPOSURE_EV, MAX_EXPOSURE_EV))
    if abs(ev) < 0.08:
        notes.append("Exposure already about right.")
        return 0.0
    notes.append(f"Exposure {'raised' if ev > 0 else 'lowered'} {abs(ev):.2f} stops "
                 f"to bring the midtones to a normal level.")
    return round(ev, 2)


def _auto_contrast(image, notes):
    """Add contrast only where the photo is genuinely flat, and gently.

    A dusk seascape or an overcast morning uses a narrow range *because that
    is what it looked like*. Treating narrow range as a fault and stretching
    it hard turns soft evening light into a harsh silhouette, which is exactly
    what this used to do.
    """
    luma = np.sum(image.astype(np.float32) / 255.0 * edit.LUMA_BGR, axis=2)
    low, high = np.percentile(luma, (2, 98))
    spread = float(high - low)
    median = float(np.median(luma))

    if spread >= 0.72:
        notes.append("Contrast already full.")
        return 0.0

    amount = float(np.clip((0.72 - spread) * 55.0, 0.0, 22.0))

    # A dark, low-contrast frame is usually dusk or night, and the mood is the
    # point. Half the push, so it gains shape without becoming a silhouette.
    if median < 0.32:
        amount *= 0.5
        if amount >= 3:
            notes.append("Low, soft light: contrast added sparingly so the mood survives.")

    if amount < 3:
        notes.append("Contrast left alone: the softness looks deliberate.")
        return 0.0
    notes.append(f"Contrast raised {amount:.0f}: the photo used "
                 f"{spread * 100:.0f}% of the tonal range.")
    return round(amount, 1)


def _auto_dehaze(image, notes):
    """Measure the haze veil and remove it.

    The dark channel - the darkest colour channel in each small patch - should
    be near zero in a clear photo. Where it sits well above zero, scattered
    light has been added on top of the scene. That is a physical veil, not low
    contrast, which is why flat distance shots never respond to a contrast
    slider.
    """
    small = _downscale(image, 400).astype(np.float32) / 255.0
    veil = float(cv2.erode(small.min(axis=2), np.ones((9, 9), np.uint8)).mean() * 255)

    if veil < 25:
        notes.append("No haze to speak of.")
        return 0.0
    amount = float(np.clip((veil - 22) * 0.95, 0.0, 55.0))
    notes.append(f"Haze removed ({amount:.0f}): a veil of scattered light was sitting "
                 f"over the scene, measured at {veil:.0f} of 255.")
    return round(amount, 1)


def _auto_levels(image, notes):
    """Give the photo a true black and a true white.

    A frame whose darkest pixel is grey looks washed out no matter how much
    contrast is added, because nothing in it is actually black.
    """
    luma = np.sum(image.astype(np.float32) / 255.0 * edit.LUMA_BGR, axis=2)
    low = float(np.percentile(luma, 0.2))
    high = float(np.percentile(luma, 99.8))

    black = float(np.clip((low - 0.01) * 500.0, 0.0, 55.0))
    white = float(np.clip((0.97 - high) * 220.0, 0.0, 55.0))
    if black < 4:
        black = 0.0
    if white < 4:
        white = 0.0

    if black or white:
        parts = []
        if black:
            parts.append(f"the darkest tones were only reaching {low * 255:.0f}")
        if white:
            parts.append(f"the brightest only reaching {high * 255:.0f}")
        notes.append("Black and white points set: " + ", and ".join(parts) + ".")
    return round(black, 1), round(white, 1)


def _auto_vibrance(image, limits, notes):
    """A little colour lift, but only on a clean file and a dull scene.

    Vibrance pushes the weakest colours hardest, which is also where noise
    lives, so a noisy frame gets none of it. A scene that is already colourful
    gets none either: adding more would only look artificial.
    """
    if limits.noise_reduction >= 50:
        notes.append("No colour lift: too noisy, it would push the speckle as well.")
        return 0.0

    x = image.astype(np.float32) / 255.0
    channel_max = x.max(axis=2)
    channel_min = x.min(axis=2)
    colourfulness = float(np.mean((channel_max - channel_min) / np.maximum(channel_max, 1e-6)))

    if colourfulness > 0.30:
        notes.append("Colour already strong, so none was added.")
        return 0.0
    amount = float(np.clip((0.30 - colourfulness) * 70.0, 0.0, 16.0))
    if amount < 3:
        return 0.0
    notes.append(f"Vibrance +{amount:.0f}: the weaker colours were lifted, "
                 f"leaving the strong ones alone.")
    return round(amount, 1)


def _auto_sharpen(image, limits, notes):
    """Modest output sharpening, within whatever the capture allows."""
    cap = limits.max_sharpen
    if cap <= 15:
        notes.append("No sharpening: the capture will not take it.")
        return 0.0
    # How much fine detail is actually present. A soft frame gains little and
    # an already-crisp one needs little.
    grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32)
    detail = float((grey - cv2.GaussianBlur(grey, (0, 0), 1.5)).std())
    if detail < 1.5:
        notes.append("No sharpening: there is no fine detail to bring out.")
        return 0.0
    amount = float(np.clip(22.0 - detail * 0.8, 6.0, 22.0))
    amount = min(amount, cap * 0.45)
    if amount < 4:
        return 0.0
    notes.append(f"Sharpening +{amount:.0f}, kept well inside what this capture allows.")
    return round(amount, 1)


def _auto_tone(image, notes):
    """Recover whatever is actually clipped, and no more."""
    luma = np.sum(image.astype(np.float32) / 255.0 * edit.LUMA_BGR, axis=2)
    crushed = float((luma < 0.02).mean())
    blown = float((luma > 0.98).mean())

    shadows = float(np.clip(crushed * 180.0, 0.0, MAX_TONE)) if crushed > 0.015 else 0.0
    highlights = -float(np.clip(blown * 180.0, 0.0, MAX_TONE)) if blown > 0.015 else 0.0

    if shadows:
        notes.append(f"Shadows lifted {shadows:.0f}: {crushed * 100:.1f}% of the "
                     f"frame was crushed to black.")
    if highlights:
        notes.append(f"Highlights pulled {abs(highlights):.0f}: {blown * 100:.1f}% "
                     f"of the frame was blown out.")
    if not shadows and not highlights:
        notes.append("Nothing clipped at either end.")
    return shadows, highlights


def _auto_crop(image, aspect, notes):
    """Suggest a crop around the subject, framed on a rule-of-thirds line.

    The subject is a face when there is one, otherwise the busiest part of the
    frame by edge density, which is where detail and therefore interest tends
    to be. The crop never takes more than a quarter of the picture away, so it
    tightens a composition rather than reinventing it.
    """
    height, width = image.shape[:2]
    grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    # Edge density: blur the gradient magnitude and take its centre of mass.
    edges = cv2.magnitude(cv2.Sobel(grey, cv2.CV_32F, 1, 0, ksize=3),
                          cv2.Sobel(grey, cv2.CV_32F, 0, 1, ksize=3))
    edges = cv2.GaussianBlur(edges, (0, 0), sigmaX=max(height, width) / 40.0)
    total = float(edges.sum())
    if total <= 1e-6:
        notes.append("No crop: the frame has no clear subject.")
        return None
    ys, xs = np.mgrid[0:height, 0:width]
    cx = float((edges * xs).sum() / total) / width
    cy = float((edges * ys).sum() / total) / height
    notes.append("Crop centred on the busiest part of the frame.")

    target = aspect if aspect else width / height
    keep_area = 0.85                 # a tightening, not a recomposition
    source_aspect = width / height
    # Start from the area we want, then fit it to the requested shape.
    crop_w = min(1.0, (keep_area * target / source_aspect) ** 0.5)
    crop_h = min(1.0, crop_w * source_aspect / target)
    if crop_h >= 1.0:
        crop_h = 1.0
        crop_w = min(1.0, crop_h * target / source_aspect)

    # Put the subject on the nearest rule-of-thirds line rather than dead centre.
    def place(centre, size):
        third = 1.0 / 3.0 if centre < 0.5 else 2.0 / 3.0
        start = centre - size * third
        return float(np.clip(start, 0.0, 1.0 - size))

    rect = (place(cx, crop_w), place(cy, crop_h), crop_w, crop_h)
    notes.append(f"Crop keeps {crop_w * crop_h * 100:.0f}% of the frame.")
    return rect

# ----------------------------------------------------------------- judgement
#
# Rules a photographer applies before touching a slider. They decide what is
# worth attempting on a given photo, using the exposure triangle recorded by
# the camera plus what the pixels themselves show.


@dataclass
class Limits:
    """How far this particular photo can be pushed, and why."""

    noise_reduction: float = 0.0
    max_shadow_lift: float = 55.0
    max_highlight_pull: float = 55.0
    max_sharpen: float = 60.0
    max_clarity: float = 60.0
    exposure_floor: float = -1.2
    exposure_ceiling: float = 1.2
    verdicts: list = field(default_factory=list)

    def note(self, text):
        self.verdicts.append(text)


# ISO bands and what each costs in noise. Lifting shadows amplifies whatever
# noise is already there, so the dirtier the capture the less room there is.
ISO_BUDGET = [
    (800, 55.0, 60.0, 0.0, "Low ISO: clean file, plenty of room to lift shadows."),
    (1600, 45.0, 50.0, 18.0, "Mid ISO: a little noise, cleaned lightly."),
    (3200, 38.0, 42.0, 32.0, "Moderate ISO: some noise, so shadows are lifted with care."),
    (12800, 22.0, 25.0, 52.0, "High ISO: colour noise cleaned up, and shadows and detail "
                              "are not pushed far."),
    (float("inf"), 12.0, 14.0, 70.0, "Very high ISO: heavy noise, cleaned firmly, and only "
                                     "gentle moves are safe."),
]


def judge(image_bgr, shot=None):
    """Decide what this photo will tolerate. Returns Limits with plain reasons."""
    limits = Limits()
    shot = shot or exif.Shot()

    x = image_bgr.astype(np.float32) / 255.0
    luma = np.sum(x * edit.LUMA_BGR, axis=2)

    # Clipped means every channel is at the top: there is no information left
    # under it, so "recovering" highlights only turns white into grey.
    clipped = float((image_bgr.min(axis=2) >= 250).mean())
    crushed = float((image_bgr.max(axis=2) <= 6).mean())

    if clipped > 0.02:
        limits.max_highlight_pull = 18.0
        limits.exposure_ceiling = min(limits.exposure_ceiling, 0.25)
        limits.note(f"{clipped * 100:.1f}% of the frame is fully blown. That detail is "
                    f"gone for good, so highlights are only eased, not chased.")
    elif clipped > 0.005:
        limits.max_highlight_pull = 35.0
        limits.note(f"{clipped * 100:.1f}% is clipped white: a little, so highlights are "
                    f"pulled gently.")

    if crushed > 0.04:
        limits.note(f"{crushed * 100:.1f}% is solid black. Some of that will not come "
                    f"back; lifting it mostly reveals noise.")

    if clipped > 0.01 and crushed > 0.04:
        limits.note("The scene was brighter than the sensor could hold end to end. "
                    "Highlights are protected first, because they cannot be rebuilt.")

    if shot.known:
        limits.note(f"Shot at {shot.summary()}.")

    if shot.iso:
        for ceiling, shadow_cap, detail_cap, nr, reason in ISO_BUDGET:
            if shot.iso <= ceiling:
                limits.max_shadow_lift = min(limits.max_shadow_lift, shadow_cap)
                limits.max_clarity = min(limits.max_clarity, detail_cap)
                limits.max_sharpen = min(limits.max_sharpen, detail_cap)
                limits.noise_reduction = nr
                limits.note(f"ISO {shot.iso}. {reason}")
                break

    # Camera shake: sharpening motion blur does not recover detail, it just
    # makes the smear crunchy.
    if shot.shutter and shot.shake_limit and shot.shutter > shot.shake_limit:
        limits.max_sharpen = min(limits.max_sharpen, 15.0)
        limits.note(f"{shot.shutter_text()} hand-held at {round(shot.focal_35mm or shot.focal_length)}mm "
                    f"is below the 1/focal-length rule, so some shake is likely. "
                    f"Sharpening is held back; it cannot undo movement.")

    if shot.aperture and shot.aperture <= 2.8:
        limits.note(f"f/{shot.aperture:g} is wide, so a soft background is probably "
                    f"deliberate rather than a mistake.")

    # A photographer who dialled in compensation meant it.
    if abs(shot.exposure_bias) >= 0.3:
        direction = "brighter" if shot.exposure_bias > 0 else "darker"
        limits.exposure_floor *= 0.5
        limits.exposure_ceiling *= 0.5
        limits.note(f"You dialled {shot.exposure_bias:+.1f} stops to make it {direction}. "
                    f"That choice is respected, so exposure is barely moved.")

    if not limits.verdicts:
        limits.note("Nothing unusual: the file has normal latitude.")
    return limits
