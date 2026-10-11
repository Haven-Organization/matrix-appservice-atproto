"""MSC4501 ("Rooms as Social Media Pages") and the related proposals the
bridge speaks, kept identical to matrix-appservice-activitypub's use of
them so a client that understands one bridge's rooms understands both.

https://github.com/matrix-org/matrix-spec-proposals/pull/4501

Everything is under its proposal's UNSTABLE name: power levels and clients
match on the literal string sent, so switching to a future stable name
early would protect and mean nothing.
"""

from __future__ import annotations

from typing import Any

# Room type of every room the bridge creates to represent a profile: a
# local user's Profile Room and a remote account's Remote User Room. A
# room's type can't be changed after creation.
SOCIAL_PROFILE_ROOM_TYPE = "org.matrix.msc4501.social.profile"

# State event naming whose profile a room is. A room's creator can't
# answer that: the bot creates every room. Only the bot may set it (see
# SOCIAL_PROFILE_USER_ID_POWER_LEVEL) -- left at the default any moderator
# could reassign a profile out from under its owner.
SOCIAL_PROFILE_USER_ID_STATE_TYPE = "org.matrix.msc4501.social.profile_user_id"
SOCIAL_PROFILE_USER_ID_POWER_LEVEL = 100

# MSC4133 profile field on a GHOST pointing at its Remote User Room. Never
# set for a local user: a real account's profile fields are its own.
SOCIAL_PROFILE_ROOM_FIELD = "org.matrix.msc4501.social.profile_room"

# Content field relating a mirrored post to another post (which may be in
# a different room), and its relation types.
SOCIAL_RELATES_TO_FIELD = "org.matrix.msc4501.social.relates_to"
SOCIAL_REL_TYPE_REPOST = "org.matrix.msc4501.social.repost"
SOCIAL_REL_TYPE_REPLY = "org.matrix.msc4501.social.reply"

# On an event that wraps a related post in presentation of its own (a
# "reposted X's post" line): the related post's own text, for a client
# that would rather render that than the wrapper.
SOCIAL_BODY_FIELD = "org.matrix.msc4501.social.body"
SOCIAL_FORMATTED_BODY_FIELD = "org.matrix.msc4501.social.formatted_body"

# MSC4144: lets a message the bot sends on a user's behalf (the "you
# reposted" card in their Profile Room) show as that user's own.
PER_MESSAGE_PROFILE_FIELD = "com.beeper.per_message_profile"

# The event TYPE proposed in place of m.room.message for a post. Unlike
# every field above this is not additive: a client that has never heard of
# it shows nothing at all. Hence bridge.use_msc4501_post_event_type, off by
# default.
SOCIAL_POST_EVENT_TYPE = "org.matrix.msc4501.social.post"

# MSC4503 (External Protocol Handles): a ghost's real handle on the other
# network, which neither its display name nor its MXID carries.
EXTERNAL_HANDLE_FIELD = "org.matrix.msc4503.external_handle"

# MSC4221 (Room Banners), under that proposal's own unstable name. Content
# mirrors m.room.avatar's: {"url": "mxc://..."}.
PROFILE_BANNER_STATE_TYPE = "page.codeberg.everypizza.room.banner"


def mirrored_post_event_type(config: Any) -> str:
    """The event type for a remote account's mirrored posts."""
    return SOCIAL_POST_EVENT_TYPE if config.bridge.use_msc4501_post_event_type else "m.room.message"


def social_relates_to(
    rel_type: str,
    *,
    event_id: str,
    room_id: str,
    sender: str,
    via: list[str],
    content: dict | None = None,
    content_inline: bool = False,
    displayname: str | None = None,
) -> dict:
    """A ``SOCIAL_RELATES_TO_FIELD`` value. ``sender`` (the related post's
    author's MXID) is mandatory in the MSC -- a viewer may share no room
    with them -- so callers must not build this without one.

    ``content`` is the related event's own full Matrix content, so the
    relation can be rendered without access to its room. ``content_inline``
    instead asserts that the OUTER event's content already is a copy of
    the related post -- valid only for a pure repost with no commentary of
    the reposter's own, never for a quote-post or a reply."""
    value: dict[str, Any] = {"rel_type": rel_type, "event_id": event_id, "room_id": room_id, "sender": sender, "via": via}
    if displayname:
        value["displayname"] = displayname
    if content_inline:
        value["content_inline"] = True
    elif content is not None:
        value["content"] = content
    return value


def external_handle_value(config: Any, *, handle: str) -> dict:
    protocol: dict[str, Any] = {"id": "atproto", "displayname": "Bluesky"}
    if config.appservice.bot_avatar_mxc:
        protocol["avatar_url"] = config.appservice.bot_avatar_mxc
    return {"handle": f"@{handle}", "protocol": protocol, "url": f"https://bsky.app/profile/{handle}"}
