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

# Everything measured in pixels - a blur radius, a grain size - is defined
# against an image this many pixels along its longest edge, then scaled to
# whatever is actually being worked on.
#
# Without this, the same slider means different things at different sizes, and
# the preview stops telling the truth. Measured on a 3120px photo: grain at 60
# added 10.6x the fine detail in a 1000px preview but only 1.7x in the export,
# because one-pixel grain on a 3120px frame vanishes the moment it is viewed or
# resized. Sharpening was 2.7x against 1.6x for the same reason.
REFERENCE_LONG_EDGE = 1000.0


def _scale_of(image):
    """How much bigger this image is than the reference size.

    Below 1 for a small preview, above 1 for a full-size export. The floor only
    stops a radius reaching zero on a thumbnail.
    """
    return max(0.25, max(image.shape[:2]) / REFERENCE_LONG_EDGE)


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
    # Selective adjustments. These need region masks to do anything, and are
    # ignored without them, so a caller that has not worked out the regions
    # simply gets the global edit.
    sky_exposure: float = 0.0        # -100 .. +100, brightness of the sky only
    foreground_exposure: float = 0.0  # -100 .. +100, everything that is not sky
    sky_contrast: float = 0.0        # -100 .. +100

    dehaze: float = 0.0        # 0 .. 100, cuts atmospheric veiling light
    black_point: float = 0.0   # 0 .. 100, how firmly to set a true black
    white_point: float = 0.0   # 0 .. 100, how firmly to reach a true white
    noise_reduction: float = 0.0  # 0 .. 100
    clarity: float = 0.0       # -100 soften .. +100 punchy midtone contrast
    structure: float = 0.0     # -100 .. +100 fine detail contrast
    vignette: float = 0.0      # -100 brighter corners .. +100 darker corners
    grain: float = 0.0         # 0 .. 100 film grain
    grain_size: float = 1.0    # 1 = fine, 2 = coarse
    sharpen: float = 0.0       # -100 soften .. +100 sharpen
    film: str = ""             # a film look name from FILM_LOOKS, or "" for none
    film_strength: float = 100.0  # how much of the film look to mix in, 0 .. 100
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

    @property
    def has_regional(self):
        return bool(self.sky_exposure or self.foreground_exposure or self.sky_contrast)

    def is_identity(self):
        """True when nothing would change, so the work can be skipped."""
        return all(
            getattr(self, f.name) == f.default
            for f in fields(self)
            if f.name not in ("lut_strength", "film_strength", "grain_size")
        )


def srgb_to_linear(x):
    """Undo the sRGB curve: display values -> light."""
    return np.where(x <= 0.04045, x / 12.92, np.power((x + 0.055) / 1.055, 2.4))


def linear_to_srgb(x):
    """Reapply the sRGB curve: light -> display values."""
    x = np.clip(x, 0.0, None)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * np.power(x, 1 / 2.4) - 0.055)


def _soft_clip(x, knee=0.85):
    """Roll everything above `knee` into the headroom left, instead of clipping.

    Every step that brightens a channel - warming the white balance, pushing
    saturation, a clarity halo on the bright side of an edge - can carry a
    pixel past white, and `np.clip` then throws the difference away. Nothing
    individually looks unreasonable, but they stack: on one photo auto had the
    frame at 0.23% clipped and a film recipe took it to 1.68%, and the clipped
    part is the part a viewer reads as "overexposed".

    This compresses the range [knee, peak] into [knee, 1] instead. It is
    adaptive and does nothing when nothing is over: with a peak of exactly 1
    the exponent below is 1 and the curve is the identity, so a photo that was
    not clipping is not darkened. The slope at the knee is 1, so no edge
    appears where the shoulder starts, and the curve is monotonic, so the
    order of tones is kept.
    """
    # From the brightest value present, but capped: one hot pixel from a lamp
    # should not flatten the shoulder for the whole photo.
    peak = min(2.0, float(x.max()))
    if peak <= 1.0:
        return np.clip(x, 0.0, 1.0)

    span = peak - knee
    room = 1.0 - knee
    t = np.clip((x - knee) / span, 0.0, 1.0)
    shouldered = knee + room * (1.0 - (1.0 - t) ** (span / room))
    return np.clip(np.where(x > knee, shouldered, x), 0.0, 1.0)


def apply_regional(image_bgr, adj, scene):
    """Apply the selective parts of `adj` using `scene` masks.

    This is the thing global sliders cannot do. Darkening a sky for drama also
    buries the ground in front of it, so an editor darkens the sky and lifts
    the foreground separately. With a mask, so can we.
    """
    if scene is None or not adj.has_regional:
        return image_bgr

    out = image_bgr
    height, width = out.shape[:2]
    # A band at a time. Done whole, this held the photo, an adjusted copy of it
    # and the blend weights as full-resolution float32 at once: on a 24MP frame
    # that is over 650MB and it was enough to get the process killed. Exposure
    # and contrast are per-pixel, so a band gives exactly the same answer.
    band_rows = max(1, 4_000_000 // max(1, width))

    for amount, mask, extra in (
        (adj.sky_exposure, scene.sky, {"contrast": adj.sky_contrast}),
        (adj.foreground_exposure, scene.foreground, {}),
    ):
        if not amount and not extra.get("contrast"):
            continue
        if mask is None or float(mask.mean()) < 0.01:
            continue
        # Full scale is two stops: recovering a foreground that dehazing has
        # darkened routinely needs more than one.
        layer_adj = Adjustments(exposure=amount / 50.0, **extra)
        blended = np.empty_like(out)
        for top in range(0, height, band_rows):
            rows = slice(top, min(height, top + band_rows))
            source = out[rows]
            layer = apply_adjustments(source, layer_adj)
            weight = mask[rows][:, :, None]
            blended[rows] = np.clip(
                source.astype(np.float32) * (1.0 - weight)
                + layer.astype(np.float32) * weight, 0, 255).astype(np.uint8)
        out = blended
    return out


def apply_adjustments(image_bgr, adj, lut=None, scene=None):
    """Apply `adj` (and optionally a LUT) to a BGR uint8 image.

    Order matters and follows what photo editors do: fix the light first
    (white balance, exposure), then shape the tones, then the colour, then the
    look (LUT), and sharpen last so earlier steps do not amplify its halos.
    """
    if adj.is_identity() and lut is None and not (scene is not None and adj.has_regional):
        return image_bgr

    rgbish = image_bgr.astype(np.float32) / 255.0

    # --- physical, light-like steps, done in linear light ---
    if adj.temperature or adj.tint or adj.exposure:
        linear = srgb_to_linear(rgbish)
        linear = _white_balance(linear, adj.temperature, adj.tint)
        if adj.exposure:
            linear = linear * (2.0 ** adj.exposure)
        rgbish = linear_to_srgb(linear)

    # Noise first, before anything amplifies it. Raising exposure, adding
    # contrast and pushing saturation all make existing noise louder, and no
    # amount of cleaning afterwards puts that back.
    if adj.noise_reduction > 0:
        rgbish = np.clip(rgbish, 0.0, 1.0)
        cleaned = _denoise((rgbish * 255.0 + 0.5).astype(np.uint8), adj.noise_reduction)
        rgbish = cleaned.astype(np.float32) / 255.0

    # Haze is physical veiling light sitting on top of the scene, so it comes
    # off before the tones are shaped - otherwise every later step is working
    # on a washed-out version of the picture.
    if adj.dehaze > 0:
        rgbish = np.clip(rgbish, 0.0, 1.0)
        rgbish = _dehaze((rgbish * 255.0 + 0.5).astype(np.uint8),
                         adj.dehaze).astype(np.float32) / 255.0

    if adj.black_point or adj.white_point:
        rgbish = _levels(rgbish, adj.black_point, adj.white_point)

    # --- tonal steps, judged on display values ---
    if adj.shadows or adj.highlights:
        rgbish = _tone_regions(rgbish, adj.shadows, adj.highlights)
    if adj.contrast:
        rgbish = _contrast(rgbish, adj.contrast)

    rgbish = _soft_clip(rgbish)
    out = (rgbish * 255.0 + 0.5).astype(np.uint8)

    # The film look is the base character, so it goes on before the user's own
    # colour tweaks - exactly as a camera applies its simulation first.
    if adj.film and adj.film_strength > 0:
        looked = apply_film_look(out, adj.film)
        # Partly applied, so one preset can be half a Velvia rather than all
        # of it. Blended on the finished pixels, which is the only honest way
        # to dilute a channel mixer and a tone curve together.
        mix = min(adj.film_strength, 100.0) / 100.0
        out = looked if mix >= 1.0 else cv2.addWeighted(looked, mix, out, 1.0 - mix, 0.0)

    if adj.clarity:
        out = _clarity(out, adj.clarity)
    if adj.structure:
        out = _structure(out, adj.structure)

    if adj.vibrance or adj.saturation:
        x = out.astype(np.float32) / 255.0
        x = _colour(x, adj.vibrance, adj.saturation)
        out = (_soft_clip(x) * 255.0 + 0.5).astype(np.uint8)

    if lut is not None and adj.lut_strength > 0:
        import lut as lut_module
        graded = lut_module.apply_lut(out, lut)
        strength = min(adj.lut_strength, 100.0) / 100.0
        if strength < 1.0:
            out = cv2.addWeighted(graded, strength, out, 1.0 - strength, 0.0)
        else:
            out = graded

    # Selective work goes in after the global edit, before grain.
    if scene is not None and adj.has_regional:
        out = apply_regional(out, adj, scene)

    if adj.sharpen:
        out = _sharpen(out, adj.sharpen)
    if adj.vignette:
        out = _vignette(out, adj.vignette)
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

    The obvious version - add a fixed amount wherever a mask says "this is a
    shadow" - breaks in two ways that are easy to miss and impossible to undo.
    It clips: lifting highlights pushed 0.90 and 0.97 both to pure white, so
    two different tones became the same pixel. And it inverts: recovering
    highlights left 0.97 DARKER than 0.90, because the mask grew faster than
    the tone it was subtracting from. An inverted gradient in a sky reads as
    posterised mush, and no later slider puts the order back.

    So each direction here is a curve that cannot do either. Every one is
    monotonic - brighter in always means brighter out - and every one is
    bounded, because the amount it moves a pixel is multiplied by the room that
    pixel actually has left (1 - x going up, x going down). Black stays black
    when crushing, white stays white when lifting, and nothing is ever fused.
    """
    luma = np.sum(x * LUMA_BGR, axis=2, keepdims=True)
    L = np.clip(luma, 0.0, 1.0)
    delta = np.zeros_like(L)

    if shadows:
        s = abs(shadows) / 100.0
        low = (1.0 - L) ** 4          # 1 at black, 0 at white
        if shadows > 0:
            # Lift. Capped at a quarter so the curve stays monotonic: the
            # slope is 1 - 4k(1-x)^3, which only stays positive for k <= 0.25.
            delta += s * 0.25 * low
        else:
            # Crush. Scaled by x itself, so true black has nothing to lose.
            delta -= s * low * L

    if highlights:
        h = abs(highlights) / 100.0
        high = L ** 4                 # 0 at black, 1 at white
        if highlights > 0:
            # Lift. Scaled by the headroom, so white cannot be exceeded.
            delta += h * high * (1.0 - L)
        else:
            # Recover. Capped at a quarter for the same monotonicity reason.
            delta -= h * 0.25 * high

    # Added equally to every channel, so the colour of a pixel is unchanged
    # and only its brightness moves.
    return x + delta


def _contrast(x, amount):
    """An S-curve, the way a darkroom print or a film stock adds contrast.

    The obvious version - multiply the distance from mid grey - drives darks
    below zero and brights above one, where they clip. At a moderate +27 that
    flattened everything under 10% brightness into identical black, which is
    what turned a dusk beach into a silhouette.

    This instead bends the curve: it steepens through the midtones and rolls
    off towards both ends, so pure black stays black, pure white stays white,
    and nothing is ever thrown away.
    """
    t = amount / 100.0
    if t > 0:
        # smoothstep: 0->0, 1->1, steeper in the middle, flat at both ends.
        shaped = x * x * (3.0 - 2.0 * x)
    else:
        # Flatten towards mid grey without collapsing the ends.
        shaped = 0.5 + (x - 0.5) * 0.45
        t = -t
    return x + (shaped - x) * t


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
    blurred = cv2.GaussianBlur(image_bgr, (0, 0), sigmaX=1.2 * _scale_of(image_bgr))
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
    "solarized": {
        "label": "Solarized (darkroom)",
        # Sabattier effect: tones rise normally, then REVERSE near the top, which
        # is what a darkroom print does when re-exposed to light mid-development.
        # np.interp is happy with a curve that goes back down.
        "curve": [(0, 0.02), (0.35, 0.52), (0.62, 0.92), (0.76, 0.58), (1, 0.1)],
        "monochrome": [0.299, 0.587, 0.114],
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
        out = (_soft_clip(x) * 255.0 + 0.5).astype(np.uint8)
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
    # Shouldered, not clipped: the bright side of every edge overshoots, and
    # clipping those overshoots is what turns clarity into white blotches.
    lab[:, :, 0] = (_soft_clip((L + (L - blurred) * strength) / 255.0) * 255.0).astype(np.uint8)
    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)


def _grain(image_bgr, amount, size):
    """Add film grain.

    Grain is strongest in the midtones: film has little in clean highlights and
    little in blocked-up shadows, and noise that ignores that looks like sensor
    noise instead.
    """
    height, width = image_bgr.shape[:2]
    rng = np.random.default_rng(12345)          # fixed, so a preview matches the export

    # Grain is drawn at the reference size and stretched up, so one grain is
    # the same share of the frame whatever the resolution. Drawing it per pixel
    # instead made grain that was obvious in the preview and invisible in a
    # full-size export.
    grain_scale = _scale_of(image_bgr) * max(1.0, float(size))
    small = (max(2, int(round(width / grain_scale))),
             max(2, int(round(height / grain_scale))))
    noise = rng.normal(0.0, 1.0, (small[1], small[0])).astype(np.float32)
    if small != (width, height):
        # Grain finer than a pixel still matters: drawing it larger and
        # averaging down is what a real export looks like when it is viewed
        # small, so a preview that does the same tells the truth.
        shrinking = small[0] > width
        noise = cv2.resize(noise, (width, height),
                           interpolation=cv2.INTER_AREA if shrinking else cv2.INTER_CUBIC)
        # Stretching averages neighbours together, which narrows the spread.
        # Put it back, or coarse grain would also be faint grain.
        spread = float(noise.std())
        if spread > 1e-6:
            noise /= spread

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


def _structure(image_bgr, amount):
    """Fine-detail contrast: like clarity, but at the scale of texture.

    Clarity uses a wide radius and shapes volume. Structure uses a small one
    and brings out surface - fabric, brick, foliage - without the edge halos
    that sharpening at this strength would give.
    """
    strength = amount / 100.0
    lab = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2LAB)
    L = lab[:, :, 0].astype(np.float32)
    detail = L - cv2.GaussianBlur(L, (0, 0), sigmaX=2.5 * _scale_of(image_bgr))
    lab[:, :, 0] = (_soft_clip((L + detail * strength * 1.4) / 255.0) * 255.0).astype(np.uint8)
    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)


def _vignette(image_bgr, amount):
    """Darken or lighten the corners.

    The falloff is measured from the centre as a share of the half-diagonal, so
    it stays circular on any aspect ratio instead of stretching with the frame.
    """
    strength = amount / 100.0
    height, width = image_bgr.shape[:2]
    ys = (np.arange(height, dtype=np.float32) - (height - 1) / 2.0) / max(height / 2.0, 1)
    xs = (np.arange(width, dtype=np.float32) - (width - 1) / 2.0) / max(width / 2.0, 1)
    radius = np.sqrt(xs[None, :] ** 2 + ys[:, None] ** 2) / np.sqrt(2.0)
    falloff = np.clip(radius, 0.0, 1.0) ** 2
    gain = 1.0 - falloff * strength * 0.85
    return np.clip(image_bgr.astype(np.float32) * gain[:, :, None], 0, 255).astype(np.uint8)


def _denoise(image_bgr, amount):
    """Reduce noise, colour blotches first.

    High-ISO noise comes in two kinds. Colour noise - the red and green
    speckle in shadows - is the ugly one, and it lives at a coarse scale, so
    it can be cleaned cheaply by smoothing only the colour channels and
    leaving brightness alone. Detail survives because detail is brightness.

    Luminance noise is finer and sits on top of real texture, so removing it
    costs sharpness. It is only touched at higher settings, and more gently.
    """
    strength = max(0.0, min(100.0, amount)) / 100.0
    lab = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2LAB)
    L, a, b = cv2.split(lab)

    # Colour is low-frequency: shrink it, median away the speckle, put it back.
    scale = _scale_of(image_bgr)
    factor = int(2 + round(strength * 4))                 # 2 .. 6
    ksize = 3 + 2 * int(round(strength * 2 * scale))      # 3, 5, 7 ... at size
    height, width = a.shape
    small = (max(1, width // factor), max(1, height // factor))
    for channel in (a, b):
        shrunk = cv2.resize(channel, small, interpolation=cv2.INTER_AREA)
        shrunk = cv2.medianBlur(shrunk, ksize)
        channel[:] = cv2.resize(shrunk, (width, height), interpolation=cv2.INTER_LINEAR)

    # Luminance only once the setting is high enough to be worth the softness.
    if strength > 0.35:
        h = (strength - 0.35) / 0.65 * 13.0
        # Patch and search windows are pixel sizes, so they grow with the image
        # or a full-size photo would be cleaned at a finer scale than its preview.
        patch = 2 * int(round(3 * scale)) + 1
        search = 2 * int(round(10 * scale)) + 1
        L = cv2.fastNlMeansDenoising(L, None, float(h), patch, search)

    return cv2.cvtColor(cv2.merge([L, a, b]), cv2.COLOR_LAB2BGR)


def _dehaze(image_bgr, amount):
    """Remove atmospheric haze using the dark channel prior.

    The idea, from He, Sun and Tang (2009): in a clear outdoor photo, almost
    every small patch contains something very dark in at least one colour
    channel - a shadow, a dark surface, a saturated colour. Haze adds a grey
    veil of scattered light, so that darkest value rises. How far it has risen
    tells you how much haze sits between the camera and that patch, and the
    veil can be subtracted in proportion.

    This is what flat, milky distance shots actually need. No amount of
    contrast fixes them, because the haze is added light, not low contrast.
    """
    strength = max(0.0, min(100.0, amount)) / 100.0
    x = image_bgr.astype(np.float32) / 255.0
    height, width = x.shape[:2]

    # Work the estimate out at a modest size: haze varies slowly across a
    # scene, and this keeps a full-resolution photo affordable.
    scale = 480.0 / max(height, width)
    small = cv2.resize(x, (max(1, int(width * scale)), max(1, int(height * scale))),
                       interpolation=cv2.INTER_AREA) if scale < 1 else x

    patch = max(3, (min(small.shape[:2]) // 40) | 1)
    dark = cv2.erode(small.min(axis=2), np.ones((patch, patch), np.uint8))

    # Atmospheric light: the colour of the haze itself, taken from the haziest
    # pixels rather than simply the brightest, which could be a light source.
    flat = dark.ravel()
    count = max(1, flat.size // 1000)
    idx = np.argpartition(flat, -count)[-count:]
    air = np.clip(small.reshape(-1, 3)[idx].mean(axis=0), 0.1, 1.0)

    # Transmission: how much of the scene survives the haze.
    omega = 0.55 + 0.40 * strength
    transmission = 1.0 - omega * cv2.erode((small / air).min(axis=2),
                                           np.ones((patch, patch), np.uint8))
    # Soften the map so it does not leave halos along strong edges.
    transmission = cv2.GaussianBlur(transmission, (0, 0),
                                    sigmaX=max(small.shape[:2]) / 50.0)
    transmission = cv2.resize(transmission, (width, height),
                              interpolation=cv2.INTER_LINEAR)
    transmission = np.clip(transmission, 0.18, 1.0)[:, :, None]

    out = (x - air) / transmission + air
    return (np.clip(out, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)


def _levels(x, black_amount, white_amount):
    """Set a true black and a true white, the first move in any edit.

    A photo whose darkest pixel is grey looks washed out however much contrast
    is added. This finds where the real tones actually start and stop and maps
    them to the ends of the range - partially, by the amounts given, so it
    tightens the picture without clipping anything that was there.
    """
    luma = np.sum(x * LUMA_BGR, axis=2)
    low = float(np.percentile(luma, 0.2))
    high = float(np.percentile(luma, 99.8))
    if high - low < 0.05:
        return x

    target_low = low * (1.0 - black_amount / 100.0)
    target_high = high + (1.0 - high) * (white_amount / 100.0)
    if target_high - target_low < 1e-3:
        return x

    scale = (target_high - target_low) / (high - low)
    return (x - low) * scale + target_low
