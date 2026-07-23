"""telegram-publisher · publishing to a linked channel.

Straightforward Bot API sendMessage/sendPhoto — the only real design
decision here is the pre-flight check: `can_post` on the stored tg_channels
record (set from my_chat_member's `can_post_messages` flag, see
handlers_connect.py) is checked BEFORE calling Telegram, not just relying on
Telegram's own 403 if it's missing. Two reasons this matters, not just
belt-and-suspenders:
  1. A clearer error message — Telegram's own 403 for "bot lacks
     can_post_messages" reads identically to "bot isn't a member at all",
     which would confuse a user who thinks they already added the bot.
  2. The channel-admin-grants-partial-rights case flagged during the
     original research (extensions/telegram-publisher.md §7 step 4) — admin
     rights are granular, "is admin" != "can post" is a real, distinct
     state Telegram allows.
"""
import logging

from imperal_sdk import ActionResult

from app import chat
from models import PostToChannelParams, ChannelIdParams, PostResult, DisconnectResult
from error_codes import TG_CHANNEL_NOT_FOUND, TG_BOT_CANNOT_POST, TG_SEND_FAILED, TG_BOT_UNREACHABLE
import storage
import telegram_client as tg

log = logging.getLogger("telegram-publisher")


@chat.function(
    "post_to_channel",
    action_type="write",
    description=(
        "Publish a post to one of your linked Telegram channels. Optionally attach a photo "
        "by URL. Requires the bot to have 'Post messages' permission on that channel."
    ),
    effects=["telegram.post"],
    event="telegram-publisher-extension.post_published",
    data_model=PostResult,
)
async def post_to_channel(ctx, params: PostToChannelParams) -> ActionResult:
    """Send a text or photo post to a linked channel, after pre-flight can_post check."""
    record = await storage.get_channel_record(ctx, params.channel_id)
    if not record:
        return ActionResult.error(
            "That channel isn't linked — check list_telegram_channels for the right channel_id.",
            code=TG_CHANNEL_NOT_FOUND,
        )
    if not record.get("can_post", False):
        return ActionResult.error(
            f"The bot is admin on \"{record.get('chat_title', params.channel_id)}\" but doesn't have "
            "'Post messages' permission — grant it from Telegram's channel admin settings and try again.",
            code=TG_BOT_CANNOT_POST,
        )

    if len(params.text) > 4096:
        return ActionResult.error(
            "Post text is too long (Telegram's limit is 4096 characters) — shorten it and try again.",
            code="TG_MESSAGE_TOO_LONG",
        )

    try:
        if params.photo_url:
            resp = await tg.tg_call(ctx, "sendPhoto", {
                "chat_id": record["chat_id"], "photo": params.photo_url, "caption": params.text,
                "parse_mode": "HTML",
            })
        else:
            resp = await tg.tg_call(ctx, "sendMessage", {
                "chat_id": record["chat_id"], "text": params.text, "parse_mode": "HTML",
                "disable_web_page_preview": params.disable_preview,
            })
    except Exception as e:
        log.error("post_to_channel: transport error: %s", e)
        return ActionResult.error("Could not reach Telegram — try again shortly.", retryable=True, code=TG_BOT_UNREACHABLE)

    if not tg.tg_ok(resp):
        return ActionResult.error(tg.tg_error_message(resp), code=TG_SEND_FAILED)

    result = tg.tg_result(resp)
    message_id = result.get("message_id", 0)
    chat_username = result.get("chat", {}).get("username")
    link = f"https://t.me/{chat_username}/{message_id}" if chat_username else None

    return ActionResult.success(
        summary=f"Posted to \"{record.get('chat_title', params.channel_id)}\".",
        data=PostResult(
            id=str(message_id), title="Post", kind="telegram_post",
            channel_id=params.channel_id, message_id=message_id, link=link,
        ),
    )


@chat.function(
    "disconnect_telegram_channel",
    action_type="destructive",
    description=(
        "Unlink a Telegram channel from Imperal. Does NOT remove the bot from the channel on "
        "Telegram's side — do that manually if you also want the bot gone. Just makes Imperal "
        "forget about it."
    ),
    effects=["telegram.disconnect"],
    event="telegram-publisher-extension.channel_disconnected",
    data_model=DisconnectResult,
)
async def disconnect_telegram_channel(ctx, params: ChannelIdParams) -> ActionResult:
    """Delete the stored tg_channels record for this channel."""
    deleted = await storage.delete_channel_record(ctx, params.channel_id)
    if not deleted:
        return ActionResult.error("That channel isn't linked — nothing to disconnect.", code=TG_CHANNEL_NOT_FOUND)
    return ActionResult.success(
        summary="Channel unlinked from Imperal.",
        data=DisconnectResult(id=params.channel_id, title="Disconnected", kind="telegram_channel",
                               channel_id=params.channel_id, disconnected=True),
        refresh_panels=["sidebar"],
    )
