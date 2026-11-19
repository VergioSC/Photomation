#!/usr/bin/env python3
"""Apply a .cube colour LUT to the photos you kept.

Reads from output/keep (the survivors of culling) and writes graded copies to
output/graded. Originals are never modified.

Usage:
    python grade.py                          # first .cube in luts/
    python grade.py luts/warm.cube           # a specific LUT
    python grade.py --no-lut --auto          # auto-adjust only, no LUT
    python grade.py warm.cube --auto --size 2048
    python grade.py --no-lut --auto --from input
"""

import argparse
import sys
from pathlib import Path

import cv2

import auto as auto_module
import config
import cull
import edit as edit_module
import exif as exif_module
import lut as lut_module
import masks as masks_module
import storage as storage_module

GRADED_DIR = config.OUTPUT_DIR / "graded"


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


def jpeg_quality(text):
    """1-100. Pillow silently accepts 0 and 150 and writes something else."""
    value = int(text)
    if not 1 <= value <= 100:
        raise argparse.ArgumentTypeError(f"{value} is outside 1-100")
    return value


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="grade.py",
        description="Grade the photos you kept, from the command line.")
    parser.add_argument("lut", nargs="?",
                        help="a .cube file, or a name inside luts/. "
                             "Omit to use the only one there.")
    # An option, not a second positional: with --no-lut there is no LUT name
    # to give, and a lone positional would silently bind to `lut` instead.
    parser.add_argument("--from", dest="source", default=str(config.KEEP_DIR),
                        metavar="DIR", help="folder to read (default: output/keep)")
    parser.add_argument("--auto", action="store_true",
                        help="auto-adjust each photo as well, the same way the "
                             "editor's Export all does for a photo you never opened")
    parser.add_argument("--no-lut", action="store_true",
                        help="skip the LUT and only auto-adjust")
    parser.add_argument("--size", type=int, default=0, metavar="PX",
                        help="longest side of the exported file (default: original)")
    parser.add_argument("--quality", type=jpeg_quality, default=95, metavar="1-100",
                        help="JPEG quality, 1-100 (default: 95)")
    args = parser.parse_args(argv[1:])
    if args.no_lut and not args.auto:
        parser.error("--no-lut on its own would copy the photos unchanged; add --auto")
    return args


def main(argv):
    args = parse_args(argv)
    table = None
    lut_path = None
    if not args.no_lut:
        lut_path = find_lut(args.lut)
        try:
            table = lut_module.load_cube(lut_path)
        except lut_module.CubeError as exc:
            raise SystemExit(f"Could not read {lut_path.name}: {exc}")

    source = Path(args.source)
    if not source.is_dir():
        raise SystemExit(f"No such folder: {source}. Run the culling step first.")

    photos = [p for p in sorted(source.iterdir())
              if p.is_file() and p.suffix.lower() in cull.EXTENSIONS]
    if not photos:
        raise SystemExit(f"No photos in {source}. Run the culling step first.")

    GRADED_DIR.mkdir(parents=True, exist_ok=True)
    what = []
    if args.auto:
        what.append("auto-adjusted")
    if table is not None:
        label = table.title or lut_path.stem
        what.append(f"graded with {lut_path.name}"
                    f"{f' ({label})' if table.title else ''}, cube size {table.size}")
    print(f"{len(photos)} photo(s) from {source}/, {' and '.join(what)}\n")

    done, skipped = 0, 0
    for path in photos:
        try:
            # Full resolution: the graded file is the deliverable.
            image, _ = cull.load_image(path, reduced=False)
        except cull.LoadError as exc:
            print(f"  SKIP  {path.name}  ({exc})")
            skipped += 1
            continue

        # Resize BEFORE editing, as the web export does. Grain, sharpening and
        # structure are a share of the frame, so shrinking afterwards shrinks
        # them too and the file stops matching what was asked for.
        if args.size:
            height, width = image.shape[:2]
            scale = args.size / max(height, width)
            if scale < 1:
                image = cv2.resize(image,
                                   (max(1, round(width * scale)), max(1, round(height * scale))),
                                   interpolation=cv2.INTER_AREA)

        if args.auto:
            adj, _, _, _ = auto_module.analyse(image, shot=exif_module.read(path))
            scene = masks_module.analyse(image) if adj.has_regional else None
            image = edit_module.apply_adjustments(image, adj, table, scene=scene)
        elif table is not None:
            image = lut_module.apply_lut(image, table)

        # Always write JPEG: the source may be RAW or HEIC, which we cannot
        # write back, and a graded file is a deliverable rather than a master.
        target = GRADED_DIR / f"{path.stem}.jpg"
        try:
            storage_module.save_jpeg(target, image, source=path, quality=args.quality)
        except OSError as exc:
            print(f"  FAIL  {path.name}  (could not write {target.name}: {exc})")
            skipped += 1
            continue
        print(f"  OK    {path.name}  ->  {target.name}")
        done += 1

    print(f"\n{done} graded, {skipped} skipped. Written to {GRADED_DIR}/")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
