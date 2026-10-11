"""Getting Matrix media into a shape ATProto will take.

Bluesky has three places a file can go, and this module picks one:

- an image (JPEG/PNG/WebP/GIF, at most 1 MB) -> an image blob,
- an MP4 video -> a video blob,
- anything else (documents, audio, other video formats) -> no native form
  exists, so the post carries a link card pointing at the file, served
  publicly by the bridge from ``/media/...``.

An image is re-encoded when it is too big, in a format Bluesky doesn't
take, or carries metadata: a photo's EXIF block routinely includes GPS
coordinates, and a Profile Room is private where a Bluesky post is not.
The re-encoded file is uploaded back into the Matrix media repository, so
the blob's bytes still live in Matrix and nowhere else -- and so they are
the same bytes every time the blob is requested, which re-running an
encoder could not promise.
"""

from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

IMAGE_MAX_BYTES = 1_000_000
VIDEO_MAX_BYTES = 100_000_000
VIDEO_MIME_TYPE = "video/mp4"

_PASSTHROUGH_IMAGE_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif"}

# (longest side in pixels, JPEG quality), tried in order until the result
# fits. The first step keeps full resolution.
_REENCODE_STEPS = ((None, 90), (2000, 85), (2000, 75), (1600, 75), (1280, 70), (1024, 65), (800, 60), (600, 50))


@dataclass(frozen=True)
class PreparedImage:
    data: bytes
    mime_type: str
    width: int
    height: int
    # False: `data` is the original file, untouched.
    reencoded: bool


def _prepare_image(data: bytes) -> PreparedImage | None:
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception:  # Pillow raises a wide assortment for bad input
        logger.info("Could not decode image", exc_info=True)
        return None
    mime_type = _PASSTHROUGH_IMAGE_FORMATS.get(image.format or "")
    has_metadata = bool(image.getexif()) or "exif" in image.info
    if mime_type and len(data) <= IMAGE_MAX_BYTES and not has_metadata:
        return PreparedImage(data, mime_type, image.width, image.height, reencoded=False)

    # Apply the EXIF rotation before dropping the EXIF block that says so.
    image = ImageOps.exif_transpose(image)
    if image.mode in ("RGBA", "LA", "P"):
        # JPEG has no transparency: flatten onto white rather than black.
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        image = background
    else:
        image = image.convert("RGB")
    for longest_side, quality in _REENCODE_STEPS:
        candidate = image
        if longest_side is not None and max(image.size) > longest_side:
            candidate = image.copy()
            candidate.thumbnail((longest_side, longest_side), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        candidate.save(buffer, format="JPEG", quality=quality, optimize=True)
        if buffer.tell() <= IMAGE_MAX_BYTES:
            return PreparedImage(buffer.getvalue(), "image/jpeg", candidate.width, candidate.height, reencoded=True)
    return None


async def prepare_image(data: bytes) -> PreparedImage | None:
    """``data`` as an image Bluesky will accept, or ``None`` if it isn't a
    decodable image (or can't be made small enough)."""
    return await asyncio.to_thread(_prepare_image, data)


def format_size(size: int) -> str:
    for unit, scale in (("GB", 1 << 30), ("MB", 1 << 20), ("kB", 1 << 10)):
        if size >= scale:
            return f"{size / scale:.1f} {unit}"
    return f"{size} bytes"
