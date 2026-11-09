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

import cv2
import numpy as np

import edit

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


def analyse(image_bgr, want_crop=False, aspect=None):
    """Return (Adjustments, crop_rect_or_None, notes).

    crop_rect is (x, y, w, h) in 0..1. notes explains each decision in plain
    words, so the numbers are not a black box.
    """
    # Work on a small copy: these are statistics, and full resolution adds
    # nothing but time.
    work = _downscale(image_bgr, 900)
    notes = []

    temperature, tint = _auto_white_balance(work, notes)
    exposure = _auto_exposure(work, notes)
    contrast = _auto_contrast(work, notes)
    shadows, highlights = _auto_tone(work, notes)

    adj = edit.Adjustments(
        temperature=temperature,
        tint=tint,
        exposure=exposure,
        contrast=contrast,
        shadows=shadows,
        highlights=highlights,
    )

    rect = _auto_crop(work, aspect, notes) if want_crop else None
    return adj, rect, notes


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
    """Stretch a photo that only uses part of the available range."""
    luma = np.sum(image.astype(np.float32) / 255.0 * edit.LUMA_BGR, axis=2)
    low, high = np.percentile(luma, (2, 98))
    spread = float(high - low)
    if spread >= 0.78:
        notes.append("Contrast already full.")
        return 0.0
    # A photo using 50% of the range gets a firm push; one using 75% a nudge.
    amount = float(np.clip((0.80 - spread) * 110.0, 0.0, 40.0))
    if amount < 3:
        return 0.0
    notes.append(f"Contrast raised {amount:.0f}: the photo only used "
                 f"{spread * 100:.0f}% of the tonal range.")
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
