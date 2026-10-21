"""Turning Matrix events into ATProto records -- and back into the same
records again, later.

The bridge doesn't keep record content (see ``bridge.record_sources``), so
every conversion here runs at least twice: once when the Matrix event
arrives, and again whenever the record is requested, straight from Synapse.
Both runs must produce identical bytes. That makes each converter a frozen
contract:

- It may depend ONLY on the Matrix event/state itself, never on the clock,
  configuration, or anything else that can differ between runs.
- Once records exist under a version it must never change. To convert
  differently, add ``..._v2`` and write new records with ``"v": 2``;
  existing records keep rebuilding with the version they were made with.

Posts: v1 is the original. v2 adds ``trailing``, a link dropped from the
end of the text because the post quotes what it links to (as the Bluesky
app does with a pasted post link); with no ``trailing`` it is v1. v3
replaces it with ``drop_link_to``, the address of the quoted post: the
link itself is found again in the event (``quoted_link_suffix``) rather
than kept, since it is part of what the user wrote. (Every piece of a
thread carries the address, as only the first carries the quote.) New
records are v1 or v3; v2 is only read.

Repost cards (``matrix_repost_card``): v1 is the original. A post linking
to something reposted through another bridge sharing the Profile Room
(``repost_card_record_v1``); ``thumb`` is the only thing kept.

Profiles: v1 is the original. v2 adds ``pinned``, the post pinned in the
Profile Room (``{"event_id", "ref"}``, ``ref`` a strong reference to it);
with no ``pinned`` it is v1.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from bridge.atproto import lexjson
from bridge.record_sources import RecordSources
from bridge.synapse_client import SynapseClient

POST_COLLECTION = "app.bsky.feed.post"
PROFILE_COLLECTION = "app.bsky.actor.profile"
PROFILE_RKEY = "self"

# Bluesky's limits are in graphemes (300 for a post, 64 for a display name,
# 256 for a bio). A grapheme is one or more code points, so staying within
# the same number of CODE POINTS is always within the limit -- stricter
# than necessary for text with combined characters or emoji sequences, but
# needs no Unicode segmentation data, which would itself have to be frozen.
MAX_POST_LENGTH = 300
# A message longer than one post becomes a thread; beyond this many posts
# it is refused rather than flooding followers' timelines.
MAX_THREAD_POSTS = 25
_MAX_DISPLAY_NAME_LENGTH = 64
_MAX_DESCRIPTION_LENGTH = 256

MEDIA_MSGTYPES = ("m.image", "m.video", "m.audio", "m.file")
EXTERNAL_EMBED_TYPE = "app.bsky.embed.external"
RECORD_EMBED_TYPE = "app.bsky.embed.record"
RECORD_WITH_MEDIA_EMBED_TYPE = "app.bsky.embed.recordWithMedia"

# MSC4501 fields read from a repost card (as in bridge.msc4501; repeated
# here so this module stays free of other bridge imports).
_SOCIAL_RELATES_TO_FIELD = "org.matrix.msc4501.social.relates_to"
_SOCIAL_REL_TYPE_REPOST = "org.matrix.msc4501.social.repost"
_SOCIAL_BODY_FIELD = "org.matrix.msc4501.social.body"
_MAX_LINK_LENGTH = 2000
_MAX_CARD_TITLE_LENGTH = 64
_MAX_CARD_DESCRIPTION_LENGTH = 300

_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_URL_TRAILING_PUNCTUATION = ".,;:!?)]}"


def _timestamp(origin_server_ts: int) -> str:
    moment = datetime.fromtimestamp(origin_server_ts / 1000, tz=timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def is_redacted(event: dict[str, Any]) -> bool:
    return "redacted_because" in (event.get("unsigned") or {}) or not event.get("content")


def media_filename(event: dict[str, Any]) -> str:
    """The name of the file a media message carries. Matrix puts it in
    ``filename`` when the message also has a caption, else in ``body``."""
    content = event.get("content") or {}
    filename = content.get("filename")
    if isinstance(filename, str) and filename:
        return filename
    body = content.get("body")
    return body if isinstance(body, str) and body else "file"


def message_body(content: dict[str, Any]) -> str:
    """A message's body without the quoted "> ..." fallback that some
    clients prepend to a reply -- the text the sender actually wrote."""
    body = content.get("body")
    if not isinstance(body, str):
        return ""
    if ((content.get("m.relates_to") or {}).get("m.in_reply_to") or {}).get("event_id"):
        lines = body.split("\n")
        while lines and lines[0].startswith(">"):
            lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
        body = "\n".join(lines)
    return body


def post_text(event: dict[str, Any], *, offset: int = 0, trailing: str | None = None) -> str | None:
    """The text a Matrix message would post: a text message's body, or a
    media message's caption (possibly empty). ``None`` if the message is of
    a kind that can't be a post at all.

    ``offset`` skips that many leading characters of the body: how a
    command's own argument (``]repost <caption>``) becomes a post without
    the command word. ``trailing`` is dropped from the end of the text if
    it is there: a link the post quotes instead, which may leave a text
    message with no text of its own."""
    content = event.get("content") or {}
    msgtype = content.get("msgtype")
    body = message_body(content)[offset:].strip()
    if trailing and body.endswith(trailing):
        body = body[: -len(trailing)].rstrip()
    if msgtype == "m.text":
        return body if body or trailing else None
    if msgtype in MEDIA_MSGTYPES:
        filename = content.get("filename")
        has_caption = isinstance(filename, str) and filename and filename != content.get("body")
        return body if has_caption else ""
    return None


def split_text(text: str, limit: int = MAX_POST_LENGTH) -> list[str]:
    """``text`` cut into pieces of at most ``limit`` characters, breaking
    at whitespace where there is any in the second half of a piece (so a
    single very long word doesn't leave a nearly empty post before it)."""
    parts: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[: limit + 1]
        cut = max(window.rfind(" "), window.rfind("\n"))
        if cut < limit // 2:
            cut = limit
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    parts.append(rest)
    return parts


def post_parts(event: dict[str, Any], *, offset: int = 0, trailing: str | None = None) -> list[str] | None:
    """The text of each post this message becomes: one, or a thread of
    several if it is longer than a post may be. ``None`` if the message
    can't be a post."""
    text = post_text(event, offset=offset, trailing=trailing)
    return None if text is None else split_text(text)


def link_urls(text: str) -> list[str]:
    """Every link in ``text``, as ``_link_facets`` marks them up."""
    return [match.group(0).rstrip(_URL_TRAILING_PUNCTUATION) for match in _URL_RE.finditer(text)]


def _link_facets(text: str) -> list[dict[str, Any]]:
    # Links are only clickable on Bluesky if marked up as "facets", which
    # address the text by UTF-8 byte offset.
    facets = []
    for match in _URL_RE.finditer(text):
        url = match.group(0).rstrip(_URL_TRAILING_PUNCTUATION)
        start = len(text[: match.start()].encode("utf-8"))
        facets.append(
            {
                "index": {"byteStart": start, "byteEnd": start + len(url.encode("utf-8"))},
                "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}],
            }
        )
    return facets


# A post's web address, as bsky.app (and other ATProto web clients) write it:
# the account (handle or DID) and the post's record key.
POST_LINK_RE = re.compile(r"^https?://[^/]+/profile/([^/?#]+)/post/([A-Za-z0-9._:~-]+)/?$")


def quoted_link_suffix(event: dict[str, Any], *, offset: int = 0, quoted: str | None = None) -> str | None:
    """The link to post ``quoted`` that ends the message's text, if it does
    -- what a quote-post drops from its text. It must be the last link and
    nothing may follow it, and its record key must be the quoted post's.
    (The account in it isn't compared: a handle can't be checked without
    asking the network, and a record key is unique enough on its own.)"""
    if not quoted:
        return None
    body = message_body(event.get("content") or {})[offset:].strip()
    urls = link_urls(body)
    if not urls or not body.endswith(urls[-1]):
        return None
    match = POST_LINK_RE.match(urls[-1])
    if match is None or quoted.rsplit("/", 1)[-1] != match.group(2):
        return None
    return urls[-1]


def post_record_v1(
    event: dict[str, Any],
    *,
    part: int = 0,
    embed: dict[str, Any] | None = None,
    reply: dict[str, Any] | None = None,
    offset: int = 0,
) -> dict[str, Any] | None:
    return post_record_v2(event, part=part, embed=embed, reply=reply, offset=offset)


def post_record_v2(
    event: dict[str, Any],
    *,
    part: int = 0,
    embed: dict[str, Any] | None = None,
    reply: dict[str, Any] | None = None,
    offset: int = 0,
    trailing: str | None = None,
) -> dict[str, Any] | None:
    """The ``app.bsky.feed.post`` for piece ``part`` of a Matrix message;
    ``None`` if the event can no longer produce it (redacted, not a
    postable kind).

    ``embed`` (attached media) and ``reply`` (the link to the previous
    piece of a thread) are not derivable from the event -- they hold blob
    hashes and record addresses fixed when the post was first made -- so
    the caller supplies them. The one exception is a link card's title,
    which is the file's name and so is taken from the event here rather
    than kept anywhere else.

    ``trailing`` (v2 only; v1 is v2 without it): see ``post_text``.
    v3 is v2 with ``trailing`` found in the event (``post_record_v3``).
    """
    if is_redacted(event):
        return None
    parts = post_parts(event, offset=offset, trailing=trailing)
    if parts is None or part >= len(parts):
        return None
    text = parts[part]
    record: dict[str, Any] = {
        "$type": POST_COLLECTION,
        "text": text,
        "createdAt": _timestamp(int(event.get("origin_server_ts", 0))),
    }
    facets = _link_facets(text)
    if facets:
        record["facets"] = facets
    if embed is not None:
        if embed.get("$type") == EXTERNAL_EMBED_TYPE:
            embed = {**embed, "external": {**embed["external"], "title": media_filename(event)}}
        elif embed.get("$type") == RECORD_WITH_MEDIA_EMBED_TYPE and embed["media"].get("$type") == EXTERNAL_EMBED_TYPE:
            media = embed["media"]
            embed = {**embed, "media": {**media, "external": {**media["external"], "title": media_filename(event)}}}
        record["embed"] = embed
    if reply is not None:
        record["reply"] = reply
    return record


def post_record_v3(
    event: dict[str, Any],
    *,
    part: int = 0,
    embed: dict[str, Any] | None = None,
    reply: dict[str, Any] | None = None,
    offset: int = 0,
    drop_link_to: str | None = None,
) -> dict[str, Any] | None:
    """v2, but the dropped link is found in the event rather than stored:
    the link to post ``drop_link_to`` ending the text, if there is one."""
    trailing = quoted_link_suffix(event, offset=offset, quoted=drop_link_to)
    return post_record_v2(event, part=part, embed=embed, reply=reply, offset=offset, trailing=trailing)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def repost_relation(content: dict[str, Any]) -> dict[str, Any] | None:
    """The MSC4501 repost relation on a message, if it is a repost card."""
    relation = content.get(_SOCIAL_RELATES_TO_FIELD)
    if isinstance(relation, dict) and relation.get("rel_type") == _SOCIAL_REL_TYPE_REPOST:
        return relation
    return None


def repost_card_link(event: dict[str, Any]) -> str | None:
    """Where the reposted post is on the web: the card's ``external_url``,
    as the bridges set it on what they mirror. ``None`` if it has none."""
    url = (event.get("content") or {}).get("external_url")
    if isinstance(url, str) and len(url) <= _MAX_LINK_LENGTH and re.fullmatch(r"https?://[^\s<>\"']+", url):
        return url
    return None


def repost_card_record_v1(event: dict[str, Any], *, thumb: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The post standing for a repost made through another bridge -- of a
    fediverse or Nostr post, say, which Bluesky can't repost natively:
    "🔁 Reposted X's post" with a link card to the original, titled with
    its author and described with its text. ``thumb`` (a blob of the
    card's preview image) is fixed when the post is first made, so the
    caller supplies it. ``None`` if the event can't produce it (redacted,
    not a repost card, no link to the original)."""
    if is_redacted(event):
        return None
    content = event.get("content") or {}
    relation = repost_relation(content)
    link = repost_card_link(event)
    if relation is None or link is None:
        return None
    name = relation.get("displayname")
    author = _clip(name, _MAX_CARD_TITLE_LENGTH) if isinstance(name, str) and name.strip() else ""
    text = content.get(_SOCIAL_BODY_FIELD)
    external: dict[str, Any] = {
        "uri": link,
        "title": author or link,
        "description": _clip(text, _MAX_CARD_DESCRIPTION_LENGTH) if isinstance(text, str) else "",
    }
    if thumb is not None:
        external["thumb"] = thumb
    return {
        "$type": POST_COLLECTION,
        "text": f"🔁 Reposted {author}'s post" if author else "🔁 Reposted a post",
        "createdAt": _timestamp(int(event.get("origin_server_ts", 0))),
        "embed": {"$type": EXTERNAL_EMBED_TYPE, "external": external},
    }


def repost_card_source(room_id: str, event_id: str, *, thumb: dict[str, Any] | None = None) -> dict[str, Any]:
    source: dict[str, Any] = {"kind": "matrix_repost_card", "v": 1, "room_id": room_id, "event_id": event_id}
    if thumb is not None:
        source["thumb"] = lexjson.to_json(thumb)
    return source


def profile_record_v1(
    name: str | None,
    topic: str | None,
    *,
    avatar: dict[str, Any] | None = None,
    banner: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """``app.bsky.actor.profile`` from a Profile Room's name and topic,
    plus blob references for its avatar and banner (prepared and recorded
    by ``bridge.profile_sync`` when either image changes)."""
    record: dict[str, Any] = {"$type": PROFILE_COLLECTION}
    if name and name.strip():
        record["displayName"] = name.strip()[:_MAX_DISPLAY_NAME_LENGTH]
    if topic and topic.strip():
        record["description"] = topic.strip()[:_MAX_DESCRIPTION_LENGTH]
    if avatar is not None:
        record["avatar"] = avatar
    if banner is not None:
        record["banner"] = banner
    return record


def profile_record_v2(
    name: str | None,
    topic: str | None,
    *,
    avatar: dict[str, Any] | None = None,
    banner: dict[str, Any] | None = None,
    pinned_post: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """v1 plus ``pinnedPost``: the post pinned in the Profile Room, as a
    strong reference fixed when it was pinned (see ``bridge.profile_sync``)."""
    record = profile_record_v1(name, topic, avatar=avatar, banner=banner)
    if pinned_post is not None:
        record["pinnedPost"] = pinned_post
    return record


def post_source(
    room_id: str,
    event_id: str,
    *,
    part: int = 0,
    embed: dict[str, Any] | None = None,
    reply: dict[str, Any] | None = None,
    offset: int = 0,
    drop_link_to: str | None = None,
) -> dict[str, Any]:
    source: dict[str, Any] = {
        "kind": "matrix_post",
        "v": 3 if drop_link_to else 1,
        "room_id": room_id,
        "event_id": event_id,
        "part": part,
    }
    if offset:
        source["offset"] = offset
    if drop_link_to:
        source["drop_link_to"] = drop_link_to
    if embed is not None:
        source["embed"] = lexjson.to_json(embed)
    if reply is not None:
        source["reply"] = lexjson.to_json(reply)
    return source


def profile_source(
    room_id: str,
    *,
    avatar: dict[str, Any] | None = None,
    banner: dict[str, Any] | None = None,
    pinned: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """``avatar``/``banner`` are ``{"mxc": <the Matrix image it was made
    from>, "blob": <blob reference, JSON form>}``. ``pinned`` is
    ``{"event_id": <the pinned Matrix event>, "ref": <strong reference to
    the post made from it>}``."""
    source: dict[str, Any] = {"kind": "matrix_profile", "v": 2 if pinned else 1, "room_id": room_id}
    if pinned is not None:
        source["pinned"] = pinned
    if avatar is not None:
        source["avatar"] = avatar
    if banner is not None:
        source["banner"] = banner
    return source


async def profile_record_from_room(
    synapse: SynapseClient, source: dict[str, Any], *, as_user_id: str
) -> dict[str, Any]:
    room_id = source["room_id"]
    name = await synapse.get_room_state(room_id, "m.room.name", as_user_id=as_user_id)
    topic = await synapse.get_room_state(room_id, "m.room.topic", as_user_id=as_user_id)
    name_value, topic_value = (name or {}).get("name"), (topic or {}).get("topic")
    avatar = lexjson.from_json(source["avatar"]["blob"]) if "avatar" in source else None
    banner = lexjson.from_json(source["banner"]["blob"]) if "banner" in source else None
    if source.get("v") == 1:
        return profile_record_v1(name_value, topic_value, avatar=avatar, banner=banner)
    return profile_record_v2(
        name_value, topic_value, avatar=avatar, banner=banner, pinned_post=dict(source["pinned"]["ref"])
    )


def register_matrix_sources(sources: RecordSources, synapse: SynapseClient, *, bot_mxid: str) -> None:
    """Teach ``sources`` to rebuild Matrix-backed records by asking Synapse.
    A failed request raises (the record is unavailable, not gone); only an
    event that is confirmed missing or redacted reports the record gone."""

    async def fetch_event(source: dict[str, Any]) -> dict[str, Any] | None:
        event = await synapse.get_event(source["room_id"], source["event_id"], as_user_id=bot_mxid)
        if event is None:
            # "Not found" is also what Synapse says when the bot simply
            # can't see the room any more (kicked, say). That must not read
            # as "every post here was deleted": only believe it while the
            # bot is demonstrably still a member -- this lookup itself
            # fails, and so raises, if it isn't.
            membership = await synapse.get_room_state(
                source["room_id"], "m.room.member", bot_mxid, as_user_id=bot_mxid
            )
            if (membership or {}).get("membership") != "join":
                raise RuntimeError(f"Bot is not joined to {source['room_id']}")
        return event

    async def rebuild_post(source: dict[str, Any]) -> Any:
        if source.get("v") not in (1, 2, 3):
            raise ValueError(f"Unknown matrix_post version {source.get('v')!r}")
        event = await fetch_event(source)
        if event is None:
            return None
        embed = lexjson.from_json(source["embed"]) if "embed" in source else None
        reply = lexjson.from_json(source["reply"]) if "reply" in source else None
        if source["v"] == 1:
            return post_record_v1(
                event, part=int(source.get("part", 0)), embed=embed, reply=reply, offset=int(source.get("offset", 0))
            )
        if source["v"] == 2:
            return post_record_v2(
                event,
                part=int(source.get("part", 0)),
                embed=embed,
                reply=reply,
                offset=int(source.get("offset", 0)),
                trailing=str(source["trailing"]),
            )
        return post_record_v3(
            event,
            part=int(source.get("part", 0)),
            embed=embed,
            reply=reply,
            offset=int(source.get("offset", 0)),
            drop_link_to=source.get("drop_link_to"),
        )

    async def rebuild_repost_card(source: dict[str, Any]) -> Any:
        if source.get("v") != 1:
            raise ValueError(f"Unknown matrix_repost_card version {source.get('v')!r}")
        event = await fetch_event(source)
        if event is None:
            return None
        thumb = lexjson.from_json(source["thumb"]) if "thumb" in source else None
        return repost_card_record_v1(event, thumb=thumb)

    async def rebuild_profile(source: dict[str, Any]) -> Any:
        if source.get("v") not in (1, 2):
            raise ValueError(f"Unknown matrix_profile version {source.get('v')!r}")
        return await profile_record_from_room(synapse, source, as_user_id=bot_mxid)

    sources.register("matrix_post", rebuild_post)
    sources.register("matrix_repost_card", rebuild_repost_card)
    sources.register("matrix_profile", rebuild_profile)
