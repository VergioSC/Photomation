#!/usr/bin/env python3
"""Photomation web app: a landing page and a culling dashboard.

Run it with:
    .venv/bin/python app.py
then open http://127.0.0.1:5000

The dashboard scores every photo ONCE and sends the scores to the browser. The
threshold slider then re-buckets those scores client-side, so tuning is instant
instead of needing a rescan. Nothing is written to output/ until you press Apply.
"""

import hashlib
import json
import os
import pathlib
import tempfile
import zipfile

import cv2
import numpy as np
from flask import Flask, Response, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

import config
import cull
import edit as edit_module
import lut as lut_module
from storage import LocalStorage

app = Flask(__name__)
# RAW files run to 50-100 MB each, so a batch upload needs real headroom.
app.config["MAX_CONTENT_LENGTH"] = config.MAX_UPLOAD_BYTES
store = LocalStorage()
# The grading step works on what survived culling, so it lists output/keep.
keep_store = LocalStorage(input_dir=config.KEEP_DIR)


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
                payload = {
                    "type": "photo",
                    "index": index,
                    "name": ref.name,
                    "score": round(score, 1),
                    "decode": label,
                    "size_bytes": ref.size_bytes,
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
    """Serve a cached thumbnail. `name` is untrusted, so it is resolved by lookup."""
    ref = store.resolve(name)
    if ref is None:
        return jsonify({"error": "not found"}), 404
    try:
        data = store.scaled_bytes(ref)
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
    """Names of the photos grading will work on (output/keep)."""
    return jsonify({"photos": [r.name for r in keep_store.list_photos()]})


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

    out = edit_module.apply_adjustments(source, _adjustments_from_args(request.args), table)
    ok, buffer = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        return jsonify({"error": "could not encode preview"}), 500
    return Response(buffer.tobytes(), mimetype="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/grade")
def api_grade():
    """Render every kept photo at full resolution, streaming progress."""
    table, error = _lut_from_args(request.args)
    if error is not None:
        return error
    adj = _adjustments_from_args(request.args)

    refs = keep_store.list_photos()
    graded_dir = config.OUTPUT_DIR / "graded"

    def generate():
        yield _sse({"type": "start", "total": len(refs)})
        graded_dir.mkdir(parents=True, exist_ok=True)
        for index, ref in enumerate(refs, start=1):
            try:
                # Full resolution: an exported photo is the deliverable.
                image, _ = cull.load_image(ref.path, reduced=False)
                out = edit_module.apply_adjustments(image, adj, table)
                target = graded_dir / f"{ref.path.stem}.jpg"
                if not cv2.imwrite(str(target), out, [cv2.IMWRITE_JPEG_QUALITY, 95]):
                    raise cull.LoadError("could not write the edited file")
                payload = {"type": "photo", "index": index, "name": ref.name,
                           "out": target.name}
            except (cull.LoadError, lut_module.CubeError) as exc:
                payload = {"type": "skipped", "index": index, "name": ref.name,
                           "reason": str(exc)}
            yield _sse(payload)
        yield _sse({"type": "done", "dir": str(graded_dir)})

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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

    return jsonify({"saved": saved, "rejected": rejected,
                    "total": len(store.list_photos())})


@app.post("/api/remove/<path:name>")
def api_remove(name):
    """Take a photo out of the input folder.

    It is MOVED to output/.trash rather than deleted, so a misclick cannot
    lose a picture.
    """
    ref = store.resolve(name)
    if ref is None:
        return jsonify({"error": "not found"}), 404
    trash = config.OUTPUT_DIR / ".trash"
    trash.mkdir(parents=True, exist_ok=True)
    target = trash / ref.name
    counter = 1
    while target.exists():
        target = trash / f"{target.stem}-{counter}{target.suffix}"
        counter += 1
    ref.path.rename(target)
    return jsonify({"moved_to": str(target), "remaining": len(store.list_photos())})


BUCKETS = {"keep": None, "reject": None, "graded": None}


def _bucket_dir(bucket):
    if bucket not in BUCKETS:
        return None
    return config.OUTPUT_DIR / bucket


@app.get("/api/files/<bucket>")
def api_files(bucket):
    """What is in one output folder, for the download list."""
    folder = _bucket_dir(bucket)
    if folder is None:
        return jsonify({"error": "unknown folder"}), 404
    if not folder.is_dir():
        return jsonify({"bucket": bucket, "files": []})
    files = [
        {"name": p.name, "size_bytes": p.stat().st_size}
        for p in sorted(folder.iterdir())
        if p.is_file() and p.suffix.lower() in cull.EXTENSIONS
    ]
    return jsonify({"bucket": bucket, "files": files})


@app.get("/api/file/<bucket>/<path:name>")
def api_file(bucket, name):
    """Download one finished photo."""
    folder = _bucket_dir(bucket)
    if folder is None:
        return jsonify({"error": "unknown folder"}), 404
    # Matched against the listing, never joined, so the name cannot escape.
    for path in folder.iterdir() if folder.is_dir() else []:
        if path.is_file() and path.name == name:
            return send_file(path, as_attachment=True, download_name=name)
    return jsonify({"error": "not found"}), 404


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
    """Liveness probe, for a container orchestrator later."""
    return jsonify({"status": "ok", "input_dir": str(config.INPUT_DIR)})


def _sse(payload):
    return f"data: {json.dumps(payload)}\n\n"


if __name__ == "__main__":
    print(f"Photomation -> http://{config.HOST}:{config.PORT}")
    print(f"Reading photos from {config.INPUT_DIR}")
    app.run(host=config.HOST, port=config.PORT, debug=False, threaded=True)
