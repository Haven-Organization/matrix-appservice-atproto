"""Mirroring remote accounts' posts and reposts into Matrix.

Each post is sent by its author's ghost user, with links, mentions and
hashtags made clickable. Images and video are downloaded from the author's
PDS and uploaded to the Matrix media repository, so they display natively
and keep working if the original goes away. A link card is appended to
the text as a link. Deleting the post on Bluesky redacts what was
mirrored.

Where a post goes depends on what it is, the same way as in
matrix-appservice-activitypub:

- An ordinary post goes in its author's Remote User Room.
- A reply to a post that is in Matrix somewhere -- another mirrored post,
  or a local user's own post -- is threaded under that post, in that
  post's room: the conversation stays in one place. If the replier is
  followed here, an annotated copy ("⤵️ Reply to X's post") is also
  cross-posted into their own room, so that room stays a complete
  timeline of the account.
- A reply to a post that isn't in Matrix goes in the replier's own room,
  headed by a link to what it answers.
- A repost goes in the reposter's room as one event, "🔁 reposted X's
  post:" above the reposted post's own content, with "post" linking to
  that post in Matrix (it is imported first if it isn't there).
- A quote-post ends with "🔁 reposted X's post" and a preview of the
  quoted post, which is imported per ``bridge.quote_import_policy``.

A post is always ONE event: its first image or video, captioned with the
text, with any further attachments listed as links (as in
matrix-appservice-activitypub -- Matrix has no multi-file event yet).
"""

from __future__ import annotations

import html
import logging
import time
from urllib.parse import quote as url_quote
from datetime import datetime
from typing import Any

from fastapi import FastAPI

from bridge.atproto.identity import get_blob, get_record, post_url, resolve_did
from bridge.matrix_records import POST_COLLECTION
from bridge.msc4501 import (
    EXTERNAL_HANDLE_FIELD,
    SOCIAL_BODY_FIELD,
    SOCIAL_FORMATTED_BODY_FIELD,
    SOCIAL_REL_TYPE_REPLY,
    SOCIAL_REL_TYPE_REPOST,
    SOCIAL_RELATES_TO_FIELD,
    external_handle_value,
    mirrored_post_event_type,
    social_relates_to,
)
from bridge.post_lookup import PostRef, locate_post_by_uri
from bridge.previews import PREVIEW_TEXT_LIMIT, fetch_post_preview, post_text_of, quote_block, user_display
from bridge.repository import MirroredPost, RemoteAccount
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)

REPOST_COLLECTION = "app.bsky.feed.repost"
ECHO_SUFFIX = "#echo"
_IMAGE_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}


def _bot(app: FastAPI) -> str:
    config = app.state.config
    return f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"


def _at_uri_to_url(uri: str) -> str | None:
    """The bsky.app page for a post's ``at://`` address."""
    parts = uri.removeprefix("at://").split("/")
    if not uri.startswith("at://") or len(parts) != 3 or parts[1] != POST_COLLECTION:
        return None
    return post_url(parts[0], parts[2])


def matrix_event_link(app: FastAPI, room_id: str, event_id: str) -> str:
    return f"https://matrix.to/#/{room_id}/{event_id}?via={app.state.config.synapse.server_name}"


def render_text(text: str, facets: Any) -> str:
    """``text`` as HTML with its facets turned into links. Facets address
    the text by UTF-8 byte range; ones that are malformed or overlap an
    earlier one are ignored rather than trusted."""
    raw = text.encode("utf-8")
    spans: list[tuple[int, int, str]] = []
    for facet in facets if isinstance(facets, list) else []:
        try:
            start, end = int(facet["index"]["byteStart"]), int(facet["index"]["byteEnd"])
            feature = facet["features"][0]
            kind = feature.get("$type", "")
            if kind.endswith("#link"):
                href = str(feature["uri"])
            elif kind.endswith("#mention"):
                href = f"https://bsky.app/profile/{feature['did']}"
            elif kind.endswith("#tag"):
                href = f"https://bsky.app/hashtag/{feature['tag']}"
            else:
                continue
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if 0 <= start < end <= len(raw) and href.startswith("https://"):
            spans.append((start, end, href))
    spans.sort()
    out: list[str] = []
    position = 0
    for start, end, href in spans:
        if start < position:
            continue
        out.append(html.escape(raw[position:start].decode("utf-8", errors="replace")))
        label = html.escape(raw[start:end].decode("utf-8", errors="replace"))
        out.append(f'<a href="{html.escape(href, quote=True)}">{label}</a>')
        position = end
    out.append(html.escape(raw[position:].decode("utf-8", errors="replace")))
    return "".join(out).replace("\n", "<br>")


def _timestamp_ms(record: dict[str, Any]) -> int | None:
    """The post's own time for display, never in the future (a post can
    claim any ``createdAt`` it likes)."""
    try:
        moment = datetime.fromisoformat(str(record.get("createdAt", "")).replace("Z", "+00:00"))
        return min(int(moment.timestamp() * 1000), int(time.time() * 1000))
    except (ValueError, OverflowError):
        return None


def _media_items(embed: Any) -> list[dict[str, Any]]:
    """The images/video of an embed as ``{kind, blob, alt, ratio}``."""
    if not isinstance(embed, dict):
        return []
    kind = embed.get("$type", "")
    if kind == "app.bsky.embed.recordWithMedia":
        return _media_items(embed.get("media"))
    items = []
    if kind == "app.bsky.embed.images":
        for image in embed.get("images") or []:
            items.append({"kind": "m.image", "blob": image.get("image"), "alt": image.get("alt"), "ratio": image.get("aspectRatio")})
    elif kind == "app.bsky.embed.video":
        items.append({"kind": "m.video", "blob": embed.get("video"), "alt": embed.get("alt"), "ratio": embed.get("aspectRatio")})
    return [item for item in items if isinstance(item["blob"], dict)]


def _link_lines(embed: Any) -> list[tuple[str, str]]:
    """Extra lines for a link card or quoted post, as (plain, html) pairs."""
    if not isinstance(embed, dict):
        return []
    kind = embed.get("$type", "")
    if kind == "app.bsky.embed.recordWithMedia":
        return _link_lines(embed.get("record")) + _link_lines(embed.get("media"))
    if kind == "app.bsky.embed.external":
        external = embed.get("external") or {}
        uri = str(external.get("uri", ""))
        if not uri.startswith(("https://", "http://")):
            return []
        title = str(external.get("title") or uri)
        return [(f"🔗 {title}: {uri}", f'🔗 <a href="{html.escape(uri, quote=True)}">{html.escape(title)}</a>')]
    return []


def _blob_url(blob_owner: tuple[str, str], cid: str, kind: str) -> str:
    """A public link to one of a post's files. An image goes through
    Bluesky's CDN, which shows it in the browser; a PDS only ever offers
    its blobs as downloads."""
    did, pds = blob_owner
    if kind == "m.image":
        return f"https://cdn.bsky.app/img/feed_fullsize/plain/{url_quote(did, safe=':')}/{url_quote(cid, safe='')}@jpeg"
    return f"{pds.rstrip('/')}/xrpc/com.atproto.sync.getBlob?did={url_quote(did, safe='')}&cid={url_quote(cid, safe='')}"


def _quoted_uri(embed: Any) -> str:
    if not isinstance(embed, dict):
        return ""
    if embed.get("$type") == "app.bsky.embed.recordWithMedia":
        return _quoted_uri(embed.get("record"))
    if embed.get("$type") == "app.bsky.embed.record":
        return str((embed.get("record") or {}).get("uri", ""))
    return ""


async def social_relation(app: FastAPI, rel_type: str, target: PostRef | None, **extra: Any) -> dict[str, Any] | None:
    """The MSC4501 relation to ``target`` (a post that is in Matrix), if
    relations are enabled. Carries the target event's own content unless
    ``content_inline`` is passed."""
    config = app.state.config
    if target is None or not config.bridge.set_msc4501_relates_to:
        return None
    content = None
    if not extra.get("content_inline"):
        try:
            event = await app.state.synapse.get_event(target.room_id, target.event_id, as_user_id=_bot(app))
        except SynapseError:
            event = None
        content = (event or {}).get("content")
    return social_relates_to(
        rel_type,
        event_id=target.event_id,
        room_id=target.room_id,
        sender=target.sender,
        via=[config.synapse.server_name],
        content=content,
        **extra,
    )


async def _ensure_ghost_in_room(app: FastAPI, ghost: str, room_id: str) -> bool:
    """Get ``ghost`` into ``room_id`` (the bot invites, the ghost joins).
    False if that isn't possible -- a room the bot can't invite into."""
    synapse = app.state.synapse
    try:
        if ghost in await synapse.get_joined_members(room_id, as_user_id=_bot(app)):
            return True
        await synapse.invite_user(room_id, ghost, as_user_id=_bot(app))
        await synapse.join_room(room_id, as_user_id=ghost)
        return True
    except SynapseError:
        logger.info("Could not bring %s into %s", ghost, room_id, exc_info=True)
        return False


async def _send_post(
    app: FastAPI,
    *,
    room_id: str,
    ghost: str,
    handle: str,
    record: dict[str, Any],
    blob_owner: tuple[str, str],
    permalink: str,
    file_stem: str,
    header: tuple[str, str] | None = None,
    relates_to: dict[str, Any] | None = None,
    relation: dict[str, Any] | None = None,
    extra_fields: dict[str, Any] | None = None,
    tail: tuple[str, str] | None = None,
) -> list[str]:
    """Send one post's content into ``room_id`` as ``ghost`` -- always as
    ONE event, however many files the post has, so "the post" is a single
    thing to react to or reply to. A first image or video is embedded,
    captioned with the text; any further attachments are listed as links
    under "Other Attachments:" (or all of them under "Attachments:" if the
    first couldn't be embedded). ``header`` goes above the text and
    ``tail`` (plain, HTML) below it. ``blob_owner`` is the ``(did, pds)``
    its media is fetched from. Returns the event ID sent, as a list --
    empty if sending failed."""
    synapse = app.state.synapse
    config = app.state.config
    event_type = mirrored_post_event_type(config)
    ts = _timestamp_ms(record)
    embed = record.get("embed")
    # Fields every event of this post carries.
    common: dict[str, Any] = {"external_url": permalink, **(extra_fields or {})}
    if config.bridge.msc4503_external_handle in ("events", "both"):
        common[EXTERNAL_HANDLE_FIELD] = external_handle_value(config, handle=handle)

    text = str(record.get("text") or "")
    plain_lines = [header[0]] if header else []
    html_lines = [header[1]] if header else []
    if text:
        plain_lines.append(text)
        html_lines.append(render_text(text, record.get("facets")))
    for plain, formatted in _link_lines(embed):
        plain_lines.append(plain)
        html_lines.append(formatted)

    plain_body = "\n\n".join(plain_lines)
    formatted_body = "<br><br>".join(html_lines)
    if tail is not None:
        plain_body = f"{plain_body}\n\n{tail[0]}".strip()
        formatted_body += tail[1]
    max_bytes = config.atproto.max_media_mb * 1_000_000
    media = _media_items(embed)

    def named(index: int, item: dict[str, Any]) -> str:
        mime_type = str(item["blob"].get("mimeType") or "")
        extension = "mp4" if item["kind"] == "m.video" else _IMAGE_EXTENSIONS.get(mime_type, "bin")
        return f"{file_stem}-{index + 1}.{extension}"

    try:
        content: dict[str, Any] = {"msgtype": "m.text", **common}
        embedded = False
        if media:
            item = media[0]
            blob = item["blob"]
            cid = str((blob.get("ref") or {}).get("$link", ""))
            mime_type = str(blob.get("mimeType") or "application/octet-stream")
            data = await get_blob(app.state.http_client, blob_owner[1], blob_owner[0], cid, max_bytes=max_bytes) if cid else None
            if data is not None:
                filename = named(0, item)
                mxc = await synapse.upload_media(data, mime_type, filename, as_user_id=ghost)
                info: dict[str, Any] = {"mimetype": mime_type, "size": len(data)}
                ratio = item["ratio"] if isinstance(item["ratio"], dict) else {}
                if isinstance(ratio.get("width"), int) and isinstance(ratio.get("height"), int):
                    info["w"], info["h"] = ratio["width"], ratio["height"]
                content.update(msgtype=item["kind"], url=mxc, info=info, filename=filename)
                embedded = True
        # Whatever isn't embedded is linked: everything past the first
        # attachment, or all of them if the first was too large or
        # unavailable.
        linked = list(enumerate(media))[1 if embedded else 0:]
        if linked:
            title = "Other Attachments:" if embedded else "Attachments:"
            links = []
            for index, item in linked:
                cid = str((item["blob"].get("ref") or {}).get("$link", ""))
                url = _blob_url(blob_owner, cid, item["kind"])
                alt = " ".join(str(item["alt"] or "").split())
                name = (alt[:77] + "..." if len(alt) > 80 else alt) or named(index, item)
                links.append((name.replace("[", "(").replace("]", ")"), url))
            plain_section = title + "\n" + "\n".join(f"[{name}]({url})" for name, url in links)
            html_section = title + "<br>" + "<br>".join(
                f'<a href="{html.escape(url, quote=True)}">{html.escape(name)}</a>' for name, url in links
            )
            plain_body = f"{plain_body}\n\n{plain_section}" if plain_body else plain_section
            formatted_body = f"{formatted_body}<br><br>{html_section}" if formatted_body else html_section
        if plain_body:
            # On an image or video, a body distinct from the filename is
            # its caption (MSC2530).
            content.update(body=plain_body, format="org.matrix.custom.html", formatted_body=formatted_body)
        elif embedded:
            # Alt text, where the author wrote any, is the best body.
            content["body"] = str(media[0]["alt"] or "") or content["filename"]
        else:
            return []
        if relates_to:
            content["m.relates_to"] = relates_to
        if relation:
            content[SOCIAL_RELATES_TO_FIELD] = relation
        return [await synapse.send_message_event(room_id, content, as_user_id=ghost, ts=ts, event_type=event_type)]
    except SynapseError:
        logger.warning("Mirroring into %s failed", room_id, exc_info=True)
        return []


def _thread_relation(root_event_id: str, parent_event_id: str) -> dict[str, Any]:
    return {
        "rel_type": "m.thread",
        "event_id": root_event_id,
        "is_falling_back": False,
        "m.in_reply_to": {"event_id": parent_event_id},
    }


async def mirror_post(
    app: FastAPI,
    remote: RemoteAccount,
    rkey: str,
    record: dict[str, Any],
    *,
    cid: str = "",
    thread_root: str | None = None,
) -> bool:
    """Mirror one post by ``remote`` (``record`` in its JSON form), as
    ``remote``'s ghost. Returns False if it was already mirrored or there
    was nothing to show.

    ``remote.room_id`` is where it goes if it isn't a reply to something
    in Matrix (see the module docstring for the placement rules).
    ``thread_root`` forces it into the Matrix thread under that event in
    ``remote.room_id`` instead -- used when the caller has already worked
    out which conversation it is fetching.
    """
    repository = app.state.repository
    uri = f"at://{remote.did}/{POST_COLLECTION}/{rkey}"
    if await repository.get_mirrored_post(uri) is not None:
        return False
    ghost = remote.ghost_user_id
    permalink = post_url(remote.did, rkey)
    reply = record.get("reply") if isinstance(record.get("reply"), dict) else {}
    parent_uri = str((reply.get("parent") or {}).get("uri", ""))
    root = reply.get("root") or {}
    root_uri, root_cid = str(root.get("uri", "")), str(root.get("cid", ""))
    parent = await locate_post_by_uri(app, parent_uri) if parent_uri else None
    common = {
        "ghost": ghost,
        "handle": remote.handle,
        "record": record,
        "blob_owner": (remote.did, remote.pds),
        "permalink": permalink,
        "file_stem": rkey,
    }

    room_id = remote.room_id
    header: tuple[str, str] | None = None
    relates_to: dict[str, Any] | None = None
    relation: dict[str, Any] | None = None
    echo_into: str | None = None

    if thread_root is not None:
        target = parent.event_id if parent is not None and parent.room_id == room_id else thread_root
        relates_to = _thread_relation(thread_root, target)
        relation = await social_relation(app, SOCIAL_REL_TYPE_REPLY, parent)
    elif parent is not None and await _ensure_ghost_in_room(app, ghost, parent.room_id):
        # The conversation is in Matrix: thread this under it, there.
        root_ref = await locate_post_by_uri(app, root_uri) if root_uri else None
        root_event = root_ref.event_id if root_ref is not None and root_ref.room_id == parent.room_id else parent.event_id
        room_id = parent.room_id
        relates_to = _thread_relation(root_event, parent.event_id)
        relation = await social_relation(app, SOCIAL_REL_TYPE_REPLY, parent)
        own = await repository.get_remote_account(remote.did)
        if own is not None and own.room_id != room_id:
            echo_into = own.room_id
    elif parent_uri:
        parent_url = _at_uri_to_url(parent_uri)
        if parent_url:
            header = (
                f"↩ Replying to {parent_url}",
                f'↩ Replying to <a href="{html.escape(parent_url, quote=True)}">{html.escape(parent_url)}</a>',
            )
    tail: tuple[str, str] | None = None
    quoted_uri = _quoted_uri(record.get("embed"))
    if quoted_uri:
        tail, quote_relation = await _quoted_post_render(app, quoted_uri)
        # A quote-post: MSC4501 treats it as a repost with commentary.
        # (A reply that also quotes keeps its reply relation.)
        relation = relation or quote_relation

    event_ids = await _send_post(
        app, room_id=room_id, header=header, relates_to=relates_to, relation=relation, tail=tail, **common
    )
    if not event_ids:
        return False
    await repository.add_mirrored_post(MirroredPost(uri, room_id, event_ids, cid, root_uri, root_cid))

    if echo_into is not None and parent is not None:
        # Cross-post into the replier's own room, pointing at the thread.
        link = matrix_event_link(app, room_id, relates_to["event_id"] if relates_to else parent.event_id)
        parent_label = await _display_label(app, parent)
        echo_header = (
            f"⤵️ Reply to {parent_label}'s post ({link})",
            f'⤵️ Reply to {html.escape(parent_label)}\'s <a href="{html.escape(link, quote=True)}">post</a>',
        )
        echo_ids = await _send_post(app, room_id=echo_into, header=echo_header, relation=relation, tail=tail, **common)
        if echo_ids:
            # Its own entry, so deleting the post redacts the echo too and
            # reacting to the echo acts on the same post.
            await repository.add_mirrored_post(
                MirroredPost(uri + ECHO_SUFFIX, echo_into, echo_ids, cid, root_uri, root_cid, shown_uri=uri, shown_cid=cid)
            )
    return True


async def _display_label(app: FastAPI, ref: PostRef) -> str:
    """A name for the author of a post that is in Matrix."""
    try:
        profile = await app.state.synapse.get_profile(ref.sender)
    except SynapseError:
        profile = {}
    return str(profile.get("displayname") or ref.sender)


async def import_post(app: FastAPI, uri: str, *, known_authors_only: bool = False) -> PostRef | None:
    """Make sure the post ``uri`` is in Matrix, mirroring it (and creating
    its author's ghost and room, with nobody invited) if it isn't.
    ``known_authors_only`` declines to do that for an author who has no
    room here yet. Returns where it is, or ``None``."""
    existing = await locate_post_by_uri(app, uri)
    if existing is not None:
        return existing
    parts = uri.removeprefix("at://").split("/")
    if len(parts) != 3 or parts[1] != POST_COLLECTION:
        return None
    repository = app.state.repository
    if await repository.get_account(parts[0]) is not None:
        return None  # a local post that isn't in Matrix can't be made to be
    if known_authors_only and await repository.get_remote_account(parts[0]) is None:
        return None
    identity = await resolve_did(app.state.http_client, parts[0], plc_directory=app.state.config.atproto.plc_directory)
    if identity is None:
        return None
    response = await get_record(app.state.http_client, identity.pds, identity.did, POST_COLLECTION, parts[2])
    if not isinstance((response or {}).get("value"), dict):
        return None
    # Imported here: bridge.remote_accounts itself builds on this module.
    from bridge.remote_accounts import ensure_remote_account

    try:
        remote, _created = await ensure_remote_account(app, identity)
        await mirror_post(app, remote, parts[2], response["value"], cid=str(response.get("cid", "")))
    except SynapseError:
        logger.info("Could not import %s", uri, exc_info=True)
        return None
    return await locate_post_by_uri(app, uri)


async def _quoted_post_render(app: FastAPI, quoted_uri: str) -> tuple[tuple[str, str], dict[str, Any] | None]:
    """What a quote-post shows of the post it quotes: a "🔁 reposted X's
    post" line -- "post" linking to the quoted post in Matrix when it is
    there -- above a preview of it. Returns that as (plain, HTML), and the
    MSC4501 repost relation if the quoted post is in Matrix."""
    config = app.state.config
    policy = config.bridge.quote_import_policy
    if policy == "never":
        quoted = await locate_post_by_uri(app, quoted_uri)
    else:
        quoted = await import_post(app, quoted_uri, known_authors_only=policy == "known")
    relation = None
    if quoted is not None:
        preview_text, full_content, _image, _video = await fetch_post_preview(app, quoted.room_id, quoted.event_id)
        link = matrix_event_link(app, quoted.room_id, quoted.event_id)
        author_name, author_html = await user_display(app, quoted.sender)
        if config.bridge.set_msc4501_relates_to and full_content:
            relation = social_relates_to(
                SOCIAL_REL_TYPE_REPOST,
                event_id=quoted.event_id,
                room_id=quoted.room_id,
                sender=quoted.sender,
                via=[config.synapse.server_name],
                content=full_content,
                displayname=author_name,
            )
    else:
        # Not in Matrix: read just enough of it to say what was quoted.
        preview_text = ""
        link = _at_uri_to_url(quoted_uri) or quoted_uri
        did = quoted_uri.removeprefix("at://").split("/", 1)[0]
        identity = await resolve_did(app.state.http_client, did, plc_directory=config.atproto.plc_directory)
        author_name = f"@{identity.handle}" if identity is not None else did
        author_html = f'<a href="https://bsky.app/profile/{html.escape(did, quote=True)}">{html.escape(author_name)}</a>'
        parts = quoted_uri.removeprefix("at://").split("/")
        if identity is not None and len(parts) == 3:
            response = await get_record(app.state.http_client, identity.pds, did, parts[1], parts[2])
            preview_text = str(((response or {}).get("value") or {}).get("text") or "")
            if len(preview_text) > PREVIEW_TEXT_LIMIT:
                preview_text = preview_text[:PREVIEW_TEXT_LIMIT].rstrip() + "…"
    quoted_plain, quoted_html = quote_block(preview_text)
    plain = f"🔁 reposted {author_name}'s post:" + (f"\n{quoted_plain}" if quoted_plain else "") + f"\n{link}"
    formatted = f'<p>🔁 reposted {author_html}\'s <a href="{html.escape(link, quote=True)}">post</a></p>{quoted_html}'
    return (plain, formatted), relation


async def mirror_repost(app: FastAPI, remote: RemoteAccount, rkey: str, record: dict[str, Any]) -> bool:
    """Mirror a repost by ``remote`` into its room, formatted the way
    matrix-appservice-activitypub formats one: a single event from the
    reposter's ghost reading "🔁 reposted X's post:" -- "post" being a
    link to the reposted post in Matrix -- above that post's own content.

    The reposted post is imported into Matrix first if it isn't there, so
    there is always a real event to point at. If it has an image or
    video, the repost event IS that image or video (the same media, not
    downloaded again), with the text as its caption.

    Per MSC4501, when the relation is attached the plain ``body`` is
    nothing but the matrix.to link to the reposted event: the rendering
    lives in ``formatted_body``, and the reposted post's own text is given
    separately for clients that want it.

    Returns False if it was already mirrored or the post can't be had.
    """
    repository = app.state.repository
    config = app.state.config
    synapse = app.state.synapse
    uri = f"at://{remote.did}/{REPOST_COLLECTION}/{rkey}"
    if await repository.get_mirrored_post(uri) is not None:
        return False
    subject = record.get("subject") if isinstance(record.get("subject"), dict) else {}
    target = await import_post(app, str(subject.get("uri", "")))
    if target is None:
        return False
    try:
        original = await synapse.get_event(target.room_id, target.event_id, as_user_id=_bot(app))
    except SynapseError:
        original = None
    original_content = (original or {}).get("content") or {}
    if not original_content:
        return False

    link = matrix_event_link(app, target.room_id, target.event_id)
    author_name, author_html = await user_display(app, target.sender)
    text = post_text_of(original_content)
    quoted_plain, _ = quote_block(text)
    # The original's own rich text if it has any (links stay links).
    inner_html = original_content.get("formatted_body") if text and original_content.get("formatted_body") else html.escape(text).replace("\n", "<br>")
    content_html = f"<blockquote>{inner_html}</blockquote>" if text else ""
    formatted_body = (
        f'<p>🔁 reposted {author_html}\'s <a href="{html.escape(link, quote=True)}">post</a>:</p>{content_html}'
    )
    plain_full = f"🔁 reposted {author_name}'s post:" + (f"\n\n{quoted_plain}" if quoted_plain else "") + f"\n\n{link}"

    relation = None
    if config.bridge.set_msc4501_relates_to:
        inline = config.bridge.use_msc4501_content_inline
        relation = social_relates_to(
            SOCIAL_REL_TYPE_REPOST,
            event_id=target.event_id,
            room_id=target.room_id,
            sender=target.sender,
            via=[config.synapse.server_name],
            displayname=author_name,
            content_inline=inline,
            content=None if inline else original_content,
        )
    content: dict[str, Any] = {
        "msgtype": "m.text",
        "body": link if relation else plain_full,
        "format": "org.matrix.custom.html",
        "formatted_body": formatted_body,
        "external_url": _at_uri_to_url(target.uri) or link,
    }
    if original_content.get("msgtype") in ("m.image", "m.video") and isinstance(original_content.get("url"), str):
        # The reposted post is an image or video: so is the repost.
        info = original_content.get("info") if isinstance(original_content.get("info"), dict) else {}
        content.update(
            msgtype=original_content["msgtype"],
            url=original_content["url"],
            info={key: info[key] for key in ("mimetype", "size", "w", "h") if key in info},
            filename=str(original_content.get("filename") or "") or post_filename(original_content),
        )
    if relation:
        content[SOCIAL_RELATES_TO_FIELD] = relation
        if text:
            content[SOCIAL_BODY_FIELD] = text
            content[SOCIAL_FORMATTED_BODY_FIELD] = inner_html
    if config.bridge.msc4503_external_handle in ("events", "both"):
        content[EXTERNAL_HANDLE_FIELD] = external_handle_value(config, handle=remote.handle)
    # Shown at the time of the repost, not of the original post.
    ts = _timestamp_ms({"createdAt": record.get("createdAt")})
    try:
        event_id = await synapse.send_message_event(
            remote.room_id, content, as_user_id=remote.ghost_user_id, ts=ts, event_type=mirrored_post_event_type(config)
        )
    except SynapseError:
        logger.warning("Mirroring repost %s failed", uri, exc_info=True)
        return False
    # A like of or reply to this event acts on the post it shows.
    await repository.add_mirrored_post(
        MirroredPost(uri, remote.room_id, [event_id], target.cid, shown_uri=target.uri, shown_cid=target.cid)
    )
    return True


def post_filename(content: dict[str, Any]) -> str:
    """A filename for a media event that has no separate one: its body is
    its filename unless it has been given a caption."""
    return str(content.get("body") or "attachment")


async def unmirror(app: FastAPI, uri: str) -> None:
    """Redact everything mirrored for the record ``uri`` (a post, with any
    cross-posted echo of it, or a repost)."""
    repository = app.state.repository
    for key in (uri, uri + ECHO_SUFFIX):
        mirrored = await repository.get_mirrored_post(key)
        if mirrored is None:
            continue
        for event_id in mirrored.event_ids:
            try:
                # As the bot, which can redact anyone's events in the rooms
                # it creates: these may be in a room other than the author's.
                await app.state.synapse.redact_event(mirrored.room_id, event_id, as_user_id=_bot(app))
            except SynapseError:
                logger.warning("Could not redact %s in %s", event_id, mirrored.room_id, exc_info=True)
        await repository.delete_mirrored_post(key)


async def unmirror_post(app: FastAPI, remote: RemoteAccount, rkey: str) -> None:
    await unmirror(app, f"at://{remote.did}/{POST_COLLECTION}/{rkey}")
