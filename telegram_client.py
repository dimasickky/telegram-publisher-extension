"""telegram-publisher · thin wrapper over ctx.http for the Telegram Bot API.

One shared bot token (app-scope secret, see app.py) — every call goes to
https://api.telegram.org/bot<token>/<method>. No per-user credential here,
unlike wp_client.py's Basic Auth (which IS per-user) — the bot identity is
constant, only chat_id varies per call.
"""
from datetime import datetime, timezone

_API_ROOT = "https://api.telegram.org"
_MAX_TEXT_LEN = 4096       # Telegram's own hard cap for a text message/caption segment
_MAX_CAPTION_LEN = 1024    # caption limit when sending photo/video (shorter than plain text)

_ERROR_MESSAGES = {
    401: "Bot token was rejected by Telegram — check telegram_bot_token in Developer Portal -> Secrets.",
    403: "The bot was blocked, kicked, or lacks permission for this chat.",
    404: "Telegram chat/message not found — it may have been deleted.",
    429: "Telegram is rate-limiting this bot — try again shortly.",
}


def now_iso() -> str:
    """Current UTC timestamp as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


async def _bot_token(ctx) -> str:
    token = await ctx.secrets.get("telegram_bot_token")
    if not token:
        raise RuntimeError("telegram_bot_token not set — configure it in Developer Portal -> Secrets")
    return token


async def tg_call(ctx, method: str, json_body: dict | None = None):
    """POST to api.telegram.org/bot<token>/<method>. Returns the raw HTTPResponse
    (caller checks .body["ok"] per Telegram's own {"ok": bool, "result"|"description"} envelope)."""
    token = await _bot_token(ctx)
    url = f"{_API_ROOT}/bot{token}/{method}"
    return await ctx.http.post(url, json=json_body or {})


def tg_ok(resp) -> bool:
    body = resp.body if hasattr(resp, "body") else resp
    return isinstance(body, dict) and body.get("ok") is True


def tg_result(resp):
    body = resp.body if hasattr(resp, "body") else resp
    return body.get("result") if isinstance(body, dict) else None


def tg_description(resp) -> str:
    body = resp.body if hasattr(resp, "body") else resp
    if isinstance(body, dict):
        return body.get("description", "")
    return ""


def tg_error_message(status_code: int, description: str = "") -> str:
    if description:
        return f"Telegram rejected the request: {description}"
    if status_code in _ERROR_MESSAGES:
        return _ERROR_MESSAGES[status_code]
    if 500 <= status_code < 600:
        return "Telegram returned a server error — try again shortly."
    return f"Telegram request failed (HTTP {status_code})."


def tg_error_from(resp) -> str:
    """Build the user-facing error message straight from an HTTPResponse.

    tg_error_message() takes a plain (status_code, description) pair, which is
    easy to call wrongly: passing the response object itself still "works"
    syntactically and then explodes on `status_code in _ERROR_MESSAGES` with
    TypeError: unhashable type. Both call sites had that bug. This wrapper is
    the resp-shaped entry point so the unpacking happens in exactly one place.
    """
    status = getattr(resp, "status_code", 0) or 0
    return tg_error_message(status, tg_description(resp))


def tg_error_code(status_code: int) -> str:
    """Map an HTTP status to a platform structured error code
    (imperal_sdk.chat.error_codes) — pairs with tg_error_message()."""
    if status_code in (401, 403):
        return "PERMISSION_DENIED"
    if status_code == 429:
        return "RATE_LIMITED"
    if status_code >= 500:
        return "BACKEND_5XX"
    return "INTERNAL"


def split_message(text: str, limit: int = _MAX_TEXT_LEN) -> list[str]:
    """Split text into Telegram-safe chunks, breaking on paragraph/line
    boundaries where possible instead of mid-word — mirrors the platform
    Telegram connector's _split_message intent (see extensions/
    telegram-publisher.md §8), reimplemented here (not shared code, different
    bot/webhook/storage)."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n\n", 0, limit)
        if cut == -1:
            cut = remaining.rfind("\n", 0, limit)
        if cut == -1:
            cut = remaining.rfind(" ", 0, limit)
        if cut == -1:
            cut = limit
        parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        parts.append(remaining)
    return parts


async def get_chat_administrators(ctx, chat_id):
    """GET-equivalent (Bot API uses POST for everything) list of a chat's
    admins — each entry carries {user: {id, ...}, status, can_post_messages,
    ...}. Used by the my_chat_member handler to verify BOTH that the linking
    telegram_user_id is really an admin AND that the bot itself has
    can_post_messages (see extensions/telegram-publisher.md §7 step 4)."""
    resp = await tg_call(ctx, "getChatAdministrators", {"chat_id": chat_id})
    if not tg_ok(resp):
        return None
    return tg_result(resp)


async def get_me(ctx):
    """getMe — returns the bot's own {id, username, ...}. Used to find the
    bot's own user_id inside a getChatAdministrators list."""
    resp = await tg_call(ctx, "getMe")
    if not tg_ok(resp):
        return None
    return tg_result(resp)


async def get_chat(ctx, chat_id):
    """getChat — resolve a chat by numeric id OR public @username into its
    {id, title, type, username, ...}. The @username form is what makes manual
    linking possible: a channel the bot was added to BEFORE this extension was
    deployed never produced a my_chat_member update (Telegram does not replay
    them), so the only way to discover it is to ask Telegram about it directly.
    """
    resp = await tg_call(ctx, "getChat", {"chat_id": chat_id})
    if not tg_ok(resp):
        return None
    return tg_result(resp)


def derive_can_post(chat_type: str, member: dict) -> bool:
    """Decide whether the bot may publish, given its admin record in a chat.

    `can_post_messages` is a CHANNEL-only admin right in Telegram's Bot API.
    In a group/supergroup it is absent from ChatMemberAdministrator entirely,
    because posting there isn't an admin privilege — any non-restricted member
    can send messages. Reading the flag unconditionally therefore mislabels an
    admin bot in a supergroup as "cannot post" and makes post_to_channel
    refuse a chat it can actually publish to.
    """
    status = member.get("status", "")
    if status not in ("administrator", "creator"):
        return False
    if (chat_type or "") == "channel":
        return bool(member.get("can_post_messages", False))
    return True
