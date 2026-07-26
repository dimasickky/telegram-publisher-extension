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

# How many cached post previews to surface per channel. Small on purpose: this
# is a style hint for the intent classifier, not the archive itself — the full
# digest stays in the store and deeper reads go through the explicit tools.
_SKELETON_RECENT = 10


@ext.skeleton(
    "channels_overview",
    alert=True,
    ttl=300,
    description=(
        "Linked Telegram channels — id, title, can_post per channel — plus each channel's "
        "cached post-style digest (typical length, recurring words, last 10 post previews) "
        "and whether a photo is already attached and waiting for the next post."
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
        for r in rows:
            cid = str(r["chat_id"])
            entry = {"id": cid, "title": r.get("chat_title", cid),
                     "can_post": r.get("can_post", False)}
            d = digests.get(cid)
            if d:
                # Cached by analyze_channel_posts — never fetched here. This is
                # what lets "write like my channel" work without a scrape in
                # the ambient path: the style signal is already on disk.
                entry["posts_analysed"] = d.get("posts_scanned", 0)
                entry["typical_post_chars"] = d.get("median_length", 0)
                entry["recurring_words"] = (d.get("top_words") or [])[:8]
                entry["recent_posts"] = (d.get("recent_previews") or [])[-_SKELETON_RECENT:]
                entry["whole_history_scanned"] = d.get("reached_start", False)
            channels.append(entry)
        staged = await storage.get_staged_photo(ctx)
        snapshot = {
            "channels_linked": len(channels),
            "channels": channels,
            "photo_attached": bool(staged and staged.get("file_id")),
        }
        if snapshot["photo_attached"]:
            snapshot["photo_name"] = staged.get("name") or "photo"
            snapshot["photo_caption_limit"] = 1024
        return {"response": snapshot}
    except Exception as e:
        log.error("skeleton refresh failed: %s", e)
        return {"response": {"channels_linked": 0, "channels": [], "photo_attached": False}}


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
