"""telegram-publisher · entrypoint (Extension + ChatExtension + app-scope secrets).

Architecture (see extensions/telegram-publisher.md for the full research):
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
    "telegram-publisher-extension",
    version="0.4.0",
    capabilities=["telegram-publisher-extension:read", "telegram-publisher-extension:write"],
    display_name="Telegram Publisher",
    description="Publish posts to your Telegram channels and read new posts for context — connect via one shared bot.",
    icon="icon.svg",
    actions_explicit=True,
)

chat = ChatExtension(
    ext, tool_name="telegram-publisher",
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


@ext.on_install
async def on_install(ctx) -> dict:
    """Register our webhook with Telegram, explicitly requesting my_chat_member.

    This is the linchpin of channel auto-discovery, and it is NOT optional
    boilerplate. Telegram's default `allowed_updates` set deliberately EXCLUDES
    `my_chat_member` (Bot API docs: it "must be specified explicitly"). Without
    it Telegram silently never delivers the event fired when the bot is promoted
    to channel admin — so handlers_connect._handle_my_chat_member never runs and
    no channel is ever linked, even though `/start` (a plain `message`, which IS
    in the default set) works fine. That asymmetry is exactly what made the
    connect flow look half-broken: identity bound, channels always empty.

    Idempotent: setWebhook overwrites any previous registration for this bot, so
    re-running on every install/redeploy is safe and self-healing. Best-effort by
    design — a failure here must not block the install itself, so it is reported
    in the returned dict rather than raised (the user-facing consequence, if it
    does fail, is only that channel auto-discovery stays dormant until the next
    install or a manual link_channel call).
    """
    import logging
    _log = logging.getLogger("telegram-publisher")

    import telegram_client as tg

    try:
        secret = await ctx.secrets.get("telegram_webhook_secret")
        url = ctx.webhook_url("telegram_updates")
        resp = await tg.tg_call(ctx, "setWebhook", {
            "url": url,
            "secret_token": secret or "",
            # Explicit list — see docstring. channel_post feeds handlers_read's
            # post ingestion; message carries the /start deep-link bind.
            "allowed_updates": ["message", "my_chat_member", "channel_post"],
        })
    except Exception as e:  # transport failure / bot token secret not set yet
        _log.warning("on_install: setWebhook failed (non-fatal): %s", e)
        return {"webhook_registered": False, "error": str(e)}

    if not tg.tg_ok(resp):
        detail = tg.tg_error_from(resp)
        _log.warning("on_install: setWebhook rejected by Telegram: %s", detail)
        return {"webhook_registered": False, "error": detail}

    return {"webhook_registered": True, "url": url}


@ext.health_check
async def health_check(ctx) -> dict:
    """Liveness probe for the extension."""
    return {"status": "ok"}
