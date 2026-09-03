"""Everything computed from a single thumbnail: perceptual hashes + quality checks."""

from __future__ import annotations

import io

import imagehash
import numpy as np
from PIL import Image, ImageFilter

from photo_sort.model import FlagReason

# tuning knobs (deliberately conservative — better to under-flag than nag)
SHARPNESS_BLUR_BELOW = 40.0     # variance-of-Laplacian on a 256px thumb
DARK_MEAN_BELOW = 32.0         # mean luminance 0-255
BRIGHT_MEAN_ABOVE = 233.0


class ThumbFacts:
    __slots__ = ("phash", "dhash", "width", "height", "sharpness", "brightness", "flags")

    def __init__(self, phash, dhash, width, height, sharpness, brightness, flags):
        self.phash = phash
        self.dhash = dhash
        self.width = width
        self.height = height
        self.sharpness = sharpness
        self.brightness = brightness
        self.flags = flags


def analyse_thumbnail(jpeg_bytes: bytes, full_width: int | None, full_height: int | None) -> ThumbFacts:
    with Image.open(io.BytesIO(jpeg_bytes)) as im:
        im = im.convert("RGB")
        phash = str(imagehash.phash(im))
        dhash = str(imagehash.dhash(im))

        gray = im.convert("L")
        arr = np.asarray(gray, dtype=np.float64)
        brightness = float(arr.mean())

        # variance of Laplacian ~ focus. Pillow has no Laplacian kernel builtin;
        # FIND_EDGES is close enough as a relative sharpness signal on thumbnails.
        edges = np.asarray(gray.filter(ImageFilter.FIND_EDGES), dtype=np.float64)
        sharpness = float(edges.var())

    flags: list[FlagReason] = []
    if sharpness < SHARPNESS_BLUR_BELOW:
        flags.append(FlagReason.BLURRY)
    if brightness < DARK_MEAN_BELOW:
        flags.append(FlagReason.TOO_DARK)
    if brightness > BRIGHT_MEAN_ABOVE:
        flags.append(FlagReason.TOO_BRIGHT)

    return ThumbFacts(
        phash=phash,
        dhash=dhash,
        width=full_width,
        height=full_height,
        sharpness=sharpness,
        brightness=brightness,
        flags=flags,
    )
