# matrix-appservice-atproto

Turns your Matrix server into an AT Protocol (Bluesky) server. Every Matrix user can have a real ATProto account, hosted by the bridge itself, and post, follow, reply, like, repost and chat across Bluesky using ordinary Matrix rooms and clients. No separate account or server needed.

Runs natively as a single Python process, no containers required (a `Dockerfile` is provided if you prefer one). It talks to your homeserver through the Client-Server API (plus the Application Service push API for inbound events), never by touching its database directly. The bridge is its own PDS (Personal Data Server), the piece of ATProto that hosts accounts, but it stores no post content or media of its own: that all lives in Matrix rooms, and each account's repository is regenerated from them on demand. The only local state is bookkeeping (accounts and their signing keys, follow relationships, and the Matrix-event/ATProto-record map), kept in a SQLite file.

It is designed as a sibling of [matrix-appservice-activitypub](https://github.com/Haven-Organization/matrix-appservice-activitypub): the same room model, command structure and MSC4501 behaviour, and the two can run side by side on one homeserver.

## Core concepts

Every ATProto identity or conversation the bridge manages is backed by an ordinary Matrix room, of one of these kinds:

- **Profile Room**: a local Matrix user's own ATProto account. `@alice:example.org` gets the handle `alice.example.org`. Posting here publishes to Bluesky, and the room's name, topic and avatar are the account's display name, bio and avatar.
- **Remote User Room**: one shared room per remote Bluesky account, mirroring everything it posts. Created the first time anyone follows or imports from that account, and reused by every local follower after that.
- **Chat room**: a private 1:1 room between a local user and a remote account, carrying Bluesky chat (direct messages).
- **Notifications room**: a private room between a local user and the bridge bot. New followers, likes, reposts, replies, mentions and quotes land here.
- **Space**: each user gets a personal space holding their Profile Room, their notifications room and the room of every account they follow.

Every remote account you interact with gets a "ghost" Matrix user (`@at_<account id>:example.org`) that posts, reacts and chats on its behalf inside Matrix. Its display name, avatar and banner stay in sync with the real profile.

## What's bridged

- Posts, replies, and threads, including posts too long for Bluesky (split into a thread)
- Likes and reposts, as 👍 and 🔁 reactions, and quote-posts
- Chat (direct messages), with replies and reactions
- Mentions, links and hashtags
- Media: images, video, and other files (as link cards)
- Follows, blocks, and backfill of older posts
- Profile and identity: display name, bio, avatar, banner
- Notifications

See [FEATURES.md](FEATURES.md) for how each of these is actually bridged.

## Controlling the bridge

Most of what's above just happens through ordinary Matrix actions: sending a message, reacting, replying, redacting. For everything else, like creating your account, following someone or starting a chat, tag the bridge bot or type a `]`-prefixed command (`]create profile`, `]follow alice.bsky.social`, `]help`, and so on). See [COMMANDS.md](COMMANDS.md) for the complete reference.

## What homeservers are supported?

Synapse is the only homeserver this bridge has been tested against. It should work against any other spec-compliant homeserver that supports Application Services. The one Synapse-specific piece is optional: with `synapse.admin_token` set, Synapse server admins count as bridge admins; without it, list admins in `bridge.admins`.

## Setup

See [INSTALL.md](INSTALL.md) for the full walkthrough, natively or in Docker. In short: ATProto handles are hostnames, so each user's handle is its own subdomain (`alice.example.org`). That means a wildcard DNS record, a wildcard TLS certificate and two nginx server blocks (`deploy/nginx.conf.example`) come first, then `./scripts/setup.sh` (or the `Dockerfile`), `config.yaml` (from `config.example.yaml`), and registering the AppService with your homeserver.

`config.example.yaml` only has the settings you need to start. Every other setting is documented in [CONFIG.md](CONFIG.md).

## Storage

Bookkeeping (accounts and their keys, handle claims, follow relationships, ghost profiles, the Matrix-event/ATProto-record map, and the PDS's event log) lives in a local SQLite file, `bridge.db` under `storage.data_dir`. No post content or media is stored outside Matrix itself.

## License

See [LICENSE](LICENSE).
