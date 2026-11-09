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

### The web dashboard

```bash
.venv/bin/python app.py
```

Then open <http://127.0.0.1:5000>. The dashboard scores every photo **once** and
sends the scores to the browser; the threshold slider re-sorts them client-side,
so tuning is instant rather than needing a rescan. Nothing is written to
`output/` until you press **Apply & copy**.

**Click any photo to overrule the threshold.** Clicking cycles it between
following the slider, forced keep, and forced reject, and a badge shows which.
This matters because the score measures the whole frame: a sharp photo of an
empty sky scores low, and dragging the threshold to rescue it drags every other
photo with it.

It also draws a score distribution so you can see where your keepers and rejects
separate, and put the threshold in the gap. The chart's axis is square-root
scaled: sharpness scores are heavily right-skewed, and on a linear axis a single
very sharp frame crushes every other photo into the first bin — which is exactly
where the threshold lives.

The server binds to `127.0.0.1` by default, so it is not reachable from your
network. It reads your local photo folders, so keep it that way unless you have
a reason not to.

### The command line

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

Copies land in `output/keep/` and `output/reject/`. Both folders are emptied of
photos first, so a photo that changes side cannot end up in both. `input/` is
never touched.

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
cull.py            scoring + CLI culling
app.py             Flask routes: landing page, dashboard, JSON/SSE API
config.py          all paths, from environment variables
storage.py         photo access behind an interface (swap in S3 later)
templates/         landing page and dashboard
static/            stylesheet and dashboard JavaScript
Dockerfile         container image
requirements.txt   pinned dependencies
input/             your photos (gitignored - image data stays out of git)
output/keep/       copies that passed
output/reject/     copies that failed
luts/              .cube colour-grading LUTs
.cache/            generated thumbnails (gitignored)
```

## Configuration

Every path is an environment variable, so nothing is hardcoded and the app moves
to a container or a mounted volume without code changes:

| Variable | Default | Purpose |
|---|---|---|
| `PHOTOMATION_INPUT` | `input` | folder to read photos from |
| `PHOTOMATION_OUTPUT` | `output` | where keep/reject copies are written |
| `PHOTOMATION_LUTS` | `luts` | `.cube` LUTs |
| `PHOTOMATION_CACHE` | `.cache` | generated thumbnails |
| `PHOTOMATION_HOST` | `127.0.0.1` | bind address |
| `PHOTOMATION_PORT` | `5000` | port |
| `PHOTOMATION_THRESHOLD` | `100` | starting threshold |

## Running in a container

```bash
docker build -t photomation .
docker run --rm -p 5000:5000 \
    -v "$PWD/input:/data/input" \
    -v "$PWD/output:/data/output" \
    photomation
```

## Roadmap

- [x] Blur-based culling
- [x] Web dashboard with live threshold tuning and per-photo overrides
- [ ] LUT (`.cube`) colour grading with trilinear interpolation
