#!/usr/bin/env python3
"""Photomation: blur-based photo culling.

Scores every photo in input/ by the variance of its Laplacian (a standard
sharpness proxy: the Laplacian is an edge filter, so a sharp photo has a wide
spread of strong edge responses and a high variance, while a blurry one is flat
and scores low). Photos are COPIED into output/keep or output/reject -
originals in input/ are never moved, renamed or deleted.

Supported: JPEG, PNG, HEIC/HEIF (iPhone) and camera RAW.

Usage:
    python cull.py [threshold]        # default threshold: 100
"""

import shutil
import sys
from pathlib import Path

import cv2
import numpy as np

INPUT_DIR = Path("input")
OUTPUT_DIR = Path("output")
KEEP_DIR = OUTPUT_DIR / "keep"
REJECT_DIR = OUTPUT_DIR / "reject"

# Every photo is scored at this size so the result does not depend on megapixels.
NORMALIZED_LONGEST_SIDE = 1000

SIMPLE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
HEIF_EXTENSIONS = {".heic", ".heif"}
RAW_EXTENSIONS = {
    ".cr2", ".cr3", ".nef", ".arw", ".dng",
    ".raf", ".orf", ".rw2", ".pef", ".srw",
}
EXTENSIONS = SIMPLE_EXTENSIONS | HEIF_EXTENSIONS | RAW_EXTENSIONS

DEFAULT_THRESHOLD = 100.0

# Optional dependencies: only needed for the formats they handle, so a missing
# one degrades to a clear "skipped" message rather than crashing the whole run.
try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    from PIL import Image

    HEIF_AVAILABLE = True
except ImportError:
    HEIF_AVAILABLE = False

try:
    from PIL import Image  # noqa: F811  (also used on its own for size probing)

    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    import rawpy

    RAW_AVAILABLE = True
except ImportError:
    RAW_AVAILABLE = False


class LoadError(Exception):
    """Raised when a file cannot be turned into an image we can score."""


def parse_threshold(argv):
    """Read the threshold from the first CLI argument, falling back to default."""
    if len(argv) < 2:
        return DEFAULT_THRESHOLD
    try:
        value = float(argv[1])
    except ValueError:
        print(f"Threshold must be a number, got {argv[1]!r}. Using {DEFAULT_THRESHOLD}.")
        return DEFAULT_THRESHOLD
    if value < 0:
        print(f"Threshold must be >= 0, got {value}. Using {DEFAULT_THRESHOLD}.")
        return DEFAULT_THRESHOLD
    return value


def find_photos(folder):
    """Return photo paths in `folder`, sorted, case-insensitive on extension."""
    return sorted(
        path
        for path in folder.iterdir()
        if path.is_file() and path.suffix.lower() in EXTENSIONS
    )


def _reduced_decode_flag(path):
    """Pick the largest JPEG/PNG decode reduction that still clears our target size.

    libjpeg can decode at 1/2, 1/4 or 1/8 scale far faster than full size. Since
    we only ever score at NORMALIZED_LONGEST_SIDE, decoding a 48MP file at full
    resolution and then shrinking it is wasted work. We only reduce while the
    result still has at least NORMALIZED_LONGEST_SIDE on its long edge, so the
    score is unaffected.

    Returns (cv2 flag, label). Falls back to a full decode if the size is unknown.
    """
    if not PIL_AVAILABLE:
        return cv2.IMREAD_COLOR, "full"
    try:
        with Image.open(path) as probe:  # lazy: reads the header, not the pixels
            width, height = probe.size
    except Exception:
        return cv2.IMREAD_COLOR, "full"

    longest = max(width, height)
    for divisor, flag in (
        (8, cv2.IMREAD_REDUCED_COLOR_8),
        (4, cv2.IMREAD_REDUCED_COLOR_4),
        (2, cv2.IMREAD_REDUCED_COLOR_2),
    ):
        if longest / divisor >= NORMALIZED_LONGEST_SIDE:
            return flag, f"1/{divisor}"
    return cv2.IMREAD_COLOR, "full"


def _load_simple(path, reduced=True):
    """Load a JPEG or PNG, decoding at a reduced size where that is lossless for us.

    reduced=False forces a full-resolution decode. Grading needs that: a graded
    photo is a deliverable, so it must keep every pixel. Scoring does not.
    """
    flag, label = _reduced_decode_flag(path) if reduced else (cv2.IMREAD_COLOR, "full")
    image = cv2.imread(str(path), flag)
    if image is None:
        raise LoadError("could not decode")
    return image, label


def _load_heif(path):
    """Load a HEIC/HEIF file (iPhone photos) via pillow-heif."""
    if not HEIF_AVAILABLE:
        raise LoadError("needs pillow-heif (pip install pillow-heif)")
    try:
        with Image.open(path) as handle:
            rgb = np.asarray(handle.convert("RGB"))
    except Exception as exc:
        raise LoadError(f"HEIF decode failed: {exc}") from exc
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), "heif"


def _load_raw(path):
    """Load a camera RAW file, preferring its embedded JPEG preview.

    Every camera stores a full-size JPEG preview inside the RAW file. Decoding
    that is dramatically faster than demosaicing the sensor data, and it is the
    same image the camera showed you - which is what you are judging. If no
    preview exists we fall back to a half-size demosaic.
    """
    if not RAW_AVAILABLE:
        raise LoadError("needs rawpy (pip install rawpy)")
    try:
        with rawpy.imread(str(path)) as raw:
            try:
                thumb = raw.extract_thumb()
            except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError):
                rgb = raw.postprocess(half_size=True, use_camera_wb=True)
                return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), "raw-demosaic"

            if thumb.format == rawpy.ThumbFormat.JPEG:
                buffer = np.frombuffer(thumb.data, dtype=np.uint8)
                image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
                if image is None:
                    raise LoadError("embedded preview would not decode")
                return image, "raw-preview"
            return cv2.cvtColor(thumb.data, cv2.COLOR_RGB2BGR), "raw-preview"
    except LoadError:
        raise
    except Exception as exc:
        raise LoadError(f"RAW decode failed: {exc}") from exc


def load_image(path, reduced=True):
    """Load any supported photo as a BGR array, plus a label for how it was decoded.

    reduced=True lets JPEG/PNG decode smaller when that cannot change the blur
    score. Pass reduced=False when the pixels themselves are the output.
    """
    suffix = path.suffix.lower()
    if suffix in RAW_EXTENSIONS:
        # shortcut: RAW always comes from the embedded preview, so grading a RAW
        # grades the camera's JPEG preview, not the sensor data. Switch to
        # raw.postprocess() here if graded RAW quality ever matters.
        return _load_raw(path)
    if suffix in HEIF_EXTENSIONS:
        return _load_heif(path)
    return _load_simple(path, reduced=reduced)


def blur_score_of(image):
    """Variance of the Laplacian for an already-loaded BGR image.

    The image is shrunk so its longest side is NORMALIZED_LONGEST_SIDE first.
    Images already at or below that size are scored as they are: enlarging them
    would interpolate new pixels, smoothing real edges away and pushing a sharp
    small image below the threshold - measured, a sharp 600px photo scores ~958
    natively but only ~94 when enlarged to 1000px.
    """
    height, width = image.shape[:2]
    scale = NORMALIZED_LONGEST_SIDE / max(height, width)
    if scale < 1:
        new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
        # INTER_AREA is the correct filter for shrinking.
        image = cv2.resize(image, new_size, interpolation=cv2.INTER_AREA)

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def clear_previous_output():
    """Delete photo copies from a previous run.

    Without this, running again at a different threshold leaves the old copy
    behind, so a photo that switched side sits in BOTH keep/ and reject/.
    Only photo files are deleted; anything else in those folders is left alone.
    """
    for folder in (KEEP_DIR, REJECT_DIR):
        for path in folder.iterdir():
            if path.is_file() and path.suffix.lower() in EXTENSIONS:
                path.unlink()


def main():
    threshold = parse_threshold(sys.argv)

    if not INPUT_DIR.is_dir():
        print(f"No {INPUT_DIR}/ folder found. Create it and put your photos there.")
        return 1

    photos = find_photos(INPUT_DIR)
    if not photos:
        print(f"No photos found in {INPUT_DIR}/.")
        print(f"Copy photos into {INPUT_DIR.resolve()} and run this again.")
        print(f"Supported: {', '.join(sorted(EXTENSIONS))}")
        return 0

    KEEP_DIR.mkdir(parents=True, exist_ok=True)
    REJECT_DIR.mkdir(parents=True, exist_ok=True)
    clear_previous_output()

    print(f"Culling {len(photos)} photo(s) from {INPUT_DIR}/ with threshold {threshold:g}\n")

    kept, rejected, skipped = 0, 0, 0
    scores = []
    decode_labels = {}

    for path in photos:
        try:
            image, label = load_image(path)
        except LoadError as exc:
            print(f"  SKIP    {'':>9}  {path.name}  ({exc})")
            skipped += 1
            continue

        decode_labels[label] = decode_labels.get(label, 0) + 1
        score = blur_score_of(image)
        scores.append(score)

        if score >= threshold:
            destination, verdict = KEEP_DIR, "KEEP"
            kept += 1
        else:
            destination, verdict = REJECT_DIR, "REJECT"
            rejected += 1

        # copy2 preserves timestamps and metadata; the original stays in input/.
        shutil.copy2(path, destination / path.name)
        print(f"  {verdict:<6}  {score:9.1f}  {path.name}")

    print(f"\nSummary: {kept} kept, {rejected} rejected", end="")
    if skipped:
        print(f", {skipped} skipped", end="")
    print(f" (threshold {threshold:g})")

    if scores:
        print(f"Scores ranged {min(scores):.1f} to {max(scores):.1f} - use these to tune your threshold.")
    if decode_labels:
        detail = ", ".join(f"{label}: {count}" for label, count in sorted(decode_labels.items()))
        print(f"Decoded via - {detail}")
    if any(k.startswith("raw") for k in decode_labels) and len(decode_labels) > 1:
        print("Note: RAW files are scored from their embedded preview, which the camera")
        print("      sharpened differently from your JPEGs. Compare scores within a format.")
    print(f"Copies are in {KEEP_DIR}/ and {REJECT_DIR}/. Originals in {INPUT_DIR}/ were not touched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
