"""Compact previews of a post that is in Matrix, for messages ABOUT it.

A repost card, a "quoted X's post" tail, and a "liked your post"
notification all show a little of the post they refer to. Same
conventions as matrix-appservice-activitypub:

- The preview text is the post's own, cut to a couple of hundred
  characters.
- If the post is an image or video, the message about it is itself sent
  as an ``m.image``/``m.video`` -- reusing the post's own ``mxc://`` (no
  re-upload), with small ``info.w``/``info.h`` so it renders as a
  thumbnail rather than at full size -- and carries its text as a caption
  (``filename`` distinct from ``body``, MSC2530). One event, not two; and
  not an inline ``<img>``, which Element X doesn't render at all.
- "post" in the text is a matrix.to link to the event (an event pill),
  never a separate link line in the HTML.
"""

from __future__ import annotations

import html
import logging
from typing import Any

from fastapi import FastAPI

from bridge.msc4501 import SOCIAL_BODY_FIELD
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)

PREVIEW_TEXT_LIMIT = 200
# Longest side, in pixels, a preview is displayed at.
PREVIEW_IMAGE_SIZE = 96
PREVIEW_VIDEO_MAX_DIMENSION = 240

_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif", "video/mp4": "mp4"}


def _bot(app: FastAPI) -> str:
    config = app.state.config
    return f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"


def scaled_dimensions(width: Any, height: Any, max_dimension: int) -> tuple[int, int]:
    """Dimensions scaled so the longer side is ``max_dimension``, keeping
    the aspect ratio; a square if the original's aren't known."""
    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        return max_dimension, max_dimension
    if width >= height:
        return max_dimension, max(1, round(height * max_dimension / width))
    return max(1, round(width * max_dimension / height)), max_dimension


def post_text_of(content: dict[str, Any]) -> str:
    """The text of a post's Matrix event: its body, unless that is just
    the filename of an uncaptioned image or video. For a quote-post or a
    cross-posted reply, the author's own words (MSC4501's body), without
    the bridge's "reposted X's post" or "Reply to" lines."""
    own = content.get(SOCIAL_BODY_FIELD)
    body = (own if isinstance(own, str) and own.strip() else str(content.get("body") or "")).strip()
    filename = str(content.get("filename") or "").strip()
    if content.get("msgtype") in ("m.image", "m.video") and (not filename or body == filename):
        return ""
    return body


async def fetch_post_preview(
    app: FastAPI, room_id: str, event_id: str
) -> tuple[str, dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
    """``(truncated text, full content, image or None, video or None)`` of
    the post at that event; ``("", {}, None, None)`` if it can't be
    fetched. ``image``/``video`` are ``{"mxc", "mimetype", "width",
    "height", "filename"}``, already scaled down to preview size."""
    try:
        event = await app.state.synapse.get_event(room_id, event_id, as_user_id=_bot(app))
    except SynapseError:
        logger.info("Could not fetch %s in %s for a preview", event_id, room_id, exc_info=True)
        return "", {}, None, None
    content = (event or {}).get("content") or {}
    text = post_text_of(content)
    if len(text) > PREVIEW_TEXT_LIMIT:
        text = text[:PREVIEW_TEXT_LIMIT].rstrip() + "…"
    msgtype = content.get("msgtype")
    info = content.get("info") if isinstance(content.get("info"), dict) else {}
    mxc = content.get("url")
    image = video = None
    if isinstance(mxc, str) and msgtype in ("m.image", "m.video"):
        default_type = "image/jpeg" if msgtype == "m.image" else "video/mp4"
        mimetype = str(info.get("mimetype") or default_type)
        limit = PREVIEW_IMAGE_SIZE if msgtype == "m.image" else PREVIEW_VIDEO_MAX_DIMENSION
        width, height = scaled_dimensions(info.get("w"), info.get("h"), limit)
        media = {
            "mxc": mxc,
            "mimetype": mimetype,
            "width": width,
            "height": height,
            # Without a separate filename, an uncaptioned attachment's
            # body is its filename.
            "filename": str(content.get("filename") or content.get("body") or "")
            or f"{msgtype[2:]}.{_EXTENSIONS.get(mimetype, 'bin')}",
        }
        image, video = (media, None) if msgtype == "m.image" else (None, media)
    return text, content, image, video


def build_preview_media_content(
    *, plain_body: str, formatted_caption: str, preview_image: dict[str, Any] | None, preview_video: dict[str, Any] | None
) -> dict[str, Any]:
    """The event content for a message with a preview: a plain ``m.text``
    if there is no media to show, else an ``m.image``/``m.video`` of the
    preview with the text as its caption."""
    media = preview_video or preview_image
    if media is None:
        return {"msgtype": "m.text", "body": plain_body, "format": "org.matrix.custom.html", "formatted_body": formatted_caption}
    return {
        "msgtype": "m.video" if preview_video is not None else "m.image",
        "body": plain_body,
        "filename": media["filename"],
        "format": "org.matrix.custom.html",
        "formatted_body": formatted_caption,
        "url": media["mxc"],
        "info": {"mimetype": media["mimetype"], "w": media["width"], "h": media["height"]},
    }


async def user_display(app: FastAPI, user_id: str) -> tuple[str, str]:
    """A Matrix user's name, and a user pill for them carrying that name
    (an empty pill anchor would vanish from notification text)."""
    try:
        profile = await app.state.synapse.get_profile(user_id)
    except SynapseError:
        profile = {}
    name = str(profile.get("displayname") or user_id)
    return name, f'<a href="https://matrix.to/#/{html.escape(user_id, quote=True)}">{html.escape(name)}</a>'


def quote_block(text: str) -> tuple[str, str]:
    """``text`` as a plain "> " quote and an HTML blockquote; both empty
    for empty text."""
    if not text:
        return "", ""
    return "\n".join(f"> {line}" for line in text.split("\n")), f"<blockquote>{html.escape(text).replace(chr(10), '<br>')}</blockquote>"
