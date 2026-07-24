"""telegram-publisher · identity bind (deep-link) + channel auto-discovery.

Telegram has no OAuth redirect — the closest equivalent is a bot deep-link:
`https://t.me/<bot_username>?start=<code>`. Opening it in Telegram starts a
chat with our bot and sends it a `/start <code>` message, which our webhook
receives as an ordinary Update. This mirrors github-connector's auth.py
two-step shape (mint state -> unauthenticated webhook resolves it) with
Telegram's own primitive standing in for GitHub's install-page redirect.

Flow:
1. `connect_telegram` (authenticated) mints a one-shot code
   (storage.save_link_code, under the shared "__webhook__" partition) and
   returns the t.me deep link — same shape as github-connector's
   create_install_url / start_github_install.
2. User taps the link, hits Start. Telegram POSTs an Update to
   `telegram_updates` (our ONE webhook for every update kind — Telegram
   doesn't support separate URLs per update type, unlike GitHub's per-event
   webhook). `_handle_start` resolves `/start <code>` via
   storage.find_and_consume_link_code, then storage.bind_telegram_user
   records the identity both ways (reverse index for future updates + the
   real user's own forward record).
3. When the user adds the bot as admin to a channel, Telegram sends a
   `my_chat_member` update for that chat with `from.id` = the promoting
   Telegram user. `_handle_my_chat_member` resolves that user via
   storage.resolve_imperal_id_for_telegram_user and auto-saves a tg_channels
   record via storage.save_channel_record_for_user — the user never has to
   manually paste a chat_id. No explicit `allowed_updates` is needed for this:
   `my_chat_member` is included in setWebhook's DEFAULT set ("all types except
   chat_member, message_reaction, message_reaction_count"); it is `chat_member`,
   about OTHER users' membership, that must be requested explicitly.
4. Steps 2 and 3 can happen in EITHER order. If the bot is promoted first, there
   is no imperal_id to attribute the chat to yet, so the update is parked in
   `tg_pending_channels` (shared "__webhook__" partition) instead of being
   dropped, and `_handle_start` claims it on bind. Dropping it used to make such
   a channel unrecoverable — Telegram never replays updates, and the Bot API
   exposes no method to enumerate the chats a bot belongs to.

No HMAC needed for authenticity the way GitHub/Vikunja webhooks do —
Telegram instead lets us set a `secret_token` on `setWebhook` that it echoes
back verbatim as `X-Telegram-Bot-Api-Secret-Token` on every delivery. We
generate that once (app-scope, alongside the bot token) and compare with
`secrets.compare_digest` — same constant-time principle as the HMAC checks
elsewhere, just a plain shared value instead of a signature over the body.
"""
import logging
import secrets as _secrets_mod
import time

from pydantic import BaseModel, Field

from imperal_sdk import ActionResult, ui
from imperal_sdk.chat.error_codes import INTERNAL, VALIDATION_MISSING_FIELD

from imperal_sdk import sdl

from app import chat, ext
from models import ConnectTelegramParams, LinkChannelParams, TelegramChannel, _NoParams
from error_codes import (
    TG_BOT_CANNOT_POST,
    TG_BOT_NOT_ADMIN,
    TG_BOT_UNREACHABLE,
    TG_CHAT_NOT_REACHABLE,
    TG_NOT_CHANNEL_ADMIN,
    TG_NOT_LINKED,
)
import storage
import telegram_client as tg

log = logging.getLogger("telegram-publisher")

_LINK_CODE_TTL_SECONDS = 900  # 15 minutes, same order of magnitude as github-connector's oauth state


class ConnectTelegramResult(BaseModel):
    deep_link: str = Field(description="Open this in Telegram to link your account — taps Start automatically")


class ConnectionStatusResult(BaseModel):
    linked: bool
    telegram_user_id: int | None = None


async def _bot_username(ctx) -> str | None:
    """Resolve the shared bot's @username, or None if it can't be determined.

    Every failure mode returns None rather than raising: this runs inside the
    sidebar's render path, where an exception blanks the whole panel instead of
    showing the "not connected yet" state. Catching bare Exception is deliberate —
    besides the RuntimeError for an unset token secret, ctx.http can raise
    transport errors, and neither should take the panel down.
    """
    try:
        resp = await tg.tg_call(ctx, "getMe")
    except Exception as e:  # unset token secret, transport failure, anything
        log.warning("_bot_username: getMe failed: %s", e)
        return None
    if not tg.tg_ok(resp):
        return None
    # tg_result can be None even on ok:true if the payload lacks "result" —
    # .get() straight off it would AttributeError inside the render.
    result = tg.tg_result(resp) or {}
    return result.get("username") or None


async def create_connect_deep_link(ctx) -> str:
    """Mint a one-shot link code and return the matching t.me Start deep-link.

    Shared by the chat function and the sidebar. Panel buttons must receive a
    concrete ``ui.Open(url)`` action at render time: a panel ``ui.Call`` only
    displays the returned ActionResult summary as a toast and does not execute
    UI actions nested in that result (same reasoning as github-connector's
    create_authorize_url).
    """
    username = await _bot_username(ctx)
    if not username:
        return ""

    code = _secrets_mod.token_urlsafe(16)
    await storage.save_link_code(ctx, code, ctx.user.imperal_id, tg.now_iso())
    return f"https://t.me/{username}?start={code}"


@chat.function(
    "connect_telegram",
    action_type="write",
    description=(
        "Get a link to connect your Telegram account to this extension — open it in "
        "Telegram and tap Start. Needed once before you can link any channel."
    ),
    effects=["telegram.connect"],
    event="telegram-publisher-extension.connect_telegram",
    data_model=ConnectTelegramResult,
)
async def connect_telegram(ctx, params: ConnectTelegramParams) -> ActionResult:
    """Mint a one-shot deep-link code and return the t.me Start link."""
    deep_link = await create_connect_deep_link(ctx)
    if not deep_link:
        return ActionResult.error(
            "Telegram bot is not configured yet (telegram_bot_token secret missing or invalid) — "
            "the developer needs to finish setting up the bot first.",
            code=INTERNAL,
        )

    return ActionResult.success(
        data={"deep_link": deep_link},
        summary=f"Open this link in Telegram and tap Start to connect: {deep_link}",
        ui=ui.Stack([
            ui.Button("Open in Telegram", icon="Send", variant="primary", on_click=ui.Open(deep_link)),
            ui.Text("Tap Start in the chat that opens, then come back here."),
        ]),
    )


@chat.function(
    "get_telegram_connection_status",
    action_type="read",
    description="Check whether the user has linked their Telegram account to this extension yet.",
    data_model=ConnectionStatusResult,
)
async def get_telegram_connection_status(ctx, params: _NoParams) -> ActionResult:
    """Read-only: is there a tg_user_link record for this user?"""
    link = await storage.get_telegram_user_link(ctx)
    if not link:
        return ActionResult.success(summary="Telegram is not linked yet.", data={"linked": False})
    return ActionResult.success(
        summary=f"Telegram linked (user id {link.get('telegram_user_id')}).",
        data={"linked": True, "telegram_user_id": link.get("telegram_user_id")},
    )


@chat.function(
    "list_telegram_channels",
    action_type="read",
    description=(
        "List the Telegram channels/groups you've linked — where the bot has been added as "
        "admin after you connected your Telegram account."
    ),
    data_model=sdl.EntityList[TelegramChannel],
)
async def list_telegram_channels(ctx, params: _NoParams) -> ActionResult:
    """Read-only: list this user's tg_channels records."""
    rows = await storage.list_channel_records(ctx)
    channels = [
        TelegramChannel(
            id=str(r["chat_id"]), title=r.get("chat_title", str(r["chat_id"])), kind="telegram_channel",
            subtitle=r.get("chat_type", ""), chat_type=r.get("chat_type", ""),
            can_post=r.get("can_post", False), linked_at=r.get("linked_at"),
        )
        for r in rows
    ]
    if not channels:
        return ActionResult.success(
            sdl.EntityList[TelegramChannel](items=[]),
            summary=(
                "No channels linked yet — add the bot as admin (with 'Post messages') to a "
                "channel and it'll show up here automatically."
            ),
        )
    return ActionResult.success(
        sdl.EntityList[TelegramChannel](items=channels),
        summary=f"{len(channels)} channel(s) linked.",
    )


@chat.function(
    "link_channel",
    action_type="write",
    description=(
        "Link a Telegram channel the bot is ALREADY an admin of, by its @username or numeric "
        "chat id. Only needed as a fallback: channels are normally picked up automatically "
        "when you add the bot as admin — in either order, before or after connecting your "
        "Telegram account. Use this when a channel still doesn't appear in "
        "list_telegram_channels, e.g. the bot was added to it long before this extension "
        "was set up, so Telegram never delivered the event."
    ),
    effects=["telegram.link_channel"],
    event="telegram-publisher-extension.channel_connected",
    data_model=TelegramChannel,
)
async def link_channel(ctx, params: LinkChannelParams) -> ActionResult:
    """Verify + store a channel the bot is already an admin of.

    Last-resort recovery path. `my_chat_member` is a point-in-time event: it fires
    the moment the bot is promoted and is never replayed, and the Bot API has no
    method to enumerate the chats a bot belongs to — so if no webhook existed when
    the bot was added (e.g. the bot was in the channel before this extension was
    ever deployed), nothing was delivered and nothing can be re-requested. Asking
    Telegram about one specific chat — getChat + getChatAdministrators — is then
    the only way back.

    NOT needed for the ordinary "added the bot before running /start" case: those
    updates ARE delivered and are parked in tg_pending_channels, then claimed
    automatically on bind (see _handle_start).

    Verifies three things against Telegram itself rather than trusting the caller:
    the chat exists and the bot can see it, the bot is actually an administrator,
    and (for channels) it holds can_post_messages.
    """
    link = await storage.get_telegram_user_link(ctx)
    if not link:
        return ActionResult.error(
            "Connect your Telegram account first (connect_telegram), then link the channel.",
            code=TG_NOT_LINKED,
        )

    raw = (params.channel or "").strip()
    if not raw:
        return ActionResult.error(
            "Give me the channel's @username or its numeric chat id.",
            code=VALIDATION_MISSING_FIELD,
        )
    # Telegram accepts "@name" for public chats and a bare negative id for any
    # chat; normalise a pasted t.me link down to the @username form too.
    if raw.startswith("https://t.me/") or raw.startswith("t.me/"):
        raw = "@" + raw.rstrip("/").split("/")[-1]
    if not raw.startswith("@") and not raw.lstrip("-").isdigit():
        raw = "@" + raw

    try:
        chat_obj = await tg.get_chat(ctx, raw)
    except Exception as e:
        log.error("link_channel: transport error on getChat: %s", e)
        return ActionResult.error(
            "Could not reach Telegram — try again shortly.", retryable=True, code=TG_BOT_UNREACHABLE,
        )

    if not chat_obj:
        return ActionResult.error(
            f"Telegram doesn't show a chat for \"{raw}\" that this bot can see. Make sure the bot "
            "has been added to that channel as an administrator, and double-check the @username.",
            code=TG_CHAT_NOT_REACHABLE,
        )

    chat_id = chat_obj.get("id")
    chat_type = chat_obj.get("type", "")
    chat_title = chat_obj.get("title", str(chat_id))

    bot = await tg.get_me(ctx)
    admins = await tg.get_chat_administrators(ctx, chat_id)
    if bot is None or admins is None:
        return ActionResult.error(
            f"Couldn't read the admin list of \"{chat_title}\" — the bot is probably not an "
            "administrator there yet. Add it as admin with 'Post messages' permission, then retry.",
            code=TG_BOT_NOT_ADMIN,
        )

    # AUTHORISATION — the caller must be an admin of this chat themselves.
    #
    # The bot identity is shared by every Imperal user (app-scope secret), so "the
    # bot is an admin here" says nothing about whether THIS caller has any claim to
    # the channel. Without this check, knowing a public @username would be enough
    # for any Imperal user to link someone else's channel and publish into it — the
    # bot's own admin rights would happily carry the request out. The webhook path
    # never had this hole: it attributes the channel to `my_chat_member.from`, i.e.
    # the person who actually performed the promotion in Telegram.
    #
    # getChatAdministrators is the authority for this: whoever we are, Telegram
    # decides who administers that chat. Creators are included in the same list.
    caller_tg_id = link.get("telegram_user_id")
    caller_is_admin = any(
        (a.get("user") or {}).get("id") == caller_tg_id for a in admins
    )
    if not caller_is_admin:
        return ActionResult.error(
            f"Your Telegram account isn't an administrator of \"{chat_title}\", so it can't be "
            "linked to your Imperal account. Ask an admin of that channel to link it from their "
            "own Imperal account, or have yourself made an admin there first.",
            code=TG_NOT_CHANNEL_ADMIN,
        )

    bot_member = next((a for a in admins if (a.get("user") or {}).get("id") == bot.get("id")), None)
    if bot_member is None:
        return ActionResult.error(
            f"The bot isn't an administrator of \"{chat_title}\" — add it as admin (with "
            "'Post messages' permission for a channel), then link it again.",
            code=TG_BOT_NOT_ADMIN,
        )

    can_post = tg.derive_can_post(chat_type, bot_member)
    if not can_post:
        return ActionResult.error(
            f"The bot is an admin on \"{chat_title}\" but doesn't have 'Post messages' permission — "
            "grant it in Telegram's channel admin settings, then link it again.",
            code=TG_BOT_CANNOT_POST,
        )

    await storage.save_channel_record_for_user(ctx, ctx.user.imperal_id, {
        "chat_id": chat_id,
        "chat_title": chat_title,
        "chat_type": chat_type,
        "can_post": can_post,
        "linked_at": tg.now_iso(),
        "chat_username": chat_obj.get("username", ""),
    })

    return ActionResult.success(
        TelegramChannel(
            id=str(chat_id), title=chat_title, kind="telegram_channel",
            subtitle=chat_type, chat_type=chat_type, can_post=can_post,
            linked_at=tg.now_iso(),
        ),
        summary=f"Linked \"{chat_title}\" — you can publish to it now.",
        refresh_panels=["sidebar"],
    )


async def _handle_start(ctx, message: dict) -> None:
    """Consume a /start <code> deep-link and record the identity bind both ways."""
    text = message.get("text", "")
    parts = text.split(maxsplit=1)
    code = parts[1].strip() if len(parts) > 1 else ""
    telegram_user = message.get("from") or {}
    telegram_user_id = telegram_user.get("id")
    if not code or telegram_user_id is None:
        return

    imperal_id = await storage.find_and_consume_link_code(ctx, code)
    if not imperal_id:
        await tg.tg_call(ctx, "sendMessage", {
            "chat_id": telegram_user_id,
            "text": "That connect link is invalid, expired, or already used. Generate a new one from Imperal.",
        })
        return

    await storage.bind_telegram_user(ctx, imperal_id, telegram_user_id, tg.now_iso())

    # Drain anything parked before we knew who this Telegram user was. Adding the
    # bot to a channel BEFORE running /start is a perfectly natural order, and
    # those my_chat_member events used to be discarded — leaving the user with a
    # correctly-configured bot and a permanently empty channel list, since
    # Telegram never replays updates and offers no way to enumerate the bot's
    # chats. Now they're claimed here, so either order works.
    claimed = await storage.claim_pending_channels(ctx, imperal_id, telegram_user_id)
    for record in claimed:
        await _emit_for_user(ctx, imperal_id, "telegram-publisher-extension.channel_connected", {
            "imperal_id": imperal_id, "chat_id": record.get("chat_id"),
        })

    if claimed:
        names = ", ".join(f"\"{r.get('chat_title', r.get('chat_id'))}\"" for r in claimed)
        tail = (
            f" I also picked up the {'channel' if len(claimed) == 1 else 'channels'} you'd "
            f"already added me to: {names}."
        )
    else:
        tail = (
            " Now add me as admin (with 'Post messages' permission) to any channel you want "
            "Imperal to publish to — it'll show up automatically once you do."
        )

    await tg.tg_call(ctx, "sendMessage", {
        "chat_id": telegram_user_id,
        "text": "Connected!" + tail,
    })


async def _handle_my_chat_member(ctx, update: dict) -> None:
    """Bot's own membership changed in a chat — the promoting user's id is `from.id`."""
    mcm = update.get("my_chat_member") or {}
    chat_obj = mcm.get("chat") or {}
    new_member = mcm.get("new_chat_member") or {}
    new_status = new_member.get("status", "")
    promoter = mcm.get("from") or {}
    telegram_user_id = promoter.get("id")
    chat_id = chat_obj.get("id")
    if chat_id is None or telegram_user_id is None:
        return

    imperal_id = await storage.resolve_imperal_id_for_telegram_user(ctx, telegram_user_id)
    if not imperal_id:
        # The promoter hasn't run /start yet, so there is no imperal_id to attach
        # this chat to — but discarding the event (what used to happen here) makes
        # the channel unrecoverable: Telegram never replays updates and the Bot API
        # has no method to list the chats a bot belongs to. Park it instead, keyed
        # by the promoting telegram_user_id; _handle_start claims it on bind.
        # Only worth parking if the bot actually became an admin — a demotion for
        # an unknown user is nothing to remember.
        if new_status in ("administrator", "creator"):
            await storage.save_pending_channel(ctx, telegram_user_id, {
                "chat_id": chat_id,
                "chat_title": chat_obj.get("title", str(chat_id)),
                "chat_type": chat_obj.get("type", ""),
                "can_post": tg.derive_can_post(chat_obj.get("type", ""), new_member),
                "linked_at": tg.now_iso(),
                "chat_username": chat_obj.get("username", ""),
            })
        return

    if new_status not in ("administrator", "creator"):
        await storage.mark_channel_disconnected_for_user(ctx, imperal_id, chat_id)
        await _emit_for_user(ctx, imperal_id, "telegram-publisher-extension.channel_disconnected", {
            "imperal_id": imperal_id, "chat_id": chat_id,
        })
        return

    # Derive via the shared helper rather than reading can_post_messages here:
    # that right only exists on CHANNEL admin records, so a bare .get() would
    # store can_post=False for an admin bot in a group/supergroup and make
    # post_to_channel refuse a chat it can genuinely publish to.
    can_post = tg.derive_can_post(chat_obj.get("type", ""), new_member)
    await storage.save_channel_record_for_user(ctx, imperal_id, {
        "chat_id": chat_id,
        "chat_title": chat_obj.get("title", str(chat_id)),
        "chat_type": chat_obj.get("type", ""),
        "can_post": can_post,
        "linked_at": tg.now_iso(),
        # Persist the public @username so get_channel_recent_posts can reach
        # the t.me/s/ preview page — handlers_read reads chat_username off this
        # record, and auto-discovery previously never wrote it (only the manual
        # link path did), leaving tone-sampling broken for auto-linked channels.
        "chat_username": chat_obj.get("username", ""),
    })

    # This handler runs entirely under the webhook's own pseudo-identity
    # (ctx.user.imperal_id == "__webhook__" — my_chat_member updates arrive
    # unauthenticated), so a plain ctx.extensions.emit would publish under
    # "__webhook__", a session the real user's own open sidebar panel is
    # never subscribed to — its refresh="on_event:..." would just never
    # fire. Emit through an ExtensionsClient rescoped to the resolved real
    # imperal_id instead (same rescoping trick storage._store_for already
    # uses), so a channel newly added while the panel is open shows up
    # without the user having to manually reopen it.
    await _emit_for_user(ctx, imperal_id, "telegram-publisher-extension.channel_connected", {
        "imperal_id": imperal_id, "chat_id": chat_id,
        "chat_title": chat_obj.get("title", str(chat_id)), "can_post": can_post,
    })


async def _emit_for_user(ctx, imperal_id: str, event: str, payload: dict) -> None:
    """Best-effort event emit, rescoped to the real user — never let a panel-refresh
    signal failure break the actual webhook processing it's attached to."""
    try:
        await storage._extensions_for(ctx, imperal_id).emit(event, payload)
    except Exception as e:
        log.warning("emit failed (non-fatal): %s", e)


@ext.webhook("telegram_updates", method="POST", secret_header="X-Telegram-Bot-Api-Secret-Token")
async def telegram_updates(ctx, headers: dict, body: str, query_params: dict) -> dict:
    """Single ingress point for every Telegram Update kind (message, my_chat_member,
    channel_post — see handlers_read.py's _ingest_channel_post for that branch, kept
    there to keep this file scoped to identity/connect concerns).

    Unauthenticated per @ext.webhook's contract (ctx.user.imperal_id == "__webhook__").
    Trust boundary is the secret_token Telegram echoes back verbatim on every
    delivery (set once via setWebhook, compared constant-time below) — NOT an
    HMAC-over-body signature, because Telegram's webhook contract doesn't offer one.
    """
    import json
    import secrets as _secrets_cmp

    expected_secret = await ctx.secrets.get("telegram_webhook_secret")
    got_secret = headers.get("x-telegram-bot-api-secret-token", "") or headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not expected_secret or not _secrets_cmp.compare_digest(expected_secret, got_secret):
        log.warning("telegram_updates: secret token mismatch, dropping delivery")
        return {"status": 401, "body": "invalid secret token"}

    try:
        update = json.loads(body)
    except Exception:
        return {"status": 400, "body": "invalid JSON"}

    message = update.get("message")
    if message and message.get("text", "").startswith("/start"):
        await _handle_start(ctx, message)
        return {"status": 200, "body": "ok"}

    if "my_chat_member" in update:
        await _handle_my_chat_member(ctx, update)
        return {"status": 200, "body": "ok"}

    if "channel_post" in update:
        from handlers_read import _ingest_channel_post
        await _ingest_channel_post(ctx, update["channel_post"])
        return {"status": 200, "body": "ok"}

    return {"status": 200, "body": "ignored"}
