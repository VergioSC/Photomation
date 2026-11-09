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

**Add photos by dragging them onto the page** (or pressing Choose photos).
They are copied into the input folder; your originals are not moved. Finished
photos come back out through **Download zip**, so neither end of the job needs
a file manager.

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

### Colour grading

Put a `.cube` LUT in `luts/`, then:

```bash
.venv/bin/python grade.py                  # the only .cube in luts/
.venv/bin/python grade.py luts/warm.cube   # a named one
.venv/bin/python grade.py warm.cube input  # and a different source folder
```

Reads from `output/keep/` by default, so you grade what survived culling, and
writes to `output/graded/`.

A 3D LUT is a cube of colours: split red, green and blue into N steps and the
file holds the output colour at each of the N&sup3; grid points. A real pixel
almost never lands on a grid point, so its colour is mixed from the eight points
around it (trilinear interpolation). `cv2.LUT` only does 1D 256-entry tables, so
this is done in numpy.

Two limits worth knowing. Graded copies are written as JPEG and **do not carry
EXIF** across. Grading a RAW file grades the camera's embedded preview, not the
sensor data.

### The editor

The **Edit** page is a photo editor. Pick a photo from the filmstrip, move the
sliders, and the preview updates live:

| Group | Controls |
|---|---|
| Film recipe | One-click presets that fill in every slider below |
| Crop | Original / 1:1 / 4:5 / 3:2 / 16:9, auto crop, full frame |
| Look | Film simulation, LUT, and how strongly to mix the LUT in |
| Light | Exposure (EV), contrast, highlights, shadows |
| Colour | Temperature, tint, vibrance, saturation |
| Detail | Clarity, sharpen, grain |
| Export | Resize the longest side, or keep the original |

### Film recipes

A recipe is a named preset. Clicking one **fills in the sliders** and then gets
out of the way, so you can take a look as a starting point and move anything
you disagree with. Touching any slider deselects the recipe, because the result
is no longer that recipe.

Twelve are included, spanning slide film, portrait negative, cinema stock and
four black-and-white looks. They live as JSON in `recipes/`, so adding your own
means dropping a file in:

```json
{
  "name": "My Look",
  "notes": "What it is for.",
  "settings": {
    "film_simulation": "Classic Chrome",
    "dynamic_range": 200,
    "highlight": 0, "shadow": 2, "color": 2,
    "sharpening": 1, "clarity": 3,
    "grain": "Weak, Small",
    "color_chrome": "Strong",
    "white_balance": {"mode": "Daylight", "red": 2, "blue": -5},
    "exposure_compensation": 0.67
  }
}
```

Those field names follow the way Fujifilm shooters write recipes down, so a
recipe you find elsewhere can be typed straight in. There is an importer for
exactly that:

```bash
.venv/bin/python import_recipe.py "Kodachrome 64"      # paste, then Ctrl-D
.venv/bin/python import_recipe.py "Velvia" recipe.txt
```

It reads `Field: value` lines in any order, understands `+2/3` fractions and
`Daylight, +2 Red & -5 Blue` white-balance shifts, and ignores anything it does
not recognise.

**These are approximations.** A camera recipe is a set of instructions the
camera applies to sensor data as it builds a JPEG. Photomation works on a photo
that has already been through that, so a recipe here reproduces the *character*
of a look, not the same processing. Two sign conventions are worth knowing: a
camera's `Shadow +2` makes shadows darker, while the Shadows slider here lifts
them, so the sign is flipped on import; `Highlight` is not flipped.

The film simulations themselves are approximations too, each built from a
channel mixer, a tone curve and a saturation setting, with the monochrome ones
using their own channel weights (which is what gives different black-and-white
films their different skies and skin tones).

### Auto adjust

**Auto adjust** analyses the photo and fills in the Light and Colour sliders.
**Auto crop** also suggests a crop. Both then leave everything adjustable, and
the panel lists what it did and why, so the numbers are not a black box.

This is image analysis, not a neural network, and nothing leaves your machine:

| Step | How |
|---|---|
| White balance | Averages the *near-neutral* pixels only. Plain grey-world assumes a scene averages to grey, which turns a blue sea brown; the pixels that are already close to neutral are the ones whose colour really is the light. |
| Exposure | Aims the median brightness at a normal midtone, but only moves 60% of the way. A dusk photo is meant to be dark, and correcting it fully flattens the mood out of it. |
| Contrast | Measures how much of the tonal range the photo actually uses and stretches it only if it is sitting in a narrow band. |
| Shadows / highlights | Measures how much is clipped at each end and recovers only that much. |
| Crop | Finds the busiest part of the frame by edge density, frames it on a rule-of-thirds line, and keeps 85% of the picture — a tightening, not a recomposition. |

There is no face detection: OpenCV 5 removed the Haar classifier API, and its
replacement needs a model file downloaded into the project. Edge density finds
portrait subjects well enough, because eyes, hair and clothing carry far more
detail than a backdrop.

Double-click a slider to reset just that one; **Reset edits** clears them all.
Tick **Compare** for a side-by-side against the original. **Export all** applies
the same edit to every kept photo at full resolution, then **Download zip**
gives you the lot.

Two things are done properly rather than quickly. Exposure and white balance
are applied in *linear light*: a JPEG stores sRGB values, which are bent so
dark tones get more of the range, and doubling the stored number does not
double the light. Those two controls undo the bend, do their work, and reapply
it, so `+1 EV` really is one stop. Contrast, saturation and LUTs are judged by
eye on the final picture, so they are applied to the display values, which is
where they are designed to work.

Vibrance is not a weaker saturation: it measures how colourful each pixel
already is and pushes the dull ones hardest, so skies and clothing gain
without skin tones going lurid.

Previews are sized for the screen. CSS pixels are not screen pixels on a
Retina display, so the browser asks for the stage size multiplied by the device
pixel ratio; a fixed 480px thumbnail was being blown up about three times and
looked pixellated. Requested sizes are snapped up to fixed steps, so the cache
holds a handful of files per photo instead of one per window width.

While a slider is moving the preview renders at 700px for speed, then
re-renders at full size about a quarter of a second after it stops, so dragging
stays responsive and the picture you judge is sharp. A LUT that
cannot be parsed is listed with the reason rather than silently skipped.

### Tests

```bash
.venv/bin/python tests/test_lut.py
.venv/bin/python tests/test_edit.py
.venv/bin/python tests/test_recipes.py
```

Plain asserts, no test framework. Covers the `.cube` parser, an identity LUT
that must change nothing, a check that red and blue are not swapped between
OpenCV's BGR and the file's RGB, and the URL traversal guard. The editor tests
check that one stop of exposure doubles the light, that vibrance spares
already-saturated colour, that shadows and highlights only move their own end
of the range, and that LUT strength blends.

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
lut.py             .cube parser and trilinear interpolation
edit.py            exposure, white balance, tone, colour, film looks, grain, crop
auto.py            automatic analysis: white balance, exposure, tone, crop
recipes.py         film recipe presets and the recipe importer
import_recipe.py   CLI: turn a written-out recipe into a preset
recipes/           preset JSON files
grade.py           CLI colour grading
tests/             runnable asserts
app.py             Flask routes: landing page, dashboard, JSON/SSE API
config.py          all paths, from environment variables
storage.py         photo access behind an interface (swap in S3 later)
templates/         landing page, dashboard and grade page
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
    -v "$PWD/input:/data/input:ro" \
    -v "$PWD/output:/data/output" \
    -v "$PWD/luts:/data/luts:ro" \
    photomation
```

Mounting `input/` and `luts/` read-only is worth doing: the app only ever reads
them, so the container has no business writing there.

The image is about 518 MB and has been built and run end to end - scan, apply
and grade all work against mounted folders. It uses `opencv-python-headless`,
which skips the `libGL` system libraries the GUI build needs; that removes the
`apt-get` layer and roughly 320 MB. Nothing here opens a window, so the GUI
build earns nothing. The trade: `cv2.imshow` is not available if you ever want
it for debugging.

## Roadmap

- [x] Blur-based culling
- [x] Web dashboard with live threshold tuning and per-photo overrides
- [x] LUT (`.cube`) colour grading with trilinear interpolation
- [x] Grading in the dashboard
- [x] Upload and download in the browser
- [x] Exposure, white balance, tone and colour controls
- [x] Film recipes, film simulations, grain and clarity
- [x] Automatic adjustment and crop suggestion
- [ ] Deployment (Dockerfile exists, never built)
