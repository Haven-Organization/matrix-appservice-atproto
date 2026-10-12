"""Loader for the bridge's ``config.yaml``.

Parses the YAML file described in ``config.example.yaml`` into typed,
validated dataclasses so the rest of the codebase never touches raw dicts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = "config.yaml"
CONFIG_PATH_ENV_VAR = "BRIDGE_CONFIG"
# Two of Bluesky's public Jetstream instances, in different regions.
DEFAULT_JETSTREAM_URLS = (
    "wss://jetstream1.us-east.bsky.network/subscribe",
    "wss://jetstream2.us-west.bsky.network/subscribe",
)


class ConfigError(Exception):
    """Raised when ``config.yaml`` is missing required fields or malformed."""


@dataclass(frozen=True)
class BridgeSection:
    # The domain local users' handles live directly under: a Matrix user
    # @alice:example.org gets the ATProto handle alice.<domain>. Normally
    # the same as synapse.server_name.
    domain: str
    # Base URL of the bridge's own PDS endpoint -- the single hostname every
    # local account's DID document points at for all actual protocol
    # traffic. Unlike handles this is one host for everyone; if it sits
    # under `domain` (e.g. https://pds.example.org), its label is
    # automatically reserved so no user can claim it as a handle.
    public_base_url: str
    listen_host: str = "127.0.0.1"
    listen_port: int = 8091
    # Enables the operator endpoints under /_bridge/admin (see
    # bridge.admin_routes) and is the bearer token they require. Unset
    # (the default) means those endpoints don't exist.
    admin_token: str | None = None
    # Address Synapse should push AppService transactions to. Defaults to
    # http://{listen_host}:{listen_port} -- directly, never through the
    # public reverse proxy. Override only if Synapse can't reach that.
    internal_base_url: str | None = None
    # MXIDs treated as bridge admins: they may choose a custom backfill
    # count and replace rooms that aren't their own. A Synapse server
    # admin counts too, if synapse.admin_token is set so that can be asked.
    admins: list[str] = field(default_factory=list)
    # Name and avatar (an mxc:// URI) of the personal space the bridge
    # keeps for each user, holding their Profile Room, notifications room
    # and the rooms of accounts they follow. With no avatar set, the bot's
    # own (appservice.bot_avatar_mxc) is used.
    space_name: str = "AT Proto"
    space_avatar_mxc: str | None = None
    # Likewise for each user's notifications room.
    notifications_room_name: str = "AT Proto Notifications"
    notifications_room_avatar_mxc: str | None = None
    # -- MSC4501 and friends: same settings, names and defaults as
    # matrix-appservice-activitypub. See bridge.msc4501.
    #
    # Mirror a remote account's posts as org.matrix.msc4501.social.post
    # events instead of m.room.message. Off by default and not recommended
    # yet: a client that doesn't know the type renders nothing at all.
    use_msc4501_post_event_type: bool = False
    # Add org.matrix.msc4501.social.relates_to to mirrored replies and
    # quote-posts. Purely additive on an ordinary event, so on by default.
    set_msc4501_relates_to: bool = True
    # For a mirrored repost: assert that the event's own content already
    # is the reposted post (content_inline) rather than duplicating that
    # content a second time inside the relation. On by default. Turn off
    # if a client you care about doesn't handle content_inline.
    use_msc4501_content_inline: bool = True
    # Whether a quote-post's TARGET, when it isn't in Matrix yet, gets
    # imported so the quote can link and relate to a real event:
    # "known" (default) only if its author already has a room here,
    # "always", or "never". A quote is often adversarial, so importing
    # unconditionally means making a room for a stranger just because
    # someone followed here made fun of them. (A plain repost always
    # imports what it reposts: there is nothing else to show.)
    quote_import_policy: str = "known"
    # Give a message the bot sends on a user's behalf (the "you reposted"
    # card in their Profile Room) an MSC4144 per-message profile, so it
    # shows as the user's own action rather than the bot's. Additive, and
    # ignored by clients that don't know MSC4144; on by default.
    msc4144_per_message_profiles: bool = True
    # Create chat rooms as Matrix direct messages (``is_direct``), so
    # clients file them under "People" with the other account. Off by
    # default: a chat room is then an ordinary room. Only turn on where
    # direct messages aren't end-to-end encrypted by default -- the bridge
    # can't read an encrypted room.
    chat_rooms_as_dms: bool = False
    # Set org.matrix.msc4501.social.profile_room on every ghost's Matrix
    # profile. Needs MSC4133 (Extensible Profiles) enabled on the
    # homeserver -- in Synapse, experimental_features.msc4133_enabled --
    # so off by default.
    set_msc4501_profile_room: bool = False
    # Where a ghost's real handle (MSC4503) is recorded: "profile" (an
    # MSC4133 profile field; same homeserver requirement as above),
    # "events" (a snapshot on every event the ghost sends), "both", or
    # "off" (default).
    msc4503_external_handle: str = "off"

    def resolved_internal_base_url(self) -> str:
        return self.internal_base_url or f"http://{self.listen_host}:{self.listen_port}"


@dataclass(frozen=True)
class SynapseSection:
    # Synapse Client-Server API base URL, as reachable from the bridge process.
    base_url: str
    # The homeserver's server_name (the part after the colon in
    # @user:server.name). Only users on this homeserver get a handle.
    server_name: str
    # Optional Synapse admin access token. Only used to ask whether a
    # user is a server admin (see bridge.admins); without it, only the
    # users listed there are admins.
    admin_token: str | None = None


@dataclass(frozen=True)
class AppserviceSection:
    registration_path: str
    id: str
    hs_token: str
    as_token: str
    # Localpart prefix reserved for ghosts of remote ATProto accounts,
    # e.g. @at_<id>:server.
    user_prefix: str = "at_"
    # The bridge's own bot account: who users talk to, and who creates and
    # administers Profile Rooms.
    bot_localpart: str = "bluesky"
    # Optional display name / avatar (an mxc:// URI) for the bot's Matrix
    # profile, applied on startup. Leave unset to not manage either.
    bot_display_name: str | None = None
    bot_avatar_mxc: str | None = None
    # What a command starts with when the bot isn't tagged: "]create
    # profile". Must differ from every other bot sharing rooms with this
    # one (matrix-appservice-activitypub uses ";").
    command_prefix: str = "]"


@dataclass(frozen=True)
class PostgresSection:
    # A full connection string (postgresql://user:pass@host:port/dbname).
    # Wins over the discrete fields below if both are given.
    dsn: str | None = None
    host: str = "localhost"
    port: int = 5432
    database: str = "matrix_appservice_atproto"
    user: str = "matrix_appservice_atproto"
    password: str | None = None

    def resolved_dsn(self) -> str:
        if self.dsn:
            return self.dsn
        # libpq keyword/value form: no URL-escaping of the password needed.
        def quoted(value: object) -> str:
            escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
            return f"'{escaped}'"

        parts = {"host": self.host, "port": self.port, "dbname": self.database, "user": self.user}
        if self.password:
            parts["password"] = self.password
        return " ".join(f"{key}={quoted(value)}" for key, value in parts.items())


@dataclass(frozen=True)
class StorageSection:
    # "sqlite" (default: a single file, bridge.db, under data_dir) or
    # "postgresql" (the server described by `postgres`).
    backend: str = "sqlite"
    data_dir: str = "./data"
    postgres: PostgresSection = field(default_factory=PostgresSection)


@dataclass(frozen=True)
class HandlesSection:
    """Who may hold which ``<label>.<bridge.domain>`` -- see
    ``bridge.atproto.handles.HandlePolicy``."""

    # Subdomains of bridge.domain this site actually uses, or wants kept
    # free, on top of the built-in list
    # (bridge.atproto.reserved_handles). Exact labels or shell-style globs
    # ("ns*"). Matched ignoring hyphens.
    reserved: list[str] = field(default_factory=list)
    # Labels from the built-in list this site is fine handing out anyway.
    # Never overrides `reserved` above or the bridge's own hostnames.
    allowed: list[str] = field(default_factory=list)
    # Before granting a handle, request a challenge from that exact
    # hostname and require the bridge itself to be what answers (see
    # bridge.atproto.handle_probe). This is what catches a subdomain taken
    # by another service that nobody listed in `reserved`. On by default;
    # only turn off where the bridge can't reach its own public hostnames
    # (local development without the wildcard DNS/TLS in place).
    verify_routing: bool = True


@dataclass(frozen=True)
class AtprotoSection:
    # The did:plc directory new accounts' DIDs are registered with. The
    # real network uses exactly one; only change this to point at a test
    # directory, since DIDs registered elsewhere don't exist on Bluesky.
    plc_directory: str = "https://plc.directory"
    # Relays asked to crawl this PDS (com.atproto.sync.requestCrawl).
    # bsky.network is Bluesky's, which is what makes local accounts visible
    # in the Bluesky app.
    relays: list[str] = field(default_factory=lambda: ["https://bsky.network"])
    # Where followed accounts' new posts are received from: Jetstream
    # endpoints (Bluesky's JSON view of the network firehose, filtered
    # server-side to just the accounts followed here). All are listened to
    # at once, so one of them going quiet loses nothing.
    jetstream_urls: list[str] = field(default_factory=lambda: list(DEFAULT_JETSTREAM_URLS))
    # How many of an account's most recent posts are mirrored when someone
    # here first follows it.
    follow_backfill_count: int = 15
    # The live feed can run well behind or miss something, so each
    # followed account is also asked directly for anything newer than the
    # last post mirrored, this often (and, every twelfth time, for its
    # current profile). 0 turns the check off.
    catchup_minutes: int = 5
    # Largest remote image/video mirrored into Matrix; anything bigger is
    # linked instead. Keep at or below Synapse's max_upload_size.
    max_media_mb: int = 50
    # Where each local account's notifications (new followers, likes,
    # reposts, replies, mentions, quotes) are read from: the AppView that
    # indexes the network, asked on the account's behalf. `appview_did` is
    # that service's identity, which the request is addressed to.
    appview_url: str = "https://api.bsky.app"
    appview_did: str = "did:web:api.bsky.app"
    # How often to ask, per account. 0 turns notifications off.
    notification_poll_seconds: int = 30
    # Bluesky chat (direct messages) runs on its own service, which the
    # bridge talks to on each account's behalf the same way.
    chat_url: str = "https://api.bsky.chat"
    chat_did: str = "did:web:api.bsky.chat"
    # How often each account's chats are checked for new messages. 0 turns
    # chat off.
    chat_poll_seconds: int = 10
    # Who may start a chat with a local account: "all", "following" (only
    # accounts it follows -- Bluesky's own default) or "none".
    chat_allow_incoming: str = "following"


@dataclass(frozen=True)
class LoggingSection:
    level: str = "INFO"


@dataclass(frozen=True)
class BridgeConfig:
    bridge: BridgeSection
    synapse: SynapseSection
    appservice: AppserviceSection
    storage: StorageSection
    handles: HandlesSection
    atproto: AtprotoSection
    logging: LoggingSection


def _require(section: dict[str, Any], key: str, section_name: str) -> Any:
    if key not in section or section[key] in (None, ""):
        raise ConfigError(f"Missing required config field: {section_name}.{key}")
    return section[key]


def _label_list(section: dict[str, Any], key: str) -> list[str]:
    value = section.get(key) or []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"handles.{key} must be a list of strings")
    return value


def _jetstream_urls(section: dict[str, Any]) -> list[str]:
    """``atproto.jetstream_urls``, or the single ``jetstream_url`` older
    configs have."""
    value = section.get("jetstream_urls", section.get("jetstream_url"))
    if value is None:
        return list(DEFAULT_JETSTREAM_URLS)
    urls = [value] if isinstance(value, str) else value
    if not isinstance(urls, list) or not urls or not all(isinstance(url, str) and url for url in urls):
        raise ConfigError("atproto.jetstream_urls must be a non-empty list of URLs")
    return list(dict.fromkeys(urls))


def load_config(path: str | os.PathLike[str] | None = None) -> BridgeConfig:
    """Load and validate ``config.yaml``.

    Resolution order for the path: explicit ``path`` argument, then the
    ``BRIDGE_CONFIG`` environment variable, then ``./config.yaml``.
    """
    resolved = Path(path or os.environ.get(CONFIG_PATH_ENV_VAR, DEFAULT_CONFIG_PATH))
    if not resolved.is_file():
        raise ConfigError(
            f"Config file not found at {resolved}. Copy config.example.yaml to "
            f"{DEFAULT_CONFIG_PATH} and fill in your values."
        )

    with resolved.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    if not isinstance(raw, dict):
        raise ConfigError(f"{resolved} must contain a YAML mapping at the top level")

    try:
        bridge_raw = raw["bridge"]
        synapse_raw = raw["synapse"]
        appservice_raw = raw["appservice"]
    except KeyError as exc:
        raise ConfigError(f"Missing required top-level config section: {exc}") from exc

    msc4503_external_handle = bridge_raw.get("msc4503_external_handle", "off")
    if msc4503_external_handle not in ("off", "profile", "events", "both"):
        raise ConfigError(
            "bridge.msc4503_external_handle must be one of 'off', 'profile', 'events', 'both', "
            f"got {msc4503_external_handle!r}"
        )
    quote_import_policy = bridge_raw.get("quote_import_policy", "known")
    if quote_import_policy not in ("always", "known", "never"):
        raise ConfigError(
            f"bridge.quote_import_policy must be 'always', 'known', or 'never', got {quote_import_policy!r}"
        )
    bridge_section = BridgeSection(
        domain=_require(bridge_raw, "domain", "bridge").lower().rstrip("."),
        public_base_url=_require(bridge_raw, "public_base_url", "bridge").rstrip("/"),
        listen_host=bridge_raw.get("listen_host", "127.0.0.1"),
        listen_port=int(bridge_raw.get("listen_port", 8091)),
        admin_token=bridge_raw.get("admin_token") or None,
        internal_base_url=(bridge_raw.get("internal_base_url") or "").rstrip("/") or None,
        admins=[str(mxid) for mxid in bridge_raw.get("admins") or []],
        space_name=str(bridge_raw.get("space_name") or "AT Proto"),
        space_avatar_mxc=bridge_raw.get("space_avatar_mxc") or None,
        notifications_room_name=str(bridge_raw.get("notifications_room_name") or "AT Proto Notifications"),
        notifications_room_avatar_mxc=bridge_raw.get("notifications_room_avatar_mxc") or None,
        use_msc4501_post_event_type=bool(bridge_raw.get("use_msc4501_post_event_type", False)),
        set_msc4501_relates_to=bool(bridge_raw.get("set_msc4501_relates_to", True)),
        use_msc4501_content_inline=bool(bridge_raw.get("use_msc4501_content_inline", True)),
        quote_import_policy=quote_import_policy,
        msc4144_per_message_profiles=bool(bridge_raw.get("msc4144_per_message_profiles", True)),
        chat_rooms_as_dms=bool(bridge_raw.get("chat_rooms_as_dms", False)),
        set_msc4501_profile_room=bool(bridge_raw.get("set_msc4501_profile_room", False)),
        msc4503_external_handle=msc4503_external_handle,
    )

    synapse_section = SynapseSection(
        base_url=_require(synapse_raw, "base_url", "synapse").rstrip("/"),
        server_name=_require(synapse_raw, "server_name", "synapse"),
        admin_token=synapse_raw.get("admin_token") or None,
    )

    command_prefix = str(appservice_raw.get("command_prefix", "]")).strip()
    if not command_prefix:
        raise ConfigError("appservice.command_prefix must not be empty")
    appservice_section = AppserviceSection(
        registration_path=_require(appservice_raw, "registration_path", "appservice"),
        id=_require(appservice_raw, "id", "appservice"),
        hs_token=_require(appservice_raw, "hs_token", "appservice"),
        as_token=_require(appservice_raw, "as_token", "appservice"),
        user_prefix=appservice_raw.get("user_prefix", "at_"),
        bot_localpart=appservice_raw.get("bot_localpart", "bluesky"),
        bot_display_name=appservice_raw.get("bot_display_name"),
        bot_avatar_mxc=appservice_raw.get("bot_avatar_mxc"),
        command_prefix=command_prefix,
    )

    storage_raw = raw.get("storage", {}) or {}
    backend = str(storage_raw.get("backend", "sqlite"))
    if backend not in ("sqlite", "postgresql"):
        raise ConfigError(f"storage.backend must be 'sqlite' or 'postgresql', got {backend!r}")
    postgres_raw = storage_raw.get("postgres", {}) or {}
    postgres_section = PostgresSection(
        dsn=postgres_raw.get("dsn") or None,
        host=str(postgres_raw.get("host", "localhost")),
        port=int(postgres_raw.get("port", 5432)),
        database=str(postgres_raw.get("database", "matrix_appservice_atproto")),
        user=str(postgres_raw.get("user", "matrix_appservice_atproto")),
        password=str(postgres_raw["password"]) if postgres_raw.get("password") not in (None, "") else None,
    )
    storage_section = StorageSection(
        backend=backend, data_dir=storage_raw.get("data_dir", "./data"), postgres=postgres_section
    )

    handles_raw = raw.get("handles", {}) or {}
    handles_section = HandlesSection(
        reserved=_label_list(handles_raw, "reserved"),
        allowed=_label_list(handles_raw, "allowed"),
        verify_routing=bool(handles_raw.get("verify_routing", True)),
    )

    atproto_raw = raw.get("atproto", {}) or {}
    chat_allow_incoming = str(atproto_raw.get("chat_allow_incoming", "following"))
    if chat_allow_incoming not in ("all", "following", "none"):
        raise ConfigError(
            f"atproto.chat_allow_incoming must be 'all', 'following' or 'none', got {chat_allow_incoming!r}"
        )
    atproto_section = AtprotoSection(
        plc_directory=atproto_raw.get("plc_directory", "https://plc.directory").rstrip("/"),
        relays=[str(relay).rstrip("/") for relay in atproto_raw.get("relays", ["https://bsky.network"]) or []],
        jetstream_urls=_jetstream_urls(atproto_raw),
        follow_backfill_count=int(atproto_raw.get("follow_backfill_count", 15)),
        catchup_minutes=int(atproto_raw.get("catchup_minutes", 5)),
        max_media_mb=int(atproto_raw.get("max_media_mb", 50)),
        appview_url=str(atproto_raw.get("appview_url", "https://api.bsky.app")).rstrip("/"),
        appview_did=str(atproto_raw.get("appview_did", "did:web:api.bsky.app")),
        notification_poll_seconds=int(atproto_raw.get("notification_poll_seconds", 30)),
        chat_url=str(atproto_raw.get("chat_url", "https://api.bsky.chat")).rstrip("/"),
        chat_did=str(atproto_raw.get("chat_did", "did:web:api.bsky.chat")),
        chat_poll_seconds=int(atproto_raw.get("chat_poll_seconds", 10)),
        chat_allow_incoming=chat_allow_incoming,
    )

    logging_raw = raw.get("logging", {}) or {}
    logging_section = LoggingSection(level=logging_raw.get("level", "INFO"))

    return BridgeConfig(
        bridge=bridge_section,
        synapse=synapse_section,
        appservice=appservice_section,
        storage=storage_section,
        handles=handles_section,
        atproto=atproto_section,
        logging=logging_section,
    )
