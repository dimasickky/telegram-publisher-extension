# Telegram Publisher

[![Imperal SDK](https://img.shields.io/badge/Imperal%20SDK-5.9.12-6c5ce7?logo=python&logoColor=white)](https://imperal.io)
[![License: LGPL v2.1](https://img.shields.io/badge/License-LGPL--2.1-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776ab?logo=python&logoColor=white)](https://www.python.org/)

> Connect the Telegram channels and groups you administer to Imperal Cloud — publish posts and read new activity, straight from chat.

**Telegram Publisher** links your Telegram channels/groups to [Imperal Cloud](https://imperal.io), the ICNLI AI Cloud OS, through one shared Imperal bot. No separate backend service, no MTProto user login, no phone number ever touches this extension — everything runs through Telegram's official Bot API.

## What it can do

| Area | Capabilities |
| --- | --- |
| 🔌 **Connect** | Link your Telegram identity with a one-tap deep link, then add the bot as admin to any channel/group you own — it's auto-discovered, no chat IDs to paste |
| ✍️ **Publish** | Post text (with a safe HTML subset — bold/italic/links/code/spoiler) or a photo with caption to any linked channel — with a **draft preview before it goes live** (see below) |
| 🪄 **AI draft** | Write a post from a brief, automatically matching the tone/style of the channel's own recent public posts, and pre-checked against Telegram's own length/formatting limits |
| 📚 **Read** | Pull recent posts from a linked *public* channel via its `t.me/s/` preview page |
| 🧭 **Multi-channel** | Link as many channels/groups as you administer — one Telegram identity, N linked destinations |
| 🩺 **Status** | Check connection state and per-channel posting permission at a glance |

## Draft preview before publishing

`post_to_channel` never posts on the first call. It always previews first —
rendered in chat exactly as it will look on Telegram (same HTML subset, same
photo if any) — **and the bot itself also DMs you the same draft**, so you
see it from inside the actual chat that will publish it, not just as a card
in Imperal's UI. Nothing reaches the channel until you say so and the tool is
called again with `confirm=true`. Same two-step shape as this workspace's
GitHub Connector (`delete_branch`, `merge_pull_request`) — no destructive or
public-facing action ever fires on a first call.

## AI draft generation with tone matching

`generate_draft` writes the post for you from a short brief:

1. If the channel is public (has a `@username`), it samples the channel's own
   recent posts (via the same `t.me/s/` preview used by `get_channel_recent_posts`)
   so the draft matches the channel's existing voice instead of writing in a
   generic tone.
2. The prompt bakes in Telegram's own hard constraints up front — the limited
   HTML subset (`b/i/u/s/a/code/pre/blockquote/spoiler`, no Markdown, no
   headings/lists/tables) and the right character cap (4096 for a text post,
   1024 if it will carry a photo caption) — so what comes back is postable
   as-is, straight into `post_to_channel`.
3. Private channels (no public `@username`) skip tone sampling automatically
   and just write to the brief — there is no way to read their history (see
   "What it deliberately does NOT do" below).

## What it deliberately does NOT do

- **No MTProto / user-client login.** Telegram's own developer terms put accounts logged in via unofficial API clients "under observation" and warn of permanent bans for automation abuse — full channel history and multi-account browsing are session-based features tied to that risk, so this extension doesn't offer them. See [`extensions/telegram-publisher.md`](../extensions/telegram-publisher.md) for the full research behind this call.
- **No full history backfill for private channels.** The Bot API itself has no "fetch old messages" method — this is a hard protocol limit, not a missing feature.
- **Not a Webbee chat channel.** If you already talk to Webbee through Telegram, that's a separate, platform-level integration — unrelated to this extension.

## Quick start

### 1. Install the extension

Install **Telegram Publisher** from Imperal Cloud when it is available in your workspace.

### 2. Connect your Telegram identity

Ask Webbee to connect Telegram — you'll get a one-tap deep link to `@ImperalConnectorBot` (or whichever bot this deployment uses). Tap it, hit **Start**, and you're linked.

### 3. Add the bot to a channel

In Telegram, open your channel/group → **Administrators** → add the bot → grant **Post Messages**. It shows up automatically the next time you list your channels.

### 4. Publish

Ask Webbee to write and post to any linked channel — e.g. "write a post about X for my channel, matching its usual tone". You'll see a draft preview (in chat and as a DM from the bot itself) before anything actually goes out; confirm to publish.

## Architecture

- One shared bot for every Imperal user (same shape as this workspace's GitHub Connector using one shared GitHub App) — not a per-user OAuth/BYO credential.
- Own storage only (`ctx.store`) — no separate backend process. Structurally closest to this workspace's WordPress Site Connector.
- One webhook endpoint receives every Telegram update kind (`message`, `my_chat_member`, `channel_post`) — Telegram doesn't support per-event webhook URLs the way GitHub does.

## Security

- The bot token is a single app-scope secret (Developer Portal → Secrets), never a per-user credential.
- Webhook authenticity is verified via Telegram's `secret_token` mechanism (`X-Telegram-Bot-Api-Secret-Token`), compared with `secrets.compare_digest`.
- No phone numbers, session strings, or 2FA credentials are ever requested or stored.

## License

LGPL-2.1 — see [LICENSE](LICENSE).
