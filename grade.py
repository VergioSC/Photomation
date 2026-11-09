#!/usr/bin/env python3
"""Apply a .cube colour LUT to the photos you kept.

Reads from output/keep (the survivors of culling) and writes graded copies to
output/graded. Originals are never modified.

Usage:
    python grade.py                     # first .cube in luts/
    python grade.py luts/warm.cube      # a specific LUT
    python grade.py warm.cube input     # and a different source folder
"""

import sys
from pathlib import Path

import cv2

import config
import cull
import lut as lut_module

GRADED_DIR = config.OUTPUT_DIR / "graded"
JPEG_QUALITY = 95


def find_lut(argument):
    """Pick the LUT file: the one named, else the only .cube in luts/."""
    if argument:
        path = Path(argument)
        if not path.is_file():
            path = config.LUTS_DIR / argument
        if not path.is_file():
            raise SystemExit(f"No such LUT: {argument}")
        return path

    cubes = sorted(p for p in config.LUTS_DIR.glob("*.cube")) if config.LUTS_DIR.is_dir() else []
    if not cubes:
        raise SystemExit(
            f"No .cube files in {config.LUTS_DIR}. Put one there, or name one as the first argument."
        )
    if len(cubes) > 1:
        names = ", ".join(p.name for p in cubes)
        raise SystemExit(f"Several LUTs in {config.LUTS_DIR} ({names}). Name the one you want.")
    return cubes[0]


def main(argv):
    lut_path = find_lut(argv[1] if len(argv) > 1 else None)
    source = Path(argv[2]) if len(argv) > 2 else config.KEEP_DIR

    try:
        table = lut_module.load_cube(lut_path)
    except lut_module.CubeError as exc:
        raise SystemExit(f"Could not read {lut_path.name}: {exc}")

    if not source.is_dir():
        raise SystemExit(f"No such folder: {source}. Run the culling step first.")

    photos = [p for p in sorted(source.iterdir())
              if p.is_file() and p.suffix.lower() in cull.EXTENSIONS]
    if not photos:
        raise SystemExit(f"No photos in {source}. Run the culling step first.")

    GRADED_DIR.mkdir(parents=True, exist_ok=True)
    label = table.title or lut_path.stem
    print(f"Grading {len(photos)} photo(s) from {source}/ with {lut_path.name}"
          f"{f' ({label})' if table.title else ''}, cube size {table.size}\n")

    done, skipped = 0, 0
    for path in photos:
        try:
            # Full resolution: the graded file is the deliverable.
            image, _ = cull.load_image(path, reduced=False)
        except cull.LoadError as exc:
            print(f"  SKIP  {path.name}  ({exc})")
            skipped += 1
            continue

        graded = lut_module.apply_lut(image, table)
        # Always write JPEG: the source may be RAW or HEIC, which we cannot
        # write back, and a graded file is a deliverable rather than a master.
        target = GRADED_DIR / f"{path.stem}.jpg"
        if not cv2.imwrite(str(target), graded, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]):
            print(f"  FAIL  {path.name}  (could not write {target.name})")
            skipped += 1
            continue
        print(f"  OK    {path.name}  ->  {target.name}")
        done += 1

    print(f"\n{done} graded, {skipped} skipped. Written to {GRADED_DIR}/")
    print("Note: EXIF metadata is not carried into the graded copies.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
