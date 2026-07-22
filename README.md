# Telegram Connector

[![Imperal SDK](https://img.shields.io/badge/Imperal%20SDK-5.9.12-6c5ce7?logo=python&logoColor=white)](https://imperal.io)
[![License: LGPL v2.1](https://img.shields.io/badge/License-LGPL--2.1-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776ab?logo=python&logoColor=white)](https://www.python.org/)

> Connect the Telegram channels and groups you administer to Imperal Cloud — publish posts and read new activity, straight from chat.

**Telegram Connector** links your Telegram channels/groups to [Imperal Cloud](https://imperal.io), the ICNLI AI Cloud OS, through one shared Imperal bot. No separate backend service, no MTProto user login, no phone number ever touches this extension — everything runs through Telegram's official Bot API.

## What it can do

| Area | Capabilities |
| --- | --- |
| 🔌 **Connect** | Link your Telegram identity with a one-tap deep link, then add the bot as admin to any channel/group you own — it's auto-discovered, no chat IDs to paste |
| ✍️ **Publish** | Post text (with a safe HTML subset — bold/italic/links/code/spoiler) or a photo with caption to any linked channel |
| 📚 **Read** | Pull recent posts from a linked *public* channel via its `t.me/s/` preview page |
| 🧭 **Multi-channel** | Link as many channels/groups as you administer — one Telegram identity, N linked destinations |
| 🩺 **Status** | Check connection state and per-channel posting permission at a glance |

## What it deliberately does NOT do

- **No MTProto / user-client login.** Telegram's own developer terms put accounts logged in via unofficial API clients "under observation" and warn of permanent bans for automation abuse — full channel history and multi-account browsing are session-based features tied to that risk, so this extension doesn't offer them. See [`extensions/telegram-connector.md`](../extensions/telegram-connector.md) for the full research behind this call.
- **No full history backfill for private channels.** The Bot API itself has no "fetch old messages" method — this is a hard protocol limit, not a missing feature.
- **Not a Webbee chat channel.** If you already talk to Webbee through Telegram, that's a separate, platform-level integration — unrelated to this extension.

## Quick start

### 1. Install the extension

Install **Telegram Connector** from Imperal Cloud when it is available in your workspace.

### 2. Connect your Telegram identity

Ask Webbee to connect Telegram — you'll get a one-tap deep link to `@ImperalConnectorBot` (or whichever bot this deployment uses). Tap it, hit **Start**, and you're linked.

### 3. Add the bot to a channel

In Telegram, open your channel/group → **Administrators** → add the bot → grant **Post Messages**. It shows up automatically the next time you list your channels.

### 4. Publish

Ask Webbee to post to any linked channel — text, or a photo by URL.

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
