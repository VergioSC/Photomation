#!/usr/bin/env python3
"""Read the exposure triangle and friends out of a photo's metadata.

The exposure triangle is aperture, shutter speed and ISO. Together they decide
how a photo was captured, and therefore what can still be done to it:

  ISO           how much the signal was amplified. High ISO means noise, and
                noise is what limits how far shadows can be lifted.
  shutter speed how long the sensor was exposed. Too slow for the lens and the
                picture is already softened by camera shake, which no amount
                of sharpening fixes - it only makes the blur crunchy.
  aperture      how wide the lens opened. Wide apertures throw the background
                out of focus on purpose, so "this photo is not sharp overall"
                can be the intent rather than a fault.
"""

from dataclasses import dataclass

try:
    from PIL import Image, ExifTags

    _TAG_IDS = {name: tag for tag, name in ExifTags.TAGS.items()}
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    _TAG_IDS = {}

EXIF_SUB_IFD = 0x8769


@dataclass(frozen=True)
class Shot:
    """What the camera recorded about how a photo was taken."""

    camera: str = ""
    iso: int = 0
    shutter: float = 0.0            # seconds
    aperture: float = 0.0           # f-number
    focal_length: float = 0.0       # millimetres, as marked on the lens
    focal_35mm: float = 0.0         # equivalent on full frame, for shake maths
    exposure_bias: float = 0.0      # stops the photographer dialled in
    flash: bool = False

    @property
    def known(self):
        """False when there is no usable metadata, e.g. an exported JPEG."""
        return bool(self.iso or self.shutter or self.aperture)

    @property
    def shake_limit(self):
        """The slowest shutter usually safe hand-held, in seconds.

        The old rule is 1 over the focal length in 35mm terms. It is a rough
        guide, not physics, and it ignores stabilisation.
        """
        focal = self.focal_35mm or self.focal_length
        return 1.0 / focal if focal else 0.0

    def shutter_text(self):
        if not self.shutter:
            return "unknown"
        if self.shutter >= 1:
            return f"{self.shutter:g}s"
        return f"1/{round(1 / self.shutter)}s"

    def summary(self):
        if not self.known:
            return "no exposure data"
        bits = []
        if self.aperture:
            bits.append(f"f/{self.aperture:g}")
        if self.shutter:
            bits.append(self.shutter_text())
        if self.iso:
            bits.append(f"ISO {self.iso}")
        if self.focal_35mm:
            bits.append(f"{round(self.focal_35mm)}mm eq")
        return ", ".join(bits)


def read(path):
    """Read a Shot from a photo. Never raises: unknown data is just absent."""
    if not PIL_AVAILABLE:
        return Shot()
    try:
        with Image.open(path) as handle:
            top = handle.getexif()
            sub = top.get_ifd(EXIF_SUB_IFD)
    except Exception:
        return Shot()

    def value(name, default=0.0):
        tag = _TAG_IDS.get(name)
        raw = sub.get(tag, top.get(tag)) if tag else None
        if raw is None:
            return default
        try:
            return float(raw)
        except (TypeError, ValueError):
            # ISO is occasionally a tuple of readings.
            try:
                return float(raw[0])
            except Exception:
                return default

    make = str(top.get(271, "") or "").strip()
    model = str(top.get(272, "") or "").strip()
    camera = " ".join(part for part in (make, model) if part)
    if model.upper().startswith(make.upper()) and make:
        camera = model

    return Shot(
        camera=camera,
        iso=int(value("ISOSpeedRatings") or value("PhotographicSensitivity")),
        shutter=value("ExposureTime"),
        aperture=value("FNumber"),
        focal_length=value("FocalLength"),
        focal_35mm=value("FocalLengthIn35mmFilm"),
        exposure_bias=value("ExposureBiasValue"),
        flash=bool(int(value("Flash"))) if value("Flash") else False,
    )
