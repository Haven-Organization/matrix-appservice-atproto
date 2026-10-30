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
import contextlib
import io
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, ImageSequence

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


# Animations become video at this frame rate; each frame is held for as
# many video frames as its own duration covers.
_ANIMATION_FPS = 25
# Longest side of the video; larger animations are scaled down.
_ANIMATION_MAX_SIDE = 1080
# Longer animations are cut off here.
_ANIMATION_MAX_SECONDS = 60
_FFMPEG_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class PreparedVideo:
    data: bytes
    width: int
    height: int


async def animation_to_mp4(data: bytes) -> PreparedVideo | None:
    """An animated image (GIF, WebP, APNG) as an MP4 video, since Bluesky
    shows images only as still pictures (its own app turns an uploaded GIF
    into a video the same way). ``None`` if ``data`` isn't an animation, or
    can't be converted -- ffmpeg not installed, say -- in which case the
    caller publishes the image as it is."""
    return await asyncio.to_thread(_animation_to_mp4, data)


def _animation_to_mp4(data: bytes) -> PreparedVideo | None:
    try:
        image = Image.open(io.BytesIO(data))
        if not getattr(image, "is_animated", False):
            return None
    except Exception:
        return None
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        logger.info("ffmpeg is not installed; publishing an animated image as a still one")
        return None
    width, height = image.size
    scale = min(1.0, _ANIMATION_MAX_SIDE / max(width, height))
    # H.264 in yuv420p needs even dimensions.
    out_w, out_h = max(2, int(width * scale) // 2 * 2), max(2, int(height * scale) // 2 * 2)
    with tempfile.TemporaryDirectory() as tmp:
        output, log = Path(tmp) / "out.mp4", Path(tmp) / "ffmpeg.log"
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{out_w}x{out_h}", "-r", str(_ANIMATION_FPS), "-i", "pipe:0",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", "-an", "-f", "mp4", str(output),
        ]  # fmt: skip
        try:
            with log.open("wb") as stderr:
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=stderr)
                try:
                    assert process.stdin is not None
                    _write_frames(image, process.stdin, (out_w, out_h))
                    process.stdin.close()
                    process.wait(timeout=_FFMPEG_TIMEOUT_SECONDS)
                except BaseException:
                    process.kill()
                    process.wait()
                    with contextlib.suppress(OSError):
                        process.stdin.close()  # unflushed frames, for an ffmpeg gone
                    raise
        except (OSError, subprocess.SubprocessError, ValueError):
            logger.warning("Could not convert an animated image to video", exc_info=True)
            return None
        if process.returncode != 0 or not output.exists():
            logger.warning("ffmpeg failed to convert an animated image: %s", log.read_text(errors="replace")[:500])
            return None
        video = output.read_bytes()
    if not video or len(video) > VIDEO_MAX_BYTES:
        return None
    return PreparedVideo(video, out_w, out_h)


def _write_frames(image: Image.Image, sink, size: tuple[int, int]) -> None:
    """Each frame, flattened onto white and scaled to ``size``, written as
    raw RGB once per video frame it lasts for."""
    shown = 0  # video frames written so far
    elapsed = 0.0  # milliseconds of animation so far
    limit = _ANIMATION_MAX_SECONDS * 1000
    for frame in ImageSequence.Iterator(image):
        duration = frame.info.get("duration") or 0
        # Browsers show frames given no (or a tiny) duration for 100ms.
        elapsed += duration if duration > 10 else 100
        target = round(min(elapsed, limit) * _ANIMATION_FPS / 1000)
        rgba = frame.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.getchannel("A"))
        if flat.size != size:
            flat = flat.resize(size, Image.Resampling.LANCZOS)
        raw = flat.tobytes()
        for _ in range(max(target - shown, 1 if shown == 0 else 0)):
            sink.write(raw)
            shown += 1
        if elapsed >= limit:
            break


def format_size(size: int) -> str:
    for unit, scale in (("GB", 1 << 30), ("MB", 1 << 20), ("kB", 1 << 10)):
        if size >= scale:
            return f"{size / scale:.1f} {unit}"
    return f"{size} bytes"
