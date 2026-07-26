"""telegram-publisher · batch analysis of a channel's whole post history.

Why this file exists — the shape of the problem it solves:

Reading posts and *analysing* posts want opposite things. `get_channel_recent
_posts` answers "show me the latest few" and must return inside one chat turn,
so it is bounded and synchronous. Understanding a channel means walking its
whole history, which is unbounded work: hundreds of posts across dozens of
paginated fetches, far past the 180s a normal tool call gets.

Three moving parts, deliberately separated:

1. `_scan_history()` — the batch walk. Pages backwards through the channel's
   public preview via the `before` cursor, accumulating posts. This is the
   part that was missing: everything here used to read exactly one page (20
   posts) and slice it, so "analyse the channel" silently meant "look at the
   most recent 20".

2. `analyze_channel_posts` — the tool. Spawns the walk via
   `ctx.background_task(long_running=True)` (1800s instead of 180s) when the
   requested depth cannot safely fit in a single turn, and runs inline when it
   can. The kernel delivers the finished result to chat by itself, so a deep
   scan does not block the conversation.

3. The digest it writes (`storage.save_post_digest`) — what the skeleton later
   reads. The skeleton NEVER scrapes: see the comment in storage.py, a network
   fetch on an ambient timer would be a slow-loop hazard for every user. Batch
   work writes the cache; ambient context reads it.

The public-preview limitation from handlers_read.py applies unchanged: a
channel with no @username has no t.me/s/ page, and the Bot API has no
history-fetch method at all, so there is nothing to page through for private
channels.
"""
import logging
import re
from collections import Counter
from datetime import datetime, timezone

from imperal_sdk import ActionResult

from app import chat
from models import AnalyzePostsParams, ChannelDigest
from error_codes import (
    TG_CHANNEL_NOT_FOUND,
    TG_NO_PUBLIC_PREVIEW,
    TG_ANALYSIS_EMPTY,
)
from handlers_read import _fetch_page, _PAGE_SIZE
import storage

log = logging.getLogger("telegram-publisher")

# How many recent posts the digest keeps verbatim (previews only, trimmed).
# The skeleton shows these, so it is a "what does this channel sound like"
# sample, not storage — hence a small number and a hard per-item trim.
_DIGEST_RECENT = 10
_PREVIEW_CHARS = 160

# Words too common to characterise a channel. Kept tiny and mechanical on
# purpose: this is a cheap frequency hint for drafting, not NLP.
_STOPWORDS = {
    # EN
    "the", "and", "for", "with", "that", "this", "from", "have", "has", "are",
    "was", "were", "you", "your", "our", "not", "but", "all", "can", "will",
    "its", "it's", "they", "their", "what", "when", "how", "why", "who",
    "more", "than", "then", "them", "there", "here", "into", "out", "about",
    "just", "also", "been", "being", "over", "only", "any", "one", "two",
    # RU
    "и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а", "то",
    "все", "она", "так", "его", "но", "да", "ты", "к", "у", "же", "вы", "за",
    "бы", "по", "только", "ее", "мне", "было", "вот", "от", "меня", "еще",
    "нет", "о", "из", "ему", "теперь", "когда", "даже", "ну", "вдруг", "ли",
    "если", "уже", "или", "ни", "быть", "был", "него", "до", "вас", "нибудь",
    "опять", "уж", "вам", "ведь", "там", "потом", "себя", "ничего", "ей",
    "может", "они", "тут", "где", "есть", "надо", "ней", "для", "мы", "тебя",
    "их", "чем", "была", "сам", "чтоб", "без", "будто", "чего", "раз", "тоже",
    "себе", "под", "будет", "ж", "тогда", "кто", "этот", "того", "потому",
    "этого", "какой", "совсем", "ним", "здесь", "этом", "один", "почти",
    "мой", "тем", "чтобы", "нее", "были", "куда", "зачем", "всех", "никогда",
    "можно", "при", "наконец", "два", "об", "другой", "хоть", "после", "над",
    "больше", "тот", "через", "эти", "нас", "про", "всего", "них", "какая",
    "много", "разве", "три", "эту", "моя", "впрочем", "хорошо", "свою",
    "этой", "перед", "иногда", "лучше", "чуть", "том", "нельзя", "такой",
    "им", "более", "всегда", "конечно", "всю", "между",
}

_WORD_RE = re.compile(r"[\w']{3,}", re.UNICODE)


def _median(values: list[int]) -> int:
    """Median without pulling in statistics — values is already small."""
    if not values:
        return 0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) // 2


def _top_words(texts: list[str], n: int = 12) -> list[str]:
    """Most frequent non-trivial words across the scanned posts.

    A frequency count is a blunt instrument, and that is the point: it is here
    to hint at recurring subject matter for drafting, cheaply and with no model
    call. Anything smarter belongs in generate_draft, which already has ctx.ai.
    """
    counter: Counter[str] = Counter()
    for text in texts:
        for raw in _WORD_RE.findall(text.lower()):
            if raw not in _STOPWORDS and not raw.isdigit():
                counter[raw] += 1
    return [word for word, _ in counter.most_common(n)]


async def _scan_history(ctx, username: str, max_posts: int) -> tuple[list[tuple[int, str]], int, bool]:
    """Page backwards through a channel's public preview, oldest cursor first.

    Returns (pairs, pages_fetched, reached_start) where `pairs` is
    (message_id, text) for every post seen, newest last.

    The walk stops on whichever comes first: the requested depth, a page that
    yields nothing new, or the start of the channel. Two independent guards
    keep it finite — the cursor must strictly decrease (a page that fails to
    move it means the preview is behaving unexpectedly, so stop rather than
    re-fetch the same page forever), and a page budget derived from max_posts
    caps the total number of requests regardless.
    """
    collected: list[tuple[int, str]] = []
    seen_ids: set[int] = set()
    cursor: int | None = None
    pages = 0
    reached_start = False
    page_budget = max(1, (max_posts + _PAGE_SIZE - 1) // _PAGE_SIZE) + 2

    while pages < page_budget and len(collected) < max_posts:
        pairs = await _fetch_page(ctx, username, before=cursor)
        pages += 1
        if not pairs:
            reached_start = True
            break

        fresh = [(mid, text) for mid, text in pairs if mid not in seen_ids]
        if not fresh:
            break
        seen_ids.update(mid for mid, _ in fresh)
        collected = fresh + collected

        oldest = min(mid for mid, _ in fresh)
        if oldest <= 1:
            reached_start = True
            break
        if cursor is not None and oldest >= cursor:
            # The cursor did not move back — stop instead of looping.
            break
        cursor = oldest

        # Progress is advisory: ctx.progress may raise TaskCancelled if the
        # user cancelled the background task, which should end the scan
        # cleanly rather than surface as a failure.
        try:
            done = min(len(collected), max_posts)
            await ctx.progress(
                min(0.95, done / max_posts),
                f"Scanned {len(collected)} posts across {pages} page(s)…",
            )
        except Exception:
            break

    return collected[-max_posts:], pages, reached_start


def _build_digest(record: dict, pairs: list[tuple[int, str]], pages: int,
                  reached_start: bool) -> dict:
    """Turn a raw scan into the small cached record the skeleton can read."""
    texts = [t for _, t in pairs if t]
    lengths = [len(t) for t in texts]
    ids = [mid for mid, _ in pairs]

    return {
        "chat_id": record.get("chat_id"),
        "channel_title": record.get("chat_title", str(record.get("chat_id"))),
        "posts_scanned": len(pairs),
        "pages_fetched": pages,
        "with_text": len(texts),
        "avg_length": (sum(lengths) // len(lengths)) if lengths else 0,
        "median_length": _median(lengths),
        "longest": max(lengths) if lengths else 0,
        "shortest": min(lengths) if lengths else 0,
        "newest_message_id": max(ids) if ids else 0,
        "oldest_message_id": min(ids) if ids else 0,
        "top_words": _top_words(texts),
        "recent_previews": [t[:_PREVIEW_CHARS] for t in texts[-_DIGEST_RECENT:]],
        "reached_start": reached_start,
        "analysed_at": datetime.now(timezone.utc).isoformat(),
    }


def _summary_for(digest: dict) -> str:
    depth = (
        "the whole channel" if digest["reached_start"]
        else f"the last {digest['posts_scanned']} posts"
    )
    words = ", ".join(digest["top_words"][:6]) or "—"
    return (
        f"Analysed {depth} of \u201c{digest['channel_title']}\u201d: "
        f"{digest['posts_scanned']} posts over {digest['pages_fetched']} page(s), "
        f"{digest['with_text']} with text. "
        f"Typical length {digest['median_length']} chars (avg {digest['avg_length']}, "
        f"range {digest['shortest']}\u2013{digest['longest']}). "
        f"Recurring words: {words}."
    )


async def _run_analysis(ctx, record: dict, username: str, max_posts: int) -> ActionResult:
    """The actual work — shared by the inline and background paths."""
    try:
        pairs, pages, reached_start = await _scan_history(ctx, username, max_posts)
    except Exception as e:
        log.error("analyze_channel_posts: scan failed: %s", e)
        return ActionResult.error(
            "Could not read the channel's public preview — try again shortly.",
            retryable=True, code=TG_NO_PUBLIC_PREVIEW,
        )

    if not pairs:
        return ActionResult.error(
            "No parsable posts found on the channel's public preview page.",
            retryable=False, code=TG_ANALYSIS_EMPTY,
        )

    digest = _build_digest(record, pairs, pages, reached_start)
    try:
        await storage.save_post_digest(ctx, digest)
    except Exception as e:
        # The analysis itself succeeded; failing to cache it must not present
        # as a failed analysis. It only means the skeleton keeps the old view.
        log.warning("analyze_channel_posts: analysed but could not cache digest: %s", e)

    return ActionResult.success(
        ChannelDigest(
            id=str(digest["chat_id"]),
            title=digest["channel_title"],
            kind="telegram_channel_digest",
            channel_id=str(digest["chat_id"]),
            channel_title=digest["channel_title"],
            posts_scanned=digest["posts_scanned"],
            pages_fetched=digest["pages_fetched"],
            with_text=digest["with_text"],
            avg_length=digest["avg_length"],
            median_length=digest["median_length"],
            longest=digest["longest"],
            shortest=digest["shortest"],
            newest_message_id=digest["newest_message_id"],
            oldest_message_id=digest["oldest_message_id"],
            top_words=digest["top_words"],
            recent_previews=digest["recent_previews"],
            reached_start=digest["reached_start"],
            analysed_at=digest["analysed_at"],
        ),
        summary=_summary_for(digest),
        refresh_panels=["sidebar"],
    )


@chat.function(
    "analyze_channel_posts",
    action_type="read",
    description=(
        "Analyse a linked PUBLIC channel's post history in batches — walks back through "
        "the channel's posts page by page (not just the latest 20), then caches a digest: "
        "how many posts, typical/median post length, longest and shortest, recurring words, "
        "and the most recent post previews. Use when the user asks to analyse a channel, "
        "study its style, or asks how long its posts usually are. A deep scan runs in the "
        "background and reports back on its own. The cached digest is what makes the "
        "channel's style available as ambient context afterwards."
    ),
    data_model=ChannelDigest,
    event="telegram-publisher-extension.posts_analyzed",
)
async def analyze_channel_posts(ctx, params: AnalyzePostsParams) -> ActionResult:
    """Scan a channel's history in pages and cache the resulting digest.

    Depth decides the execution mode. A shallow scan (a couple of pages) is
    quick enough to answer inside the turn, and answering immediately is
    better than a background hand-off the user has to wait for anyway. A deep
    scan is spawned via ctx.background_task(long_running=True), because paging
    through hundreds of posts cannot be promised inside the 180s a normal tool
    call gets — and a timeout mid-walk would waste every fetch already made.
    """
    record = await storage.get_channel_record(ctx, params.channel_id)
    if not record:
        return ActionResult.error(
            "That channel isn't linked — check list_telegram_channels for the right channel_id.",
            retryable=False, code=TG_CHANNEL_NOT_FOUND,
        )

    username = (record.get("chat_username") or "").lstrip("@")
    if not username:
        return ActionResult.error(
            "This channel has no public @username, so it has no t.me/s/ preview page to scan. "
            "Telegram's Bot API has no history-fetch method, so there is no other way to read "
            "past posts of a private channel.",
            retryable=False, code=TG_NO_PUBLIC_PREVIEW,
        )

    # One page is ~20 posts and one HTTP fetch; two pages stay comfortably
    # inside a turn. Beyond that, hand off rather than risk the federal cap.
    inline = params.max_posts <= _PAGE_SIZE * 2
    if inline:
        return await _run_analysis(ctx, record, username, params.max_posts)

    try:
        await ctx.background_task(
            _run_analysis(ctx, record, username, params.max_posts),
            long_running=True,
            name=f"analyze {record.get('chat_title', username)}",
        )
    except RuntimeError:
        # No kernel spawn hook (dev mode / test harness): the work is still
        # worth doing, just synchronously. Better a slow answer than none.
        log.info("analyze_channel_posts: no background hook, running inline")
        return await _run_analysis(ctx, record, username, params.max_posts)

    return ActionResult.success(
        summary=(
            f"Scanning up to {params.max_posts} posts of "
            f"\u201c{record.get('chat_title', username)}\u201d in the background — "
            "I'll report back with the analysis when the walk finishes."
        ),
    )
