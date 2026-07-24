"""telegram-publisher · AI draft generation for a channel post.

Ties together two things that already existed separately but were never
connected: `_fetch_recent_post_texts` (handlers_read.py's t.me/s/ scrape,
same public-preview limitation — no history for private channels) as a tone
sample, and `ctx.ai.complete()` (the SDK's LLM bridge, same call shape as
sql-db's nl_to_sql / tasks' ai_breakdown_task) to actually write the post.

The output is plain text meant to be handed straight to post_to_channel —
this handler does not post anything itself, it only writes the draft. The
prompt bakes in Telegram's own hard constraints (limited HTML subset, no
headings/tables/lists as HTML) so what comes back is postable as-is rather
than needing a second pass to strip Markdown or unsupported tags.
"""
import logging

from imperal_sdk import ActionResult

from app import chat
from models import GenerateDraftParams, DraftResult
from error_codes import TG_CHANNEL_NOT_FOUND, TG_DRAFT_GENERATION_FAILED
import storage
from handlers_read import _fetch_recent_post_texts
from telegram_client import _MAX_TEXT_LEN, _MAX_CAPTION_LEN

log = logging.getLogger("telegram-publisher")

_ALLOWED_TAGS = "b, i, u, s, a, code, pre, blockquote, spoiler"


def _build_prompt(brief: str, char_limit: int, samples: list[str]) -> str:
    constraints = (
        "You are writing a Telegram channel post. Hard constraints:\n"
        f"- Output ONLY the post body — no explanation, no markdown fences, no quotes around it.\n"
        f"- Max length: {char_limit} characters (Telegram's own hard cap for this post type).\n"
        f"- Formatting: ONLY this limited HTML subset is allowed: {_ALLOWED_TAGS}. "
        "Do NOT use Markdown (**bold**, # headings, - lists) and do NOT use <h1>/<ul>/<table> — "
        "Telegram channel posts don't render them.\n"
        "- Plain unformatted text is also fine if no emphasis is needed."
    )
    if samples:
        joined = "\n\n---\n\n".join(samples)
        constraints += (
            "\n\nMatch the tone, voice, length, and typical emoji/formatting habits of these "
            f"recent posts from the SAME channel (for style reference only — don't repeat their "
            f"content):\n\n{joined}"
        )
    return f"{constraints}\n\nWrite a new post about: {brief}"


@chat.function(
    "generate_draft",
    action_type="read",
    description=(
        "Generate a draft post for a linked Telegram channel from a brief/topic — written "
        "within Telegram's own posting limits (character cap, limited HTML subset), and "
        "optionally matched to the channel's own tone by sampling its recent public posts. "
        "Does not post anything — hand the returned text straight to post_to_channel "
        "(confirm=false first) to preview and then publish it."
    ),
    data_model=DraftResult,
)
async def generate_draft(ctx, params: GenerateDraftParams) -> ActionResult:
    """Sample recent posts (if requested and available) for tone, then ask
    ctx.ai.complete() to write a new post to the brief within Telegram's limits."""
    record = await storage.get_channel_record(ctx, params.channel_id)
    if not record:
        return ActionResult.error(
            "That channel isn't linked — check list_telegram_channels for the right channel_id.",
            code=TG_CHANNEL_NOT_FOUND,
        )

    samples: list[str] = []
    sample_note = ""
    if params.sample_size > 0:
        texts, reason = await _fetch_recent_post_texts(ctx, record, params.sample_size)
        if texts:
            samples = texts
        else:
            sample_note = f" (tone sample skipped: {reason})" if reason else ""

    char_limit = _MAX_CAPTION_LEN if params.has_photo else _MAX_TEXT_LEN
    prompt = _build_prompt(params.brief, char_limit, samples)

    try:
        completion = await ctx.ai.complete(prompt)
        text = (completion.text or "").strip().strip("`").strip()
    except Exception as e:
        log.error("generate_draft: ctx.ai.complete failed: %s", e)
        return ActionResult.error(
            "Couldn't generate a draft right now — try again shortly.",
            retryable=True, code=TG_DRAFT_GENERATION_FAILED,
        )

    if not text:
        return ActionResult.error(
            "The AI returned an empty draft — try rephrasing the brief.",
            retryable=True, code=TG_DRAFT_GENERATION_FAILED,
        )

    if len(text) > char_limit:
        text = text[:char_limit].rstrip()

    chat_title = record.get("chat_title", params.channel_id)
    summary = f"Draft written for \"{chat_title}\""
    summary += f", matched to the tone of {len(samples)} recent post(s)" if samples else " (no tone sample used"
    summary += ")" if not samples else ""
    summary += sample_note
    summary += " — this is just the text, nothing posted. Pass it to post_to_channel to preview and publish."

    return ActionResult.success(
        DraftResult(
            id=params.channel_id, title="Draft", kind="telegram_draft",
            channel_id=params.channel_id, text=text,
            based_on_sample=bool(samples), sample_count=len(samples),
        ),
        summary=summary,
    )
