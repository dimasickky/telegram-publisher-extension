# Changelog

All notable changes to Telegram Publisher are documented here.

## [0.4.0] - 2026-07-24

### Fixed

- **Channels were never auto-discovered at all.** The extension never called
  `setWebhook`, so registering the webhook URL was an undocumented manual step —
  and, critically, Telegram EXCLUDES `my_chat_member` from `setWebhook`'s default
  `allowed_updates` set (it must be listed explicitly). Without it Telegram
  silently never delivers the "bot promoted to channel admin" event, so
  `_handle_my_chat_member` never ran and no channel was ever linked — while
  `/start` kept working fine, because plain `message` updates ARE in the default
  set. That asymmetry made the connect flow look half-broken: identity bound,
  channel list permanently empty. Now registered by an `@ext.on_install` hook
  that requests `message`, `my_chat_member` and `channel_post` explicitly.
  Idempotent (`setWebhook` overwrites), so redeploys are self-healing.
- `can_post` was derived by reading `can_post_messages` unconditionally, but that
  admin right exists only on CHANNELS — in a group/supergroup it is absent from
  the admin record entirely, since posting there isn't an admin privilege. An
  admin bot in a supergroup was therefore stored as `can_post=false` and
  `post_to_channel` refused a chat it could genuinely publish to. Both the
  webhook path and the new manual path now go through `derive_can_post()`.
- `tg_error_message()` was being called with the whole response object instead of
  its `(status_code, description)` pair, which raised `TypeError: unhashable
  type: 'HTTPResponse'` instead of producing an error message — meaning any
  failed `sendMessage`/`sendPhoto` crashed the handler rather than reporting why.
  Added the resp-shaped `tg_error_from()` wrapper so the unpacking lives in one
  place, and moved both call sites onto it.
- Auto-discovery never persisted the channel's public `@username`, which
  `get_channel_recent_posts` and `generate_draft`'s tone sampling read off the
  stored record — so tone matching silently didn't work for auto-linked public
  channels. Now saved on both paths.

### Added

- `link_channel` — link a channel the bot was ALREADY an admin of, by `@username`,
  numeric chat id, or a pasted `t.me/...` link. Necessary because
  `my_chat_member` is a point-in-time event that Telegram never replays: a
  channel the bot joined before the webhook existed produces no update, ever, and
  is otherwise unrecoverable. Verifies against Telegram rather than trusting the
  caller — `getChat` (chat visible to the bot), then `getChatAdministrators` +
  `getMe` (bot really is an admin), then `derive_can_post` (it may actually post),
  with a distinct error for each failure. This finally uses the
  `get_chat_administrators`/`get_me` helpers, which were written for the webhook
  handler but had never been called from anywhere.

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
