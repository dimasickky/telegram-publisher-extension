"""telegram-publisher · Skeleton tools."""
import logging

from app import ext
import storage

log = logging.getLogger("telegram-publisher")


@ext.skeleton(
    "channels_overview",
    alert=True,
    ttl=300,
    description=(
        "Linked Telegram channels — id, title, can_post per channel — plus whether a photo "
        "is already attached and waiting for the next post."
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
        channels = [
            {"id": str(r["chat_id"]), "title": r.get("chat_title", str(r["chat_id"])),
             "can_post": r.get("can_post", False)}
            for r in rows
        ]
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
