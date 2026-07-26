"""telegram-publisher · staging a photo for the next post.

WHY THIS EXISTS
A file the user attaches lives inside Imperal and has no public URL, so
`sendPhoto` cannot fetch it the way it fetches `photo_url`. Telegram's Bot
API takes a file three ways — public URL, previously-returned `file_id`, or
raw bytes as multipart — and only the last two work here.

So the upload does both in one move: it POSTs the bytes (multipart, via
telegram_client.tg_call_upload) to the *user's own DM with the bot*, and
keeps the `file_id` Telegram returns. That single call buys three things:

  1. A durable handle. `file_id` is Telegram's own pointer to the stored
     file — reusable in any chat this bot can reach, for as long as the file
     exists on their servers. Imperal stores the pointer, never the bytes.
  2. A real preview. The photo lands in the author's Telegram immediately,
     so they see the actual image in the same client that will publish it —
     no guessing whether the right file got attached.
  3. No token leak. The alternative (getFile) yields a URL of the shape
     api.telegram.org/file/bot<TOKEN>/<path> — it embeds the bot token, and
     the token is an app-scope secret shared by every user of this
     extension. Putting that in a panel <img src> would publish it to the
     browser. file_id carries no credential, so it is the only form safe to
     store and to render around.

WHY A SINGLE SLOT
`tg_staged_photo` holds at most one document per user (see storage.py). "The
photo I just attached" is a staging area, not a media library — a second
upload replaces the first. Without that rule every abandoned draft would
leave an orphan record that nothing ever cleans up, and the next post would
have to guess which of them the user meant.

The slot is cleared automatically once a post that used it is actually
published (see handlers_publish.post_to_channel), so a photo cannot silently
ride along on the next, unrelated post.
"""
import base64
import logging

from imperal_sdk import ActionResult, ui

from app import chat
from models import UploadPostPhotoParams, _NoParams, StagedPhotoResult
from error_codes import (
    TG_NOT_LINKED,
    TG_PHOTO_UPLOAD_FAILED,
    TG_PHOTO_TOO_LARGE,
    TG_NO_STAGED_PHOTO,
    TG_BOT_UNREACHABLE,
)
import storage
import telegram_client as tg

log = logging.getLogger("telegram-publisher")

# Telegram's own cap for a photo sent via sendPhoto. Larger images must go as
# a document instead, which posts as a file attachment rather than an inline
# picture — not what a channel post wants, so we reject with a clear message.
_MAX_PHOTO_BYTES = 10 * 1024 * 1024


def _extract_b64(payload) -> tuple[str, str, str]:
    """Return (data_base64, filename, content_type) from a FileUpload payload.

    Same shape/parsing as notes/handlers_attachments.py and
    tasks/handlers_attachments.py: the panel sends a list[dict] (or a single
    dict) with data_base64/name/content_type; a data: URI prefix is stripped
    if present.
    """
    if isinstance(payload, list) and payload:
        item = payload[0] if isinstance(payload[0], dict) else {}
    elif isinstance(payload, dict):
        item = payload
    else:
        return "", "", ""
    b64 = item.get("data_base64", "")
    if b64.startswith("data:") and "," in b64:
        b64 = b64.split(",", 1)[1]
    return b64, item.get("name", "photo.jpg"), item.get("content_type", "image/jpeg")


def _staged_ui(record: dict):
    """Panel/chat card for a freshly staged photo.

    Deliberately renders NO <img>: the only URL form Telegram offers for a
    stored file embeds the bot token (see module docstring), so the actual
    preview is the copy the bot just DM'd to the author.
    """
    name = record.get("name") or "photo"
    dims = ""
    if record.get("width") and record.get("height"):
        dims = f" · {record['width']}×{record['height']}"
    return ui.Stack(gap=2, children=[
        ui.Stack(direction="h", gap=2, children=[
            ui.Icon("Image"),
            ui.Text(f"Photo attached: {name}{dims}"),
        ]),
        ui.Alert(
            title="Ready for the next post",
            message=("Sent to you in Telegram so you can check it. The next post you "
                     "publish will go out with this photo as its image; the text becomes "
                     "the caption (1024-character limit instead of 4096)."),
            type="info",
        ),
    ])


@chat.function(
    "upload_post_photo",
    action_type="write",
    description=(
        "Attach an image to the NEXT post — upload it from the Telegram panel's photo "
        "picker. The file is sent to Telegram and kept as the pending photo until you "
        "publish a post or clear it. Only one photo can be pending at a time; uploading "
        "another replaces it. Files can ONLY arrive through the panel's upload widget — "
        "a file attached to a chat message never reaches this extension, so never invent "
        "the 'files' argument or claim a chat attachment was received."
    ),
    effects=["telegram.upload"],
    event="telegram-publisher-extension.photo_staged",
    data_model=StagedPhotoResult,
)
async def upload_post_photo(ctx, params: UploadPostPhotoParams) -> ActionResult:
    """Upload image bytes to Telegram, keep the returned file_id as the pending photo."""
    b64, filename, content_type = _extract_b64(params.files)
    if not b64:
        return ActionResult.error(
            "No image received. Attach the photo using the upload area in the Telegram panel — "
            "a file attached to a chat message doesn't reach this extension.",
            code=TG_PHOTO_UPLOAD_FAILED,
        )

    try:
        data = base64.b64decode(b64)
    except Exception:
        return ActionResult.error("That upload isn't valid image data.", code=TG_PHOTO_UPLOAD_FAILED)

    if len(data) > _MAX_PHOTO_BYTES:
        mb = len(data) / (1024 * 1024)
        return ActionResult.error(
            f"That image is {mb:.1f} MB — Telegram's limit for a photo is 10 MB. "
            "Compress or resize it and upload again.",
            code=TG_PHOTO_TOO_LARGE,
        )

    # The bot can only upload into a chat it can write to. The author's own DM
    # is the one such chat guaranteed to exist the moment they linked (the link
    # itself happens via /start in that very chat), and using it means the
    # upload doubles as the preview.
    link = await storage.get_telegram_user_link(ctx)
    if not link or not link.get("telegram_user_id"):
        return ActionResult.error(
            "Connect your Telegram account first — the photo is uploaded through your own chat "
            "with the bot.",
            code=TG_NOT_LINKED,
        )

    try:
        resp = await tg.tg_call_upload(
            ctx, "sendPhoto", "photo", filename, data, content_type,
            fields={
                "chat_id": link["telegram_user_id"],
                "caption": "📎 Attached — the next post you publish will use this photo.",
            },
        )
    except Exception as e:
        log.error("upload_post_photo: transport error: %s", e)
        return ActionResult.error(
            "Could not reach Telegram to upload the photo — try again shortly.",
            retryable=True, code=TG_BOT_UNREACHABLE,
        )

    if not tg.tg_ok(resp):
        return ActionResult.error(tg.tg_error_from(resp), code=TG_PHOTO_UPLOAD_FAILED)

    # sendPhoto returns `photo` as an array of PhotoSize (Telegram's own
    # re-encoded thumbnails, ascending by size). The last entry is the largest
    # — that's the one worth reposting.
    result = tg.tg_result(resp) or {}
    sizes = result.get("photo") or []
    if not sizes:
        return ActionResult.error(
            "Telegram accepted the upload but returned no photo reference — try again.",
            retryable=True, code=TG_PHOTO_UPLOAD_FAILED,
        )
    largest = sizes[-1]

    record = {
        "file_id": largest.get("file_id", ""),
        "file_unique_id": largest.get("file_unique_id", ""),
        "name": filename,
        "width": largest.get("width", 0),
        "height": largest.get("height", 0),
        "size": len(data),
        "staged_at": tg.now_iso(),
    }
    if not record["file_id"]:
        return ActionResult.error(
            "Telegram accepted the upload but returned no file reference — try again.",
            retryable=True, code=TG_PHOTO_UPLOAD_FAILED,
        )

    await storage.save_staged_photo(ctx, record)

    return ActionResult.success(
        data=StagedPhotoResult(
            id=record["file_id"], title=filename, kind="telegram_staged_photo",
            file_id=record["file_id"], file_name=filename,
            width=record["width"], height=record["height"],
            staged_at=record["staged_at"],
        ),
        summary=(
            f"Photo \u201c{filename}\u201d attached — it'll go out with your next post automatically, "
            "no need to reference it explicitly (post_to_channel picks it up; leave photo_url "
            "empty). Keep the text within 1024 characters, since it becomes the caption."
        ),
        ui=_staged_ui(record),
        refresh_panels=["sidebar"],
    )


@chat.function(
    "clear_staged_photo",
    action_type="write",
    description=(
        "Remove the pending photo so the next post goes out as text only. Does not delete "
        "anything already published."
    ),
    effects=["telegram.upload"],
    event="telegram-publisher-extension.photo_cleared",
    data_model=StagedPhotoResult,
)
async def clear_staged_photo(ctx, params: _NoParams) -> ActionResult:
    """Drop the pending photo, if there is one."""
    existing = await storage.get_staged_photo(ctx)
    if not existing:
        return ActionResult.error(
            "There's no photo attached right now.", code=TG_NO_STAGED_PHOTO,
        )
    await storage.clear_staged_photo(ctx)
    return ActionResult.success(
        data=StagedPhotoResult(
            id=existing.get("file_id", ""), title="Cleared", kind="telegram_staged_photo",
            file_id=existing.get("file_id", ""), file_name=existing.get("name", ""),
            cleared=True,
        ),
        summary="Photo removed — the next post will be text only.",
        refresh_panels=["sidebar"],
    )
