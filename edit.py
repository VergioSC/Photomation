#!/usr/bin/env python3
"""Photo adjustments: white balance, exposure, tone, colour, sharpening.

Everything here takes and returns a BGR uint8 image, the form OpenCV uses.

Why some of this is done in "linear light": a JPEG does not store brightness
directly. It stores sRGB values, which are bent so dark tones get more of the
range (that is what the human eye needs). Doubling an sRGB number does NOT
double the light. Exposure and white balance are physical, light-like changes,
so they are done after undoing that bend, then the bend is reapplied. Contrast,
saturation and LUTs are judged by eye on the final picture, so they are applied
to the bent (display) values, which is where they are designed to work.
"""

from dataclasses import dataclass, fields

import cv2
import numpy as np

# Rec.709 luminance weights, in BGR order to match OpenCV's channel order.
LUMA_BGR = np.array([0.0722, 0.7152, 0.2126], dtype=np.float32)


@dataclass(frozen=True)
class Adjustments:
    """Every control, scaled so 0 always means "change nothing"."""

    temperature: float = 0.0   # -100 cooler/blue .. +100 warmer/amber
    tint: float = 0.0          # -100 green .. +100 magenta
    exposure: float = 0.0      # stops, -3 .. +3
    contrast: float = 0.0      # -100 .. +100
    highlights: float = 0.0    # -100 recover .. +100 lift
    shadows: float = 0.0       # -100 crush .. +100 lift
    vibrance: float = 0.0      # -100 .. +100, spares already-saturated colour
    saturation: float = 0.0    # -100 greyscale .. +100
    sharpen: float = 0.0       # 0 .. 100
    lut_strength: float = 100.0  # how much of the LUT to mix in, 0 .. 100

    @classmethod
    def from_dict(cls, data):
        """Build from untrusted JSON: unknown keys ignored, bad values dropped."""
        known = {f.name: f.type for f in fields(cls)}
        clean = {}
        for key, value in (data or {}).items():
            if key not in known:
                continue
            try:
                clean[key] = float(value)
            except (TypeError, ValueError):
                continue
        return cls(**clean)

    def is_identity(self):
        """True when nothing would change, so the work can be skipped."""
        return all(
            getattr(self, f.name) == f.default
            for f in fields(self)
            if f.name != "lut_strength"
        )


def srgb_to_linear(x):
    """Undo the sRGB curve: display values -> light."""
    return np.where(x <= 0.04045, x / 12.92, np.power((x + 0.055) / 1.055, 2.4))


def linear_to_srgb(x):
    """Reapply the sRGB curve: light -> display values."""
    x = np.clip(x, 0.0, None)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * np.power(x, 1 / 2.4) - 0.055)


def apply_adjustments(image_bgr, adj, lut=None):
    """Apply `adj` (and optionally a LUT) to a BGR uint8 image.

    Order matters and follows what photo editors do: fix the light first
    (white balance, exposure), then shape the tones, then the colour, then the
    look (LUT), and sharpen last so earlier steps do not amplify its halos.
    """
    if adj.is_identity() and lut is None:
        return image_bgr

    rgbish = image_bgr.astype(np.float32) / 255.0

    # --- physical, light-like steps, done in linear light ---
    if adj.temperature or adj.tint or adj.exposure:
        linear = srgb_to_linear(rgbish)
        linear = _white_balance(linear, adj.temperature, adj.tint)
        if adj.exposure:
            linear = linear * (2.0 ** adj.exposure)
        rgbish = linear_to_srgb(linear)

    # --- tonal steps, judged on display values ---
    if adj.shadows or adj.highlights:
        rgbish = _tone_regions(rgbish, adj.shadows, adj.highlights)
    if adj.contrast:
        rgbish = _contrast(rgbish, adj.contrast)
    if adj.vibrance or adj.saturation:
        rgbish = _colour(rgbish, adj.vibrance, adj.saturation)

    rgbish = np.clip(rgbish, 0.0, 1.0)
    out = (rgbish * 255.0 + 0.5).astype(np.uint8)

    if lut is not None and adj.lut_strength > 0:
        import lut as lut_module
        graded = lut_module.apply_lut(out, lut)
        strength = min(adj.lut_strength, 100.0) / 100.0
        if strength < 1.0:
            out = cv2.addWeighted(graded, strength, out, 1.0 - strength, 0.0)
        else:
            out = graded

    if adj.sharpen > 0:
        out = _sharpen(out, adj.sharpen)
    return out


def _white_balance(linear, temperature, tint):
    """Scale the channels. Warmer means more red and less blue."""
    t = temperature / 100.0
    g = tint / 100.0
    # +-30% on red and blue at the extremes: strong, still usable.
    # Positive tint means magenta, which is the same as holding green back.
    scale = np.array([1.0 - 0.30 * t,     # blue
                      1.0 - 0.20 * g,     # green
                      1.0 + 0.30 * t],    # red
                     dtype=np.float32)
    return linear * scale


def _tone_regions(x, shadows, highlights):
    """Lift or crush dark and bright areas separately.

    Masks are smooth, so a pixel halfway between shadow and highlight is barely
    touched by either and no hard edge appears.
    """
    luma = np.sum(x * LUMA_BGR, axis=2, keepdims=True)
    if shadows:
        mask = np.clip(1.0 - luma / 0.5, 0.0, 1.0) ** 2
        x = x + mask * (shadows / 100.0) * 0.35
    if highlights:
        mask = np.clip((luma - 0.5) / 0.5, 0.0, 1.0) ** 2
        x = x + mask * (highlights / 100.0) * 0.35
    return x


def _contrast(x, amount):
    """Push values away from (or towards) mid grey."""
    factor = 1.0 + amount / 100.0
    return (x - 0.5) * factor + 0.5


def _colour(x, vibrance, saturation):
    """Saturation moves every colour; vibrance spares the already-vivid ones."""
    grey = np.sum(x * LUMA_BGR, axis=2, keepdims=True)
    delta = x - grey

    if saturation:
        delta = delta * (1.0 + saturation / 100.0)

    if vibrance:
        # How colourful each pixel already is, 0..1.
        current = np.clip(np.max(np.abs(delta), axis=2, keepdims=True) * 2.0, 0.0, 1.0)
        # Dull pixels get the full push, vivid ones almost none.
        boost = (vibrance / 100.0) * (1.0 - current)
        delta = delta * (1.0 + boost)

    return grey + delta


def _sharpen(image_bgr, amount):
    """Unsharp mask: subtract a blurred copy to make edges crisper."""
    strength = amount / 100.0
    blurred = cv2.GaussianBlur(image_bgr, (0, 0), sigmaX=1.2)
    return cv2.addWeighted(image_bgr, 1.0 + strength, blurred, -strength, 0.0)
