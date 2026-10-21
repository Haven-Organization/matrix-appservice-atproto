"""Repository abstraction over the bridge's own bookkeeping.

``BridgeRepository`` is the seam between the bridge and its storage, the
same way ``ActorRepository`` is in the ActivityPub bridge.
``bridge.sqlite_repository.SqliteBridgeRepository`` is the durable default
(and, pointed at ``:memory:``, what tests use).

What is deliberately NOT here: record content. A post's text lives in its
Matrix event and nowhere else. For each ATProto record the bridge keeps
only its CID and a ``source`` -- a small recipe for rebuilding the exact
record bytes from Matrix when someone asks for them (see
``bridge.record_sources``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class HandleClaim:
    """A Matrix user's hold on ``<label>.<bridge.domain>``.

    One label per user and one user per label. ``did`` is unset between the
    name being claimed and the ATProto identity behind it being created;
    ``/.well-known/atproto-did`` only answers once it's set.
    """

    label: str
    matrix_user_id: str
    did: str | None
    claimed_at: str


@dataclass(frozen=True)
class Account:
    """A local Matrix user's ATProto account.

    ``signing_key_hex`` is the repository signing key, held by the bridge
    because the bridge is what writes the repository. ``genesis_operation``
    is the signed did:plc operation the DID was derived from, kept so that
    registering it with the directory can be retried verbatim.

    ``rev``/``commit_cid``/``commit_block``/``data_cid`` are the
    repository's current head: its latest signed commit and the root of
    the record tree that commit covers.
    """

    did: str
    matrix_user_id: str
    label: str
    signing_key_hex: str
    genesis_operation: str
    plc_registered: bool
    active: bool
    rev: str
    commit_cid: str
    commit_block: bytes
    data_cid: str
    created_at: str
    # The Matrix room whose messages are this account's posts and whose
    # name/topic are its display name/bio. Unset until `create profile`.
    profile_room_id: str | None = None


@dataclass(frozen=True)
class RecordRow:
    """One record in an account's repository: where it is, what it hashes
    to, and how to rebuild it (``source``, JSON -- see
    ``bridge.record_sources``)."""

    did: str
    collection: str
    rkey: str
    cid: str
    source: str
    # The Matrix event this record was made from, if any -- what a
    # redaction of that event is matched against.
    matrix_event_id: str | None = None

    @property
    def path(self) -> str:
        return f"{self.collection}/{self.rkey}"


@dataclass(frozen=True)
class BlobRow:
    """A blob (image, video) one of an account's records refers to. The
    bytes live in the Matrix media repository at ``mxc``; this row is how
    ``com.atproto.sync.getBlob`` finds them again from the CID."""

    did: str
    cid: str
    mxc: str
    mime_type: str
    size: int
    matrix_event_id: str | None = None


@dataclass(frozen=True)
class PublishedMedia:
    """A Matrix media item the bridge serves publicly at ``/media/...`` --
    how a file with no native ATProto form (a PDF, audio) is linked from a
    post. Matrix media needs an access token to download, so nothing is
    reachable this way unless it is listed here. Its name and type are not
    kept: they come from the media repository when it is served."""

    mxc: str
    matrix_event_id: str | None = None


@dataclass(frozen=True)
class RemoteAccount:
    """An ATProto account hosted elsewhere that someone here follows: its
    ghost Matrix user and the Remote User Room its posts are mirrored
    into. One room per remote account, shared by every local follower."""

    did: str
    handle: str
    pds: str
    ghost_user_id: str
    room_id: str


@dataclass(frozen=True)
class MirroredPost:
    """Which Matrix events a remote post was mirrored as (one for its
    text, one per attachment), so deleting the post can redact them and a
    post seen twice isn't mirrored twice."""

    uri: str
    room_id: str
    event_ids: list[str]
    # The post's record hash: with the URI, what a like or repost of it
    # must reference.
    cid: str = ""
    # The root of the conversation the post belongs to, if it is a reply
    # (what a further reply to it must name as its own root).
    root_uri: str = ""
    root_cid: str = ""
    # The post these events SHOW, when that isn't the record they were
    # mirrored for: a repost's events show the reposted post, and a like
    # of or reply to them must act on that one.
    shown_uri: str = ""
    shown_cid: str = ""


@dataclass(frozen=True)
class ChatRoom:
    """The Matrix room standing for one Bluesky chat: between a local
    account and a remote one (``convo_id`` is the chat service's own name
    for that conversation)."""

    room_id: str
    local_did: str
    remote_did: str
    convo_id: str


@dataclass(frozen=True)
class FirehoseEventMeta:
    seq: int
    did: str
    kind: str


class BridgeRepository(Protocol):
    # -- handles ------------------------------------------------------------
    async def get_handle_claim(self, label: str) -> HandleClaim | None: ...

    async def get_handle_claim_for_user(self, matrix_user_id: str) -> HandleClaim | None: ...

    async def list_handle_claims(self) -> list[HandleClaim]: ...

    async def claim_handle(self, label: str, matrix_user_id: str) -> HandleClaim | None:
        """Atomically claim ``label`` for ``matrix_user_id``. Returns
        ``None`` if the label is held by someone else or the user already
        holds a different one -- never overwrites either."""
        ...

    async def set_handle_did(self, label: str, did: str) -> None: ...

    async def release_handle(self, label: str) -> None: ...

    # -- server keys --------------------------------------------------------
    async def get_or_create_server_key(self, name: str, candidate_hex: str) -> str:
        """The stored private key called ``name``; stores and returns
        ``candidate_hex`` if there is none yet."""
        ...

    # -- accounts -----------------------------------------------------------
    async def create_account(self, account: Account) -> None: ...

    async def get_account(self, did: str) -> Account | None: ...

    async def get_account_for_user(self, matrix_user_id: str) -> Account | None: ...

    async def get_account_for_label(self, label: str) -> Account | None: ...

    async def list_accounts(self) -> list[Account]: ...

    async def mark_plc_registered(self, did: str) -> None: ...

    async def set_profile_room(self, did: str, room_id: str) -> None: ...

    async def get_account_for_profile_room(self, room_id: str) -> Account | None: ...

    async def clear_profile_room(self, did: str) -> None: ...

    async def delete_account(self, did: str) -> None:
        """Erase an account and everything kept about it: its records,
        blobs, follows, chats, handle claim, and its user's space and
        notifications-room bookkeeping."""
        ...

    # -- records ------------------------------------------------------------
    async def list_records(self, did: str) -> list[RecordRow]: ...

    async def get_record(self, did: str, collection: str, rkey: str) -> RecordRow | None: ...

    async def get_records_for_event(self, matrix_event_id: str) -> list[RecordRow]:
        """Every record made from one Matrix event, in record-key order (a
        long message becomes several posts)."""
        ...

    # -- media --------------------------------------------------------------
    async def add_blob(self, blob: BlobRow) -> None: ...

    async def get_blob(self, did: str, cid: str) -> BlobRow | None: ...

    async def list_blobs(self, did: str) -> list[BlobRow]: ...

    async def publish_media(self, media: PublishedMedia) -> None: ...

    async def get_published_media(self, mxc: str) -> PublishedMedia | None: ...

    async def forget_media_for_event(self, matrix_event_id: str) -> None:
        """Drop the blobs and published media that came from this event."""
        ...

    async def commit_repo(
        self,
        did: str,
        *,
        rev: str,
        commit_cid: str,
        commit_block: bytes,
        data_cid: str,
        upserts: list[RecordRow],
        deletes: list[tuple[str, str]],
    ) -> None:
        """Move the account's head and apply the record changes it covers,
        all or nothing."""
        ...

    # -- firehose -----------------------------------------------------------
    async def next_firehose_seq(self, did: str, kind: str) -> int:
        """Record that an event happened and return its sequence number.
        Only this metadata is kept; event payloads are not."""
        ...

    async def current_firehose_seq(self) -> int: ...

    async def firehose_events_between(self, after: int, upto: int) -> list[FirehoseEventMeta]:
        """Events with ``after < seq <= upto``, oldest first."""
        ...

    # -- remote accounts ----------------------------------------------------
    async def save_remote_account(self, account: RemoteAccount) -> None: ...

    async def get_remote_account(self, did: str) -> RemoteAccount | None: ...

    async def get_remote_account_for_room(self, room_id: str) -> RemoteAccount | None: ...

    async def list_remote_accounts(self) -> list[RemoteAccount]:
        """Every remote account with a room here, followed or not."""
        ...

    async def add_follow(self, local_did: str, remote_did: str, rkey: str) -> None: ...

    async def get_follow_rkey(self, local_did: str, remote_did: str) -> str | None: ...

    async def remove_follow(self, local_did: str, remote_did: str) -> None: ...

    async def list_follows(self, local_did: str) -> list[RemoteAccount]: ...

    async def list_followed_accounts(self) -> list[RemoteAccount]:
        """Remote accounts at least one local account follows."""
        ...

    async def add_mirrored_post(self, post: MirroredPost) -> bool:
        """Record a mirrored post. False if it was already recorded."""
        ...

    async def get_mirrored_post(self, uri: str) -> MirroredPost | None: ...

    async def get_shown_post_for_event(self, event_id: str) -> tuple[str, str] | None:
        """``(uri, cid)`` of the post a mirrored Matrix event shows."""
        ...
    async def get_mirrored_post_for_event(self, event_id: str) -> MirroredPost | None:
        """The mirrored post a Matrix event is part of."""
        ...

    async def delete_mirrored_post(self, uri: str) -> None: ...

    async def get_value(self, key: str) -> str | None:
        """Small pieces of bridge state, e.g. the ingestion cursor."""
        ...

    async def set_value(self, key: str, value: str) -> None: ...

    async def delete_value(self, key: str) -> None: ...

    # -- per-user rooms -----------------------------------------------------
    async def get_user_space(self, matrix_user_id: str) -> str | None: ...

    async def set_user_space(self, matrix_user_id: str, room_id: str) -> None: ...

    async def get_notifications_room(self, matrix_user_id: str) -> str | None: ...

    async def set_notifications_room(self, matrix_user_id: str, room_id: str) -> None: ...

    async def get_notifications_room_owner(self, room_id: str) -> str | None: ...

    async def get_space_owner(self, room_id: str) -> str | None: ...

    # -- chats --------------------------------------------------------------
    async def save_chat_room(self, chat: ChatRoom) -> None:
        """Record (or re-point) the room for a local/remote pair's chat."""
        ...

    async def get_chat_room(self, local_did: str, remote_did: str) -> ChatRoom | None: ...

    async def get_chat_room_for_convo(self, local_did: str, convo_id: str) -> ChatRoom | None: ...

    async def get_chat_room_for_room(self, room_id: str) -> ChatRoom | None: ...

    async def list_chat_rooms(self, local_did: str) -> list[ChatRoom]: ...

    async def add_chat_message(self, message_id: str, room_id: str, event_id: str) -> bool:
        """Record that a chat message and a Matrix event are the same
        message. False if the message was already recorded."""
        ...

    async def get_chat_message_for_event(self, event_id: str) -> str | None: ...

    async def get_event_for_chat_message(self, message_id: str) -> tuple[str, str] | None:
        """``(room_id, event_id)`` of a chat message."""
        ...

    # -- Matrix AppService --------------------------------------------------
    async def has_processed_event(self, event_id: str) -> bool:
        """Synapse sends a transaction again when it got no 200 for it; an
        event already handled must not be handled twice."""
        ...

    async def mark_event_processed(self, event_id: str) -> None:
        """Also forgets events handled more than a week ago."""
        ...

    async def close(self) -> None: ...
