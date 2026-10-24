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

A single-post edit (the post was, and still is, exactly one thread part)
is applied as a true in-place AT Protocol edit: ``Pds.put_record`` with the
SAME rkey, so the post's ``at://`` URI never changes and any existing
likes/reposts/quotes/replies on it stay valid. An edit that changes the
post's *shape* -- e.g. it used to spill into a second thread post and the
edited text now fits in one, or vice versa -- can't be done in place (a
thread is a real chain of distinct records, each later one's own
``reply.parent`` strong-ref pinned to the CID of the one before it, not
something resizable without re-chaining every part after the change), so
the old records are deleted and the edited content is published fresh
under new rkeys, keeping the SAME Matrix event id so a later edit or
redaction still finds it. This does orphan any likes/reposts/quotes/
replies that existed on the deleted records -- there's no way around that
for a shape-changing edit.

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

import logging

from fastapi import Request

from bridge.atproto import cbor
from bridge.atproto.cid import CID
from bridge.commands import bot_mxid, notice
from bridge.matrix_records import (
    MAX_THREAD_POSTS,
    MEDIA_MSGTYPES,
    POST_COLLECTION,
    RECORD_EMBED_TYPE,
    RECORD_WITH_MEDIA_EMBED_TYPE,
    post_record_v4,
    post_source,
    post_text,
    quoted_link_suffix,
    rich_post_parts,
)
from bridge.pds import PdsError, Write
from bridge.post_lookup import find_linked_post
from bridge.profile_posts import _build_embed
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

    text = post_text(synthetic, offset=0)
    trailing = None
    quoted = None
    if text:
        linked = await find_linked_post(request.app, text)
        if linked is not None:
            _url, quoted = linked
            trailing = quoted_link_suffix(synthetic, offset=0, quoted=quoted["uri"])
    drop_link_to = quoted["uri"] if trailing is not None else None
    parts = rich_post_parts(synthetic, offset=0, trailing=trailing)
    if parts is None:
        if new_content.get("msgtype") != "m.text":
            await notice(request, room_id, "That kind of message can't be published to Bluesky.")
        return True
    if len(parts) > MAX_THREAD_POSTS:
        await notice(
            request, room_id,
            f"That edit would take a thread of {len(parts)} posts; the limit is {MAX_THREAD_POSTS}, "
            "so the Bluesky post wasn't updated.",
        )
        return True

    embed = None
    if new_content.get("msgtype") in MEDIA_MSGTYPES:
        embed = await _build_embed(request, account, synthetic)
        if embed is None:
            return True  # _build_embed already told the room why

    if quoted is not None:
        record_embed = {"$type": RECORD_EMBED_TYPE, "record": quoted}
        embed = {"$type": RECORD_WITH_MEDIA_EMBED_TYPE, "record": record_embed, "media": embed} if embed else record_embed

    if len(parts) == 1 and len(rows) == 1:
        await _edit_in_place(
            request, account, rows[0], synthetic, embed,
            edit_event_id=edit_event_id, target_event_id=target_event_id, drop_link_to=drop_link_to,
        )
    else:
        await _delete_and_republish(
            request, account, rows, synthetic, parts, embed,
            edit_event_id=edit_event_id, target_event_id=target_event_id, drop_link_to=drop_link_to,
        )
    return True


async def _edit_in_place(
    request: Request, account: Account, row: RecordRow, event: dict, embed: dict | None,
    *, edit_event_id: str, target_event_id: str, drop_link_to: str | None,
) -> None:
    record = post_record_v4(event, part=0, embed=embed, reply=None, offset=0, drop_link_to=drop_link_to)
    if record is None:
        return
    source = post_source(
        event["room_id"], edit_event_id, part=0, embed=embed, reply=None, offset=0, drop_link_to=drop_link_to,
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
    *, edit_event_id: str, target_event_id: str, drop_link_to: str | None,
) -> None:
    repository = request.app.state.repository
    pds = request.app.state.pds
    room_id = event["room_id"]
    try:
        await pds.apply_writes(rows[0].did, [Write("delete", row.collection, row.rkey) for row in rows])
    except PdsError as exc:
        logger.warning(
            "Deleting %s's old records for edit of %s failed: %s", rows[0].did, target_event_id, exc.message
        )
        await notice(request, room_id, "Couldn't update that post on Bluesky.")
        return
    await repository.forget_media_for_event(target_event_id)

    # Same loop publish_event_as_posts runs for a fresh post -- not reused
    # directly because it ties a write's ``source["event_id"]`` and
    # ``matrix_event_id`` to the SAME id, and here they must differ (see
    # module docstring).
    writes: list[Write] = []
    root_ref: dict[str, str] | None = None
    parent_ref: dict[str, str] | None = None
    for index in range(len(parts)):
        reply = {"root": root_ref, "parent": parent_ref} if parent_ref is not None else None
        part_embed = embed if index == 0 else None
        record = post_record_v4(event, part=index, embed=part_embed, reply=reply, offset=0, drop_link_to=drop_link_to)
        if record is None:
            return
        rkey = pds.next_record_key()
        writes.append(Write(
            "create", POST_COLLECTION, rkey, record,
            source=post_source(
                room_id, edit_event_id, part=index, embed=part_embed, reply=reply, offset=0,
                drop_link_to=drop_link_to,
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
        return
    logger.info(
        "Republished %s (edit) as %d post(s) for %s (source event %s)",
        target_event_id, len(writes), account.did, edit_event_id,
    )
