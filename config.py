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

# Longest side of generated thumbnails, in pixels.
THUMB_SIZE = 480
