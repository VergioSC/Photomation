#!/usr/bin/env python3
"""Work out what the regions of a photo are, so edits can be selective.

Every adjustment elsewhere in this project is global: it moves all the pixels.
That is why darkening a sky for drama also buries the sand in front of it. A
human editor does not work that way - they darken the sky and lift the
foreground, because they know which is which.

This module produces soft masks (0..1 per pixel) for the regions that matter.
Today they are estimated from colour, position and texture, which needs no
model and no download. The interface is deliberately a single function
returning a SceneMasks, so a neural segmentation model can be put behind it
later without anything else changing - see estimate_classical and the
`backend` argument of analyse.
"""

from dataclasses import dataclass

import cv2
import numpy as np

# Masks are estimated small and scaled back up: a region boundary does not
# need full resolution, and this keeps a 26MP file affordable.
WORK_SIZE = 384


@dataclass
class SceneMasks:
    """Soft masks, the same size as the photo, values 0..1."""

    sky: np.ndarray

    @property
    def foreground(self):
        # Derived, not stored: on a 24MP photo a second full-resolution float
        # array is 96MB for a value that is one subtraction away.
        return 1.0 - self.sky

    @property
    def has_sky(self):
        return float(self.sky.mean()) > 0.03


def analyse(image_bgr):
    """Return SceneMasks for a BGR image."""
    height, width = image_bgr.shape[:2]
    scale = WORK_SIZE / max(height, width)
    small = (cv2.resize(image_bgr, (max(1, int(width * scale)), max(1, int(height * scale))),
                        interpolation=cv2.INTER_AREA) if scale < 1 else image_bgr)

    sky = cv2.resize(estimate_classical_sky(small), (width, height),
                     interpolation=cv2.INTER_LINEAR)
    return SceneMasks(sky=np.clip(sky, 0.0, 1.0).astype(np.float32))


def estimate_classical_sky(small_bgr):
    """Find sky from the things that make sky look like sky.

    It sits at the top of the frame, it is smooth where land is textured, it
    is brighter than what is under it, and it leans blue or neutral-grey
    rather than warm. None of those is decisive alone - a white wall is smooth
    and bright too - so they are scored together, and the result must connect
    to the top edge, because a patch of sky in the middle of a photo with
    ground above it is not sky.
    """
    x = small_bgr.astype(np.float32) / 255.0
    height, width = x.shape[:2]
    blue, green, red = x[:, :, 0], x[:, :, 1], x[:, :, 2]
    luma = np.sum(x * np.array([0.0722, 0.7152, 0.2126], np.float32), axis=2)

    # Smoothness: sky has little fine detail.
    detail = cv2.GaussianBlur(np.abs(luma - cv2.GaussianBlur(luma, (0, 0), 2.0)), (0, 0), 6.0)
    smooth = np.exp(-detail * 55.0)

    # Height: strongly favours the top, but does not forbid a low horizon.
    rows = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None]
    high = np.clip(1.0 - rows * 1.4, 0.0, 1.0)

    # Brightness relative to the frame.
    bright = np.clip((luma - float(np.percentile(luma, 35))) * 3.0, 0.0, 1.0)

    # Cool rather than warm. Grey counts: overcast skies have no blue at all.
    coolness = np.clip(0.5 + (blue - red) * 3.5, 0.0, 1.0)

    score = smooth * (0.45 + 0.55 * high) * (0.35 + 0.65 * bright) * (0.4 + 0.6 * coolness)
    score = cv2.GaussianBlur(score, (0, 0), 3.0)

    # Must reach the top of the frame: flood from the top row through the
    # plausible area, so bright smooth ground is not mistaken for sky.
    seed = (score > 0.35).astype(np.uint8)
    connected = np.zeros_like(seed)
    number, labels = cv2.connectedComponents(seed)[:2]
    top_labels = set(np.unique(labels[0:max(1, height // 25), :])) - {0}
    for label in top_labels:
        connected[labels == label] = 1
    if not connected.any():
        return np.zeros((height, width), np.float32)

    mask = score * cv2.GaussianBlur(connected.astype(np.float32), (0, 0), 4.0)
    mask = np.clip((mask - 0.18) * 3.2, 0.0, 1.0)

    # Follow the real edge between sky and land rather than a blurred guess.
    # (A guided filter would be better here, but it lives in cv2.ximgproc,
    # which is not in the opencv-python-headless build this project pins.)
    mask = cv2.bilateralFilter(mask, 9, 0.2, 9)
    return np.clip(mask, 0.0, 1.0).astype(np.float32)
