# Features

How each thing is bridged. Most of it needs no commands at all; for the commands themselves see [COMMANDS.md](COMMANDS.md).

## What happens without a command

Most of the bridge needs no commands at all.

**In your own Profile Room**

- A **text message** from you is published as a post. Text longer than one post (300 characters) becomes a thread of up to 25 posts, split at word boundaries; anything longer is refused with a notice.
- An **image** is published as a native image. It is re-encoded as JPEG if it is larger than 1 MB, in a format Bluesky doesn't accept, or carries metadata (photo EXIF data often includes GPS coordinates).
- An **MP4 video** is published as a native video.
- **Any other file** (documents, audio, other video formats) is published as a link card pointing at the file, served by the bridge.
- A caption on any of these becomes the post's text.
- **Redacting** a message deletes everything published from it.
- Changing the room's **name, topic or avatar** changes your display name, bio and avatar.
- Replies to your posts from Bluesky appear as a thread under the post, each from its author's ghost user, and you can answer in the thread.
- Messages from anyone else in the room (unless they reply to a post, which publishes a reply from *their* account) and edits are not published.

**On any post, in any room**

These work on a mirrored Bluesky post and on a local user's own post alike, and act as whoever does them (so you need an identity).

- **Reacting with 👍**, in any skin tone, likes the post. Removing the reaction removes the like. Bluesky has no other reactions, so other emoji stay in Matrix.
- **Reacting with 🔁** reposts it (see [`]repost`](COMMANDS.md#repost-reply-to-a-post)).
- **Replying** to it, as a rich reply or in its thread, publishes a reply from your account. Text, images, video and files work as they do in your Profile Room, and a long reply becomes a chain. Redacting the message deletes the reply.

Going the other way, a like or repost made on Bluesky of a post that is in Matrix shows as a 👍 or 🔁 reaction on the Matrix message, from that account's ghost user. For your own posts this covers everyone (you are also notified); for other posts it covers accounts followed here. A like removed on Bluesky is un-reacted here if it was by an account followed here; for anyone else the bridge is never told, so the reaction stays.

**In a Remote User Room**

- The account's posts appear as messages from its ghost user, at their original time: text with links, mentions and hashtags clickable, images and video copied into Matrix, link cards as links. A post is always one message: its first image or video, captioned with the text. Any further attachments are listed under "Other Attachments:" as links, since a Matrix message can hold only one file.
- A reply to a post that is in Matrix somewhere (another mirrored post, or a local user's post) is threaded under that post, in that post's room, so a conversation stays in one place. A copy headed "⤵️ Reply to *author*'s post" is also cross-posted into the replier's own room, so that room stays a complete timeline of the account.
- A reply to a post that isn't in Matrix appears in the replier's room under the same "⤵️ Reply to *author*'s post:" heading, with "post" linking to what it answers on Bluesky.
- A repost appears as one message, "🔁 reposted *author*'s post:" above the original's content, with "post" linking to the original in Matrix. If the original isn't in Matrix yet it is imported into its author's own room first (nobody is invited there). If the original is an image or video, the repost message is that same image or video with the text as its caption. Liking or replying to it acts on the original.
- A quote-post ends with "🔁 reposted *author*'s post" and a preview of the quoted post. If the quoted post has an image or video and the quote-post has no attachment of its own, the message is that image or video at preview size; if it has its own, the quoted one is shown inside the preview. Whether a quoted post that isn't in Matrix gets imported is set by `bridge.quote_import_policy`: `known` (default, only if its author already has a room here), `always`, or `never`; otherwise the line links to bsky.app.
- Posts and reposts the live feed didn't deliver are picked up by a direct check of each followed account every `atproto.catchup_minutes` (default 5).
- Deleting a post on Bluesky redacts it here.
- Media larger than `atproto.max_media_mb` isn't copied into Matrix; it is linked under "Attachments:" in the post's message instead.

## Your rooms

Besides your Profile Room, the bridge keeps two rooms for every user. Their names and avatars are set by the host (`bridge.space_name`, `bridge.space_avatar_mxc`, `bridge.notifications_room_name`, `bridge.notifications_room_avatar_mxc`); the defaults are "AT Proto" and "AT Proto Notifications" with the bot's avatar.

**Your space** holds every room the bridge manages for you: your Profile Room first, your notifications room second, then the room of each account you follow or imported a post from. Rooms are added when you join them and removed when you leave a followed account's room. The bot creates the space and you sit one power level below it (99), as in your Profile Room.

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

These are kept out of your Profile Room so that anyone else you invite there can't see who is interacting with you. They are ordinary messages that tag nobody, so whether the room notifies is entirely your own per-room setting. They are checked every `atproto.notification_poll_seconds` (default 30).

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
