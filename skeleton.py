"""telegram-publisher · Skeleton tools.

The section is ambient context: the kernel refreshes it on a timer for every
user, whether or not the conversation is about Telegram. That budget decides
what may go in here — cheap store reads only.

So the recent-posts sample is READ from the digest that analyze_channel_posts
wrote, never scraped here. Fetching t.me from this path would put several
network requests on a hot loop per user, and a slow or failing fetch would
degrade the ambient snapshot instead of one explicit call. Batch work writes
the cache; ambient context reads it.
"""
import logging

from app import ext
import storage

log = logging.getLogger("telegram-publisher")

# Budgets below are the kernel's, not ours. The classifier envelope
# (imperal_kernel/hub/classifier/skeleton_summary.py) renders a skeleton
# section through hard caps: ~110 chars per list item, ~700 chars per whole
# list value, 10 items max, 6 scalar fields per item. Anything past that is
# silently dropped or truncated, so the sample is sized to ARRIVE INTACT
# rather than to look generous here:
#
#   6 previews x ~105 rendered chars ≈ 630 < 700  -> all six arrive
#   10 previews x ~105               ≈ 1050 > 700 -> cut off mid-list
#
# Hence six, not ten. A style hint that reaches the classifier beats a longer
# one that gets chopped. The full digest stays in the store; deeper reads go
# through the explicit tools.
_SKELETON_RECENT = 6
_PREVIEW_CHARS = 72   # leaves room for the ` channel=<name>` field inside 110
_CHANNEL_NAME_CHARS = 24
_SKELETON_WORDS = 3   # see the per-item budget note below


def _one_line(text: str, cap: int) -> str:
    """Collapse a post preview to a single capped line.

    Posts are multi-line by nature (headline, blank line, body). The classifier
    envelope is a one-line-per-section format, so an embedded newline splits a
    value across lines and corrupts the block. Whitespace is collapsed before
    the cap is applied, or the cap could spend its budget on invisible padding.
    """
    return " ".join((text or "").split())[:cap]


@ext.skeleton(
    "channels_overview",
    alert=True,
    # 60s, matching the platform's own mail section. The old 300s was copied
    # without thinking: this snapshot changes as a RESULT of our own tools
    # (a post published, a photo staged, a channel analysed), and the SDK's
    # SkeletonClient is read-only — there is no ctx.skeleton.invalidate() a
    # handler can call after a write. So the only thing closing the staleness
    # window is the tick, and a 5-minute window meant the assistant could
    # reason from a channel state five minutes out of date.
    #
    # Not lower than this on purpose: the platform's tick interval is derived
    # from the MINIMUM ttl across all of a user's sections (floor 15s), so an
    # aggressive value here speeds up every other extension's refresh too.
    ttl=60,
    description=(
        "Linked Telegram channels — id, title, can_post, plus each channel's cached "
        "post-style digest (posts scanned, avg_chars, recurring words, full_scan) — a "
        "sample of recent post previews, and whether a photo is already attached for "
        "the next post."
    ),
)
async def channels_overview(ctx):
    """Ambient context for the intent classifier: linked channels + staged photo.

    The staged-photo flags are here for a specific failure this fixed: with no
    ambient signal that a photo was already attached, the only honest thing an
    assistant could conclude from "use the photo I attached" was that it must
    somehow read the file — which no extension can do, since nothing in the
    SDK's Context carries chat attachments. So it refused a request that was
    in fact ready to run: the bytes were already on Telegram's side and
    post_to_channel picks them up by file_id on its own.

    Hence a flag, not the file: `photo_attached` says a photo is staged and
    will ride along automatically, and `photo_caption_limit` states the cap
    that comes with it (a photo post is a captioned post — 1024 characters,
    not 4096), so drafting targets the right length from the start instead of
    being rejected afterwards.
    """
    try:
        rows = await storage.list_channel_records(ctx)
        digests = {str(d.get("chat_id")): d for d in await storage.list_post_digests(ctx)}
        channels = []
        recent_posts = []
        for r in rows:
            cid = str(r["chat_id"])
            title = r.get("chat_title", cid)
            entry = {"id": cid, "title": title,
                     "can_post": r.get("can_post", False)}
            d = digests.get(cid)
            if d:
                # Cached by analyze_channel_posts — never fetched here. This is
                # what lets "write like my channel" work without a scrape in
                # the ambient path: the style signal is already on disk.
                #
                # THREE constraints shape the lines below, all of them the
                # renderer's, all three learned the hard way by rendering this
                # very snapshot through the kernel's own code:
                #
                # 1. SCALARS ONLY. _render_dict_item skips nested dict/list
                #    values outright ("to keep items flat and cheap"), so a
                #    list nested in a channel entry is not truncated — it
                #    VANISHES before the classifier sees it. The first version
                #    of this section nested recurring_words and recent_posts
                #    inside each channel, and neither ever reached the brain.
                #    Hence a joined string here, previews hoisted to their own
                #    top-level list below.
                # 2. NAME LENGTH IS BUDGET. Each item is truncated at ~110
                #    chars, and a key name spends that budget exactly like a
                #    value. With `posts_analysed` / `typical_post_chars` /
                #    `whole_history_scanned`, the title plus the long numeric
                #    chat id already ate ~108 chars and the style hint was cut
                #    off. Short names keep the whole item within one render.
                # 3. ORDER IS PRIORITY. Only the first 6 scalar fields render,
                #    so cheap flags go first and the variable-length string
                #    last, where losing its tail costs least.
                entry["posts"] = d.get("posts_scanned", 0)
                entry["avg_chars"] = d.get("median_length", 0)
                entry["full_scan"] = d.get("reached_start", False)
                entry["words"] = ", ".join(
                    (d.get("top_words") or [])[:_SKELETON_WORDS])

                # A top-level list of dicts DOES expand — each item renders its
                # label (a `title` key is one of the label keys the kernel looks
                # for) plus its scalar fields.
                for text in (d.get("recent_previews") or [])[-_SKELETON_RECENT:]:
                    recent_posts.append({
                        "title": _one_line(text, _PREVIEW_CHARS),
                        "channel": _one_line(title, _CHANNEL_NAME_CHARS),
                    })
            channels.append(entry)
        staged = await storage.get_staged_photo(ctx)
        snapshot = {
            "channels_linked": len(channels),
            "channels": channels,
            "recent_posts": recent_posts[-_SKELETON_RECENT:],
            "photo_attached": bool(staged and staged.get("file_id")),
        }
        if snapshot["photo_attached"]:
            snapshot["photo_name"] = staged.get("name") or "photo"
            snapshot["photo_caption_limit"] = 1024
        return {"response": snapshot}
    except Exception as e:
        log.error("skeleton refresh failed: %s", e)
        return {"response": {"channels_linked": 0, "channels": [],
                             "recent_posts": [], "photo_attached": False}}


@ext.tool(
    "skeleton_alert_channels_overview",
    description="Alert on channels linked or unlinked.",
)
async def skeleton_alert_channels_overview(
    ctx,
    old: dict | None = None,
    new: dict | None = None,
) -> dict:
    """Called by platform when channels_overview snapshot changes between ticks."""
    if not old or not new:
        return {"response": ""}

    old_ids = {c["id"] for c in old.get("channels", [])}
    new_ids = {c["id"] for c in new.get("channels", [])}
    added = new_ids - old_ids
    removed = old_ids - new_ids

    if not added and not removed:
        return {"response": ""}

    parts = []
    if added:
        parts.append(f"{len(added)} channel{'s' if len(added) > 1 else ''} linked")
    if removed:
        parts.append(f"{len(removed)} channel{'s' if len(removed) > 1 else ''} unlinked")

    return {"response": " and ".join(parts)}
