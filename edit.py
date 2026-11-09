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
    clarity: float = 0.0       # -100 soften .. +100 punchy midtone contrast
    grain: float = 0.0         # 0 .. 100 film grain
    grain_size: float = 1.0    # 1 = fine, 2 = coarse
    sharpen: float = 0.0       # -100 soften .. +100 sharpen
    film: str = ""             # a film look name from FILM_LOOKS, or "" for none
    lut_strength: float = 100.0  # how much of the LUT to mix in, 0 .. 100

    @classmethod
    def from_dict(cls, data):
        """Build from untrusted JSON: unknown keys ignored, bad values dropped."""
        known = {f.name for f in fields(cls)}
        clean = {}
        for key, value in (data or {}).items():
            if key not in known:
                continue
            if key == "film":
                name = str(value or "")
                if name in FILM_LOOKS:
                    clean[key] = name
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
            if f.name not in ("lut_strength", "grain_size")
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

    rgbish = np.clip(rgbish, 0.0, 1.0)
    out = (rgbish * 255.0 + 0.5).astype(np.uint8)

    # The film look is the base character, so it goes on before the user's own
    # colour tweaks - exactly as a camera applies its simulation first.
    if adj.film:
        out = apply_film_look(out, adj.film)

    if adj.clarity:
        out = _clarity(out, adj.clarity)

    if adj.vibrance or adj.saturation:
        x = out.astype(np.float32) / 255.0
        x = _colour(x, adj.vibrance, adj.saturation)
        out = (np.clip(x, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)

    if lut is not None and adj.lut_strength > 0:
        import lut as lut_module
        graded = lut_module.apply_lut(out, lut)
        strength = min(adj.lut_strength, 100.0) / 100.0
        if strength < 1.0:
            out = cv2.addWeighted(graded, strength, out, 1.0 - strength, 0.0)
        else:
            out = graded

    if adj.sharpen:
        out = _sharpen(out, adj.sharpen)
    # Grain last: anything after it would smear or sharpen the grain itself.
    if adj.grain > 0:
        out = _grain(out, adj.grain, adj.grain_size)
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
    """Unsharp mask. Negative softens instead, which some film looks want."""
    # One formula covers both directions: a negative strength blends towards
    # the blurred copy instead of away from it.
    strength = amount / 100.0
    blurred = cv2.GaussianBlur(image_bgr, (0, 0), sigmaX=1.2)
    return cv2.addWeighted(image_bgr, 1.0 + strength, blurred, -strength, 0.0)

# ---------------------------------------------------------------- film looks
#
# These are APPROXIMATIONS of the Fujifilm simulations, not the real thing.
# Fujifilm's simulations run inside the camera on sensor data; this works on an
# already-processed image. Each look is three parts:
#   matrix     a 3x3 channel mixer (in RGB order) for the colour response
#   curve      control points for a tone curve, (input, output) in 0..1
#   saturation a final multiplier on colourfulness
# Monochrome looks convert with their own channel weights, which is what gives
# different black-and-white films their different skies and skin tones.

FILM_LOOKS = {
    "provia": {
        "label": "Provia / Standard",
        "curve": [(0, 0), (0.25, 0.25), (0.75, 0.76), (1, 1)],
        "saturation": 1.02,
    },
    "velvia": {
        "label": "Velvia / Vivid",
        "curve": [(0, 0), (0.22, 0.18), (0.5, 0.5), (0.78, 0.84), (1, 1)],
        "saturation": 1.35,
        "matrix": [[1.06, -0.03, -0.03],
                   [-0.03, 1.05, -0.02],
                   [-0.04, -0.04, 1.08]],
    },
    "astia": {
        "label": "Astia / Soft",
        "curve": [(0, 0.012), (0.25, 0.27), (0.75, 0.75), (1, 0.985)],
        "saturation": 1.12,
        "matrix": [[1.02, 0.0, -0.02],
                   [-0.01, 1.01, 0.0],
                   [-0.01, 0.0, 1.01]],
    },
    "classic-chrome": {
        "label": "Classic Chrome",
        "curve": [(0, 0.02), (0.22, 0.2), (0.5, 0.5), (0.8, 0.82), (1, 0.98)],
        "saturation": 0.82,
        # Muted, slightly ochre response: red borrows a little green, blue is held back.
        "matrix": [[0.98, 0.06, -0.04],
                   [-0.02, 1.0, 0.02],
                   [0.0, 0.05, 0.92]],
    },
    "classic-neg": {
        "label": "Classic Negative",
        "curve": [(0, 0.03), (0.2, 0.17), (0.5, 0.5), (0.82, 0.86), (1, 0.97)],
        "saturation": 1.08,
        # Cyan-leaning shadows, warm midtones: the drugstore-film look.
        "matrix": [[1.04, 0.0, -0.04],
                   [-0.05, 1.02, 0.03],
                   [0.02, -0.06, 1.04]],
    },
    "eterna": {
        "label": "Eterna / Cinema",
        "curve": [(0, 0.05), (0.25, 0.28), (0.5, 0.5), (0.75, 0.72), (1, 0.94)],
        "saturation": 0.78,
        "matrix": [[0.99, 0.03, -0.02],
                   [-0.01, 0.99, 0.02],
                   [0.01, 0.03, 0.96]],
    },
    "nostalgic-neg": {
        "label": "Nostalgic Negative",
        "curve": [(0, 0.04), (0.25, 0.26), (0.5, 0.52), (0.8, 0.84), (1, 0.98)],
        "saturation": 1.05,
        "matrix": [[1.06, 0.02, -0.08],
                   [-0.01, 1.0, 0.01],
                   [-0.02, 0.02, 0.96]],
    },
    "pro-neg-std": {
        "label": "Pro Neg Standard",
        "curve": [(0, 0.015), (0.25, 0.26), (0.75, 0.74), (1, 0.99)],
        "saturation": 0.9,
        "matrix": [[1.0, 0.02, -0.02],
                   [-0.01, 1.0, 0.01],
                   [0.0, 0.01, 0.99]],
    },
    "pro-neg-hi": {
        "label": "Pro Neg High",
        "curve": [(0, 0.0), (0.22, 0.21), (0.78, 0.8), (1, 1)],
        "saturation": 0.96,
        "matrix": [[1.01, 0.02, -0.03],
                   [-0.01, 1.0, 0.01],
                   [0.0, 0.01, 0.99]],
    },
    "acros": {
        "label": "Acros (black & white)",
        "curve": [(0, 0.0), (0.25, 0.23), (0.5, 0.51), (0.78, 0.83), (1, 1)],
        "monochrome": [0.26, 0.60, 0.14],     # R, G, B weights
    },
    "monochrome": {
        "label": "Monochrome",
        "curve": [(0, 0), (0.5, 0.5), (1, 1)],
        "monochrome": [0.299, 0.587, 0.114],
    },
    "acros-r": {
        "label": "Acros + red filter",
        "curve": [(0, 0.0), (0.25, 0.22), (0.5, 0.5), (0.78, 0.84), (1, 1)],
        "monochrome": [0.62, 0.32, 0.06],     # darkens blue skies
    },
    "acros-g": {
        "label": "Acros + green filter",
        "curve": [(0, 0.0), (0.25, 0.23), (0.5, 0.51), (0.78, 0.83), (1, 1)],
        "monochrome": [0.2, 0.68, 0.12],      # kinder to skin
    },
    "sepia": {
        "label": "Sepia",
        "curve": [(0, 0.02), (0.5, 0.5), (1, 0.98)],
        "monochrome": [0.299, 0.587, 0.114],
        "tone": [0.78, 0.95, 1.18],           # B, G, R multipliers after mono
    },
}


def film_look_names():
    """(key, label) for every look, for building a menu."""
    return [(key, look["label"]) for key, look in FILM_LOOKS.items()]


def _curve_table(points):
    """Build a 256-entry lookup table from control points.

    A 1D curve is exactly what cv2.LUT is for, so the per-pixel work is a table
    lookup rather than maths on every pixel.
    """
    xs = np.array([p[0] for p in points], dtype=np.float32)
    ys = np.array([p[1] for p in points], dtype=np.float32)
    grid = np.linspace(0.0, 1.0, 256, dtype=np.float32)
    out = np.interp(grid, xs, ys).astype(np.float32)
    return np.clip(out * 255.0 + 0.5, 0, 255).astype(np.uint8)


_CURVE_CACHE = {}


def _cached_curve(key, points):
    if key not in _CURVE_CACHE:
        _CURVE_CACHE[key] = _curve_table(points)
    return _CURVE_CACHE[key]


def apply_film_look(image_bgr, name):
    """Apply one film look to a BGR uint8 image."""
    look = FILM_LOOKS.get(name)
    if look is None:
        return image_bgr

    out = image_bgr
    mono = look.get("monochrome")
    if mono:
        # Weights are given R, G, B; the image is B, G, R.
        weights = np.array([mono[2], mono[1], mono[0]], dtype=np.float32)
        grey = np.sum(out.astype(np.float32) * weights, axis=2)
        grey = np.clip(grey, 0, 255).astype(np.uint8)
        out = cv2.merge([grey, grey, grey])
    elif "matrix" in look:
        m = np.asarray(look["matrix"], dtype=np.float32)      # rows/cols in RGB
        # Convert the RGB matrix to BGR order so it can multiply the image directly.
        flip = np.array([[0, 0, 1], [0, 1, 0], [1, 0, 0]], dtype=np.float32)
        m_bgr = flip @ m @ flip
        out = cv2.transform(out, m_bgr)

    out = cv2.LUT(out, _cached_curve(name, look["curve"]))

    if look.get("tone"):
        out = cv2.transform(out, np.diag(np.asarray(look["tone"], dtype=np.float32)))
    elif look.get("saturation", 1.0) != 1.0 and not mono:
        x = out.astype(np.float32) / 255.0
        grey = np.sum(x * LUMA_BGR, axis=2, keepdims=True)
        x = grey + (x - grey) * float(look["saturation"])
        out = (np.clip(x, 0, 1) * 255.0 + 0.5).astype(np.uint8)
    return out


def _clarity(image_bgr, amount):
    """Midtone local contrast: a wide unsharp mask, applied to brightness only.

    Done on the luminance channel so colours are not dragged around, and with a
    big radius so it shapes volume rather than sharpening edges.
    """
    strength = amount / 100.0
    lab = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2LAB)
    L = lab[:, :, 0].astype(np.float32)
    blurred = cv2.GaussianBlur(L, (0, 0), sigmaX=max(image_bgr.shape[:2]) / 110.0)
    lab[:, :, 0] = np.clip(L + (L - blurred) * strength, 0, 255).astype(np.uint8)
    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)


def _grain(image_bgr, amount, size):
    """Add film grain.

    Grain is strongest in the midtones: film has little in clean highlights and
    little in blocked-up shadows, and noise that ignores that looks like sensor
    noise instead.
    """
    height, width = image_bgr.shape[:2]
    rng = np.random.default_rng(12345)          # fixed, so a preview matches the export
    noise = rng.normal(0.0, 1.0, (height, width)).astype(np.float32)
    if size > 1.0:
        noise = cv2.GaussianBlur(noise, (0, 0), sigmaX=float(size) - 0.6)
        noise *= float(size)                    # blurring lowers the spread; put it back

    x = image_bgr.astype(np.float32)
    luma = np.sum(x * LUMA_BGR, axis=2) / 255.0
    midtones = 1.0 - np.abs(luma - 0.5) * 2.0   # 1 at mid grey, 0 at both ends
    strength = (amount / 100.0) * 22.0
    return np.clip(x + (noise * midtones * strength)[:, :, None], 0, 255).astype(np.uint8)


def crop_image(image_bgr, rect):
    """Crop to `rect`, given as (x, y, w, h) in 0..1 of the image size."""
    if not rect:
        return image_bgr
    height, width = image_bgr.shape[:2]
    x, y, w, h = (float(v) for v in rect)
    left = max(0, min(width - 1, int(round(x * width))))
    top = max(0, min(height - 1, int(round(y * height))))
    right = max(left + 1, min(width, int(round((x + w) * width))))
    bottom = max(top + 1, min(height, int(round((y + h) * height))))
    return image_bgr[top:bottom, left:right]
