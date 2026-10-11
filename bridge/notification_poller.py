"""Turning what happens to a local account on Bluesky into Matrix notifications.

ATProto delivers nothing to a PDS when someone likes, reposts, replies to,
mentions, quotes or follows one of its accounts: those are records in
OTHER people's repositories. The AppView indexes the whole network and
knows, so the bridge asks it, on each local account's behalf, with a
short-lived token signed by that account's own key (see
``bridge.atproto.service_auth``) -- the same thing an ordinary PDS does
when a client asks for its notifications.

Each account's position is remembered, so a notification is sent once. The
first time an account is checked, whatever is already there is skipped
rather than replayed.
"""

from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

from fastapi import FastAPI

from bridge.atproto.identity import post_url, resolve_did
from bridge.atproto.keys import SigningKey
from bridge.atproto.service_auth import service_auth_token
from bridge.notifications import notify_user
from bridge.post_lookup import locate_post_by_uri
from bridge.post_mirroring import matrix_event_link, mirror_post
from bridge.previews import PREVIEW_TEXT_LIMIT, build_preview_media_content, fetch_post_preview, quote_block
from bridge.reaction_mirroring import LIKE_EMOJI, REPOST_EMOJI, mirror_reaction
from bridge.remote_accounts import ensure_ghost
from bridge.repository import Account, RemoteAccount

logger = logging.getLogger(__name__)

_LIST_METHOD = "app.bsky.notification.listNotifications"

# The interactions that produce a notification, and how each reads after
# the name of whoever did it. Anything else the AppView reports is ignored.
_REASONS = {
    "follow": "followed you",
    "like": "liked your post",
    "repost": "reposted your post",
    "reply": "replied to your post",
    "mention": "mentioned you",
    "quote": "quoted your post",
}
# Reasons also shown as a reaction on the Matrix message of the post.
_REACTION_EMOJI = {"like": LIKE_EMOJI, "repost": REPOST_EMOJI}
# Reasons where the notification is itself a post worth showing.
_REASONS_WITH_TEXT = ("reply", "mention", "quote")


def _seen_key(did: str) -> str:
    return f"notifications_seen:{did}"


def _position(notification: dict[str, Any]) -> str:
    """Sortable position of a notification: when it was indexed, then its
    address to separate two indexed in the same instant."""
    return f"{notification.get('indexedAt', '')}|{notification.get('uri', '')}"


async def fetch_notifications(app: FastAPI, account: Account) -> list[dict[str, Any]] | None:
    """The account's latest notifications, newest first; ``None`` if the
    AppView couldn't be asked."""
    config = app.state.config
    token = service_auth_token(
        SigningKey.from_hex(account.signing_key_hex),
        issuer=account.did,
        audience=config.atproto.appview_did,
        method=_LIST_METHOD,
    )
    try:
        response = await app.state.http_client.get(
            f"{config.atproto.appview_url}/xrpc/{_LIST_METHOD}",
            params={"limit": 50},
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code != 200:
            logger.info("Notifications for %s: HTTP %d %s", account.did, response.status_code, response.text[:120])
            return None
        notifications = response.json().get("notifications")
    except Exception as exc:  # network trouble or a malformed body; try again next round
        logger.info("Notifications for %s unavailable: %r", account.did, exc)
        return None
    return notifications if isinstance(notifications, list) else None


# The emoji a notification leads with.
_TYPE_EMOJI = {"follow": "👤", "like": "👍", "repost": "🔁", "reply": "💬", "mention": "💬", "quote": "🔁"}


async def build_notification(app: FastAPI, account: Account, notification: dict[str, Any]) -> dict[str, Any] | None:
    """The Matrix message for one notification, or ``None`` if it is of a
    kind that isn't reported. Formatted like
    matrix-appservice-activitypub's: who did it as a user pill, "your
    post" as a link to the post in Matrix, and a compact preview of the
    post it is about -- text, or, if that post is an image or video, the
    notification is itself that image or video at thumbnail size with the
    text as its caption."""
    reason = str(notification.get("reason"))
    phrase = _REASONS.get(reason)
    author = notification.get("author") if isinstance(notification.get("author"), dict) else None
    if phrase is None or author is None or not author.get("did"):
        return None
    handle = str(author.get("handle") or author["did"])
    name = str(author.get("displayName") or "").strip() or f"@{handle}"
    label = f"{name} (@{handle})" if name != f"@{handle}" else name
    emoji = _TYPE_EMOJI[reason]

    # Whoever did it: a real Matrix pill if they have a ghost here, else a
    # link to their profile. Either way the anchor carries their name --
    # an empty one would vanish from notification text.
    ghost = await app.state.repository.get_remote_account(str(author["did"]))
    href = f"https://matrix.to/#/{ghost.ghost_user_id}" if ghost else f"https://bsky.app/profile/{author['did']}"
    actor_html = f'<a href="{html.escape(href, quote=True)}" title="@{html.escape(handle, quote=True)}">{html.escape(label if not ghost else name)}</a>'

    # The post this is about: the one they wrote, where there is one in
    # Matrix (a reply that was just threaded into the Profile Room), else
    # the local user's own post.
    own = await locate_post_by_uri(app, str(notification.get("reasonSubject") or "")) if reason != "follow" else None
    theirs = await locate_post_by_uri(app, str(notification.get("uri") or "")) if reason in _REASONS_WITH_TEXT else None
    shown = theirs or (own if reason not in _REASONS_WITH_TEXT else None)
    preview_text, _full, preview_image, preview_video = (
        await fetch_post_preview(app, shown.room_id, shown.event_id) if shown is not None else ("", {}, None, None)
    )
    their_url = ""
    if reason in _REASONS_WITH_TEXT:
        their_parts = str(notification.get("uri", "")).removeprefix("at://").split("/")
        their_url = post_url(their_parts[0], their_parts[2]) if len(their_parts) == 3 else ""
        if not preview_text:
            record = notification.get("record") if isinstance(notification.get("record"), dict) else {}
            preview_text = str(record.get("text") or "").strip()
            if len(preview_text) > PREVIEW_TEXT_LIMIT:
                preview_text = preview_text[:PREVIEW_TEXT_LIMIT].rstrip() + "…"

    link = matrix_event_link(app, own.room_id, own.event_id) if own is not None else ""
    plain = f"{emoji} {label} {phrase}"
    phrase_html = html.escape(phrase)
    if link:
        phrase_html = phrase_html.replace("your post", f'<a href="{html.escape(link, quote=True)}">your post</a>')
    quoted_plain, quoted_html = quote_block(preview_text)
    if quoted_plain:
        plain += f"\n{quoted_plain}"
    formatted = f"<p>{emoji} {actor_html} {phrase_html}</p>{quoted_html}"
    reply_link = matrix_event_link(app, theirs.room_id, theirs.event_id) if theirs is not None else their_url
    if reply_link:
        plain += f"\n{reply_link}"
        label_text = "View in thread" if theirs is not None else "View on Bluesky"
        formatted += f'<p><a href="{html.escape(reply_link, quote=True)}">{label_text}</a></p>'
    elif link:
        plain += f"\n{link}"
    return build_preview_media_content(
        plain_body=plain, formatted_caption=formatted, preview_image=preview_image, preview_video=preview_video
    )


async def _mirror_reply(app: FastAPI, notification: dict[str, Any]) -> None:
    """Put a reply to a local user's post where the conversation is: in a
    thread under that post, in their Profile Room, from the replier's
    ghost. (``mirror_post`` works out the placement; a replier who is
    followed here also gets it cross-posted into their own room.)"""
    author = notification.get("author") if isinstance(notification.get("author"), dict) else {}
    record = notification.get("record")
    parts = str(notification.get("uri", "")).removeprefix("at://").split("/")
    did = str(author.get("did", ""))
    if not did or len(parts) != 3 or not isinstance(record, dict):
        return
    repository = app.state.repository
    if await repository.get_account(did) is not None:
        return  # another local user's reply already lives in Matrix
    remote = await repository.get_remote_account(did)
    if remote is None:
        identity = await resolve_did(app.state.http_client, did, plc_directory=app.state.config.atproto.plc_directory)
        parent = await locate_post_by_uri(app, str(((record.get("reply") or {}).get("parent") or {}).get("uri", "")))
        if identity is None or parent is None:
            return
        ghost, *_ = await ensure_ghost(app, identity)
        # Not followed here, so no room of their own: only the thread.
        remote = RemoteAccount(did, identity.handle, identity.pds, ghost, parent.room_id)
    await mirror_post(app, remote, parts[2], record, cid=str(notification.get("cid", "")))


async def poll_account(app: FastAPI, account: Account) -> int:
    """Send any new notifications for ``account``. Returns how many."""
    notifications = await fetch_notifications(app, account)
    if not notifications:
        return 0
    repository = app.state.repository
    key = _seen_key(account.did)
    seen = await repository.get_value(key)
    newest = max(_position(n) for n in notifications)
    if seen is None:
        # First look at this account: start from here, don't replay history.
        await repository.set_value(key, newest)
        return 0
    sent = 0
    for notification in sorted(notifications, key=_position):
        if _position(notification) <= seen:
            continue
        reason = notification.get("reason")
        # A reply is threaded into the Profile Room first, so that its
        # notification can link to (and preview) it there.
        if reason == "reply":
            try:
                await _mirror_reply(app, notification)
            except Exception:
                logger.exception("Mirroring reply %s failed", notification.get("uri"))
        content = await build_notification(app, account, notification)
        if content is not None and await notify_user(app, account.matrix_user_id, content) is not None:
            sent += 1
        try:
            if reason in _REACTION_EMOJI:
                await mirror_reaction(
                    app,
                    actor_did=str((notification.get("author") or {}).get("did", "")),
                    record_uri=str(notification.get("uri", "")),
                    subject_uri=str(notification.get("reasonSubject", "")),
                    emoji=_REACTION_EMOJI[reason],
                )
        except Exception:
            logger.exception("Mirroring %s %s failed", reason, notification.get("uri"))
        # Advance per notification, so a failure part-way doesn't resend
        # the ones already delivered.
        await repository.set_value(key, _position(notification))
    return sent


async def notification_loop(app: FastAPI) -> None:
    interval = app.state.config.atproto.notification_poll_seconds
    if interval <= 0:
        return
    while True:
        await asyncio.sleep(interval)
        try:
            for account in await app.state.repository.list_accounts():
                if account.plc_registered and account.active:
                    await poll_account(app, account)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Notification check failed")
