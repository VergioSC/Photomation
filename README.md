# Photomation

A photo culling and editing tool. Point it at a folder of photos and it sorts
them into keepers and rejects by sharpness, so you don't hand-review a few
hundred frames after a shoot.

**Originals are never modified.** Photos are only ever read and copied with
`shutil.copy2`; nothing in `input/` is moved, renamed or deleted.

## How the culling works

Blur is measured as the **variance of the Laplacian**. The Laplacian is a
second-derivative edge filter: it responds strongly where brightness changes
quickly. A sharp photo therefore produces a wide spread of edge responses and a
high variance, while a blurry photo is comparatively flat and scores low.

Two details make the score trustworthy:

- **Size normalization.** Every photo is shrunk so its longest side is 1000px
  before scoring. Without this, a 48MP file and a 12MP file of the same scene
  score very differently and no single threshold could serve both.
- **Never enlarging.** Photos already at or below 1000px are scored as they are.
  Enlarging interpolates new pixels, which smooths real edges away: a sharp
  600px test photo scores ~958 natively but only ~94 when enlarged to 1000px,
  which would wrongly reject it.

Large JPEGs are decoded at a reduced size by libjpeg where that still leaves at
least 1000px on the long edge. Measured, this is 1.1–1.5x faster and shifts
scores by under 1% in the range near the threshold, with no verdict changes.

## Setup

Requires Python 3.14 (developed against 3.14.6).

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Usage

```bash
# Put photos in input/, then:
.venv/bin/python cull.py           # default threshold: 100
.venv/bin/python cull.py 250       # stricter: demands sharper photos
```

Output:

```
Culling 4 photo(s) from input/ with threshold 100

  REJECT        6.2  DSC_0012.jpg
  KEEP       1660.7  DSC_0013.jpg
  SKIP               notes.dng  (RAW decode failed: ...)

Summary: 1 kept, 1 rejected, 1 skipped (threshold 100)
Scores ranged 6.2 to 1660.7 - use these to tune your threshold.
```

Copies land in `output/keep/` and `output/reject/`. Re-running is safe: it
overwrites those copies and leaves `input/` untouched.

## Choosing a threshold

The default of 100 is a starting point, not a universal answer — absolute scores
depend on your camera, lens and subject matter. Tune it from real data:

```bash
.venv/bin/python cull.py | sort -k2 -n     # scores, ascending
```

Look for the gap between the photos you would keep and those you would bin, and
set the threshold inside it. Then check `output/reject/` for anything sharp that
was wrongly rejected.

**Known limitation:** this measures edge energy across the whole frame, not
whether your subject is in focus. A portrait with heavy bokeh, a minimalist shot
with lots of empty sky, or a low-contrast foggy scene all score low while being
perfectly sharp. A busy, textured snapshot scores high even when slightly soft.
Expect to tune per shoot rather than finding one permanent number.

## Supported formats

| Format | Extensions | Notes |
|---|---|---|
| JPEG | `.jpg` `.jpeg` | Reduced-size decode where possible |
| PNG | `.png` | |
| HEIF | `.heic` `.heif` | Default iPhone format, via `pillow_heif` |
| RAW | `.cr2` `.cr3` `.nef` `.arw` `.dng` `.raf` `.orf` `.rw2` `.pef` `.srw` | Scored from the embedded JPEG preview |

RAW files are scored from the preview the camera embedded, which is far faster
than demosaicing the sensor data and is the image the camera showed you. That
preview carries the camera's own sharpening, so RAW scores are not directly
comparable with JPEG scores — compare within a format. If a RAW has no usable
preview, it falls back to a half-size demosaic.

Unreadable or unsupported files are reported as `SKIP` with a reason; they never
abort the run.

## Project layout

```
cull.py            culling script
requirements.txt   pinned dependencies
input/             your photos (gitignored - image data stays out of git)
output/keep/       copies that passed
output/reject/     copies that failed
luts/              .cube colour-grading LUTs
```

## Roadmap

- [x] Blur-based culling
- [ ] LUT (`.cube`) colour grading with trilinear interpolation
