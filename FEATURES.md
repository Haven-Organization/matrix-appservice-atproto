# Features

How each thing is bridged. Most of it needs no commands at all; for the commands themselves see [COMMANDS.md](COMMANDS.md).

## What happens without a command

Most of the bridge needs no commands at all.

**In your own Profile Room**

- A **text message** from you is published as a post. Text longer than one post (300 characters) becomes a thread of up to 25 posts, split at word boundaries; anything longer is refused with a notice. Links are clickable: a bare address links to itself, and a Markdown link (`[label](https://…)`, which is how Matrix clients send a link) shows just its label, linking to the address, as does text your client's formatted message links somewhere. A thread is split outside links where possible.
- An **image** is published as a native image. It is re-encoded as JPEG if it is larger than 1 MB, in a format Bluesky doesn't accept, or carries metadata (photo EXIF data often includes GPS coordinates).
- An **MP4 video** is published as a native video.
- **Any other file** (documents, audio, other video formats) is published as a link card pointing at the file, served by the bridge.
- A caption on any of these becomes the post's text.
- A **link to a Bluesky post** (`bsky.app/profile/.../post/...`) makes it a quote of that post, as pasting one into the Bluesky app does. If the link comes last it is dropped from the text, so a message that is only the link is a bare quote. The first link to a post that can be found is quoted; one that can't stays an ordinary link. A captioned image or video becomes a quote with that media.
- **Redacting** a message deletes everything published from it.
- **Editing** a message updates the post. Bluesky's app shows a post as it was first published and ignores later changes, so the post is published again as a new post (same date, still answering what it answered, still pinned if it was). That loses the old post's likes, reposts, replies and quotes, so it is only done when it has none; otherwise the post is changed in place (which other apps may show) and the room is told that `]republish`, as a reply to the post, forces it. An edit that turns a post into a thread, or a thread into a single post, is always published anew.
- Changing the room's **name, topic or avatar** changes your display name, bio and avatar.
- **Pinning** one of your posts in the room pins it on your profile, as in matrix-appservice-activitypub. Bluesky pins one post, so it is the one pinned most recently; pins of anything that isn't one of your posts are passed over. A post made into a thread pins its first post. Unpinning it, or redacting it, falls back to the next pinned post, or to none.
- Replies to your posts from Bluesky appear as a thread under the post, each from its author's ghost user, and you can answer in the thread.
- Messages from anyone else in the room (unless they reply to a post, which publishes a reply from *their* account) are not published.
- <a id="chatting-in-your-profile-room"></a>**Chatting in your Profile Room** with someone you've invited doesn't post anything when you either:
  - **reply to something that isn't a post**, such as their message or one of your asides, as a rich reply or in a thread. A reply to one of your posts is still published as a reply;
  - **start the message with a command prefix and a space** (an aside): `] see you tomorrow`. Any of the other bots' prefixes (`appservice.other_command_prefixes`) work too, so `; ` and `[ ` keep the message off the other networks as well, if those bridges follow the same rule. The bot doesn't answer an aside, not even `] ` on its own.

**On any post, in any room**

These work on a mirrored Bluesky post and on a local user's own post alike, and act as whoever does them (so you need an identity).

- **Reacting with 👍**, in any skin tone, likes the post. Removing the reaction removes the like. Bluesky has no other reactions, so other emoji stay in Matrix.
- **Reacting with 🔁** reposts it (see [`]repost`](COMMANDS.md#repost-reply-to-a-post)).
- **Replying** to it, as a rich reply or in its thread, publishes a reply from your account. Text, images, video, files and post links work as they do in your Profile Room, and a long reply becomes a chain. Redacting the message deletes the reply.

Going the other way, a like or repost made on Bluesky of a post that is in Matrix shows as a 👍 or 🔁 reaction on the Matrix message, from that account's ghost user. For your own posts this covers everyone (you are also notified); for other posts it covers accounts followed here. A like removed on Bluesky is un-reacted here if it was by an account followed here; for anyone else the bridge is never told, so the reaction stays.

**In a Remote User Room**

- The account's posts appear as messages from its ghost user, at their original time: text with links, mentions and hashtags clickable, images and video copied into Matrix, link cards as links. A post is always one message: its first image or video, captioned with the text. Any further attachments are listed under "Other Attachments:" as links, since a Matrix message can hold only one file.
- A reply to a post that is in Matrix somewhere (another mirrored post, or a local user's post) is threaded under that post, in that post's room, so a conversation stays in one place. A copy headed "⤵️ Reply to *author*'s post" is also cross-posted into the replier's own room, so that room stays a complete timeline of the account.
- When a followed account replies to a post that isn't in Matrix, the conversation is brought in first, as in matrix-appservice-activitypub: its first post is imported into its author's own room (nobody is invited there) and the posts between are threaded under it. The reply is then threaded and cross-posted as above, so the copy in the replier's room is a cross-posted reply even when the reply also quotes a post.
- A reply whose conversation can't be fetched, or one met while filling in an account's history, appears in the replier's room under the same "⤵️ Reply to *author*'s post:" heading, with "post" linking to what it answers on Bluesky.
- A repost appears as one message, "🔁 reposted *author*'s post:" above the original's content, with "post" linking to the original in Matrix. If the original isn't in Matrix yet it is imported into its author's own room first (nobody is invited there). If the original is an image or video, the repost message is that same image or video with the text as its caption. Liking or replying to it acts on the original.
- A quote-post ends with "🔁 reposted *author*'s post" and a preview of the quoted post. If the quoted post has an image or video and the quote-post has no attachment of its own, the message is that image or video at preview size; if it has its own, the quoted one is shown inside the preview. Whether a quoted post that isn't in Matrix gets imported is set by `bridge.quote_import_policy`: `known` (default, only if its author already has a room here), `always`, or `never`; otherwise the line links to bsky.app.
- Posts and reposts the live feed didn't deliver are picked up by a direct check of each followed account every `atproto.catchup_minutes` (default 5).
- Deleting a post on Bluesky redacts it here.
- The account's **pinned post** is pinned in the room by its ghost, imported first if it isn't in Matrix yet. Pinning another post or unpinning on Bluesky moves or removes that pin; anything someone here pinned in the room stays pinned.
- Media larger than `atproto.max_media_mb` isn't copied into Matrix; it is linked under "Attachments:" in the post's message instead.

## Your rooms

Besides your Profile Room, the bridge keeps two rooms for every user. Their names and avatars are set by the host (`bridge.space_name`, `bridge.space_avatar_mxc`, `bridge.notifications_room_name`, `bridge.notifications_room_avatar_mxc`); the defaults are "AT Proto" and "AT Proto Notifications" with the bot's avatar.

**Your space** holds every room the bridge manages for you: your Profile Room first, your notifications room second, then the room of each account you follow or imported a post from. Rooms are added when you join them and removed when you leave a followed account's room. The bot creates the space and you sit one power level below it (99), as in your Profile Room.

**Getting into a room.** Remote User Rooms and Profile Rooms are knockable by default (`bridge.ghost_room_join_rule`, `bridge.local_profile_room_join_rule`), as in matrix-appservice-activitypub. Knock on any Remote User Room and the bridge lets you in straight away: its ghost invites you. Joining doesn't follow the account; only `]follow` does that. On a Profile Room only its owner's knock is let in, so you can always get back into your own. Knocks from other homeservers are left for someone inside to answer unless `bridge.accept_federated_knocks` is on. With `bridge.world_readable_remote_rooms` on, a Remote User Room's posts and profile can be previewed without joining at all.

**Sharing a Profile Room with other bridges.** One room can be your profile on several networks at once: this bridge, [matrix-appservice-activitypub](https://github.com/Haven-Organization/matrix-appservice-activitypub) and matrix-appservice-nostr can all publish from the same Profile Room. Create it with any of them, invite the other bridges' bots, and run each one's link command there (`]link profile` here). The room's name, topic, avatar, banner and pins are then your profile everywhere. Each bridge has its own command prefix (`]` here, `;` and `[` by default for the others), and with the `appservice.other_*` settings ([CONFIG.md](CONFIG.md)) this bridge stays out of the others' way:
- A command for another bot (`;follow ...`, `[help`, or a message tagging their bot) is never published here, as a post or as a reply.
- The other bridges' ghosts (the fediverse or Nostr people whose likes and replies they mirror in) are never answered or obeyed: no "you need a Bluesky identity" notices, no commands.
- **Replies made through another bridge** (its widget, say) to a fediverse or Nostr post are published headed "↩ In reply to X's post", with "post" linking to the answered post, so the post makes sense on its own. The address comes from the reply's MSC4501 relation, or from the answered post if the bot can read it; without one the header is unlinked. A reply to a Bluesky post becomes a native reply instead. Editing the reply keeps the header.
- **Reposts made through another bridge** reach Bluesky too. When you repost a fediverse or Nostr post, that bridge leaves a repost card in the room, and this bridge turns it into a post of yours: "🔁 Reposted X's post", with "post" linking to the original, the reposted text, and the reposted image or (MP4) video embedded. A card with no media Bluesky can show becomes a link card to the original instead, showing its author and text. A card reposting a Bluesky post becomes a native repost instead. Undoing the repost (the other bridge redacts its card) deletes it here too. The card must come from one of `appservice.other_bot_users` acting for you (`com.beeper.per_message_profile`). The link is the card's `external_url`, or, when the card has none, the reposted post's own (the bridges set it on posts they mirror), read once if the bot can see that post. Without either, a card with media or text still goes out, unlinked; one with neither is left alone.
- `]replace room` refuses in a shared room, since the other bridges would be left behind, and `]delete profile` leaves the room's name alone. A room counts as shared when it carries another bridge's `m.bridge` state, or one of the configured bots or ghosts is in it.

**The room widget.** Every room the bridge creates (and, once, every room it made before the widget existed) carries the bridge's widget: in clients that show Matrix widgets, such as Element, a panel with buttons for following, blocking, chatting, importing posts and follows, backfilling, banners and the Danger Zone, suited to the room it's in. It is matrix-appservice-activitypub's widget, adapted; see [Widget vs. commands](COMMANDS.md#widget-vs-commands).

**Leaving a room you follow from unfollows.** A followed account's room is where its posts arrive, so leaving it (or being removed) unfollows the account, as in matrix-appservice-activitypub. Nobody can follow and then leave, keeping the bridge collecting posts no one reads. A leave the bridge missed, say while it was down, is caught when it next starts.

**Your notifications room** is a private room with the bot where it tells you about:

| Event | Message |
|---|---|
| New follower | "*name* followed you" |
| Like | "*name* liked your post", linking to your Matrix message |
| Repost | "*name* reposted your post" |
| Reply | "*name* replied to your post", with their text and a link |
| Mention | "*name* mentioned you", with their text and a link |
| Quote | "*name* quoted your post", with their text and a link |

Each names who did it with a user pill, links "your post" to your message in Matrix, and shows a short preview of the post it is about. If that post is an image or video, the notification is itself that image or video, shown small, with the text as its caption.

These are kept out of your Profile Room so that anyone else you invite there can't see who is interacting with you. They are ordinary messages that tag nobody, so whether the room notifies is entirely your own per-room setting. They are checked every `atproto.notification_poll_seconds` (default 30); when the account that did it is one somebody here follows, the notification is sent the moment the like, repost, reply, quote or mention itself arrives rather than at the next check.

## MSC4501

Rooms and events follow MSC4501 ("Rooms as Social Media Pages") the same way matrix-appservice-activitypub does, with the same settings (`bridge` section of the config):

| Setting | Default | Effect |
|---|---|---|
| (always) | | Profile Rooms and Remote User Rooms are created with the `org.matrix.msc4501.social.profile` room type and an `org.matrix.msc4501.social.profile_user_id` state event naming whose profile the room is. |
| `set_msc4501_relates_to` | on | Mirrored reposts and quote-posts, and replies cross-posted outside their thread, carry `org.matrix.msc4501.social.relates_to`. A reply inside its thread doesn't: the thread already says what it answers. |
| (with the relation) | | Any event carrying that relation also carries `org.matrix.msc4501.social.body` / `social.formatted_body`: the author's own words alone (or, for a plain repost, the reposted post's text), without the "🔁 reposted X's post" or "⤵️ Reply to" line the bridge adds for other clients. Left out when there is no text. |
| `use_msc4501_content_inline` | on | A mirrored repost says its own content is the reposted post, instead of repeating that content inside the relation. |
| `quote_import_policy` | `known` | Whether a quote-post's target is imported into Matrix when it isn't there yet. |
| `msc4144_per_message_profiles` | on | The repost/quote card the bot leaves in your Profile Room carries an MSC4144 per-message profile, so it shows as yours. |
| `use_msc4501_post_event_type` | off | Mirrored posts are sent as `org.matrix.msc4501.social.post` instead of `m.room.message`. Clients that don't know the type show nothing. |
| `set_msc4501_profile_room` | off | Each ghost's Matrix profile points at its room. Needs MSC4133 on the homeserver. |
| `msc4503_external_handle` | off | Records each ghost's real Bluesky handle: `profile`, `events` or `both`. |
