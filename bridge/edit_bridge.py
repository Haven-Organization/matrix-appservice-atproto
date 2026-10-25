"""Federates Matrix message edits (``m.replace``) of an already-published
Profile Room post out to Bluesky.

Runs ahead of ``bridge.profile_posts`` in the dispatch chain and CONSUMES
every ``m.replace`` edit of a Profile Room post it recognizes, mirroring
the sibling ``matrix-appservice-activitypub`` bridge's own
``edit_bridge.py``: an edit event's own top-level ``body`` is just the
``"* ..."`` fallback rendering, and before this module existed
``bridge.profile_posts.maybe_publish_profile_post`` only ever swallowed an
edit as a silent no-op (see its own now-defensive ``"m.relates_to" in
content`` guard) -- the edit never reached Bluesky at all.

Bluesky's app (bsky.app's AppView) shows a post as it was first
published and ignores later changes to it (confirmed live: in-place edits
reached the relay but never showed), so an edit only shows if the post is
published again: the old records deleted and the edited content published
under new rkeys in the same commit, keeping the SAME Matrix event id so a
later edit or redaction still finds it, and the first post still replying
to what it replied to. That orphans the old post's likes/reposts/quotes/
replies, so it is done only when the AppView reports none (or the edit
changes the post's shape -- one post to a thread or back -- which can't be
done in place anyway). Otherwise the record is changed in place with
``Pds.put_record`` (same rkey, which other AppViews may show) and the owner
is told ``]republish`` forces it (``republish``). A republished pinned post
is pinned again (``refresh_pin``).

A record's own ``source`` recipe (``room_id``, ``event_id``, ...) is what
``Pds``'s self-heal (``_rebuild``/``_snapshot_locked``) uses to re-derive a
record's content whenever the repository is read -- it re-fetches that
event from Synapse with a plain GET and rebuilds from it. A plain GET never
bundles ``m.replace`` aggregations, and the ORIGINAL post's own event
content never changes, so a source pointing at the original event would
get silently REVERTED back to the pre-edit text the next time anything
triggers a snapshot (confirmed live: it happened within seconds of the
first version of this fix). ``source["event_id"]`` is therefore always the
EDIT event's own id here -- ``bridge.matrix_records.fetch_event`` now
resolves an edit event's ``m.replace`` shape back to its ``m.new_content``
when asked to fetch one, so self-heal reconstructs the edited content
correctly -- while the separate ``matrix_event_id`` column stays the
ORIGINAL event id, since that's what a later edit or redaction looks
records up by (``repository.get_records_for_event``). One accepted edge
case from this: redacting the EDIT event itself (not the original post --
not something any normal client does) would make a later self-heal see
the source event as gone and delete the post.

Replies published via ``bridge.interactions`` (not Profile Room top-level
posts) are deliberately NOT covered yet -- same incremental-scope pattern
as the AP sibling leaving DM/Chat edits for later; that module's own
``"m.new_content" in content`` guard keeps dropping those edits silently,
unchanged.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import Request

from bridge.atproto import cbor, lexjson
from bridge.atproto.identity import _PUBLIC_APPVIEW, fetch_public
from bridge.atproto.cid import CID
from bridge.commands import bot_mxid, notice
from bridge.matrix_records import (
    MAX_THREAD_POSTS,
    MEDIA_MSGTYPES,
    POST_COLLECTION,
    RECORD_EMBED_TYPE,
    RECORD_WITH_MEDIA_EMBED_TYPE,
    post_record_v5,
    post_source,
    post_text,
    quoted_link_suffix,
    reply_header_link,
    rich_post_parts,
    social_reply_relation,
)
from bridge.pds import PdsError, Write
from bridge.post_lookup import find_linked_post
from bridge.profile_posts import _build_embed, refresh_pin

from bridge.repository import Account, RecordRow
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)


async def maybe_publish_edit(request: Request, event: dict) -> bool:
    """Returns True if this was an ``m.replace`` edit of a Profile Room
    post (ALWAYS handled once that much is true, whether or not anything
    ends up republished to Bluesky -- see module docstring)."""
    if event.get("type") != "m.room.message":
        return False
    content = event.get("content") or {}
    relates_to = content.get("m.relates_to") or {}
    if relates_to.get("rel_type") != "m.replace":
        return False

    edit_event_id = event.get("event_id", "")
    target_event_id = relates_to.get("event_id")
    new_content = content.get("m.new_content")
    if not target_event_id or not edit_event_id or not isinstance(new_content, dict):
        return True

    repository = request.app.state.repository
    room_id = event.get("room_id", "")
    account = await repository.get_account_for_profile_room(room_id)
    if account is None or event.get("sender") != account.matrix_user_id:
        return True  # not a Profile Room post's own edit -- leave it to bridge.interactions / the no-op guard

    rows = await repository.get_records_for_event(target_event_id)
    if not rows:
        return True  # was never published to Bluesky -- stays Matrix-only, like the AP sibling

    synthetic = dict(event)
    synthetic["content"] = new_content
    try:
        original = await request.app.state.synapse.get_event(
            room_id, target_event_id, as_user_id=bot_mxid(request.app.state.config)
        )
    except SynapseError:
        original = None
    if original is not None and isinstance(original.get("origin_server_ts"), int):
        # Keep Bluesky's createdAt stable across an edit, same convention
        # as the content itself: only what the post SAYS changes.
        synthetic["origin_server_ts"] = original["origin_server_ts"]
    await _apply_edit(
        request, account, rows, synthetic, original, edit_event_id=edit_event_id, target_event_id=target_event_id
    )
    return True


async def _apply_edit(
    request: Request,
    account: Account,
    rows: list[RecordRow],
    synthetic: dict,
    original: dict | None,
    *,
    edit_event_id: str,
    target_event_id: str,
    force: bool = False,
) -> bool:
    """Bring the posts made from ``target_event_id`` in line with
    ``synthetic`` (the event as it now reads). Bluesky's app shows a post as
    it was first published and ignores later changes to it, so an edit only
    shows if the post is published again, as a new post, which loses its
    likes, reposts, replies and quotes. That is done when it has none (or
    ``force``); otherwise it is changed in place, which only other apps
    show, and the owner is told how to force it (``]republish``). True if
    Bluesky was updated (in place or anew)."""
    room_id = synthetic["room_id"]
    new_content = synthetic.get("content") or {}
    text = post_text(synthetic, offset=0)
    trailing = None
    quoted = None
    if text:
        linked = await find_linked_post(request.app, text)
        if linked is not None:
            _url, quoted = linked
            trailing = quoted_link_suffix(synthetic, offset=0, quoted=quoted["uri"])
    drop_link_to = quoted["uri"] if trailing is not None else None
    reply_header = _kept_reply_header(_first_part(rows), new_content, original)
    parts = rich_post_parts(synthetic, offset=0, trailing=trailing, reply_to=reply_header)
    if parts is None:
        if new_content.get("msgtype") != "m.text":
            await notice(request, room_id, "That kind of message can't be published to Bluesky.")
        return False
    if len(parts) > MAX_THREAD_POSTS:
        await notice(
            request, room_id,
            f"That edit would take a thread of {len(parts)} posts; the limit is {MAX_THREAD_POSTS}, "
            "so the Bluesky post wasn't updated.",
        )
        return False

    embed = None
    if new_content.get("msgtype") in MEDIA_MSGTYPES:
        embed = await _build_embed(request, account, synthetic)
        if embed is None:
            return False  # _build_embed already told the room why

    if quoted is not None:
        record_embed = {"$type": RECORD_EMBED_TYPE, "record": quoted}
        embed = {"$type": RECORD_WITH_MEDIA_EMBED_TYPE, "record": record_embed, "media": embed} if embed else record_embed

    first = _first_part(rows)
    if len(parts) == 1 and len(rows) == 1 and not force:
        interactions = await _interactions(request, rows)
        if interactions != "":
            await _edit_in_place(
                request, account, first, synthetic, embed,
                edit_event_id=edit_event_id, target_event_id=target_event_id, drop_link_to=drop_link_to,
                reply_header=reply_header,
            )
            p = request.app.state.config.appservice.command_prefix
            why = (
                f"it already has {interactions} there" if interactions
                else "I couldn't check whether anyone has interacted with it there"
            )
            await notice(
                request, room_id,
                f"Bluesky doesn't show edits to posts, and {why}, so it still shows the original. "
                f"To show the edit anyway, as a new post (losing those), reply to the post with {p}republish.",
            )
            return True
    if not await _delete_and_republish(
        request, account, rows, synthetic, parts, embed,
        edit_event_id=edit_event_id, target_event_id=target_event_id, drop_link_to=drop_link_to,
        reply_header=reply_header,
    ):
        return False
    await refresh_pin(request, account.did, target_event_id)
    return True


def _source(row: RecordRow) -> dict[str, Any]:
    try:
        source = json.loads(row.source)
    except ValueError:
        return {}
    return source if isinstance(source, dict) else {}


def _first_part(rows: list[RecordRow]) -> RecordRow:
    return min(rows, key=lambda row: int(_source(row).get("part", 0)))


def _thread_reply(row: RecordRow) -> dict[str, Any] | None:
    """What the first post answers, if it is a reply: kept through an edit."""
    reply = _source(row).get("reply")
    return lexjson.from_json(reply) if reply is not None else None


def _count(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s" if not word.endswith("y") else f"{n} {word[:-1]}ies"


async def _interactions(request: Request, rows: list[RecordRow]) -> str | None:
    """The likes, reposts, replies and quotes the posts have on Bluesky,
    said in words ("2 likes, 1 reply"); "" for none (or not on Bluesky
    yet); ``None`` if Bluesky couldn't be asked."""
    uris = [f"at://{row.did}/{row.collection}/{row.rkey}" for row in rows]
    response = await fetch_public(
        request.app.state.http_client, f"{_PUBLIC_APPVIEW}/xrpc/app.bsky.feed.getPosts", params={"uris": uris}
    )
    if response is None:
        return None
    try:
        posts = response.json().get("posts") or []
    except ValueError:
        return None
    totals = {"like": 0, "repost": 0, "reply": 0, "quote": 0}
    for post in posts:
        for kind in totals:
            count = post.get(f"{kind}Count")
            totals[kind] += count if isinstance(count, int) and count > 0 else 0
    return ", ".join(_count(n, kind) for kind, n in totals.items() if n)


async def republish(request: Request, account: Account, target_event_id: str) -> bool | None:
    """Publish the posts made from ``target_event_id`` again, as they now
    read, so an edit Bluesky's app ignores shows (``]republish``). ``None``
    if none of the account's posts were made from it; False if it couldn't
    be done (the room has been told why)."""
    rows = [row for row in await request.app.state.repository.get_records_for_event(target_event_id)
            if row.did == account.did and row.collection == POST_COLLECTION]
    if not rows:
        return None
    source = _source(_first_part(rows))
    room_id, source_event_id = source.get("room_id"), source.get("event_id")
    if not isinstance(room_id, str) or not isinstance(source_event_id, str):
        return None
    synapse = request.app.state.synapse
    bot = bot_mxid(request.app.state.config)
    original = None
    try:
        latest = await synapse.get_event(room_id, source_event_id, as_user_id=bot)
        original = latest if source_event_id == target_event_id else await synapse.get_event(
            room_id, target_event_id, as_user_id=bot
        )
    except SynapseError:
        latest = None
    if latest is None:
        await notice(request, room_id, "I couldn't read that post's message, so it wasn't republished.")
        return False
    synthetic = dict(latest)
    new_content = (latest.get("content") or {}).get("m.new_content")
    if isinstance(new_content, dict):
        synthetic["content"] = new_content
    if original is not None and isinstance(original.get("origin_server_ts"), int):
        synthetic["origin_server_ts"] = original["origin_server_ts"]
    return await _apply_edit(
        request, account, rows, synthetic, original,
        edit_event_id=source_event_id, target_event_id=target_event_id, force=True,
    )


def _kept_reply_header(row: RecordRow, new_content: dict, original: dict | None) -> dict[str, Any] | None:
    """An edited reply keeps its "↩ In reply to X's post" header (v5). Edits
    rarely repeat the MSC4501 relation, so when the new content lacks it,
    what the header needs is taken from the original message and kept."""
    try:
        source = json.loads(row.source)
    except ValueError:
        return None
    if source.get("v") != 5:
        return None
    kept = dict(source.get("reply_to") or {})
    relation = social_reply_relation((original or {}).get("content") or {})
    if social_reply_relation(new_content) is None and relation is not None:
        name = relation.get("displayname")
        if isinstance(name, str) and name.strip():
            kept["displayname"] = name
        link = kept.get("link") or reply_header_link(original or {})
        if link:
            kept["link"] = link
        else:
            kept.setdefault("displayname", "")
    return kept


async def _edit_in_place(
    request: Request, account: Account, row: RecordRow, event: dict, embed: dict | None,
    *, edit_event_id: str, target_event_id: str, drop_link_to: str | None, reply_header: dict[str, Any] | None,
) -> None:
    reply = _thread_reply(row)
    record = post_record_v5(
        event, part=0, embed=embed, reply=reply, offset=0, drop_link_to=drop_link_to, reply_to=reply_header
    )
    if record is None:
        return
    source = post_source(
        event["room_id"], edit_event_id, part=0, embed=embed, reply=reply, offset=0, drop_link_to=drop_link_to,
        reply_to=reply_header,
    )
    try:
        await request.app.state.pds.put_record(
            account.did, row.collection, row.rkey, record, source=source, matrix_event_id=target_event_id,
        )
    except PdsError as exc:
        logger.warning("Editing %s for %s failed: %s", target_event_id, account.did, exc.message)
        await notice(request, event["room_id"], "Couldn't update that post on Bluesky.")
    else:
        logger.info(
            "Edited %s in place as %s/%s for %s (source event %s)",
            target_event_id, row.collection, row.rkey, account.did, edit_event_id,
        )


async def _delete_and_republish(
    request: Request, account: Account, rows: list[RecordRow], event: dict, parts: list, embed: dict | None,
    *, edit_event_id: str, target_event_id: str, drop_link_to: str | None, reply_header: dict[str, Any] | None,
) -> bool:
    """The old posts deleted and the new ones made in one commit, so a
    failure leaves the old ones in place. True if it went through."""
    repository = request.app.state.repository
    pds = request.app.state.pds
    room_id = event["room_id"]
    # Same loop publish_event_as_posts runs for a fresh post -- not reused
    # directly because it ties a write's ``source["event_id"]`` and
    # ``matrix_event_id`` to the SAME id, and here they must differ (see
    # module docstring). A post that answered another still does.
    first_reply = _thread_reply(_first_part(rows))
    root_ref = first_reply["root"] if first_reply else None
    parent_ref = first_reply["parent"] if first_reply else None
    writes: list[Write] = [Write("delete", row.collection, row.rkey) for row in rows]
    for index in range(len(parts)):
        reply = {"root": root_ref, "parent": parent_ref} if parent_ref is not None else None
        part_embed = embed if index == 0 else None
        record = post_record_v5(
            event, part=index, embed=part_embed, reply=reply, offset=0, drop_link_to=drop_link_to,
            reply_to=reply_header,
        )
        if record is None:
            return False
        rkey = pds.next_record_key()
        writes.append(Write(
            "create", POST_COLLECTION, rkey, record,
            source=post_source(
                room_id, edit_event_id, part=index, embed=part_embed, reply=reply, offset=0,
                drop_link_to=drop_link_to, reply_to=reply_header,
            ),
            matrix_event_id=target_event_id,
        ))
        parent_ref = {
            "uri": f"at://{account.did}/{POST_COLLECTION}/{rkey}",
            "cid": str(CID.for_cbor(cbor.encode(record))),
        }
        root_ref = root_ref or parent_ref

    try:
        await pds.apply_writes(account.did, writes)
    except PdsError as exc:
        logger.warning("Republishing edited %s for %s failed: %s", target_event_id, account.did, exc.message)
        await notice(request, room_id, "Couldn't update that post on Bluesky.")
        return False
    if edit_event_id != target_event_id:
        # The old version's media; the new version's is kept under the
        # edit's own id (unless it is the same id: republished unedited).
        await repository.forget_media_for_event(target_event_id)
    logger.info(
        "Republished %s as %d post(s) for %s (source event %s)",
        target_event_id, len(writes) - len(rows), account.did, edit_event_id,
    )
    return True
