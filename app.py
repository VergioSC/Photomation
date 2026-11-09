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
