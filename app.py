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

import cv2
from flask import Flask, Response, jsonify, render_template, request

import config
import cull
import lut as lut_module
from storage import LocalStorage

app = Flask(__name__)
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
        data = store.thumbnail_bytes(ref)
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


@app.get("/api/preview/<path:name>")
def api_preview(name):
    """A graded thumbnail, for the side-by-side preview."""
    ref = keep_store.resolve(name)
    if ref is None:
        return jsonify({"error": "not found"}), 404

    lut_name = request.args.get("lut", "")
    try:
        table, error = _load_lut_or_400(lut_name)
    except lut_module.CubeError as exc:
        return jsonify({"error": str(exc)}), 422
    if error is not None:
        return error

    cached = config.THUMB_DIR / f"{ref.cache_key}-{_lut_key(lut_name)}.jpg"
    if cached.exists():
        data = cached.read_bytes()
    else:
        try:
            thumb = keep_store.thumbnail_array(ref)
        except cull.LoadError as exc:
            return jsonify({"error": str(exc)}), 422
        graded = lut_module.apply_lut(thumb, table)
        ok, buffer = cv2.imencode(".jpg", graded, [cv2.IMWRITE_JPEG_QUALITY, 82])
        if not ok:
            return jsonify({"error": "could not encode preview"}), 500
        data = buffer.tobytes()
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(data)
    return Response(data, mimetype="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"})


def _lut_key(name):
    """Part of the preview cache name: changes when the LUT file changes."""
    path = config.LUTS_DIR / name
    stamp = path.stat().st_mtime if path.is_file() else 0
    return hashlib.sha256(f"{name}|{stamp}".encode()).hexdigest()[:12]


@app.get("/api/grade")
def api_grade():
    """Grade every kept photo at full resolution, streaming progress."""
    lut_name = request.args.get("lut", "")
    try:
        table, error = _load_lut_or_400(lut_name)
    except lut_module.CubeError as exc:
        return jsonify({"error": str(exc)}), 422
    if error is not None:
        return error

    refs = keep_store.list_photos()
    graded_dir = config.OUTPUT_DIR / "graded"

    def generate():
        yield _sse({"type": "start", "total": len(refs)})
        graded_dir.mkdir(parents=True, exist_ok=True)
        for index, ref in enumerate(refs, start=1):
            try:
                # Full resolution: a graded photo is the deliverable.
                image, _ = cull.load_image(ref.path, reduced=False)
                out = lut_module.apply_lut(image, table)
                target = graded_dir / f"{ref.path.stem}.jpg"
                if not cv2.imwrite(str(target), out, [cv2.IMWRITE_JPEG_QUALITY, 95]):
                    raise cull.LoadError("could not write the graded file")
                payload = {"type": "photo", "index": index, "name": ref.name,
                           "out": target.name}
            except (cull.LoadError, lut_module.CubeError) as exc:
                payload = {"type": "skipped", "index": index, "name": ref.name,
                           "reason": str(exc)}
            yield _sse(payload)
        yield _sse({"type": "done", "dir": str(graded_dir)})

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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
