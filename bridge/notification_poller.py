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

The AppView takes a few seconds to report something. When whoever did it
is followed here, the live feed has already delivered their record, so the
notification is made from that at once (``notify_directly``) and the
AppView's later report of the same record is passed over.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
from typing import Any

from fastapi import FastAPI

from bridge.atproto.identity import post_url, resolve_did
from bridge.atproto.keys import SigningKey
from bridge.atproto.service_auth import service_auth_token
from bridge.matrix_records import POST_COLLECTION
from bridge.notifications import notify_user
from bridge.post_lookup import locate_post_by_uri
from bridge.post_mirroring import matrix_event_link, mirror_post
from bridge.previews import PREVIEW_TEXT_LIMIT, build_preview_media_content, fetch_post_preview, quote_block
from bridge.reaction_mirroring import LIKE_EMOJI, REPOST_EMOJI, mirror_reaction
from bridge.remote_accounts import ensure_ghost
from bridge.repository import Account, RemoteAccount

logger = logging.getLogger(__name__)

_LIST_METHOD = "app.bsky.notification.listNotifications"
_BLOCK_COLLECTION = "app.bsky.graph.block"
_LIKE_COLLECTION = "app.bsky.feed.like"
_REPOST_COLLECTION = "app.bsky.feed.repost"
# How many records an account was last notified about are remembered, to
# tell the AppView's report of one from the live feed's.
_DELIVERED_KEPT = 200

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


def _delivered_key(did: str) -> str:
    return f"notifications_delivered:{did}"


async def _delivered(app: FastAPI, did: str) -> list[str]:
    """Addresses of the records ``did`` was most recently notified about,
    oldest first."""
    try:
        uris = json.loads(await app.state.repository.get_value(_delivered_key(did)) or "[]")
    except ValueError:
        return []
    return uris if isinstance(uris, list) else []


async def _note_delivered(app: FastAPI, did: str, uri: str) -> None:
    uris = [known for known in await _delivered(app, did) if known != uri]
    uris.append(uri)
    await app.state.repository.set_value(_delivered_key(did), json.dumps(uris[-_DELIVERED_KEPT:]))


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


async def _author_ghost(app: FastAPI, did: str) -> str | None:
    """The Matrix user standing for whoever a notification is from: their
    ghost, created if this is the first the bridge has seen of them
    (someone nobody here follows), or, for a local account, its owner.
    ``None`` if they can't be looked up."""
    remote = await app.state.repository.get_remote_account(did)
    if remote is not None:
        return remote.ghost_user_id
    account = await app.state.repository.get_account(did)
    if account is not None:
        return account.matrix_user_id
    try:
        identity = await resolve_did(app.state.http_client, did, plc_directory=app.state.config.atproto.plc_directory)
        if identity is None:
            return None
        ghost, *_ = await ensure_ghost(app, identity)
        return ghost
    except Exception:
        logger.info("Could not make a ghost for %s", did, exc_info=True)
        return None


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

    # Whoever did it: a Matrix pill to their ghost, or, if no ghost could
    # be made, a link to their profile. Either way the anchor carries their name --
    # an empty one would vanish from notification text.
    ghost = await _author_ghost(app, str(author["did"]))
    href = f"https://matrix.to/#/{ghost}" if ghost else f"https://bsky.app/profile/{author['did']}"
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


def _poll_lock(app: FastAPI, did: str) -> asyncio.Lock:
    locks = getattr(app.state, "notification_locks", None)
    if locks is None:
        locks = app.state.notification_locks = {}
    return locks.setdefault(did, asyncio.Lock())


async def poll_account(app: FastAPI, account: Account) -> int:
    """Send any new notifications for ``account``. Returns how many were
    new, counting those the live feed had already delivered.
    One check per account at a time: the regular round and an early
    check (``nudge_notifications``) overlapping would send things twice."""
    async with _poll_lock(app, account.did):
        return await _poll_account(app, account)


async def _poll_account(app: FastAPI, account: Account) -> int:
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
    handled = 0
    for notification in sorted(notifications, key=_position):
        if _position(notification) <= seen:
            continue
        reason = notification.get("reason")
        uri = str(notification.get("uri", ""))
        # A reply is threaded into the Profile Room first, so that its
        # notification can link to (and preview) it there.
        if reason == "reply":
            try:
                await _mirror_reply(app, notification)
            except Exception:
                logger.exception("Mirroring reply %s failed", notification.get("uri"))
        if uri in await _delivered(app, account.did):
            handled += 1  # the live feed got there first
        else:
            content = await build_notification(app, account, notification)
            if content is not None and await notify_user(app, account.matrix_user_id, content) is not None:
                handled += 1
                await _note_delivered(app, account.did, uri)
        try:
            if reason in _REACTION_EMOJI:
                await mirror_reaction(
                    app,
                    actor_did=str((notification.get("author") or {}).get("did", "")),
                    record_uri=uri,
                    subject_uri=str(notification.get("reasonSubject", "")),
                    emoji=_REACTION_EMOJI[reason],
                )
        except Exception:
            logger.exception("Mirroring %s %s failed", reason, notification.get("uri"))
        # Advance per notification, so a failure part-way doesn't resend
        # the ones already delivered.
        await repository.set_value(key, _position(notification))
    return handled


# After the live feed shows something aimed at a local account, its
# notifications are checked this many seconds later -- again, a little
# later each time, until the AppView has caught up and reports it.
_NUDGE_DELAYS = (1.0, 2.0, 4.0, 8.0)


def _did_of(uri: Any) -> str:
    return uri[5:].split("/", 1)[0] if isinstance(uri, str) and uri.startswith("at://") else ""


def addressed_dids(record: dict[str, Any]) -> set[str]:
    """The accounts a record would notify: whose post it likes, reposts,
    replies to or quotes, and whom it mentions."""
    dids: set[str] = set()
    subject = record.get("subject")
    if isinstance(subject, dict):
        dids.add(_did_of(subject.get("uri")))
    reply = record.get("reply")
    if isinstance(reply, dict):
        for part in ("parent", "root"):
            if isinstance(reply.get(part), dict):
                dids.add(_did_of(reply[part].get("uri")))
    embed = record.get("embed")
    if isinstance(embed, dict):
        quoted = embed.get("record")
        if isinstance(quoted, dict):
            # app.bsky.embed.record, or recordWithMedia wrapping one.
            inner = quoted.get("record") if isinstance(quoted.get("record"), dict) else quoted
            dids.add(_did_of(inner.get("uri")))
    for facet in record.get("facets") or []:
        for feature in (facet.get("features") or []) if isinstance(facet, dict) else []:
            if isinstance(feature, dict) and isinstance(feature.get("did"), str):
                dids.add(feature["did"])
    dids.discard("")
    return dids


def _post_uri(uri: Any) -> str:
    return uri if isinstance(uri, str) and f"/{POST_COLLECTION}/" in uri else ""


def direct_reasons(collection: str, record: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """Whom a record notifies and how, as far as the record alone says:
    each account's DID with the reason and the post of theirs it is about
    (none for a mention). One reason per account, as the AppView has it:
    a reply before a quote before a mention."""
    found: dict[str, tuple[str, str]] = {}

    def add(reason: str, uri: str, did: str = "") -> None:
        did = did or _did_of(uri)
        if did:
            found.setdefault(did, (reason, uri))

    if collection in (_LIKE_COLLECTION, _REPOST_COLLECTION):
        subject = record.get("subject")
        if isinstance(subject, dict):
            add("like" if collection == _LIKE_COLLECTION else "repost", _post_uri(subject.get("uri")))
    elif collection == POST_COLLECTION:
        reply = record.get("reply")
        if isinstance(reply, dict) and isinstance(reply.get("parent"), dict):
            add("reply", _post_uri(reply["parent"].get("uri")))
        embed = record.get("embed")
        quoted = embed.get("record") if isinstance(embed, dict) else None
        if isinstance(quoted, dict):
            inner = quoted.get("record") if isinstance(quoted.get("record"), dict) else quoted
            add("quote", _post_uri(inner.get("uri")))
        for facet in record.get("facets") or []:
            for feature in (facet.get("features") or []) if isinstance(facet, dict) else []:
                if isinstance(feature, dict) and isinstance(feature.get("did"), str):
                    add("mention", "", feature["did"])
    return found


async def _blocks(app: FastAPI, account: Account, did: str) -> bool:
    """Whether ``account`` blocks ``did`` -- the AppView reports nothing
    from a blocked account, so neither does the shortcut."""
    for row in await app.state.repository.list_records(account.did):
        if row.collection == _BLOCK_COLLECTION and json.loads(row.source).get("record", {}).get("subject") == did:
            return True
    return False


async def _ghost_name(app: FastAPI, remote: RemoteAccount) -> str:
    """The display name ``remote``'s ghost was given, without the handle
    that follows it; empty if it has none."""
    try:
        name = str((await app.state.synapse.get_profile(remote.ghost_user_id)).get("displayname") or "")
    except Exception:
        return ""
    name = name.removesuffix(f"(@{remote.handle})").strip()
    return "" if name.startswith("@") else name


async def notify_directly(
    app: FastAPI, remote: RemoteAccount, collection: str, rkey: str, record: dict[str, Any], *, cid: str = ""
) -> None:
    """The live feed just delivered ``record`` from a followed account. If
    it is something a local account is notified of, send that notification
    now, built from the record itself, instead of waiting for the AppView
    to report it."""
    if app.state.config.atproto.notification_poll_seconds <= 0:
        return
    uri = f"at://{remote.did}/{collection}/{rkey}"
    author = {"did": remote.did, "handle": remote.handle}
    for did, (reason, subject) in direct_reasons(collection, record).items():
        account = await app.state.repository.get_account(did)
        if account is None or not (account.plc_registered and account.active):
            continue
        try:
            async with _poll_lock(app, did):
                if uri in await _delivered(app, did) or await _blocks(app, account, remote.did):
                    continue
                author.setdefault("displayName", await _ghost_name(app, remote))
                notification = {
                    "uri": uri, "cid": cid, "reason": reason, "reasonSubject": subject, "record": record,
                    "author": author,
                }
                content = await build_notification(app, account, notification)
                if content is not None and await notify_user(app, account.matrix_user_id, content) is not None:
                    await _note_delivered(app, did, uri)
        except Exception:
            logger.exception("Notifying %s of %s failed", did, uri)


async def _check_early(app: FastAPI, account: Account) -> None:
    try:
        for delay in _NUDGE_DELAYS:
            await asyncio.sleep(delay)
            if await poll_account(app, account):
                return
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Early notification check for %s failed", account.did)
    finally:
        app.state.notification_nudges.discard(account.did)


async def nudge_notifications(app: FastAPI, record: dict[str, Any]) -> None:
    """The live feed just delivered ``record``. If it is aimed at a local
    account, check that account's notifications now instead of waiting for
    the next regular round: that settles the AppView's own report of what
    ``notify_directly`` has just sent, and finds whatever the record alone
    doesn't show (a reply further down a local user's thread). (The live
    feed only carries followed accounts; everyone else's interactions are
    still found by the regular round.)"""
    if app.state.config.atproto.notification_poll_seconds <= 0:
        return
    pending = getattr(app.state, "notification_nudges", None)
    if pending is None:
        pending = app.state.notification_nudges = set()
    for did in addressed_dids(record):
        if did in pending:
            continue
        account = await app.state.repository.get_account(did)
        if account is None or not (account.plc_registered and account.active):
            continue
        pending.add(did)
        task = asyncio.create_task(_check_early(app, account))
        app.state.background_tasks.add(task)
        task.add_done_callback(app.state.background_tasks.discard)


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
