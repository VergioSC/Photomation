"""Configuration for Photomation.

Every path comes from an environment variable with a local default. That is the
whole "cloud-ready" trick: nothing in the app hardcodes a folder, so moving to a
container or a mounted volume is a matter of setting env vars, not editing code.

    PHOTOMATION_INPUT    folder to read photos from       (default: input)
    PHOTOMATION_OUTPUT   folder to write keep/reject into (default: output)
    PHOTOMATION_LUTS     folder holding .cube LUTs        (default: luts)
    PHOTOMATION_CACHE    thumbnail cache                  (default: .cache)
    PHOTOMATION_HOST     bind address                     (default: 127.0.0.1)
    PHOTOMATION_PORT     port                             (default: 5000)
    PHOTOMATION_THRESHOLD  default blur threshold         (default: 100)
"""

import os
from pathlib import Path


def _path(env_name, default):
    return Path(os.environ.get(env_name, default)).expanduser().resolve()


INPUT_DIR = _path("PHOTOMATION_INPUT", "input")
OUTPUT_DIR = _path("PHOTOMATION_OUTPUT", "output")
LUTS_DIR = _path("PHOTOMATION_LUTS", "luts")
CACHE_DIR = _path("PHOTOMATION_CACHE", ".cache")

KEEP_DIR = OUTPUT_DIR / "keep"
REJECT_DIR = OUTPUT_DIR / "reject"
THUMB_DIR = CACHE_DIR / "thumbs"

# Bind to localhost by default: this server reads your local photo folders, so it
# should not be reachable from the network unless you deliberately say so.
# A container sets PHOTOMATION_HOST=0.0.0.0 to accept forwarded traffic.
HOST = os.environ.get("PHOTOMATION_HOST", "127.0.0.1")
PORT = int(os.environ.get("PHOTOMATION_PORT", "5000"))

DEFAULT_THRESHOLD = float(os.environ.get("PHOTOMATION_THRESHOLD", "100"))

# Biggest single upload request. RAW files are 50-100 MB each, so a batch of
# them needs real headroom.
MAX_UPLOAD_BYTES = int(os.environ.get("PHOTOMATION_MAX_UPLOAD_MB", "2048")) * 1024 * 1024

# Longest side of grid and filmstrip thumbnails, in pixels.
THUMB_SIZE = 480

# How large the thumbnail cache may grow before the oldest entries are dropped.
# Every photo caches a file per size it has been asked for, and the editor asks
# for several, so without a ceiling this folder grows for as long as the app is
# used. Override with PHOTOMATION_CACHE_MB.
CACHE_LIMIT_MB = int(os.environ.get("PHOTOMATION_CACHE_MB", "512"))

# A batch is one shoot. Starting the server starts a new one, so a session does
# not open on the photos from the last shoot. Nothing is deleted - the old
# batch is moved to output/.trash. Set PHOTOMATION_KEEP_BATCH=1 to carry the
# previous batch over instead, which is what you want if the server crashed
# in the middle of one.
CLEAR_ON_START = os.environ.get("PHOTOMATION_KEEP_BATCH", "") not in ("1", "true", "yes")

# Where the photos come from. "local" reads input/; "s3" keeps input/ in step
# with a bucket (see storage_s3.py). Credentials are never read here: boto3
# takes them from the environment, ~/.aws or the instance role.
STORAGE_BACKEND = os.environ.get("PHOTOMATION_STORAGE", "local").lower()
S3_BUCKET = os.environ.get("PHOTOMATION_S3_BUCKET", "")
S3_PREFIX = os.environ.get("PHOTOMATION_S3_PREFIX", "")

# When set, every request must carry this password over HTTP Basic auth.
# Unset means no authentication, which is correct on 127.0.0.1 and wrong
# anywhere else - app.py refuses to listen on a public interface without it.
PASSWORD = os.environ.get("PHOTOMATION_PASSWORD", "")
USERNAME = os.environ.get("PHOTOMATION_USERNAME", "photomation")

# Longest side of the editor's preview. It is shown large on a Retina screen,
# where CSS pixels are doubled, so a 480px thumbnail was being blown up about
# five times and looked pixellated.
PREVIEW_SIZE = int(os.environ.get("PHOTOMATION_PREVIEW_PX", "1400"))

# A smaller render used while a slider is moving, swapped for the full-size one
# once it settles. Keeps dragging responsive without leaving the result soft.

# Guard rails for the size a browser may ask for.
PREVIEW_MIN, PREVIEW_MAX = 240, 2400

# Requested sizes are snapped up to one of these, so the cache holds a handful
# of files per photo rather than one per window width.
PREVIEW_STEPS = (480, 700, 1000, 1400, 1800, 2400)
