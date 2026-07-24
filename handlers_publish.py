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

from imperal_sdk import ActionResult, ui

from app import chat
from models import PostToChannelParams, ChannelIdParams, PostResult, DisconnectResult
from error_codes import TG_CHANNEL_NOT_FOUND, TG_BOT_CANNOT_POST, TG_SEND_FAILED, TG_BOT_UNREACHABLE
import storage
import telegram_client as tg

log = logging.getLogger("telegram-publisher")


def _preview_ui(record: dict, params: PostToChannelParams):
    """Render exactly what post_to_channel would send, so the draft looks the
    same in chat as the real Telegram post would: same HTML subset, same
    photo (if any), plus the confirmation reminder itself."""
    chat_title = record.get("chat_title", params.channel_id)
    children = [
        ui.Stack(direction="h", gap=2, children=[
            ui.Icon("Send"),
            ui.Text(f"Draft \u2192 \"{chat_title}\""),
        ]),
    ]
    if params.photo_url:
        children.append(ui.Image(src=params.photo_url, alt="Post photo"))
    children.append(ui.Html(content=params.text or "<i>(empty)</i>", theme="dark"))
    children.append(ui.Alert(
        title="Not sent yet",
        message="This is a preview only — nothing has been posted to Telegram. "
                "Call post_to_channel again with confirm=true to actually publish it.",
        type="info",
    ))
    return ui.Stack(gap=3, children=children)


async def _dm_draft_to_user(ctx, record: dict, params: PostToChannelParams) -> None:
    """Best-effort: have the SAME bot that will actually publish the post also
    DM the linked Telegram user a plain-text draft of it and where it's
    headed — no inline buttons, just a message, since confirmation stays in
    chat with Webbee (a second post_to_channel call with confirm=true). This
    is purely so the draft is seen from inside the bot's own chat, not only
    as a card in the Imperal chat UI.

    Fire-and-forget by design: the in-chat preview (_preview_ui, already
    rendered by the caller) is the source of truth for the ActionResult — a
    failure to DM (user never linked telegram_user_id, blocked the bot,
    transport hiccup) must never turn a successful preview into an error, so
    any failure here is only logged, never raised or returned.
    """
    try:
        link = await storage.get_telegram_user_link(ctx)
        if not link or not link.get("telegram_user_id"):
            return  # user never linked their Telegram account to this bot — nothing to DM
        chat_title = record.get("chat_title", params.channel_id)
        lines = [f"\U0001F4DD Draft \u2014 will post to \u201c{chat_title}\u201d:", ""]
        lines.append(params.text or "(empty)")
        lines.append("")
        lines.append(
            "Not sent yet. Confirm back in chat with Webbee to actually publish it."
        )
        text = "\n".join(lines)
        if params.photo_url:
            await tg.tg_call(ctx, "sendPhoto", {
                "chat_id": link["telegram_user_id"], "photo": params.photo_url,
                "caption": text[:1024], "parse_mode": "HTML",
            })
        else:
            await tg.tg_call(ctx, "sendMessage", {
                "chat_id": link["telegram_user_id"], "text": text[:4096], "parse_mode": "HTML",
            })
    except Exception as e:
        log.warning("post_to_channel: could not DM draft to linked user: %s", e)


@chat.function(
    "post_to_channel",
    action_type="write",
    description=(
        "Publish a post to one of your linked Telegram channels. Optionally attach a photo "
        "by URL. Requires the bot to have 'Post messages' permission on that channel. "
        "First call (confirm=false, the default) only shows a draft preview of the post and "
        "where it will go — nothing is sent to Telegram until you call it again with confirm=true."
    ),
    effects=["telegram.post"],
    event="telegram-publisher-extension.post_published",
    data_model=PostResult,
)
async def post_to_channel(ctx, params: PostToChannelParams) -> ActionResult:
    """Send a text or photo post to a linked channel, after pre-flight can_post check.

    Own explicit two-step confirm-flow (same pattern as github-connector's
    merge_pull_request/delete_branch): the first call (confirm=false, the
    default) renders a draft preview — exactly what will be posted and to
    which channel — and never touches the Telegram API. Only a second call
    with confirm=true actually calls sendMessage/sendPhoto.
    """
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

    if not params.confirm:
        await ctx.log(
            f"post_to_channel: preview only (awaiting confirm) \u2014 channel '{params.channel_id}'",
            level="info",
        )
        await _dm_draft_to_user(ctx, record, params)
        return ActionResult.success(
            PostResult(
                id=params.channel_id, title="Draft", kind="telegram_post_draft",
                channel_id=params.channel_id, needs_confirmation=True,
            ),
            summary=(
                f"Draft ready for \"{record.get('chat_title', params.channel_id)}\" \u2014 nothing sent yet. "
                "Call again with confirm=true to publish it."
            ),
            ui=_preview_ui(record, params),
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
