# Bot commands

The bridge is controlled from inside Matrix by either tagging/mentioning the bot followed by the command (e.g. `@bluesky:example.org create profile`), or starting a message with `]<command>` (e.g. `]follow alice.bsky.social`). A few general rules apply to all of them:

- A command is only recognized on the **first line** of a message. If the message is a reply, the quoted fallback some clients prepend is skipped first.
- Keyword matching is case-insensitive.
- The `]` prefix only counts when a real command word follows it immediately. A message that merely starts with `]` is an ordinary message (and, in your Profile Room, an ordinary post).
- Every command **except `]help`** only works for users on the bridge's own homeserver. Anyone on any server can invite the bot to a room, and this stops them minting identities under your domain.
- The bridge's own bot and ghost accounts never trigger commands, so a mirrored post that happens to start with `]` is never misread as one.
- Tagging the bot with no recognized command shows the `]help` section overview (if nothing else was said) or a short pointer to `]help` (if something else was said that wasn't recognized).
- A message addressed to the bot is never published as a post, even if the command in it was mistyped.
- The prefix is configurable (`appservice.command_prefix`). `]` is the default, chosen not to clash with matrix-appservice-activitypub's `;`, so both bots can sit in the same room.

Everything that happens without a command (posting, liking, replying, what shows up in which room) is described in [FEATURES.md](FEATURES.md).

## Contents

- [Profile](#profile)
  - [`]create profile`](#create-profile)
  - [`]status`](#status)
  - [`]banner`](#banner-mxcservermediaid)
- [General](#general)
  - [`]help` / `]help <section>`](#help--help-section)
  - [`]follow`](#follow-handleexamplecom)
  - [`]unfollow`](#unfollow-handleexamplecom)
  - [`]following`](#following)
  - [`]import <url>`](#import-url)
  - [`]repost`](#repost-reply-to-a-post)
  - [`]chat` / `]dm`](#chat-handleexamplecom--dm-handleexamplecom)
- [Advanced/Maintenance](#advancedmaintenance)
  - [`]block`](#block-handleexamplecom)
  - [`]unblock`](#unblock-handleexamplecom)
  - [`]backfill`](#backfill-n)
  - [`]refresh`](#refresh-handleexamplecom)
- [Danger Zone](#danger-zone)
  - [`]link profile`](#link-profile)
  - [`]unlink profile`](#unlink-profile)
  - [`]replace room`](#replace-room)
  - [`]replace dm` / `]replace chat`](#replace-dmchat-handleexamplecom)
  - [`]delete profile`](#delete-profile)

---

## Profile

Setting up and maintaining your own Bluesky identity.

### `]create profile`

**Syntax:** `]create profile`, no other argument.

**What it does:** One-shot setup. Claims the handle `<your username>.<bridge domain>`, creates your ATProto account and registers its DID with the public PLC directory, creates a new Matrix room (bot as creator/admin), invites you and sets your power level to 99 (one below the bot), copies your current Matrix display name and avatar onto the room, marks it as an MSC4501 profile room owned by you, and publishes your profile. It also gives you a notifications room and a personal space holding all of your bridge rooms (see [Your rooms](#your-rooms)). When you join the Profile Room, the bot posts a welcome message explaining how it works.

**Who can run it:** Any local Matrix user. If you already have an identity, this doesn't create a second one: it makes sure your space and notifications room exist and re-invites you to any of your rooms you're not in (a harmless no-op if nothing is missing).

**Notes:** Characters in your username that a hostname can't hold become hyphens (`john_smith` becomes `john-smith`). The handle is refused if it is reserved, already taken by another user, or is a subdomain another service on the site answers for; the reply says which. Registering a DID is public and permanent.

---

### `]status`

**Syntax:** `]status`, no argument.

**What it does:** Shows your handle, DID, Profile Room and bsky.app profile link.

**Who can run it:** Any local user with an identity.

---

### `]banner mxc://server/mediaid`

**Syntax:** `]banner mxc://server/mediaid`; or reply to an image message with just `]banner`; or `]banner remove`.

**What it does:** Sets your profile's banner image. Matrix has no stable room-banner state yet, so it is recorded in your Profile Room as MSC4221's banner state (under that proposal's unstable name) and published from there. Any client that sets that state itself has the same effect.

**Who can run it:** Requires an identity. Works from any room the bot is in; it always applies to your own profile.

**Notes:** The image is re-encoded if it is over 1 MB or carries metadata (see [FEATURES.md](FEATURES.md#what-happens-without-a-command)).

---

## General

Following and everyday commands.

### `]help` / `]help <section>`

**Syntax:** `]help`, or tag the bot with nothing else, shows a table of every section (Profile, General, Advanced/Maintenance) with a one-line blurb each. `]help <section>` (e.g. `]help profile`, `]help advanced`) shows that section's commands. `]help all` shows every section's commands in one table.

**What it does:** Sends a table as a rich `m.text` message (not a notice, so "hide notices" client settings don't suppress it).

**Who can run it:** Anyone, including users on other Matrix homeservers. This is the one exception to the local-users-only rule.

**Notes:** Read-only. An unrecognized section name falls back to the section overview.

---

### `]follow handle.example.com`

**Syntax:** `]follow handle.example.com`. A leading `@`, a DID, or a `bsky.app/profile/...` link also work. With no argument inside an account's Remote User Room, follows that account.

**What it does:** Follows the account as you: publishes a follow in your repository, creates or reuses the account's Remote User Room (with a ghost user carrying its name and avatar), and invites you into it. The first time anyone here follows an account, its most recent posts are mirrored in (`atproto.follow_backfill_count`, default 15). New posts then arrive live.

**Who can run it:** Requires an identity first.

**Notes:** No-op with a notice if you already follow them. A handle only counts if the account it points at claims that handle back. Following an account hosted on this same bridge isn't supported yet.

---

### `]unfollow handle.example.com`

**Syntax:** `]unfollow handle.example.com`, or no argument inside that account's room.

**What it does:** Deletes your follow and removes you from the room.

**Who can run it:** Requires an identity.

**Notes:** Leaving (or being removed from) a Remote User Room does the same thing. The room itself stays for anyone else here who follows the account.

---

### `]following`

**Syntax:** `]following`, no argument.

**What it does:** Lists the accounts you follow and their rooms.

---

### `]import <url>`

**Syntax:** `]import https://bsky.app/profile/<handle>/post/<id>` (an `at://` post address also works).

**What it does:** Mirrors that one post into its author's Remote User Room, creating the room if needed, and invites you to it, without following the author.

**Who can run it:** Any local user.

---

### `]repost` (reply to a post)

**Syntax:** Send `]repost` as a reply to a post to repost it. Add text after it (`]repost Worth reading`) to quote-post it with that text instead. Reacting to a post with 🔁 is the same as a bare `]repost`.

**What it does:** Reposts (or quotes) that post from your account, and leaves a card in your own Profile Room: "🔁 *you* reposted *author*'s post" with a preview and a link to the post. The repost may have happened in someone else's room, and your Profile Room is your timeline. The bot sends the card, with an MSC4144 per-message profile so that clients which support it show it as yours.

**Who can run it:** Requires an identity. "A post" is any message that stands for one: a mirrored Bluesky post in any room (including a mirrored repost, which acts on the original), or a local user's own post.

**Notes:** Undo it by removing the 🔁 reaction, redacting the command message, or redacting the card; any of the three works, and the other is cleaned up. Reposting something twice does nothing the second time.

---

### `]chat handle.example.com` / `]dm handle.example.com`

**Syntax:** `]chat handle.example.com` or `]dm handle.example.com` (the same command: Bluesky has one kind of direct message, which it calls Chat). With no argument inside an account's Remote User Room, chats with that account.

**What it does:** Starts, or reuses (re-inviting you if you'd left), a room for your Bluesky chat with that account, named "<handle> (Chat)". It is an ordinary room by default; the host can have chat rooms created as Matrix direct messages instead (`bridge.chat_rooms_as_dms`, see [CONFIG.md](CONFIG.md#bridgechat_rooms_as_dms)). Messages you send there are delivered as chat messages; the other person's replies appear from their ghost user.

**Who can run it:** Requires an identity. Refuses if the handle belongs to a user of this bridge; just start an ordinary Matrix DM with them.

**Notes:**
- A chat someone on Bluesky starts with you creates its room and invites you automatically. Who may start one is set by the host (`atproto.chat_allow_incoming`; Bluesky's own default is only accounts you follow), and the other side has the same kind of setting: if they don't accept chats from you, the reply says so.
- Inviting a ghost user directly into a new DM also starts a chat with that account.
- Chat messages are text only. A file you send is delivered as a link to it. A message longer than 1000 characters is split into several.
- Emoji reactions carry over both ways: react to a chat message and the other person sees it, and theirs appear from their ghost. Removing one removes it on the other side.
- A reply is a reply on both sides: replying to a message in the room sends a Bluesky reply to it, and a reply made on Bluesky shows as a Matrix reply.
- Redacting a message you sent deletes it for you on Bluesky; the chat service doesn't let you delete it for the other person. A message they delete is redacted here.
- Chat doesn't go through your repository; it lives on Bluesky's chat service, which the bridge talks to on your behalf. New messages are checked every `atproto.chat_poll_seconds` (default 10).
- The bridge has no end-to-end encryption support. If your homeserver forces encryption on new private rooms, you'll be told the chat room can't work until a server admin changes that; see [`]replace chat`](#replace-dmchat-handleexamplecom) for replacing such a room afterwards.

---

## Advanced/Maintenance

Blocking, and repairing or refilling rooms.

### `]block handle.example.com`

**Syntax:** `]block handle.example.com`, or no argument inside that account's room.

**What it does:** Publishes a block of the account from yours and unfollows it. On ATProto a block is a public record that Bluesky enforces in both directions.

**Who can run it:** Requires an identity.

---

### `]unblock handle.example.com`

**Syntax:** `]unblock handle.example.com`, or no argument inside that account's room.

**What it does:** Removes your block. It does not re-follow.

---

### `]backfill [N]`

**Syntax:** `]backfill` (uses the configured default count, `atproto.follow_backfill_count`) or `]backfill N`. Run inside a Remote User Room: at the room root to backfill that account's own posts, or as a reply to a mirrored post (or inside its thread) to backfill that conversation's replies instead.

**What it does:** Pulls up to N posts (or that post's replies) through the same mirroring path a live delivery uses, so a backfilled post is indistinguishable from a live one and already-mirrored posts are never duplicated. Replies are placed in a Matrix thread under the post, each sent by its own author's ghost. Runs in the background, with a summary posted once done.

**Who can run it:** Any local user, with the default count. Only a server admin (`bridge.admins`, or a Synapse server admin if `synapse.admin_token` is set) can specify a custom N, since an arbitrarily large one means unbounded fetches against someone else's server.

**Notes:** A brand-new follow already triggers one automatic backfill. This is for topping that up or pulling in a specific conversation. A reply written by a user of this bridge isn't pulled into the thread; it already lives in their own Profile Room.

---

### `]refresh [handle.example.com]`

**Syntax:** `]refresh` inside a Remote User Room, or `]refresh handle.example.com` anywhere.

**What it does:** Re-reads the account's identity and profile and brings everything the bridge keeps in sync with it up to date now, rather than waiting for the live feed to report a change:

- The ghost's handle, display name and avatar.
- The room's name, avatar, topic (bio) and banner.
- The room's MSC4501 owner (`org.matrix.msc4501.social.profile_user_id`).
- The ghost's MSC4501 profile room and MSC4503 handle profile fields, brought into line with `bridge.set_msc4501_profile_room` and `bridge.msc4503_external_handle` either way: set if the setting wants them, removed if it no longer does.
- The room's join rule and history visibility, brought into line with `bridge.ghost_room_join_rule` and `bridge.world_readable_remote_rooms`.

**Who can run it:** Server admins only (`bridge.admins`, or a Synapse server admin if `synapse.admin_token` is set), as in matrix-appservice-activitypub.

**Notes:** Best-effort per piece: one part failing doesn't stop the rest. A ghost's Matrix ID is derived from the account's DID, not its handle, so it never changes when the handle does. Every Remote User Room is also brought into line with the settings each time the bridge starts.

---

## Danger Zone

The commands below directly manipulate the binding between a Matrix room and a Bluesky identity: which room an identity lives in, or whether it exists at all. Used on the wrong room, or without understanding what they do, they can leave things in an inconsistent state, and one of them is irreversible. Every command here is confirmation-gated for that reason: running it only checks that it would be allowed and posts a warning explaining exactly what will happen. Nothing changes until you **reply "confirm" to that specific warning message**.

- Only the person who ran the command can confirm it.
- The "confirm" must be a reply to the warning. A bare "confirm" is just an ordinary message (and, in your Profile Room, an ordinary post).
- Everything is checked again when you confirm, so a stale warning can't do something that's no longer allowed.

---

### `]link profile`

**Syntax:** `]link profile`, then reply "confirm" to the warning. Run inside the room you want to bind your identity to.

**What it does:**
1. `]link profile` alone checks that you may link this room (you don't already have a Profile Room, the room isn't already used by the bridge for something else, it isn't encrypted, and you have enough power in it to change its name), then warns that this makes the room your permanent Bluesky profile.
2. Replying "confirm" binds your identity to the room, creating and registering an identity if you don't have one, or reattaching a previously unlinked one. The room's name, topic and avatar become your display name, bio and avatar.

**Who can run it:** Any local user without a Profile Room, in a room they control that the bridge isn't already using.

**Notes:** Unlike [`]create profile`](#create-profile), this doesn't make a room for you: you need to already control one and have invited the bot. A room's type can't be changed after creation, so a linked room doesn't carry the MSC4501 profile room type; everything else is set up the same.

---

### `]unlink profile`

**Syntax:** `]unlink profile`, then reply "confirm" to the warning.

**What it does:**
1. `]unlink profile` alone warns that the room stops publishing for you immediately and becomes linkable as a different identity by anyone with enough power there.
2. Replying "confirm" detaches your Profile Room from your identity. Nothing is told to Bluesky and nothing is deleted: your handle, DID, existing posts, follows and followers are all preserved. This is how you move your profile: unlink, then `]link profile` or `]create profile` to attach the same identity to another room.

**Who can run it:** Any local user with a Profile Room.

**Notes:** Posts you already made stay published and are still rebuilt from the old room's messages, so don't remove the bot from it. Contrast with `]delete profile`, which erases the identity itself.

---

### `]replace room`

**Syntax:** `]replace room`, then reply "confirm" to the warning. Run inside the room to be replaced.

**What it does:**
1. `]replace room` alone works out what kind of room this is and whether you may replace it, then warns, naming that kind.
2. Replying "confirm" creates a new room for the same purpose (your Profile Room, a Remote User Room, a Chat room, or your notifications room), set up the way the bridge sets rooms up now, with a `predecessor` link to the old one. The old room is tombstoned and renamed with a "(Replaced MM/DD/YYYY)" suffix. For a Profile Room or Remote User Room, the other local members are invited to the new room too. Nothing changes on Bluesky.

**Who can run it:**
- Your own Profile Room: you, or a server admin.
- A Remote User Room: a server admin only.
- A Chat room or notifications room: its owner, or a server admin.

**Notes:** Nobody is removed from the retired room. Refuses outright if the room turns out to be registered as more than one kind of room at once; that means something is already wrong and this isn't the way to fix it.

---

### `]replace dm/chat handle.example.com`

**Syntax:** `]replace chat handle.example.com` or `]replace dm handle.example.com`, then reply "confirm" to the warning. Unlike `]replace room`, run from **anywhere**.

**What it does:** The Chat-room equivalent of `]replace room`, for the case that command can't reach: a chat room the bot can't read commands in (one that ended up encrypted). It looks your chat room with that account up directly. Otherwise identical: new room for the same conversation, predecessor link, old room tombstoned.

**Who can run it:** The chat's owner.

**Notes:** Warning and result go to wherever you ran the command, never into the old room.

---

### `]delete profile`

**Syntax:** `]delete profile`, then reply "confirm" to the warning.

**What it does:**
1. `]delete profile` alone posts an itemized warning of exactly what will happen.
2. Replying "confirm" deletes the identity: tells the Bluesky network the account is deleted (so the profile and every post disappear there), permanently retires its DID, removes you from every other room the bridge put you in (followed accounts, chats, notifications) and from your space, renames your Profile Room to end in "(Deleted)", and erases the identity here: signing key, follows, and all record of its posts. The handle is freed.

**Who can run it:** Any local user with an identity. It always acts on whoever confirms, never on someone else.

**Notes:** Irreversible, and the only command here that is. The Profile Room itself is left for you to leave when you like. Running `]create profile` afterwards gives you a brand new account with a different DID; nothing carries over.
