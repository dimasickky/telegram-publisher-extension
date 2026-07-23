# Changelog

All notable changes to Telegram Publisher are documented here.

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
