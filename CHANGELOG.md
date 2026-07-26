# Changelog

All notable changes to Telegram Publisher are documented here.

## [0.6.1] - 2026-07-26

### Fixed

- **An attached photo was invisible to the assistant, so it refused to use it.**
  0.6.0 shipped the upload and the automatic pickup, but nothing ever *told*
  the assistant a photo was waiting. Asked to "use the photo I attached", the
  only honest conclusion available to it was that it had to read the file
  first — which no extension can do, since nothing in the SDK's `Context`
  carries attachments. So it declined a request that was in fact ready to run:
  the bytes were already on Telegram's side and `post_to_channel` attaches
  them by `file_id` without ever seeing them.

  The state is now visible where decisions get made, as a flag rather than as
  file content:
  - `channels_overview` (skeleton) reports `photo_attached`, the file name, and
    the 1024-character caption limit that comes with it — ambient context, so
    it is known before a tool is even chosen, and drafting targets the right
    length from the start.
  - `get_telegram_connection_status` reports the same, and says the photo will
    be sent automatically.
  - `post_to_channel`'s own description now states plainly that the image does
    not need to be seen, cannot be, and that a post must never be stalled over
    it.
  - `upload_post_photo`'s confirmation spells out the next step: just write the
    post, leave `photo_url` empty.

## [0.6.0] - 2026-07-26

### Added

- **Posting a photo no longer requires hosting it somewhere public first.**
  `post_to_channel` could only take `photo_url`, so illustrating a post meant
  finding a public URL for an image the user already had in front of them.
  There is now an upload widget in the sidebar: drop an image in, and the next
  post goes out with it. The `photo_url` route is untouched and still wins when
  both are present — something named explicitly must never be overridden by a
  photo staged earlier.

  Mechanically, the upload does not store bytes in Imperal. It POSTs them to
  the author's own DM with the bot (multipart, the only form the Bot API
  accepts for a file with no public URL) and keeps the `file_id` Telegram
  returns — a durable handle that `sendPhoto` accepts in the same field as a
  URL. Two things fall out of that: the author sees the actual image in the
  same client that will publish it, and nothing token-bearing is ever stored.
  The obvious alternative — `getFile` — hands back a URL with the bot token
  embedded in the path, and that token is an app-scope secret shared by every
  user of this extension, so it must never reach a browser.

  The staging slot holds exactly one photo per user: "the photo I just
  attached" is a staging area, not a media library, so a second upload replaces
  the first rather than leaving orphans nobody clears. It is freed
  automatically once a post that used it is confirmed sent — never before, so a
  failed send leaves the photo in place for the retry, and never after a post
  that carried a URL instead.

- `clear_staged_photo` — drop a pending photo without posting it.

### Changed

- The post-length check now depends on whether the post carries an image: a
  photo post is a captioned post, and Telegram caps captions at 1024 characters
  against 4096 for plain text. Previously a caption-length overflow was only
  discovered by Telegram rejecting the send; it is now caught up front, with an
  error that says *why* the limit is lower.

### Note

- A file attached to a **chat message** still cannot be used — not a gap in
  this extension. Nothing in the SDK's `Context` carries message attachments,
  so no extension can read one; the panel upload widget is the only route a
  file has in.

## [0.5.0] - 2026-07-24

### Security

- **`link_channel` never checked that the caller had any claim to the channel.**
  It verified the BOT was an admin — but the bot identity is an app-scope secret
  shared by every Imperal user, so "the bot is an admin here" says nothing about
  who is asking. Knowing a public @username was therefore enough for any user to
  link someone else's channel to their own account and publish into it: the bot's
  own admin rights would carry the request out. Now the caller's linked Telegram
  account must itself appear in `getChatAdministrators` for that chat (creators
  included), checked before the bot's own rights. The webhook path never had this
  hole — it attributes a channel to `my_chat_member.from`, the person who actually
  performed the promotion.

### Fixed

- **Draft previews could silently never arrive.** The preview DM is our own
  framing wrapped around the author's text, but it was sent with
  `parse_mode: HTML` — so any markup outside Telegram's narrow subset (an
  unclosed tag, a heading, a list) made the whole `sendMessage` fail. Since the
  DM helper swallows every exception by design (a failed DM must not turn a
  successful preview into an error), the draft then vanished with no trace: the
  post published fine on confirm, but the author never saw it first. Sent as plain
  text now, which is what a draft should show anyway — markup included.
- **Drafts came out far too short.** The prompt stated only Telegram's hard cap
  ("max 4096 characters"), and a ceiling is not a goal: output landed at three
  terse paragraphs that read like placeholders in a real feed. It now states a
  target range (900–1500 chars, 600–950 for photo captions) plus the shape a post
  that length should have — hook, context, why it matters, lead-in to the link —
  while explicitly forbidding padding. The tone-matching block no longer tells the
  model to match the samples' *length*, which would have perpetuated short posts
  in a channel that already had them.
- The two-step draft→confirm flow was described as "first call previews" but
  nothing stopped a caller passing `confirm=true` immediately, publishing straight
  to a public channel with no preview. The tool and parameter descriptions now
  state that a go-ahead authorises the DRAFT, not the publication, and that every
  post in a batch must be drafted first.

## [0.4.0] - 2026-07-24

### Fixed

- **A channel could be added correctly and stay invisible forever.**
  `_handle_my_chat_member` resolved the promoting Telegram user to an
  `imperal_id` and, when there wasn't one yet, silently discarded the update.
  But promoting the bot BEFORE running `/start` is a perfectly natural order and
  nothing prevents it — and that discarded event was unrecoverable: Telegram
  never replays updates, and the Bot API exposes no method to enumerate the chats
  a bot belongs to. The result was the worst kind of failure: the user had done
  everything right (bot added, admin, can post) and the channel list stayed empty
  with no diagnostic and no way back. Such promotions are now parked in
  `tg_pending_channels` (shared `__webhook__` partition, keyed by promoting
  `telegram_user_id`) and claimed by `_handle_start` on bind, so either order
  works. The bind confirmation names the channels it picked up, and the sidebar
  refreshes via the usual `channel_connected` event.
- `can_post` was derived by reading `can_post_messages` unconditionally, but that
  admin right exists only on CHANNELS — in a group/supergroup it is absent from
  the admin record entirely, since posting there isn't an admin privilege. An
  admin bot in a supergroup was therefore stored as `can_post=false` and
  `post_to_channel` refused a chat it could genuinely publish to. All three paths
  (webhook, parked, manual) now go through `derive_can_post()`.
- `tg_error_message()` was being called with the whole response object instead of
  its `(status_code, description)` pair, which raised `TypeError: unhashable
  type: 'HTTPResponse'` instead of producing an error message — meaning any
  failed `sendMessage`/`sendPhoto` crashed the handler rather than reporting why.
  Added the resp-shaped `tg_error_from()` wrapper so the unpacking lives in one
  place, and moved both call sites onto it.
- Auto-discovery never persisted the channel's public `@username`, which
  `get_channel_recent_posts` and `generate_draft`'s tone sampling read off the
  stored record — so tone matching silently didn't work for auto-linked public
  channels. Now saved on every path.

### Added

- `link_channel` — link a channel by `@username`, numeric chat id, or a pasted
  `t.me/...` link. Fallback path only, for the one case that genuinely cannot be
  automatic: the bot was made admin when no webhook existed at all (e.g. before
  this extension was set up), so Telegram delivered nothing and, since updates
  are never replayed and there is no "list my chats" method, nothing can be
  re-requested. Verifies against Telegram rather than trusting the caller —
  `getChat` (chat visible to the bot), then `getChatAdministrators` + `getMe`
  (bot really is an admin), then `derive_can_post` (it may actually post) — with
  a distinct error code per failure (`TG_CHAT_NOT_REACHABLE`,
  `TG_BOT_NOT_ADMIN`, `TG_BOT_CANNOT_POST`).

### Notes

- No `setWebhook` call was added, deliberately. `my_chat_member` IS delivered
  under `setWebhook`'s default `allowed_updates` — the default is "all types
  except `chat_member`, `message_reaction`, `message_reaction_count`"; it is
  `chat_member` (other users' membership) that must be requested explicitly, not
  `my_chat_member` (the bot's own). Passing an explicit list would only narrow
  delivery, and calling `setWebhook` from a per-user install hook would rewrite
  the shared bot's webhook for every user on every install.

## [0.3.0] - 2026-07-24

### Added

- `generate_draft` — writes a channel post from a short brief using the SDK's
  `ctx.ai.complete()` bridge (same call shape as sql-db's `nl_to_sql` / tasks'
  `ai_breakdown_task`). If the target channel is public, it samples the
  channel's own recent posts (reusing `handlers_read`'s `t.me/s/` scraper) to
  match its existing tone instead of writing generically; private channels
  (no history available) skip sampling and just write to the brief. The
  prompt bakes in Telegram's hard constraints up front — the limited HTML
  subset and the correct character cap (4096 text / 1024 photo caption) — so
  the result is postable as-is straight into `post_to_channel`.
- `post_to_channel` preview call now also has the bot itself DM the linked
  Telegram user the same draft (best-effort, fire-and-forget — a DM failure
  never turns a successful preview into an error), so the draft is seen from
  inside the actual publishing bot's chat, not only as a card in Imperal's UI.
  Confirmation still happens back in chat (`confirm=true`), not via any
  in-Telegram button — kept deliberately simple, no `callback_query` handling.

## [0.2.0] - 2026-07-24

### Changed

- `post_to_channel` now has an explicit two-step confirm flow, same pattern as
  github-connector's destructive tools (`delete_branch`/`merge_pull_request`):
  the first call (`confirm=false`, the default) renders a draft preview in
  chat — the actual HTML-formatted text, the photo if any, and which channel
  it targets — and does **not** contact Telegram at all. Only a second call
  with `confirm=true` (same arguments) actually publishes. `PostResult` gained
  a `needs_confirmation` field to reflect which state a given response is.
- Rationale: Telegram's HTML subset is limited (b/i/u/s/a/code/pre/blockquote/
  spoiler only) — seeing the rendered draft before it goes live catches a bad
  render (wrong formatting, wrong channel) before the post is public, instead
  of after, when it would need a manual delete/edit in the channel itself.

## [0.1.0] - 2026-07-22

### Added

- Initial extension skeleton — own-storage design (no separate backend service), one shared Imperal bot for all users.
- `connect_telegram` — one-shot deep-link (`t.me/<bot>?start=<code>`) identity bind, mirroring the deep-link pattern used by the platform's own Telegram connector (researched, not reused directly — see `extensions/telegram-publisher.md` §2).
- `get_telegram_connection_status` — read-only connection check.
- `telegram_updates` webhook — single endpoint handling `/start <code>` linking, `my_chat_member` channel auto-discovery (records `can_post_messages` per channel), and a `channel_post` ingest stub for a future live archive.
- `list_telegram_channels` — SDL entity list of linked channels.
- `post_to_channel` — text or photo post to a linked channel, with a pre-flight `can_post` check (distinct from "is admin" — Telegram admin rights are granular).
- `disconnect_telegram_channel` — unlink a channel record (does not remove the bot from the chat itself).
- `get_channel_recent_posts` — best-effort recent-post read for PUBLIC channels via their `t.me/s/<username>` preview page (the Bot API itself has no history-fetch method).
- Skeleton: `channels_overview` ambient context + link/unlink alerting.

### Known limitations (v1)

- No full history for private channels — Telegram's Bot API has no equivalent method; this is a protocol limit, not a gap in this extension.
- No live-forward archive of channel posts yet — `channel_post` updates are received but not persisted (v2 idea).
- MTProto/user-client login intentionally not offered — see README "What it deliberately does NOT do".

### Status

Code complete, not yet deployed. Requires a registered Telegram bot token (Developer Portal → Secrets: `telegram_bot_token`, `telegram_webhook_secret`) before any live testing can begin.
