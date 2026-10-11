# Configuration reference

`config.example.yaml` holds only the settings you need to get going. Everything else has a default and is listed here. To change one, add it to your `config.yaml` under the section named in its heading, for example:

```yaml
bridge:
  chat_rooms_as_dms: true

atproto:
  follow_backfill_count: 30
```

The bridge reads `config.yaml` once, at startup, so restart it after a change. The path is the first argument to `main.py`, else the `BRIDGE_CONFIG` environment variable, else `./config.yaml`.

## Contents

- [In the sample config](#in-the-sample-config)
- [`bridge`](#bridge)
  - [Access and networking](#access-and-networking)
  - [Chat rooms](#chat-rooms)
  - [MSC4501 and related proposals](#msc4501-and-related-proposals)
- [`synapse`](#synapse)
- [`storage`](#storage)
  - [Moving from SQLite to Postgres](#moving-from-sqlite-to-postgres)
- [`handles`](#handles)
- [`atproto`](#atproto)
  - [Network endpoints](#network-endpoints)
  - [Mirroring](#mirroring)
  - [Notifications](#notifications)
  - [Chat](#chat)

---

## In the sample config

These are documented inline in `config.example.yaml` and only summarised here.

| Setting | Default | What it is |
|---|---|---|
| `bridge.domain` | required | Domain handles live under (`alice.<domain>`). |
| `bridge.public_base_url` | required | The PDS's own address. Written into every account's public identity record, so choose it before creating accounts. |
| `bridge.listen_host` / `listen_port` | `127.0.0.1` / `8091` | Where the bridge's HTTP server binds. |
| `bridge.space_name` / `space_avatar_mxc` | `AT Proto` / the bot's avatar | Each user's personal space. |
| `bridge.notifications_room_name` / `notifications_room_avatar_mxc` | `AT Proto Notifications` / the bot's avatar | Each user's notifications room. |
| `synapse.base_url` | required | Homeserver Client-Server API, as reachable from the bridge. |
| `synapse.server_name` | required | The homeserver's `server_name`. Only its users get a handle. |
| `appservice.registration_path` | required | Where the registration file is written. |
| `appservice.id`, `hs_token`, `as_token` | required | AppService identity and its two secrets. |
| `appservice.bot_localpart` | `bluesky` | The bot's username. |
| `appservice.bot_display_name` / `bot_avatar_mxc` | unset (not managed) | The bot's profile, applied at startup. |
| `appservice.user_prefix` | `at_` | Username prefix of ghost users. Changing it later orphans existing ghosts and needs the registration regenerated. |
| `appservice.command_prefix` | `]` | What a command starts with when the bot isn't tagged. |
| `storage.backend` | `sqlite` | `sqlite` or `postgresql`. See [`storage`](#storage). |
| `storage.data_dir` | `./data` | Where `bridge.db` is kept (SQLite). |
| `handles.reserved` | `[]` | Subdomains nobody may claim as a handle. |
| `logging.level` | `INFO` | Log level. |

Changing `space_name`, `notifications_room_name` or their avatars affects rooms created afterwards; existing ones keep what they have.

---

## `bridge`

### Access and networking

#### `bridge.internal_base_url`

**Default:** `http://<listen_host>:<listen_port>`

The address your homeserver pushes AppService transactions to. It is written into the registration file, so regenerate that after changing it. Set it only when the homeserver can't reach the bridge at the default, which is always the case in Docker (for example `http://bridge:8091` on a shared Docker network).

#### `bridge.admins`

**Default:** `[]`

Matrix user IDs treated as bridge admins. An admin may choose a custom `]backfill` count and replace rooms that aren't their own.

```yaml
bridge:
  admins:
    - "@you:example.org"
```

Synapse server admins count as well if [`synapse.admin_token`](#synapseadmin_token) is set.

#### `bridge.admin_token`

**Default:** unset (the endpoints don't exist)

Enables the operator HTTP endpoints under `/_bridge/admin` (create an account, write test records, request a relay crawl) and is the bearer token they require. Never expose `/_bridge/` publicly; `deploy/nginx.conf.example` doesn't.

### Chat rooms

#### `bridge.chat_rooms_as_dms`

**Default:** `false`

Whether the room for a Bluesky chat is created as a Matrix direct message. Off, it is an ordinary room, listed with your other rooms. On, the invite is marked as a direct message, so clients file the room under "People" with the other account.

Only turn it on if direct messages on your homeserver are **not** end-to-end encrypted by default. The bridge can't read an encrypted room, and a chat in one is not relayed. In Synapse that means `encryption_enabled_by_default_for_room_type` is `off` (its default); clients may also have their own setting for new DMs, though that only applies to rooms a user creates themselves.

Affects rooms created afterwards. An existing chat room can be recreated with `]replace chat`.

### MSC4501 and related proposals

Same settings, names and defaults as matrix-appservice-activitypub. What each one looks like in a room is described in [FEATURES.md](FEATURES.md#msc4501).

Profile Rooms and Remote User Rooms always get the `org.matrix.msc4501.social.profile` room type and an `org.matrix.msc4501.social.profile_user_id` state event; that part isn't configurable.

#### `bridge.use_msc4501_post_event_type`

**Default:** `false`

Mirror remote accounts' posts as `org.matrix.msc4501.social.post` events instead of `m.room.message`. Not recommended yet: a client that doesn't know the event type shows nothing at all.

#### `bridge.set_msc4501_relates_to`

**Default:** `true`

Add `org.matrix.msc4501.social.relates_to` to mirrored replies, reposts and quote-posts. Purely additive on an ordinary message.

#### `bridge.use_msc4501_content_inline`

**Default:** `true`

A mirrored repost says its own content *is* the reposted post (`content_inline`) instead of repeating that content a second time inside the relation. Turn off if a client you care about doesn't handle `content_inline`.

#### `bridge.quote_import_policy`

**Default:** `known`. One of `known`, `always`, `never`.

When a quote-post quotes something that isn't in Matrix yet, whether to import it so the quote can link to a real event:

- `known`: only if the quoted post's author already has a room here.
- `always`: always, creating a room for the author if needed.
- `never`: never; the quote links to bsky.app instead.

A quote is often adversarial, so `always` means making a room for a stranger just because someone followed here quoted them. A plain repost always imports what it reposts, since there is nothing else to show.

#### `bridge.msc4144_per_message_profiles`

**Default:** `true`

The "you reposted" card the bot leaves in a user's Profile Room carries an MSC4144 per-message profile, so it shows as the user's own action. Ignored by clients that don't know MSC4144.

#### `bridge.set_msc4501_profile_room`

**Default:** `false`

Point each ghost's Matrix profile at its room (`org.matrix.msc4501.social.profile_room`). Needs MSC4133 (Extensible Profiles) on the homeserver; in Synapse, `experimental_features.msc4133_enabled: true`.

#### `bridge.msc4503_external_handle`

**Default:** `off`. One of `off`, `profile`, `events`, `both`.

Where each ghost's real Bluesky handle is recorded (MSC4503): as a field on its Matrix profile (`profile`, same homeserver requirement as above), as a snapshot on every event it sends (`events`), or `both`.

---

## `synapse`

#### `synapse.admin_token`

**Default:** unset

A Synapse admin access token. It is used for one thing: asking whether a user is a server admin, so that server admins count as bridge admins. Without it, only the users in [`bridge.admins`](#bridgeadmins) are. Leave it unset on a homeserver other than Synapse.

---

## `storage`

The bridge's bookkeeping (accounts and their signing keys, handle claims, follows, the Matrix-event/record map, the PDS's event log) is kept in one of two places. Same data and behaviour either way; no post content or media is stored in either.

#### `storage.backend`

**Default:** `sqlite`. One of `sqlite`, `postgresql`.

- `sqlite`: a single file, `bridge.db`, under `storage.data_dir`. Nothing to set up.
- `postgresql`: the Postgres server described by `storage.postgres`. Useful if you already run Postgres for something else, such as your homeserver, and want one database system to back up.

Switching backends does not move your data by itself; see [Moving from SQLite to Postgres](#moving-from-sqlite-to-postgres).

#### `storage.postgres`

Only read when the backend is `postgresql`. Either give a full connection string, or the separate fields; `dsn` wins if both are present.

```yaml
storage:
  backend: postgresql
  postgres:
    # dsn: "postgresql://matrix_appservice_atproto:replace_me@localhost:5432/matrix_appservice_atproto"
    host: localhost
    port: 5432
    database: matrix_appservice_atproto
    user: matrix_appservice_atproto
    password: "replace_me"
```

| Setting | Default |
|---|---|
| `storage.postgres.dsn` | unset |
| `storage.postgres.host` | `localhost` |
| `storage.postgres.port` | `5432` |
| `storage.postgres.database` | `matrix_appservice_atproto` |
| `storage.postgres.user` | `matrix_appservice_atproto` |
| `storage.postgres.password` | unset |

Create the role and an empty database first; the bridge creates its tables on startup:

```sh
sudo -u postgres createuser --pwprompt matrix_appservice_atproto
sudo -u postgres createdb --owner matrix_appservice_atproto matrix_appservice_atproto
```

The database's locale doesn't matter: the bridge's text columns use byte-order ("C") collation regardless. It needs the `psycopg2` Python package, which `requirements.txt` and the Docker image include (on a distro-packaged install, `python-psycopg2` or `python3-psycopg2`). The bridge uses a single connection, waits up to a minute for the database at startup, and reconnects if the connection is lost later.

### Moving from SQLite to Postgres

`bridge.db` holds every account's signing keys, so an existing install must be migrated, not started empty. With the bridge **stopped**:

1. Create the role and database as above, and add the `storage.postgres` settings to `config.yaml` (leave `backend` as it is for now).
2. Copy the data:

   ```sh
   .venv/bin/python -m bridge.storage migrate config.yaml
   ```

   In Docker: `docker compose run --rm --entrypoint python bridge -m bridge.storage migrate /config/config.yaml`.

   It copies every table in one transaction, checks the row counts, and carries the firehose sequence number over so relays resume where they left off. It refuses to write into a database that already has data, and it only reads `bridge.db`.
3. Set `storage.backend: postgresql` and start the bridge.

Keep `bridge.db` as a backup. There is no tool for the reverse direction.

---

## `handles`

A handle is a real hostname on your domain, so three layers decide which names may be handed out: a built-in list of names any site is likely to care about (`bridge/atproto/reserved_handles.py`), your own `handles.reserved`, and a live check that the hostname reaches this bridge.

#### `handles.allowed`

**Default:** `[]`

Names from the built-in list you're fine handing out anyway.

```yaml
handles:
  allowed:
    - talk
```

It never overrides `handles.reserved` or the bridge's own hostnames.

#### `handles.verify_routing`

**Default:** `true`

Before granting a handle, the bridge requests a challenge from that exact hostname over the public internet and requires that it is itself what answers. This is what catches a subdomain taken by another service that nobody listed in `reserved`. Leave it on in production. Turn it off only where the bridge can't reach its own public hostnames, such as local development without the wildcard DNS and certificate in place.

---

## `atproto`

### Network endpoints

These point at the public ATProto network. Change them only to use a different relay or AppView, or for testing.

#### `atproto.plc_directory`

**Default:** `https://plc.directory`

The did:plc directory new accounts are registered with. The real network uses exactly one; a DID registered anywhere else doesn't exist on Bluesky.

#### `atproto.relays`

**Default:** `["https://bsky.network"]`

Relays asked to crawl this PDS. `bsky.network` is Bluesky's, which is what makes local accounts visible in the Bluesky app. An empty list means posts never leave this server.

#### `atproto.jetstream_url`

**Default:** `wss://jetstream2.us-east.bsky.network/subscribe`

Where followed accounts' new posts are received from: a Jetstream endpoint, filtered to just the accounts followed here.

#### `atproto.appview_url` / `atproto.appview_did`

**Default:** `https://api.bsky.app` / `did:web:api.bsky.app`

The AppView that each account's notifications are read from, and that service's identity.

#### `atproto.chat_url` / `atproto.chat_did`

**Default:** `https://api.bsky.chat` / `did:web:api.bsky.chat`

Bluesky's chat service and its identity.

### Mirroring

#### `atproto.follow_backfill_count`

**Default:** `15`

How many of an account's most recent posts are mirrored when someone here first follows it. Also the count `]backfill` uses when none is given.

#### `atproto.catchup_minutes`

**Default:** `5`

The live feed can lag or miss something, so every this many minutes each followed account is asked directly for anything newer than the last post mirrored (and, every twelfth time, for its current profile). `0` turns the check off.

#### `atproto.max_media_mb`

**Default:** `50`

The largest remote image or video copied into Matrix, in megabytes. Anything bigger is linked instead. Keep it at or below your homeserver's upload limit (`max_upload_size` in Synapse).

### Notifications

#### `atproto.notification_poll_seconds`

**Default:** `30`

How often each account's notifications (new followers, likes, reposts, replies, mentions, quotes) are checked. `0` turns notifications off.

### Chat

#### `atproto.chat_poll_seconds`

**Default:** `10`

How often each account's chats are checked for new messages. `0` turns chat off entirely.

#### `atproto.chat_allow_incoming`

**Default:** `following`. One of `all`, `following`, `none`.

Who may start a chat with a local account: anyone, only accounts it follows (Bluesky's own default), or nobody. Published as each account's chat declaration.
