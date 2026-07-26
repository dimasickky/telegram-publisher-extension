"""telegram-publisher · reading recent posts from a linked channel.

Channel listing + connect status live in handlers_connect.py (identity/
connect concerns, alongside connect_telegram); disconnecting a channel lives
in handlers_publish.py (disconnect_telegram_channel, next to post_to_channel
since both act on an already-linked channel record) — this file only covers
reading a channel's recent public posts.

Two distinct data sources for "recent posts", both real limits noted in
extensions/telegram-publisher.md §3/§9:

1. `t.me/s/<username>` — Telegram's own public web preview of a channel.
   Works WITHOUT any bot/token for any PUBLIC channel (one with a username),
   and returns real history (not just from-now-on). No official JSON API —
   this is an HTML page we parse; it is explicitly the "backfill" path, not
   the live one. Private channels (no @username) have no such page at all.
2. Nothing else. Bot API itself has NO getHistory-equivalent method — this
   is a hard protocol limit (see extensions/telegram-publisher.md §3), not
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

log = logging.getLogger("telegram-publisher")

_POST_BLOCK_RE = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.DOTALL
)
_TAG_RE = re.compile(r"<[^>]+>")

# One page of t.me/s/<user> serves 20 posts, each announced by a
# data-post="<channel>/<message_id>" attribute that appears BEFORE its text
# block — so scanning for (id, text) pairs in document order keeps them
# aligned without guessing. The oldest id on a page is the cursor for the
# next one: /s/<user>?before=<oldest_id> serves the 20 posts before it.
_POST_ID_RE = re.compile(r'data-post="[^/"]+/(\d+)"')
_PAGE_SIZE = 20


def _strip_tags(fragment: str) -> str:
    """HTML-unescape then strip tags — good enough for the widget's own
    simple <br>/<a> markup; not a general HTML parser."""
    text = fragment.replace("<br>", "\n").replace("<br/>", "\n")
    text = _TAG_RE.sub("", text)
    return html.unescape(text).strip()


def _parse_page(body: str) -> list[tuple[int, str]]:
    """Extract (message_id, text) pairs from one t.me/s/ page, in document order.

    The id attribute and the text block are matched by position rather than by
    nesting: both appear once per post and in the same order, so zipping the
    two scans keeps them aligned. Posts with no text at all (a bare photo, a
    poll) yield an id with an empty body and are dropped by the caller — they
    carry nothing to analyse, but their id still counts as seen, which is why
    the raw pairs are returned rather than only the texts.
    """
    ids = [int(m) for m in _POST_ID_RE.findall(body)]
    blocks = _POST_BLOCK_RE.findall(body)

    # Ids drive pagination, but they must not gate extraction: if Telegram ever
    # renames the data-post attribute, a page whose text blocks still parse
    # should keep yielding text (with id 0, which stops paging) rather than
    # silently reporting an empty channel. Text is the payload; the id is
    # navigation.
    if not ids:
        return [(0, _strip_tags(b)) for b in blocks]

    pairs: list[tuple[int, str]] = []
    for i, mid in enumerate(ids):
        text = _strip_tags(blocks[i]) if i < len(blocks) else ""
        pairs.append((mid, text))
    return pairs


async def _fetch_page(ctx, username: str, before: int | None = None) -> list[tuple[int, str]]:
    """Fetch ONE page of a channel's public preview (20 posts), oldest-first.

    `before` is the pagination cursor: the oldest message_id already seen.
    Telegram then serves the 20 posts published before it.
    """
    url = f"https://t.me/s/{username}"
    if before:
        url = f"{url}?before={before}"
    resp = await ctx.http.get(url)
    body = resp.body if hasattr(resp, "body") else str(resp)
    if not isinstance(body, str):
        return []
    return _parse_page(body)


async def _fetch_recent_post_texts(ctx, record: dict, limit: int) -> tuple[list[str], str]:
    """Recent post texts, newest-last — walking back through pages as needed.

    Previously this read a single page and sliced it, which silently capped
    everything at one page (20 posts) no matter what `limit` asked for. Now it
    pages backwards via the `before` cursor until it has `limit` texts or the
    channel runs out, so a limit above 20 means what it says.

    Returns (texts, reason_if_empty) — reason is "" when texts were found.
    """
    username = (record.get("chat_username") or "").lstrip("@")
    if not username:
        return [], (
            "This channel has no public @username, so it has no t.me/s/ preview page. "
            "Only posts made after linking could ever be visible here, and that live-archive "
            "ingest isn't built yet."
        )

    collected: list[tuple[int, str]] = []
    cursor: int | None = None
    # Bounded: each page must yield a strictly older cursor, and a channel with
    # fewer posts than `limit` simply stops early. The page budget is a
    # belt-and-braces guard against a malformed page pinning the cursor.
    max_pages = max(1, (limit + _PAGE_SIZE - 1) // _PAGE_SIZE) + 2
    for _ in range(max_pages):
        pairs = await _fetch_page(ctx, username, before=cursor)
        if not pairs:
            break
        collected = pairs + collected
        oldest = min(mid for mid, _ in pairs)
        if len([t for _, t in collected if t]) >= limit or oldest <= 1:
            break
        cursor = oldest

    texts = [t for _, t in collected if t][-limit:]
    if not texts:
        return [], "No parsable posts found on the public preview page."
    return texts, ""


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

    texts, reason = await _fetch_recent_post_texts(ctx, record, params.limit)
    if not texts:
        return ActionResult.success(sdl.EntityList[TelegramPost](items=[]), summary=reason)

    posts = [
        TelegramPost(id=str(i), title=t[:80] or "(untitled)", kind="telegram_post", text=t)
        for i, t in enumerate(texts)
    ]
    username = (record.get("chat_username") or "").lstrip("@")
    return ActionResult.success(
        sdl.EntityList[TelegramPost](items=posts),
        summary=f"{len(posts)} recent post(s) from the public preview page (source: t.me/s/{username}).",
    )


async def _ingest_channel_post(ctx, channel_post: dict) -> None:
    """Hook for a future forward-only live archive of posts made after linking.
    Not implemented in v1 — channel_post updates are currently acknowledged
    and dropped (see handlers_connect.py's telegram_updates dispatcher)."""
    return None
