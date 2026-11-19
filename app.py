#!/usr/bin/env python3
"""Photomation web app: a landing page and a culling dashboard.

Run it with:
    .venv/bin/python app.py
then open http://127.0.0.1:5000

The dashboard scores every photo ONCE and sends the scores to the browser. The
threshold slider then re-buckets those scores client-side, so tuning is instant
instead of needing a rescan. Nothing is written to output/ until you press Apply.
"""

import dataclasses
import hashlib
import json
import os
import pathlib
import tempfile
import zipfile

import cv2
import numpy as np
from PIL import Image as PILImage
from flask import Flask, Response, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

import secrets

import config
import cull
import auto as auto_module
import exif as exif_module
import edit as edit_module
import lut as lut_module
import masks as masks_module
import recipes as recipes_module
import storage as storage_module
import storage_s3
from storage import LocalStorage

app = Flask(__name__)
# RAW files run to 50-100 MB each, so a batch upload needs real headroom.
app.config["MAX_CONTENT_LENGTH"] = config.MAX_UPLOAD_BYTES
store = storage_s3.from_env() or LocalStorage()
# The grading step works on what survived culling, so it lists output/keep.
keep_store = LocalStorage(input_dir=config.KEEP_DIR)


@app.before_request
def _require_password():
    """Gate every route when PHOTOMATION_PASSWORD is set.

    /api/upload accepts files from anyone who can reach the port. That is
    harmless on 127.0.0.1 and an open file-upload endpoint anywhere else, so
    the check guards everything rather than that one route - a thumbnail is
    somebody's photograph too.

    HTTP Basic because the browser handles the prompt, the retry and the
    credential store itself, and it covers images and streams that cannot
    carry a header of their own.
    """
    # A plain HTML form posts across sites without the browser asking us
    # first, so any page you have open could fire POST /api/empty-trash at
    # 127.0.0.1 and permanently delete every file this app has ever moved
    # aside. Browsers label that request; non-browser clients send no label
    # at all, and they are not the attack.
    if request.method not in ("GET", "HEAD", "OPTIONS") \
            and request.headers.get("Sec-Fetch-Site") == "cross-site":
        return jsonify({"error": "cross-site request refused"}), 403

    if not config.PASSWORD or request.path == "/api/health":
        return None
    sent = request.authorization
    ok = (sent is not None
          and secrets.compare_digest(sent.username or "", config.USERNAME)
          and secrets.compare_digest(sent.password or "", config.PASSWORD))
    if ok:
        return None
    return Response("Authentication required.", 401,
                    {"WWW-Authenticate": 'Basic realm="Photomation"'})

# Checked before anything else happens, and at import rather than in
# __main__, so it also guards `gunicorn app:app`, where __main__ never runs.
# Listening on anything but the loopback address means other machines can
# reach this, and without a password every one of them can read the photos and
# upload more. Refuse rather than quietly serve them - and refuse BEFORE the
# batch below is cleared, or a misconfigured start costs a shoot as well.
if config.HOST not in ("127.0.0.1", "localhost", "::1") and not config.PASSWORD:
    raise SystemExit(
        f"Refusing to serve on {config.HOST} with no password: anyone who can "
        f"reach this port could read the photos and upload their own.\n"
        f"Set PHOTOMATION_PASSWORD, or use PHOTOMATION_HOST=127.0.0.1.")

# A batch is one shoot, and starting the server starts a new one. Without this
# the dashboard opens on whatever was left in input/ from the last session and
# scans 62 photos nobody asked for. Moved to output/.trash, never deleted, and
# PHOTOMATION_KEEP_BATCH=1 turns it off for when a server died mid-shoot.
if config.CLEAR_ON_START:
    _cleared = storage_module.clear_batch(config.INPUT_DIR, config.OUTPUT_DIR)
    if _cleared:
        print(f"Started a new batch: moved {_cleared} file(s) from the last "
              f"one to {config.OUTPUT_DIR / '.trash'}")


@app.get("/")
def landing():
    """Marketing / explanation page."""
    return render_template(
        "index.html",
        photo_count=len(store.list_photos()),
        formats=sorted(e.lstrip(".") for e in cull.EXTENSIONS),
    )


@app.get("/dashboard")
def dashboard():
    """The working tool."""
    return render_template(
        "dashboard.html",
        default_threshold=config.DEFAULT_THRESHOLD,
        input_dir=str(config.INPUT_DIR),
        photo_count=len(store.list_photos()),
    )


@app.get("/api/scan")
def api_scan():
    """Stream one scored photo at a time as Server-Sent Events.

    Streaming rather than one big response means a 300-photo folder shows
    progress immediately instead of appearing to hang for a minute.
    """
    refs = store.list_photos()

    def generate():
        yield _sse({"type": "start", "total": len(refs)})
        for index, ref in enumerate(refs, start=1):
            try:
                image, label = store.load_image(ref)
                score = cull.blur_score_of(image)
                shot = exif_module.read(ref.path)
                black, blown = cull.exposure_of(image)
                payload = {
                    "type": "photo",
                    "index": index,
                    "name": ref.name,
                    "score": round(score, 1),
                    "decode": label,
                    "size_bytes": ref.size_bytes,
                    # The capture settings explain a lot of low scores, so they
                    # belong next to the verdict rather than hidden away.
                    "shot": shot.summary() if shot.known else "",
                    "iso": shot.iso,
                    # What the frame looks like, as 64 bits, so the page can
                    # spot the same shot fired ten times. Hex because
                    # JavaScript numbers cannot hold 64 bits of integer.
                    "signature": f"{cull.signature_of(image):016x}",
                    "black": round(black, 4),
                    "blown": round(blown, 4),
                }
            except cull.LoadError as exc:
                payload = {
                    "type": "skipped",
                    "index": index,
                    "name": ref.name,
                    "reason": str(exc),
                }
            yield _sse(payload)
        yield _sse({"type": "done"})

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/thumb/<path:name>")
def api_thumb(name):
    """Serve a cached thumbnail. `name` is untrusted, so it is resolved by lookup.

    `from` picks the folder, the same way the preview route does. The editor
    works on output/keep, so asking input/ for its thumbnails broke every
    filmstrip image as soon as input/ was cleared for the next shoot.
    """
    picker = keep_store if request.args.get("from") == "keep" else store
    ref = picker.resolve(name)
    if ref is None:
        return jsonify({"error": "not found"}), 404
    try:
        data = picker.scaled_bytes(ref)
    except cull.LoadError as exc:
        return jsonify({"error": str(exc)}), 422
    return Response(
        data,
        mimetype="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.post("/api/apply")
def api_apply():
    """Copy originals into output/keep and output/reject.

    The browser sends the final bucket per photo, not just a threshold, so a
    manual override on a single photo is honoured here too.

    This is the only endpoint that writes. Originals are copied with copy2 and
    input/ is never touched. Previous copies in output/ ARE deleted first - see
    below.
    """
    body = request.get_json(silent=True) or {}

    buckets = body.get("buckets")
    if not isinstance(buckets, dict) or not buckets:
        return jsonify({"error": "no buckets supplied; run a scan first"}), 400

    bad = [n for n, b in buckets.items() if b not in ("keep", "reject")]
    if bad:
        return jsonify({"error": f"bucket must be keep or reject: {bad[:3]}"}), 400

    # Wipe both folders first. Re-applying at a different threshold used to
    # leave the old copy in place, so a photo that changed side appeared in
    # keep/ AND reject/ at once.
    store.clear_bucket("keep")
    store.clear_bucket("reject")

    # One directory listing, not one per photo: resolving each name separately
    # made this O(n^2) and cost 800ms of stat() calls at 300 photos.
    by_name = {ref.name: ref for ref in store.list_photos()}

    kept, rejected, missing = [], [], []
    for name, bucket in buckets.items():
        ref = by_name.get(name)
        if ref is None:
            missing.append(name)
            continue
        store.copy_to_bucket(ref, bucket)
        (kept if bucket == "keep" else rejected).append(name)

    return jsonify(
        {
            "kept": len(kept),
            "rejected": len(rejected),
            "missing": missing,
            "keep_dir": str(config.KEEP_DIR),
            "reject_dir": str(config.REJECT_DIR),
        }
    )


@app.get("/grade")
def grade_page():
    """Colour grading: pick a LUT, preview it, write graded copies."""
    return render_template(
        "grade.html",
        keep_count=len(keep_store.list_photos()),
        luts_dir=str(config.LUTS_DIR),
        keep_dir=str(config.KEEP_DIR),
    )


def _available_luts():
    """Every readable .cube in the LUT folder, plus why any were unreadable."""
    if not config.LUTS_DIR.is_dir():
        return [], []
    good, bad = [], []
    for path in sorted(config.LUTS_DIR.glob("*.cube")):
        try:
            table = lut_module.load_cube(path)
        except lut_module.CubeError as exc:
            bad.append({"name": path.name, "reason": str(exc)})
            continue
        good.append({"name": path.name, "title": table.title, "size": table.size})
    return good, bad


@app.get("/api/keepers")
def api_keepers():
    """Photos grading will work on, with their real shape.

    The editor needs the ORIGINAL aspect to build a crop. Measuring it from
    the preview on screen would compound, because that preview may already be
    cropped by whatever was applied before.
    """
    out = []
    for ref in keep_store.list_photos():
        width = height = 0
        try:
            with PILImage.open(ref.path) as probe:   # lazy: header only
                width, height = probe.size
                # The stored size ignores the EXIF rotation tag, but OpenCV
                # applies it when decoding, so a rotated photo would otherwise
                # be reported with its width and height the wrong way round.
                orientation = probe.getexif().get(274, 1)
                if orientation in (5, 6, 7, 8):
                    width, height = height, width
        except Exception:
            pass
        out.append({"name": ref.name, "width": width, "height": height})
    return jsonify({"photos": [p["name"] for p in out], "sizes": out})


@app.get("/api/luts")
def api_luts():
    good, bad = _available_luts()
    return jsonify({"luts": good, "unreadable": bad, "luts_dir": str(config.LUTS_DIR)})


def _load_lut_or_400(name):
    """Resolve a LUT name from the browser. Matched against the listing, never joined."""
    for path in sorted(config.LUTS_DIR.glob("*.cube")):
        if path.name == name:
            return lut_module.load_cube(path), None
    return None, (jsonify({"error": f"no such LUT: {name}"}), 404)


def _preview_step(asked):
    """Snap a requested preview width up to the next cached step."""
    asked = max(config.PREVIEW_MIN, min(config.PREVIEW_MAX, asked))
    for step in config.PREVIEW_STEPS:
        if asked <= step:
            return step
    return config.PREVIEW_STEPS[-1]


def _crop_from(source):
    """Read a crop rectangle out of a JSON edit or a query string, or None.

    Both arrive as something that indexes by key and yields a string or a
    number, so one reader covers them and they cannot drift apart.
    """
    try:
        rect = tuple(float(source[k]) for k in ("crop_x", "crop_y", "crop_w", "crop_h"))
    except (KeyError, TypeError, ValueError):
        return None
    x, y, w, h = rect
    if w <= 0 or h <= 0 or w > 1 or h > 1:
        return None
    return (max(0.0, min(1.0, x)), max(0.0, min(1.0, y)), w, h)


def _adjustments_from_args(args):
    """Read slider values off a query string."""
    return edit_module.Adjustments.from_dict({key: args[key] for key in args.keys()})


def _lut_from_args(args):
    """Resolve the optional LUT. Returns (table, error_response)."""
    name = args.get("lut", "")
    if not name or name == "none":
        return None, None
    try:
        return _load_lut_or_400(name)
    except lut_module.CubeError as exc:
        return None, (jsonify({"error": str(exc)}), 422)


@app.get("/api/preview/<path:name>")
def api_preview(name):
    """A small edited preview, rendered live for the current slider values.

    The adjusted result is deliberately NOT cached: the sliders change it on
    every drag, so a cache would only fill the disk. The decoded thumbnail
    underneath it IS cached, and that is the slow part.
    """
    picker = keep_store if request.args.get("from", "keep") == "keep" else store
    ref = picker.resolve(name)
    if ref is None:
        return jsonify({"error": "not found"}), 404

    table, error = _lut_from_args(request.args)
    if error is not None:
        return error

    # The browser asks for the size it will actually display, so a Retina
    # screen gets real pixels instead of a blown-up thumbnail, and a draft
    # size can be used while a slider is moving.
    try:
        asked = int(float(request.args.get("size", config.PREVIEW_SIZE)))
    except ValueError:
        asked = config.PREVIEW_SIZE
    # Rounded up to a fixed step. The browser derives the size from its window
    # width, so without this every window resize would cache a new file.
    longest = _preview_step(asked)

    try:
        raw = picker.scaled_bytes(ref, longest)
    except cull.LoadError as exc:
        return jsonify({"error": str(exc)}), 422
    source = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if source is None:
        return jsonify({"error": "could not decode the preview source"}), 422

    source = edit_module.crop_image(source, _crop_from(request.args))
    adj = _adjustments_from_args(request.args)
    # Masks cost real time, so they are only worked out when a selective
    # slider is actually asking for them.
    scene = masks_module.analyse(source) if adj.has_regional else None
    out = edit_module.apply_adjustments(source, adj, table, scene=scene)
    ok, buffer = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        return jsonify({"error": "could not encode preview"}), 500
    return Response(buffer.tobytes(), mimetype="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.post("/api/grade")
def api_grade():
    """Render every kept photo at full resolution, streaming progress.

    The body carries one edit per photo, because an edit belongs to a photo:

        {"edits": {"DSC_1.JPG": {"exposure": 0.5, "film": "velvia", ...}},
         "lut": "none", "resize": 0, "auto_fill": true}

    A photo with no edit of its own is analysed and auto-adjusted on the way
    past when auto_fill is set, so photos you never opened still come out
    sensibly rather than untouched.
    """
    body = request.get_json(silent=True) or {}
    edits = body.get("edits") or {}
    if not isinstance(edits, dict):
        return jsonify({"error": "edits must be an object keyed by filename"}), 400

    lut_name = str(body.get("lut", "none"))
    try:
        table, error = _load_lut_or_400(lut_name) if lut_name not in ("", "none") else (None, None)
    except lut_module.CubeError as exc:
        return jsonify({"error": str(exc)}), 422
    if error is not None:
        return error

    try:
        resize_to = int(float(body.get("resize", 0)))
    except (TypeError, ValueError):
        resize_to = 0
    auto_fill = bool(body.get("auto_fill", True))

    only = body.get("only")
    refs = keep_store.list_photos()
    if isinstance(only, list) and only:
        wanted = set(only)
        refs = [ref for ref in refs if ref.name in wanted]
        if not refs:
            return jsonify({"error": "none of those photos are in the keepers"}), 404
    graded_dir = config.OUTPUT_DIR / "graded"

    def generate():
        yield _sse({"type": "start", "total": len(refs)})
        graded_dir.mkdir(parents=True, exist_ok=True)
        # Clear the previous export first, or a photo you have since rejected
        # stays in the folder and ships with the zip. Not when only some photos
        # were asked for: exporting one frame must not throw away the other
        # fifty-five that are already done.
        if not (isinstance(only, list) and only):
            for stale in sorted(graded_dir.iterdir()):
                if stale.is_file() and stale.suffix.lower() in cull.EXTENSIONS:
                    storage_module.trash_file(stale, config.OUTPUT_DIR)
        for index, ref in enumerate(refs, start=1):
            try:
                # Full resolution: an exported photo is the deliverable.
                image, _ = cull.load_image(ref.path, reduced=False)

                own = edits.get(ref.name)
                if own is None and auto_fill:
                    adj, _, _, _ = auto_module.analyse(
                        image, shot=exif_module.read(ref.path))
                    crop = None
                    source = "auto"
                else:
                    adj = edit_module.Adjustments.from_dict(own or {})
                    crop = _crop_from(own or {})
                    source = "yours" if own else "untouched"

                image = edit_module.crop_image(image, crop)
                # Resize BEFORE the edit, not after. Grain, sharpening and
                # structure are a share of the frame, so shrinking afterwards
                # shrinks them too and the file no longer matches what the
                # preview promised. Doing it first also saves the work of
                # editing pixels that are about to be thrown away.
                if resize_to:
                    h, w = image.shape[:2]
                    scale = resize_to / max(h, w)
                    if scale < 1:
                        image = cv2.resize(
                            image, (max(1, round(w * scale)), max(1, round(h * scale))),
                            interpolation=cv2.INTER_AREA)
                scene = masks_module.analyse(image) if adj.has_regional else None
                out = edit_module.apply_adjustments(image, adj, table, scene=scene)
                target = graded_dir / f"{ref.path.stem}.jpg"
                try:
                    storage_module.save_jpeg(target, out, source=ref.path)
                except OSError as exc:
                    raise cull.LoadError(f"could not write the edited file: {exc}")
                payload = {"type": "photo", "index": index, "name": ref.name,
                           "out": target.name, "edit": source}
            except (cull.LoadError, lut_module.CubeError) as exc:
                payload = {"type": "skipped", "index": index, "name": ref.name,
                           "reason": str(exc)}
            yield _sse(payload)
        yield _sse({"type": "done", "dir": str(graded_dir)})

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/recipes")
def api_recipes():
    """Film presets, each with the slider values it sets."""
    found, problems = recipes_module.load_all()
    return jsonify({
        "recipes": [
            {
                "id": r["id"],
                "name": r["name"],
                "group": r["group"],
                "notes": r["notes"],
                "source": r["source"],
                "crop_aspect": r["crop_aspect"],
                "values": dataclasses.asdict(r["adjustments"]),
                "sets": r["sets"],
            }
            for r in found
        ],
        "problems": problems,
        "folder": str(recipes_module.RECIPES_DIR),
    })


RECIPE_CELL = 128          # one preview tile in the contact sheet, square


@app.get("/api/recipe-sheet/<path:name>")
def api_recipe_sheet(name):
    """Every recipe applied to one photo, as a single stacked image.

    A list of 27 names tells you nothing about what a preset does to YOUR
    photo, and 27 separate requests would queue up behind each other. So the
    tiles are rendered into one column and sent as one picture; the page slices
    it with background-position. Tiles are tiny, so the whole sheet costs about
    as much as a single preview.

    The order is the order /api/recipes returns, which is what the page indexes
    into. Both come from recipes.load_all().
    """
    ref = keep_store.resolve(name)
    if ref is None:
        return jsonify({"error": "not found"}), 404
    try:
        raw = keep_store.scaled_bytes(ref, RECIPE_CELL * 3)
    except cull.LoadError as exc:
        return jsonify({"error": str(exc)}), 422
    source = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if source is None:
        return jsonify({"error": "could not decode the preview source"}), 422

    # A square centre crop: a tile has to be the same shape for every photo or
    # the sheet's rows stop lining up with the buttons.
    height, width = source.shape[:2]
    side = min(height, width)
    top, left = (height - side) // 2, (width - side) // 2
    tile = cv2.resize(source[top:top + side, left:left + side],
                      (RECIPE_CELL, RECIPE_CELL), interpolation=cv2.INTER_AREA)

    found, _ = recipes_module.load_all()
    sheet = np.zeros((RECIPE_CELL * max(1, len(found)), RECIPE_CELL, 3), dtype=np.uint8)
    for index, recipe in enumerate(found):
        adj = recipe["adjustments"]
        # Selective work needs masks this tile is far too small to estimate,
        # and no recipe sets them anyway.
        sheet[index * RECIPE_CELL:(index + 1) * RECIPE_CELL] = \
            edit_module.apply_adjustments(tile, adj)

    ok, buffer = cv2.imencode(".jpg", sheet, [cv2.IMWRITE_JPEG_QUALITY, 82])
    if not ok:
        return jsonify({"error": "could not encode the recipe sheet"}), 500
    return Response(buffer.tobytes(), mimetype="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/films")
def api_films():
    """The film looks a recipe or the user can choose from."""
    return jsonify({"films": [{"id": k, "label": v} for k, v in edit_module.film_look_names()]})


@app.get("/api/auto/<path:name>")
def api_auto(name):
    """Analyse one photo and suggest settings. Returns values, not an image."""
    picker = keep_store if request.args.get("from", "keep") == "keep" else store
    ref = picker.resolve(name)
    if ref is None:
        return jsonify({"error": "not found"}), 404

    want_crop = request.args.get("crop", "0") in ("1", "true", "yes")
    aspect = None
    if request.args.get("aspect"):
        try:
            aspect = float(request.args["aspect"])
        except ValueError:
            aspect = None

    try:
        # Analysis is statistics, so a medium copy is plenty and much faster.
        raw = picker.scaled_bytes(ref, 1000)
    except cull.LoadError as exc:
        return jsonify({"error": str(exc)}), 422
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return jsonify({"error": "could not decode the photo"}), 422

    shot = exif_module.read(ref.path)
    adj, rect, notes, limits = auto_module.analyse(
        image, want_crop=want_crop, aspect=aspect, shot=shot)
    return jsonify({
        "values": dataclasses.asdict(adj),
        "crop": list(rect) if rect else None,
        "notes": notes,
        "shot": shot.summary(),
        "camera": shot.camera,
        "limits": {
            "max_shadow_lift": limits.max_shadow_lift,
            "max_highlight_pull": limits.max_highlight_pull,
            "max_sharpen": limits.max_sharpen,
            "max_clarity": limits.max_clarity,
        },
    })


@app.post("/api/upload")
def api_upload():
    """Take photos from the browser and put them in the input folder.

    Filenames from a browser are untrusted, so they go through
    secure_filename and the extension is checked against the formats we can
    actually read. An existing name is never overwritten; a counter is added.
    """
    uploaded = request.files.getlist("photos")
    if not uploaded:
        return jsonify({"error": "no files were sent"}), 400

    # "replace" means these photos are a new shoot, not more of this one. The
    # page asks which, and only when there is already a batch to replace.
    cleared = 0
    if request.form.get("replace") in ("1", "true", "yes"):
        cleared = storage_module.clear_batch(config.INPUT_DIR, config.OUTPUT_DIR)

    config.INPUT_DIR.mkdir(parents=True, exist_ok=True)
    saved, rejected = [], []
    for item in uploaded:
        safe = secure_filename(item.filename or "")
        if not safe:
            rejected.append({"name": item.filename or "(unnamed)", "reason": "unusable filename"})
            continue
        if pathlib.Path(safe).suffix.lower() not in cull.EXTENSIONS:
            rejected.append({"name": safe, "reason": "not a supported photo format"})
            continue

        target = config.INPUT_DIR / safe
        stem, suffix = pathlib.Path(safe).stem, pathlib.Path(safe).suffix
        counter = 1
        while target.exists():
            target = config.INPUT_DIR / f"{stem}-{counter}{suffix}"
            counter += 1
        item.save(target)
        saved.append(target.name)

    return jsonify({"saved": saved, "rejected": rejected, "cleared": cleared,
                    "total": len(store.list_photos())})


EDITS_FILE = "edits.json"


def _edits_path():
    return config.OUTPUT_DIR / EDITS_FILE


@app.get("/api/edits")
def api_edits_read():
    """The saved edits for this batch, keyed by filename.

    Edits used to live only in the page, so a reload threw away every slider
    on every photo. They belong with the batch, next to the photos they
    describe.
    """
    try:
        data = json.loads(_edits_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return jsonify({"edits": data if isinstance(data, dict) else {}})


@app.put("/api/edits")
def api_edits_write():
    """Replace the saved edits. The page sends the whole set, not a patch."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("edits"), dict):
        return jsonify({"error": "expected an object with an 'edits' object"}), 400

    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # Written beside the target and renamed: a half-written file here would
    # lose an afternoon of work, and rename is atomic on the same filesystem.
    target = _edits_path()
    temporary = target.with_suffix(".json.part")
    temporary.write_text(json.dumps(body["edits"], indent=1), encoding="utf-8")
    temporary.replace(target)
    return jsonify({"saved": len(body["edits"])})


@app.post("/api/reject/<path:name>")
def api_reject(name):
    """Move one photo out of the keepers, from inside the editor.

    Spotting a bad frame while editing used to mean going back to Cull and
    starting the sort again.
    """
    ref = keep_store.resolve(name)
    if ref is None:
        return jsonify({"error": "not found"}), 404

    reject_dir = config.OUTPUT_DIR / "reject"
    reject_dir.mkdir(parents=True, exist_ok=True)
    target = reject_dir / ref.path.name
    if target.exists():
        storage_module.trash_file(target, config.OUTPUT_DIR)
    ref.path.rename(target)
    # Its export has to go too, or Download keepers still ships the photo that
    # was just rejected: a single-photo export does not clear output/graded.
    stale = config.OUTPUT_DIR / "graded" / f"{ref.path.stem}.jpg"
    if stale.exists():
        storage_module.trash_file(stale, config.OUTPUT_DIR)
    return jsonify({"moved": ref.name, "to": "reject",
                    "left": len(keep_store.list_photos())})


@app.post("/api/empty-trash")
def api_empty_trash():
    """Delete everything in output/.trash, for real this time.

    Every other destructive thing in this app moves files here instead of
    deleting them, which means the folder only grows - it reached 671MB on the
    machine this was written on. Emptying it is therefore the one genuinely
    unrecoverable action in Photomation, and it is behind its own button and
    its own confirmation.
    """
    trash = config.OUTPUT_DIR / ".trash"
    removed, freed = 0, 0
    if trash.is_dir():
        for path in sorted(trash.iterdir()):
            if not path.is_file():
                continue
            try:
                freed += path.stat().st_size
                path.unlink()
                removed += 1
            except OSError:
                pass
    return jsonify({"removed": removed, "freed_bytes": freed})


@app.get("/api/backend")
def api_backend():
    """Which storage this instance is running on, so the page can adapt."""
    return jsonify({"backend": "s3" if isinstance(store, storage_s3.S3Storage) else "local"})


@app.get("/api/trash")
def api_trash():
    """How much is sitting in the trash, so the button can say so."""
    trash = config.OUTPUT_DIR / ".trash"
    files = [p for p in trash.iterdir() if p.is_file()] if trash.is_dir() else []
    return jsonify({"files": len(files),
                    "bytes": sum(p.stat().st_size for p in files)})


@app.post("/api/new-batch")
def api_new_batch():
    """Finish with this shoot and start an empty one.

    Clears the photos and everything made from them - the keep/reject sort and
    any export - so the next shoot does not inherit half of the last one.
    Every file is moved to output/.trash, so a mistake is recoverable.
    """
    # Under S3 the photos come from the bucket, so clearing them locally only
    # means the next scan downloads them all again. Saying "moved 62 files"
    # and changing nothing is worse than refusing.
    if isinstance(store, storage_s3.S3Storage):
        return jsonify({"error": "This batch comes from the bucket, so clearing "
                                 "it here would only download it again. Move or "
                                 "delete the objects in S3 instead."}), 409
    moved = storage_module.clear_batch(config.INPUT_DIR, config.OUTPUT_DIR)
    return jsonify({"moved": moved, "trash": str(config.OUTPUT_DIR / ".trash")})


@app.get("/api/download/<bucket>")
def api_download(bucket):
    """Download a whole output folder as one zip, so no Finder digging."""
    folder = _bucket_dir(bucket)
    if folder is None:
        return jsonify({"error": "unknown folder"}), 404
    photos = [p for p in sorted(folder.iterdir())
              if p.is_file() and p.suffix.lower() in cull.EXTENSIONS] if folder.is_dir() else []
    if not photos:
        return jsonify({"error": f"nothing in {bucket} yet"}), 404

    # Written to a temporary file rather than held in memory: a shoot of
    # full-size JPEGs is far too big to build as one bytes object.
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_STORED) as archive:
        for path in photos:
            archive.write(path, arcname=path.name)

    response = send_file(tmp.name, as_attachment=True,
                         download_name=f"photomation-{bucket}.zip",
                         mimetype="application/zip")

    @response.call_on_close
    def _cleanup():
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    return response


@app.get("/api/health")
def api_health():
    """Liveness probe. The one route that is never behind the password.

    A health check is made by the orchestrator, which has no credentials and
    should not be given any, so this answers without them - and says nothing
    about the machine it is running on when a password is set, since anyone
    at all can ask it.
    """
    if config.PASSWORD:
        return jsonify({"status": "ok"})
    return jsonify({"status": "ok", "input_dir": str(config.INPUT_DIR)})


def _sse(payload):
    return f"data: {json.dumps(payload)}\n\n"


if __name__ == "__main__":
    print(f"Photomation -> http://{config.HOST}:{config.PORT}")
    print(f"Reading photos from {config.INPUT_DIR}")
    app.run(host=config.HOST, port=config.PORT, debug=False, threaded=True)
