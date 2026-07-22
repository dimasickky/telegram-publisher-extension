"""telegram-connector · Skeleton tools."""
import logging

from app import ext
import storage

log = logging.getLogger("telegram-connector")


@ext.skeleton(
    "channels_overview",
    alert=True,
    ttl=300,
    description="Linked Telegram channels — id, title, can_post per channel.",
)
async def channels_overview(ctx):
    """Ambient context for the intent classifier: linked Telegram channels."""
    try:
        rows = await storage.list_channel_records(ctx)
        channels = [
            {"id": str(r["chat_id"]), "title": r.get("chat_title", str(r["chat_id"])),
             "can_post": r.get("can_post", False)}
            for r in rows
        ]
        return {"response": {"channels_linked": len(channels), "channels": channels}}
    except Exception as e:
        log.error("skeleton refresh failed: %s", e)
        return {"response": {"channels_linked": 0, "channels": []}}


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
