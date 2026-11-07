#!/usr/bin/env python3
"""Photomation: blur-based photo culling.

Scores every photo in input/ by the variance of its Laplacian (a standard
sharpness proxy: the Laplacian highlights edges, so a sharp photo has a wide
spread of edge responses and a high variance, while a blurry one is flat and
scores low). Photos are COPIED into output/keep or output/reject - originals
in input/ are never moved, renamed or deleted.

Usage:
    python cull.py [threshold]        # default threshold: 100
"""

import shutil
import sys
from pathlib import Path

import cv2

INPUT_DIR = Path("input")
OUTPUT_DIR = Path("output")
KEEP_DIR = OUTPUT_DIR / "keep"
REJECT_DIR = OUTPUT_DIR / "reject"

EXTENSIONS = {".jpg", ".jpeg", ".png"}
DEFAULT_THRESHOLD = 100.0
NORMALIZED_LONGEST_SIDE = 1000


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


def blur_score(path):
    """Variance of the Laplacian, computed at a normalized size.

    The image is shrunk so its longest side is NORMALIZED_LONGEST_SIDE before
    scoring. Without this, a 48MP photo and a 12MP photo of the same scene get
    very different scores, and one threshold could not serve both.

    Images already at or below that size are scored as they are. Enlarging them
    would interpolate new pixels, smoothing real edges away and pushing a sharp
    small image below the threshold - measured: a sharp 600px photo scores ~958
    natively but only ~94 when enlarged to 1000px.

    Returns None if the file could not be decoded as an image.
    """
    image = cv2.imread(str(path))
    if image is None:
        return None

    height, width = image.shape[:2]
    scale = NORMALIZED_LONGEST_SIDE / max(height, width)
    if scale < 1:
        new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
        # INTER_AREA is the correct filter for shrinking.
        image = cv2.resize(image, new_size, interpolation=cv2.INTER_AREA)

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def main():
    threshold = parse_threshold(sys.argv)

    if not INPUT_DIR.is_dir():
        print(f"No {INPUT_DIR}/ folder found. Create it and put your photos there.")
        return 1

    photos = find_photos(INPUT_DIR)
    if not photos:
        print(f"No photos found in {INPUT_DIR}/.")
        print(f"Copy some .jpg, .jpeg or .png files into {INPUT_DIR.resolve()} and run this again.")
        return 0

    KEEP_DIR.mkdir(parents=True, exist_ok=True)
    REJECT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Culling {len(photos)} photo(s) from {INPUT_DIR}/ with threshold {threshold:g}\n")

    kept, rejected, skipped = 0, 0, 0
    scores = []

    for path in photos:
        score = blur_score(path)
        if score is None:
            print(f"  SKIP     (unreadable)  {path.name}")
            skipped += 1
            continue

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
        print(f", {skipped} skipped (unreadable)", end="")
    print(f" (threshold {threshold:g})")

    if scores:
        print(f"Scores ranged {min(scores):.1f} to {max(scores):.1f} - use these to tune your threshold.")
    print(f"Copies are in {KEEP_DIR}/ and {REJECT_DIR}/. Originals in {INPUT_DIR}/ were not touched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
