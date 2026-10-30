"""bridge.encrypted_media: one decrypted copy of an encrypted attachment,
asked for once and shared by the bridges listed in encrypted_media.bridges.

Run with: python -m pytest tests
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import itertools
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import bridge.appservice_routes as routes
from bridge import encrypted_media
from bridge.config import EncryptedMediaSection
from bridge.encrypted_media import ANNOUNCE_TYPE, PROMPT_FIELD
from bridge.synapse_client import MediaDownload

ROOM = "!profile:example.org"
OWNER = "@alice:example.org"
US = "@bluesky:example.org"
AP = "@fediverse:example.org"
NOSTR = "@nostr:example.org"
PLAIN = b"\x89PNG the user's picture"
KEY = bytes(range(32))
IV = bytes(range(8)) + bytes(8)


def _b64(data: bytes, *, urlsafe: bool = False) -> str:
    encoded = base64.urlsafe_b64encode(data) if urlsafe else base64.b64encode(data)
    return encoded.decode().rstrip("=")


def _encrypt(data: bytes) -> bytes:
    encryptor = Cipher(algorithms.AES(KEY), modes.CTR(IV)).encryptor()
    return encryptor.update(data) + encryptor.finalize()


class FakeSynapse:
    def __init__(self) -> None:
        self.events: dict[str, dict] = {}
        self.media: dict[str, bytes] = {}
        self._ids = itertools.count(1)
        self.uploads = 0

    def store(self, room_id: str, event_type: str, content: dict, sender: str) -> dict:
        event = {"event_id": f"$e{next(self._ids)}", "type": event_type, "room_id": room_id,
                 "sender": sender, "content": content}
        self.events[event["event_id"]] = event
        return event

    def sent(self, *, sender: str = US, event_type: str = "m.room.message") -> list[dict]:
        return [e for e in self.events.values() if e["sender"] == sender and e["type"] == event_type]

    async def send_message_event(self, room_id, content, *, as_user_id, ts=None, event_type="m.room.message"):
        return self.store(room_id, event_type, content, as_user_id)["event_id"]

    async def get_event(self, room_id, event_id, *, as_user_id):
        return self.events.get(event_id)

    async def get_relations(self, room_id, event_id, rel_type, *, as_user_id, limit=100):
        return [e for e in reversed(list(self.events.values()))
                if ((e["content"].get("m.relates_to") or {}).get("rel_type") == rel_type
                    and e["content"]["m.relates_to"].get("event_id") == event_id)]

    async def get_profile(self, user_id):
        return {"displayname": "Alice"}

    async def upload_media(self, data, content_type, filename, *, as_user_id):
        self.uploads += 1
        mxc = f"mxc://example.org/upload{next(self._ids)}"
        self.media[mxc] = data
        return mxc

    async def download_media(self, mxc):
        data = self.media.get(mxc)
        return MediaDownload(content=data, content_type="application/octet-stream") if data is not None else None


class FakeRepository:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get_value(self, key):
        return self.values.get(key)

    async def set_value(self, key, value):
        self.values[key] = value


@pytest.fixture
def bridge(monkeypatch):
    synapse = FakeSynapse()
    config = SimpleNamespace(
        appservice=SimpleNamespace(bot_localpart="bluesky"),
        synapse=SimpleNamespace(server_name="example.org"),
        encrypted_media=EncryptedMediaSection(),
    )
    app = SimpleNamespace(state=SimpleNamespace(config=config, synapse=synapse, repository=FakeRepository(),
                                                background_tasks=set()))
    handled: list[dict] = []

    async def record(request, event):
        if await encrypted_media.maybe_handle_encrypted_media(request, event):
            return
        handled.append(event)

    # The encrypted media hook runs as in the real chain; what the usual
    # handlers would do after it is theirs to test: here, just what they
    # were given.
    monkeypatch.setattr(routes, "_handle_event", record)
    encrypted_media._held.clear()
    return SimpleNamespace(app=app, synapse=synapse, handled=handled, request=SimpleNamespace(app=app))


def _listing(bridge, *bridges: str) -> None:
    bridge.app.state.config.encrypted_media = EncryptedMediaSection(bridges=bridges, wait_seconds=0)


def _post(bridge) -> dict:
    ciphertext = _encrypt(PLAIN)
    bridge.synapse.media["mxc://example.org/encrypted"] = ciphertext
    return bridge.synapse.store(ROOM, "m.room.message", {
        "msgtype": "m.image", "body": "image.png",
        "info": {"mimetype": "image/png", "size": len(PLAIN), "w": 7, "h": 8,
                 "thumbnail_file": {"url": "mxc://example.org/thumb"}},
        "file": {"url": "mxc://example.org/encrypted", "v": "v2", "iv": _b64(IV),
                 "key": {"alg": "A256CTR", "k": _b64(KEY, urlsafe=True), "kty": "oct"},
                 "hashes": {"sha256": _b64(hashlib.sha256(ciphertext).digest())}},
    }, OWNER)


def _run(coroutine):
    async def main():
        result = await coroutine
        return result
    return asyncio.run(main())


async def _settle(app) -> None:
    for _ in range(20):
        tasks = list(app.state.background_tasks)
        if not tasks:
            return
        await asyncio.gather(*tasks)


async def _held(bridge, post) -> None:
    """A publishing path meeting the post's encrypted file."""
    token = encrypted_media.handling(post)
    try:
        encrypted_media.hold(bridge.app)
    finally:
        encrypted_media.done_handling(token)
    await _settle(bridge.app)


async def _event(bridge, event) -> None:
    await routes.handle_event(bridge.request, event)
    await _settle(bridge.app)


def _prompts(bridge) -> list[dict]:
    return [e for e in bridge.synapse.sent() if PROMPT_FIELD in e["content"]]


def test_alone_it_asks(bridge) -> None:
    post = _post(bridge)
    _run(_held(bridge, post))
    [prompt] = _prompts(bridge)
    content = prompt["content"]
    assert content[PROMPT_FIELD] == {"prompt_for": post["event_id"]}
    assert content["m.relates_to"]["rel_type"] == "m.thread" and content["m.relates_to"]["event_id"] == post["event_id"]
    assert content["m.mentions"] == {"user_ids": [OWNER]}
    assert "this message is encrypted" in content["body"] and "react with 👍" in content["body"]
    assert len(bridge.synapse.sent()) == 1  # no refusal


def test_the_profile_post_path_holds_instead_of_refusing(bridge) -> None:
    from bridge.profile_posts import _build_embed

    post = _post(bridge)

    async def build():
        token = encrypted_media.handling(post)
        try:
            return await _build_embed(bridge.request, None, post)
        finally:
            encrypted_media.done_handling(token)

    async def main():
        embed = await build()
        await _settle(bridge.app)
        return embed

    assert asyncio.run(main()) is None
    assert [e["content"].get("msgtype") for e in bridge.synapse.sent()] == ["m.text"]  # the prompt only
    assert _prompts(bridge)


def test_a_later_bridge_leaves_it_to_an_earlier_ones_prompt(bridge) -> None:
    _listing(bridge, AP, US, NOSTR)
    post = _post(bridge)
    bridge.synapse.store(ROOM, "m.room.message", {
        "msgtype": "m.text", "body": "⚠️ this message is encrypted",
        "m.relates_to": {"rel_type": "m.thread", "event_id": post["event_id"]},
        PROMPT_FIELD: {"prompt_for": post["event_id"]},
    }, AP)
    _run(_held(bridge, post))
    assert _prompts(bridge) == []


def test_answers_to_another_bridges_prompt_are_never_posted(bridge) -> None:
    _listing(bridge, AP, US, NOSTR)
    post = _post(bridge)
    question = bridge.synapse.store(ROOM, "m.room.message", {
        "msgtype": "m.text", "body": "⚠️ this message is encrypted",
        "m.relates_to": {"rel_type": "m.thread", "event_id": post["event_id"]},
        PROMPT_FIELD: {"prompt_for": post["event_id"]},
    }, AP)
    _run(_event(bridge, _confirm_reply(bridge, question)))
    _run(_event(bridge, _reaction(bridge, question, "👍")))
    assert bridge.handled == [] and bridge.synapse.uploads == 0


def test_a_later_bridge_asks_when_nobody_before_it_did(bridge) -> None:
    _listing(bridge, AP, US, NOSTR)
    post = _post(bridge)
    # A prompt from someone who isn't a listed bridge doesn't count.
    bridge.synapse.store(ROOM, "m.room.message", {
        "msgtype": "m.text", "body": "x", "m.relates_to": {"rel_type": "m.thread", "event_id": post["event_id"]},
        PROMPT_FIELD: {"prompt_for": post["event_id"]},
    }, "@mallory:example.org")
    _run(_held(bridge, post))
    assert len(_prompts(bridge)) == 1


def _announce(bridge, post, copy_bytes: bytes, sender: str = AP) -> dict:
    copy_mxc = f"mxc://example.org/copy{len(bridge.synapse.media)}"
    bridge.synapse.media[copy_mxc] = copy_bytes
    return bridge.synapse.store(ROOM, ANNOUNCE_TYPE, {
        "m.relates_to": {"rel_type": ANNOUNCE_TYPE, "event_id": post["event_id"]},
        "url": copy_mxc, "encrypted_url": "mxc://example.org/encrypted", "info": {"mimetype": "image/png"},
    }, sender)


def test_another_bridges_copy_is_checked_then_used(bridge) -> None:
    _listing(bridge, AP, US)
    post = _post(bridge)
    announce = _announce(bridge, post, PLAIN)
    _run(_event(bridge, announce))
    [replayed] = bridge.handled
    content = replayed["content"]
    assert content["url"] == announce["content"]["url"] and "file" not in content
    assert "thumbnail_file" not in content["info"]
    assert bridge.synapse.uploads == 0 and _prompts(bridge) == []


def test_a_copy_that_isnt_the_senders_file_is_ignored(bridge) -> None:
    _listing(bridge, AP, US)
    post = _post(bridge)
    _run(_event(bridge, _announce(bridge, post, b"something else entirely")))
    assert bridge.handled == [] and bridge.app.state.repository.values == {}


def test_an_unlisted_announcer_is_ignored(bridge) -> None:
    _listing(bridge, AP, US)
    post = _post(bridge)
    _run(_event(bridge, _announce(bridge, post, PLAIN, sender="@mallory:example.org")))
    assert bridge.handled == []


def test_a_later_bridge_finding_an_announcement_uses_it(bridge) -> None:
    _listing(bridge, AP, US)
    post = _post(bridge)
    _announce(bridge, post, PLAIN)
    _run(_held(bridge, post))
    assert _prompts(bridge) == [] and len(bridge.handled) == 1


def _confirm_reply(bridge, prompt, sender=OWNER) -> dict:
    return bridge.synapse.store(ROOM, "m.room.message", {
        "msgtype": "m.text", "body": "> <@bluesky:example.org> ⚠️...\n\nconfirm",
        "m.relates_to": {"m.in_reply_to": {"event_id": prompt["event_id"]}},
    }, sender)


def _reaction(bridge, prompt, key, sender=OWNER) -> dict:
    return bridge.synapse.store(ROOM, "m.reaction", {
        "m.relates_to": {"rel_type": "m.annotation", "event_id": prompt["event_id"], "key": key},
    }, sender)


def _confirmed_once(bridge, post) -> None:
    assert bridge.synapse.uploads == 1
    [copy_mxc] = [m for m in bridge.synapse.media if m.startswith("mxc://example.org/upload")]
    assert bridge.synapse.media[copy_mxc] == PLAIN
    [announce] = bridge.synapse.sent(event_type=ANNOUNCE_TYPE)
    assert announce["content"]["m.relates_to"] == {"rel_type": ANNOUNCE_TYPE, "event_id": post["event_id"]}
    assert announce["content"]["url"] == copy_mxc and announce["content"]["encrypted_url"] == "mxc://example.org/encrypted"
    assert announce["content"]["info"] == {"mimetype": "image/png", "size": len(PLAIN), "w": 7, "h": 8}
    [replayed] = bridge.handled
    assert replayed["content"]["url"] == copy_mxc
    assert bridge.synapse.sent()[-1]["content"]["body"] == "✅ Sent."


def test_confirm_by_reply(bridge) -> None:
    post = _post(bridge)
    _run(_held(bridge, post))
    [prompt] = _prompts(bridge)
    _run(_event(bridge, _confirm_reply(bridge, prompt)))
    _confirmed_once(bridge, post)
    # A retried transaction doesn't decrypt or upload it again.
    bridge.handled.clear()
    _run(_event(bridge, _confirm_reply(bridge, prompt)))
    assert bridge.synapse.uploads == 1


@pytest.mark.parametrize("key", ["👍", "👍🏻", "👍🏽", "👍🏿", "👍\ufe0f"])
def test_confirm_by_reaction(bridge, key) -> None:
    post = _post(bridge)
    _run(_held(bridge, post))
    [prompt] = _prompts(bridge)
    _run(_event(bridge, _reaction(bridge, prompt, key)))
    _confirmed_once(bridge, post)


def test_other_reactions_and_other_peoples_confirmations_do_nothing(bridge) -> None:
    post = _post(bridge)
    _run(_held(bridge, post))
    [prompt] = _prompts(bridge)
    for key in ("👎", "✅", "✔️", "☑️"):
        other = _reaction(bridge, prompt, key)
        _run(_event(bridge, other))
        assert bridge.handled == [other]  # an ordinary reaction, for the usual handlers
        bridge.handled.clear()
    _run(_event(bridge, _reaction(bridge, prompt, "👍", sender="@mallory:example.org")))
    _run(_event(bridge, _confirm_reply(bridge, prompt, sender="@mallory:example.org")))
    assert bridge.synapse.uploads == 0 and bridge.handled == []


def test_reactions_only_confirm_these_prompts(bridge) -> None:
    """A Danger Zone warning (or anything else of the bot's) is left to its own handler."""
    warning = bridge.synapse.store(ROOM, "m.room.message", {"msgtype": "m.text", "body": "delete?"}, US)
    reaction = _reaction(bridge, warning, "👍")
    _run(_event(bridge, reaction))
    assert bridge.handled == [reaction] and bridge.synapse.uploads == 0


def test_a_known_copy_is_used_without_asking(bridge) -> None:
    _listing(bridge, AP, US)
    post = _post(bridge)
    _run(_held(bridge, post))  # nobody asked before it: it asks...
    [prompt] = _prompts(bridge)
    _run(_event(bridge, _reaction(bridge, prompt, "👍")))  # ...and it's confirmed
    copy_mxc = bridge.handled[-1]["content"]["url"]
    bridge.handled.clear()
    # The same file forwarded again: no prompt, no upload, and the copy is
    # announced for the new post so the other bridges needn't ask either.
    again = _post(bridge)
    _run(_event(bridge, again))
    [handled] = bridge.handled
    assert handled["content"]["url"] == copy_mxc and len(_prompts(bridge)) == 1 and bridge.synapse.uploads == 1
    announced = [e["content"]["m.relates_to"]["event_id"] for e in bridge.synapse.sent(event_type=ANNOUNCE_TYPE)]
    assert announced == [post["event_id"], again["event_id"]]
    # Seen again (a redelivery): not announced twice.
    _run(_event(bridge, again))
    assert len(bridge.synapse.sent(event_type=ANNOUNCE_TYPE)) == 2


def test_config(tmp_path) -> None:
    from bridge.config import ConfigError, load_config

    base = (
        "bridge: {domain: example.org, public_base_url: https://pds.example.org}\n"
        "synapse: {base_url: http://127.0.0.1:8008, server_name: example.org}\n"
        "appservice: {id: x, hs_token: a, as_token: b, bot_localpart: bluesky, registration_path: ./r.yaml}\n"
    )
    path = tmp_path / "config.yaml"
    path.write_text(base)
    assert load_config(path).encrypted_media == EncryptedMediaSection()
    path.write_text(base + f"encrypted_media: {{bridges: ['{AP}', '{US}'], wait_seconds: 2}}\n")
    assert load_config(path).encrypted_media == EncryptedMediaSection(bridges=(AP, US), wait_seconds=2.0)
    path.write_text(base + "encrypted_media: {bridges: [fediverse]}\n")
    with pytest.raises(ConfigError):
        load_config(path)
