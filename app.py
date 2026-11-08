#!/usr/bin/env python3
"""Photomation web app: a landing page and a culling dashboard.

Run it with:
    .venv/bin/python app.py
then open http://127.0.0.1:5000

The dashboard scores every photo ONCE and sends the scores to the browser. The
threshold slider then re-buckets those scores client-side, so tuning is instant
instead of needing a rescan. Nothing is written to output/ until you press Apply.
"""

import json

from flask import Flask, Response, jsonify, render_template, request

import config
import cull
from storage import LocalStorage

app = Flask(__name__)
store = LocalStorage()


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


@app.get("/api/photos")
def api_photos():
    """Cheap listing, so the UI can show a count before any decoding happens."""
    refs = store.list_photos()
    return jsonify(
        {
            "count": len(refs),
            "input_dir": str(config.INPUT_DIR),
            "photos": [{"name": r.name, "size_bytes": r.size_bytes} for r in refs],
        }
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
    """Copy originals into output/keep and output/reject for a given threshold.

    This is the only endpoint that writes anything. It copies with copy2 and
    never moves or deletes, so input/ is untouched whatever happens here.
    """
    body = request.get_json(silent=True) or {}
    try:
        threshold = float(body.get("threshold", config.DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        return jsonify({"error": "threshold must be a number"}), 400
    if threshold < 0:
        return jsonify({"error": "threshold must be >= 0"}), 400

    scores = body.get("scores")
    if not isinstance(scores, dict) or not scores:
        return jsonify({"error": "no scores supplied; run a scan first"}), 400

    kept, rejected, missing = [], [], []
    for name, raw_score in scores.items():
        ref = store.resolve(name)
        if ref is None:
            missing.append(name)
            continue
        try:
            score = float(raw_score)
        except (TypeError, ValueError):
            missing.append(name)
            continue
        bucket = "keep" if score >= threshold else "reject"
        store.copy_to_bucket(ref, bucket)
        (kept if bucket == "keep" else rejected).append(name)

    return jsonify(
        {
            "threshold": threshold,
            "kept": len(kept),
            "rejected": len(rejected),
            "missing": missing,
            "keep_dir": str(config.KEEP_DIR),
            "reject_dir": str(config.REJECT_DIR),
        }
    )


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
