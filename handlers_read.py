"""telegram-connector · reading recent posts from a linked channel.

Channel listing + connect status live in handlers_connect.py (identity/
connect concerns, alongside connect_telegram); disconnecting a channel lives
in handlers_publish.py (disconnect_telegram_channel, next to post_to_channel
since both act on an already-linked channel record) — this file only covers
reading a channel's recent public posts.

Two distinct data sources for "recent posts", both real limits noted in
extensions/telegram-connector.md §3/§9:

1. `t.me/s/<username>` — Telegram's own public web preview of a channel.
   Works WITHOUT any bot/token for any PUBLIC channel (one with a username),
   and returns real history (not just from-now-on). No official JSON API —
   this is an HTML page we parse; it is explicitly the "backfill" path, not
   the live one. Private channels (no @username) have no such page at all.
2. Nothing else. Bot API itself has NO getHistory-equivalent method — this
   is a hard protocol limit (see extensions/telegram-connector.md §3), not
   something worth working around with retries or pagination tricks. Posts
   made AFTER the bot was added as admin arrive live via the `channel_post`
   webhook update; `_ingest_channel_post` (called from handlers_connect.py's
   webhook dispatcher) is the hook where a forward-only live archive would
   be built — not implemented in this pass (v2 idea, noted in the spec).
"""
import html
import logging
import re

from imperal_sdk import ActionResult, sdl

from app import chat
from models import ChannelIdParams, GetRecentPostsParams, TelegramPost
from error_codes import TG_CHANNEL_NOT_FOUND
import storage

log = logging.getLogger("telegram-connector")

_POST_BLOCK_RE = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.DOTALL
)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(fragment: str) -> str:
    """HTML-unescape then strip tags — good enough for the widget's own
    simple <br>/<a> markup; not a general HTML parser."""
    text = fragment.replace("<br>", "\n").replace("<br/>", "\n")
    text = _TAG_RE.sub("", text)
    return html.unescape(text).strip()


@chat.function(
    "get_channel_recent_posts",
    action_type="read",
    description=(
        "Read recent posts from a linked PUBLIC channel via its public t.me/s/ preview page. "
        "Only works for channels with a public @username — private channels have no such page "
        "(Telegram's Bot API has no history-fetch method at all, this is the only backfill path)."
    ),
    data_model=sdl.EntityList[TelegramPost],
)
async def get_channel_recent_posts(ctx, params: GetRecentPostsParams) -> ActionResult:
    """Fetch and parse https://t.me/s/<username> for a channel's recent public posts."""
    record = await storage.get_channel_record(ctx, params.channel_id)
    if not record:
        return ActionResult.error("Channel not found — check list_telegram_channels first.",
                                  retryable=False, code=TG_CHANNEL_NOT_FOUND)

    username = (record.get("chat_username") or "").lstrip("@")
    if not username:
        return ActionResult.success(
            sdl.EntityList[TelegramPost](items=[]),
            summary=(
                "This channel has no public @username, so it has no t.me/s/ preview page. "
                "Only posts made after linking could ever be visible here, and that live-archive "
                "ingest isn't built yet."
            ),
        )

    resp = await ctx.http.get(f"https://t.me/s/{username}")
    body = resp.body if hasattr(resp, "body") else str(resp)
    if not isinstance(body, str):
        return ActionResult.success(sdl.EntityList[TelegramPost](items=[]),
                                    summary="Unexpected response reading the public preview page.")

    blocks = _POST_BLOCK_RE.findall(body)[-params.limit:]
    posts = [
        TelegramPost(id=str(i), title=_strip_tags(b)[:80] or "(untitled)", kind="telegram_post",
                    text=_strip_tags(b))
        for i, b in enumerate(blocks)
    ]
    return ActionResult.success(
        sdl.EntityList[TelegramPost](items=posts),
        summary=f"{len(posts)} recent post(s) from the public preview page (source: t.me/s/{username}).",
    )


async def _ingest_channel_post(ctx, channel_post: dict) -> None:
    """Hook for a future forward-only live archive of posts made after linking.
    Not implemented in v1 — channel_post updates are currently acknowledged
    and dropped (see handlers_connect.py's telegram_updates dispatcher)."""
    return None
