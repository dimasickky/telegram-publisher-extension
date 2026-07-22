"""telegram-connector · entrypoint (Extension + ChatExtension + app-scope secrets).

Architecture (see extensions/telegram-connector.md for the full research):
ONE shared Telegram bot for every Imperal user — same shape as github-connector's
shared GitHub App, NOT a per-user OAuth/BYO credential like wp-site-connector or
vikunja-bridge. That means the bot token is an APP-scope secret (one value,
same for all users), not a per-user encrypted credential in ctx.store.

No separate backend service (no api-server FastAPI process like
article-writer-api/db-service/vikunja-bridge/notes-api). Everything runs
extension-side:
  - ctx.http           -> calls api.telegram.org/bot<token>/... directly
  - ctx.store          -> per-user channel bindings + link-code state
                          (same shape as wp-site-connector's `sites` collection)
  - @ext.webhook(...)  -> receives Telegram's Bot API updates (message,
                          my_chat_member, channel_post), proxied through
                          panel.imperal.io exactly like github-connector's
                          install_callback / spotify's OAuth callback
This makes it structurally closer to wp-site-connector ("own storage, no
shared backend") than to tasks/notes/sql-db (which do have one).
"""
from imperal_sdk import Extension, ChatExtension

ext = Extension(
    "telegram-connector-extension",
    version="0.1.0",
    capabilities=["telegram-connector-extension:read", "telegram-connector-extension:write"],
    display_name="Telegram Connector",
    description="Publish posts to your Telegram channels and read new posts for context — connect via one shared bot.",
    icon="icon.svg",
    actions_explicit=True,
)

chat = ChatExtension(
    ext, tool_name="telegram-connector",
    description="Connect Telegram channels you administer, publish posts, and read new channel activity",
)


# ─── Secrets (app-scope: ONE shared bot identity, same for every user) ────── #
# Per-user data (which chat_id is bound to which imperal_id, link-code state)
# lives in ctx.store — see storage.py. These two secrets are the bot's own
# identity, shared by all users of this extension — never a per-user value.

ext.secret(
    name="telegram_bot_token",
    description=(
        "Bot token from @BotFather (e.g. '123456:ABC-DEF...'). ONE shared bot "
        "for every Imperal user — not a per-user credential. Used to call "
        "api.telegram.org/bot<token>/... and to register the webhook."
    ),
    scope="app",
    required=True,
    max_bytes=128,
)(lambda: None)

ext.secret(
    name="telegram_webhook_secret",
    description=(
        "Random string used as Telegram's `secret_token` for setWebhook, "
        "verified against the `X-Telegram-Bot-Api-Secret-Token` header on "
        "every inbound update (constant-time compare) before trusting the "
        "payload. Generate once with `secrets.token_urlsafe(32)` and set in "
        "Developer Portal -> Secrets."
    ),
    scope="app",
    required=True,
    max_bytes=256,
)(lambda: None)


@ext.health_check
async def health_check(ctx) -> dict:
    """Liveness probe for the extension."""
    return {"status": "ok"}
