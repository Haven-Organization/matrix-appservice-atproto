"""Encrypted attachments: one decrypted copy, shared by every bridge.

A message forwarded out of an end-to-end encrypted room often keeps its
file encrypted (``content.file``, Matrix's EncryptedFile, instead of
``content.url``) even though the event itself is plain. Bluesky can't take
ciphertext, so posting it means decrypting it and putting a plaintext copy
on the homeserver: something the sender has to agree to.

Several bridges may want the same file (matrix-appservice-activitypub,
-nostr and this one share Profile Rooms), and the sender should neither be
asked three times nor end up with three plaintext copies. So the bridges
listed in ``encrypted_media.bridges`` take turns, in that order:

- The first one that wants to publish the post asks (a thread reply to it,
  carrying ``PROMPT_FIELD``). Each later one waits ``wait_seconds`` per
  place in the list, then looks at the post's relations: a prompt or an
  announcement from a bridge before it means it has nothing to ask.
- The sender confirms by replying "confirm" to the prompt, or reacting to
  it with 👍 (any skin tone). Only the bridge that asked acts on it:
  it decrypts the file, uploads it once, and announces the copy with a
  ``software.haven.decrypted_media`` event related to the post (a type
  clients don't show).
- Every other listed bridge, on seeing that announcement, checks the copy
  is the sender's exact file -- re-encrypting it with the key and IV in
  the post must give the ciphertext's hash, so the announcing bridge
  needn't be trusted -- and then handles the post as if it had always
  had a plain ``url``.

Each bridge remembers "encrypted mxc -> plaintext mxc" (bookkeeping, no
content), so a file confirmed once is never asked about or decrypted again.
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import copy
import hashlib
import html
import logging

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import FastAPI, Request

from bridge.commands import _first_line, bot_mxid, reply_target_event_id
from bridge.follows import run_in_background
from bridge.matrix_records import MEDIA_MSGTYPES
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)

# On a prompt: {"prompt_for": <the post's event ID>}. Shared by the bridges.
PROMPT_FIELD = "software.haven.encrypted_media"
# The announcement of a decrypted copy, and its relation to the post.
ANNOUNCE_TYPE = "software.haven.decrypted_media"
# "encrypted mxc -> plaintext mxc", in the key-value store.
_RECORD_KEY = "decrypted_media:{}"
_VARIATION_SELECTOR = "️"
CONFIRM_REACTIONS = frozenset(
    key.replace(_VARIATION_SELECTOR, "")
    for key in ("👍", "👍🏻", "👍🏼", "👍🏽", "👍🏾", "👍🏿")
)
_THUMBNAIL_FIELDS = ("thumbnail_file", "thumbnail_info", "thumbnail_url")

# The event the current transaction is handling: what a publishing path
# that runs into an encrypted file holds back (an edit's synthetic event
# isn't a real one to answer).
_current_event: contextvars.ContextVar[dict | None] = contextvars.ContextVar("encrypted_media_event", default=None)
# Posts this process is already waiting on or has asked about.
_held: set[str] = set()


class _AppOnly:
    """What the event handlers need of a request (``.app``), for running a
    post through them again outside of the transaction it came in."""

    def __init__(self, app: FastAPI) -> None:
        self.app = app


def handling(event: dict) -> contextvars.Token:
    return _current_event.set(event)


def done_handling(token: contextvars.Token) -> None:
    _current_event.reset(token)


def _b64(value: str, *, urlsafe: bool) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded) if urlsafe else base64.b64decode(padded)


def encrypted_file(content: dict) -> dict | None:
    """``content``'s EncryptedFile, if it's a media message whose file is
    encrypted and has what decrypting it needs."""
    if content.get("msgtype") not in MEDIA_MSGTYPES or isinstance(content.get("url"), str):
        return None
    file = content.get("file")
    if not isinstance(file, dict) or not isinstance(file.get("url"), str) or not file["url"].startswith("mxc://"):
        return None
    if not isinstance((file.get("key") or {}).get("k"), str) or not isinstance(file.get("iv"), str):
        return None
    if not isinstance((file.get("hashes") or {}).get("sha256"), str):
        return None
    return file


def _media_content(event: dict) -> dict:
    """The content an event's media is in: an edit's ``m.new_content``."""
    content = event.get("content") or {}
    new_content = content.get("m.new_content")
    return new_content if isinstance(new_content, dict) else content


def with_plain_copy(event: dict, plain_mxc: str) -> dict:
    """``event`` with its encrypted file swapped for the plaintext copy."""
    event = copy.deepcopy(event)
    content = event.get("content") or {}
    for part in (content, content.get("m.new_content")):
        if isinstance(part, dict) and encrypted_file(part) is not None:
            part.pop("file", None)
            part["url"] = plain_mxc
            info = part.get("info")
            if isinstance(info, dict):
                for field in _THUMBNAIL_FIELDS:
                    info.pop(field, None)
    return event


def _cipher(file: dict) -> Cipher:
    return Cipher(algorithms.AES(_b64(file["key"]["k"], urlsafe=True)), modes.CTR(_b64(file["iv"], urlsafe=False)))


def _expected_hash(file: dict) -> bytes:
    return _b64(file["hashes"]["sha256"], urlsafe=False)


async def known_copy(app: FastAPI, file: dict) -> str | None:
    return await app.state.repository.get_value(_RECORD_KEY.format(file["url"]))


async def _remember(app: FastAPI, file: dict, plain_mxc: str) -> None:
    await app.state.repository.set_value(_RECORD_KEY.format(file["url"]), plain_mxc)


def _bridges(app: FastAPI) -> tuple[str, ...]:
    return app.state.config.encrypted_media.bridges


def _rank(app: FastAPI) -> int:
    bridges = _bridges(app)
    bot = bot_mxid(app.state.config)
    return bridges.index(bot) if bot in bridges else 0


async def _related(app: FastAPI, room_id: str, post_id: str) -> list[dict]:
    """The prompts and announcements about the post, from the listed bridges."""
    synapse = app.state.synapse
    bot = bot_mxid(app.state.config)
    found = []
    for rel_type in ("m.thread", ANNOUNCE_TYPE):
        try:
            found += await synapse.get_relations(room_id, post_id, rel_type, as_user_id=bot)
        except SynapseError:
            logger.info("Could not read the relations of %s", post_id, exc_info=True)
    listed = set(_bridges(app)) | {bot}
    return [e for e in found if e.get("sender") in listed]


def _is_announce(event: dict, post_id: str) -> bool:
    relation = (event.get("content") or {}).get("m.relates_to") or {}
    return event.get("type") == ANNOUNCE_TYPE and relation.get("event_id") == post_id


def _is_prompt(event: dict, post_id: str) -> bool:
    field = (event.get("content") or {}).get(PROMPT_FIELD)
    return isinstance(field, dict) and field.get("prompt_for") == post_id


# -- before the handlers: a copy already known ---------------------------------


async def resolve(request: Request, event: dict) -> dict:
    """``event`` with a plaintext copy in place of its encrypted file, if
    one was confirmed before (here, or announced by another bridge);
    otherwise ``event`` unchanged. With a copy, and other bridges to tell,
    it's announced for this post too if nobody has yet, so they don't ask."""
    if event.get("type") != "m.room.message":
        return event
    file = encrypted_file(_media_content(event))
    if file is None:
        return event
    app = request.app
    plain = await known_copy(app, file)
    if plain is None:
        return event
    room_id, post_id = event.get("room_id", ""), event.get("event_id", "")
    others = [b for b in _bridges(app) if b != bot_mxid(app.state.config)]
    if others and room_id and post_id and not any(_is_announce(e, post_id) for e in await _related(app, room_id, post_id)):
        await _announce(app, room_id, post_id, _media_content(event), file, plain, size=None)
    return with_plain_copy(event, plain)


# -- a publishing path that met an encrypted file ------------------------------


def hold(app: FastAPI) -> None:
    """Called by a publishing path instead of refusing an encrypted file:
    the event being handled waits for a decrypted copy -- asked for here,
    or by a bridge before this one in the list. Nothing is said in the
    room until then."""
    event = _current_event.get()
    if not event or event.get("type") != "m.room.message":
        return
    post_id = event.get("event_id")
    if not post_id or post_id in _held or encrypted_file(_media_content(event)) is None:
        return
    _held.add(post_id)
    run_in_background(app, _wait_or_ask(app, event))


async def _wait_or_ask(app: FastAPI, event: dict) -> None:
    room_id, post_id = event["room_id"], event["event_id"]
    try:
        rank = _rank(app)
        if rank:
            await asyncio.sleep(rank * app.state.config.encrypted_media.wait_seconds)
            related = await _related(app, room_id, post_id)
            announce = next((e for e in related if _is_announce(e, post_id)), None)
            if announce is not None:
                await handle_announce(app, announce)
                return
            if any(_is_prompt(e, post_id) for e in related):
                return  # a bridge before this one asked: its announcement will come
        await _ask(app, event)
    except Exception:
        logger.exception("Waiting on a decrypted copy of %s failed", post_id)
    finally:
        _held.discard(post_id)


async def _ask(app: FastAPI, event: dict) -> None:
    room_id, post_id, sender = event["room_id"], event["event_id"], event.get("sender", "")
    synapse = app.state.synapse
    name = sender
    try:
        name = (await synapse.get_profile(sender)).get("displayname") or sender
    except SynapseError:
        pass
    pill = f'<a href="https://matrix.to/#/{html.escape(sender, quote=True)}">{html.escape(name)}</a>'
    lead = (
        "this message is encrypted. To post it to your linked networks, the bridges need one decrypted copy "
        "of it on your homeserver, which they all share."
    )
    ask = 'Reply "confirm" to this message, or react with 👍, to upload it.'
    content = {
        "msgtype": "m.text",
        "body": f"⚠️{name}, {lead}\n\n{ask}",
        "format": "org.matrix.custom.html",
        "formatted_body": f"<p>⚠️{pill}, {html.escape(lead)}</p><p>{html.escape(ask)}</p>",
        "m.mentions": {"user_ids": [sender]},
        "m.relates_to": {
            "rel_type": "m.thread", "event_id": post_id, "is_falling_back": True,
            "m.in_reply_to": {"event_id": post_id},
        },
        PROMPT_FIELD: {"prompt_for": post_id},
    }
    try:
        await synapse.send_message_event(room_id, content, as_user_id=bot_mxid(app.state.config))
    except SynapseError:
        logger.warning("Could not ask about the encrypted file in %s", post_id, exc_info=True)


# -- confirmations and announcements --------------------------------------------


def _confirm_target(event: dict) -> str | None:
    """The event a "confirm" reply or a confirming reaction answers."""
    content = event.get("content") or {}
    if event.get("type") == "m.reaction":
        relation = content.get("m.relates_to") or {}
        key = str(relation.get("key") or "").replace(_VARIATION_SELECTOR, "")
        if relation.get("rel_type") == "m.annotation" and key in CONFIRM_REACTIONS:
            target = relation.get("event_id")
            return target if isinstance(target, str) and target else None
        return None
    if event.get("type") == "m.room.message" and _first_line(content).strip().lower() == "confirm":
        return reply_target_event_id(content)
    return None


async def maybe_handle_encrypted_media(request: Request, event: dict) -> bool:
    """Returns True if this was a confirmation of one of this bridge's
    prompts, or another bridge's announcement of a decrypted copy."""
    app = request.app
    if event.get("type") == ANNOUNCE_TYPE:
        if event.get("sender") in _bridges(app) and event.get("sender") != bot_mxid(app.state.config):
            run_in_background(app, handle_announce(app, event))
        return True
    target = _confirm_target(event)
    if target is None:
        return False
    room_id = event.get("room_id", "")
    bot = bot_mxid(app.state.config)
    try:
        prompt = await app.state.synapse.get_event(room_id, target, as_user_id=bot)
    except SynapseError:
        return False
    prompt_content = (prompt or {}).get("content") or {}
    field = prompt_content.get(PROMPT_FIELD)
    if (prompt or {}).get("sender") in set(_bridges(app)) - {bot} and (
        isinstance(field, dict) or "this message is encrypted" in str(prompt_content.get("body") or "")
    ):
        return True  # an answer to a sibling bridge's prompt: it acts on it, and it's never posted here
    if (prompt or {}).get("sender") != bot or not isinstance(field, dict) or not isinstance(field.get("prompt_for"), str):
        return False  # not one of this bridge's prompts (a Danger Zone warning, say)
    run_in_background(app, _confirmed(app, room_id, field["prompt_for"], event.get("sender", "")))
    return True


async def _thread_notice(app: FastAPI, room_id: str, post_id: str, text: str) -> None:
    content = {
        "msgtype": "m.notice", "body": text,
        "m.relates_to": {"rel_type": "m.thread", "event_id": post_id, "is_falling_back": True,
                         "m.in_reply_to": {"event_id": post_id}},
    }
    try:
        await app.state.synapse.send_message_event(room_id, content, as_user_id=bot_mxid(app.state.config))
    except SynapseError:
        logger.warning("Could not send a notice to %s", room_id, exc_info=True)


async def _confirmed(app: FastAPI, room_id: str, post_id: str, confirmer: str) -> None:
    synapse = app.state.synapse
    try:
        post = await synapse.get_event(room_id, post_id, as_user_id=bot_mxid(app.state.config))
    except SynapseError:
        post = None
    if not post or post.get("sender") != confirmer:
        return  # only the sender may have their file decrypted
    content = _media_content(post)
    file = encrypted_file(content)
    if file is None:
        return
    plain = await known_copy(app, file)
    size = None
    if plain is None:
        decrypted = await _decrypt(app, file)
        if decrypted is None:
            await _thread_notice(app, room_id, post_id, "Couldn't decrypt/upload that file, so it wasn't posted.")
            return
        info = content.get("info") if isinstance(content.get("info"), dict) else {}
        mime = str(info.get("mimetype") or "application/octet-stream")
        filename = str(content.get("body") or "file")
        try:
            plain = await synapse.upload_media(decrypted, mime, filename, as_user_id=bot_mxid(app.state.config))
        except SynapseError:
            logger.warning("Could not upload the decrypted copy of %s", file["url"], exc_info=True)
            await _thread_notice(app, room_id, post_id, "Couldn't decrypt/upload that file, so it wasn't posted.")
            return
        size = len(decrypted)
        await _remember(app, file, plain)
    await _announce(app, room_id, post_id, content, file, plain, size=size)
    await _replay(app, post)
    await _thread_notice(app, room_id, post_id, "✅ Sent.")


async def _decrypt(app: FastAPI, file: dict) -> bytes | None:
    try:
        download = await app.state.synapse.download_media(file["url"])
    except SynapseError:
        logger.warning("Could not download encrypted file %s", file["url"], exc_info=True)
        return None
    if download is None:
        return None
    try:
        if hashlib.sha256(download.content).digest() != _expected_hash(file):
            logger.warning("Encrypted file %s doesn't match its hash; not decrypting it", file["url"])
            return None
        decryptor = _cipher(file).decryptor()
        return decryptor.update(download.content) + decryptor.finalize()
    except (ValueError, KeyError, TypeError):
        logger.warning("Could not decrypt %s", file["url"], exc_info=True)
        return None


async def _announce(
    app: FastAPI, room_id: str, post_id: str, content: dict, file: dict, plain: str, *, size: int | None
) -> None:
    info = {k: v for k, v in (content.get("info") or {}).items() if k not in _THUMBNAIL_FIELDS}
    if size is not None:
        info["size"] = size
    announcement = {
        "m.relates_to": {"rel_type": ANNOUNCE_TYPE, "event_id": post_id},
        "url": plain,
        "encrypted_url": file["url"],
        "info": info,
    }
    try:
        await app.state.synapse.send_message_event(
            room_id, announcement, as_user_id=bot_mxid(app.state.config), event_type=ANNOUNCE_TYPE
        )
    except SynapseError:
        logger.warning("Could not announce the decrypted copy of %s", post_id, exc_info=True)


async def handle_announce(app: FastAPI, announce: dict) -> None:
    """Another bridge's decrypted copy of a post's file: checked, remembered,
    and the post handled as if it had always been plain."""
    content = announce.get("content") or {}
    post_id = (content.get("m.relates_to") or {}).get("event_id")
    plain = content.get("url")
    room_id = announce.get("room_id", "")
    if not isinstance(post_id, str) or not isinstance(plain, str) or not plain.startswith("mxc://"):
        return
    synapse = app.state.synapse
    try:
        post = await synapse.get_event(room_id, post_id, as_user_id=bot_mxid(app.state.config))
    except SynapseError:
        return
    file = encrypted_file(_media_content(post or {}))
    if not post or file is None or file["url"] != content.get("encrypted_url"):
        logger.info("Ignoring an announcement of %s: it doesn't match the post's file", post_id)
        return
    known = await known_copy(app, file)
    if known is None:
        if not await _is_exact_copy(app, file, plain):
            logger.warning("Ignoring %s's copy of %s: it isn't the sender's file", announce.get("sender"), file["url"])
            return
        await _remember(app, file, plain)
    await _replay(app, post)


async def _is_exact_copy(app: FastAPI, file: dict, plain: str) -> bool:
    """Whether ``plain`` encrypts, with the post's own key and IV, to the
    ciphertext the post's hash names: the user's exact file."""
    try:
        download = await app.state.synapse.download_media(plain)
    except SynapseError:
        logger.info("Could not download %s to check it", plain, exc_info=True)
        return False
    if download is None:
        return False
    try:
        encryptor = _cipher(file).encryptor()
        ciphertext = encryptor.update(download.content) + encryptor.finalize()
        return hashlib.sha256(ciphertext).digest() == _expected_hash(file)
    except (ValueError, KeyError, TypeError):
        return False


async def _replay(app: FastAPI, post: dict) -> None:
    """The post through the usual handlers again, now with its copy known
    (``resolve`` swaps it in), so every usual check still applies."""
    from bridge.appservice_routes import handle_event

    try:
        await handle_event(_AppOnly(app), post)
    except Exception:
        logger.exception("Publishing %s with its decrypted copy failed", post.get("event_id"))
