from pydantic import BaseModel, Field
from imperal_sdk import sdl


class _NoParams(BaseModel):
    pass


class ConnectTelegramParams(BaseModel):
    label: str = Field(
        default="",
        description=(
            "Optional friendly label for this connection, e.g. if the user has "
            "already connected once and wants a second bot-link flow for a "
            "different set of channels. Omit for the default single link."
        ),
    )


class ChannelIdParams(BaseModel):
    channel_id: str = Field(description="Channel id from a previous list_telegram_channels call — never invent it")


class PostToChannelParams(BaseModel):
    channel_id: str = Field(description="Channel id from a previous list_telegram_channels call — never invent it")
    text: str = Field(description=(
        "Post body. Accepts a LIMITED HTML subset only — Telegram channel posts "
        "support b/i/u/s/a/code/pre/blockquote/spoiler, NOT headings/tables/"
        "lists as HTML elements. Plain text also works unformatted."
    ))
    photo_url: str | None = Field(default=None, description="Optional public image URL to attach as a photo post (caption = text)")
    disable_preview: bool = Field(default=False, description="Suppress link preview card for URLs in the text")
    confirm: bool = Field(
        default=False,
        description=(
            "Must be explicitly set true to actually publish. First call (confirm=false, the "
            "default) only previews what will be posted and where — shows the rendered text/photo "
            "and the target channel — and does not contact Telegram at all. Call again with "
            "confirm=true, same arguments, once the preview looks right."
        ),
    )


class GetRecentPostsParams(BaseModel):
    channel_id: str = Field(description="Channel id from a previous list_telegram_channels call — never invent it")
    limit: int = Field(default=20, ge=1, le=100, description="Max recent posts to return, 1-100")


# ── SDL entities. sdl.Entity already provides: id, title, kind, subtitle, description, status, url. ──

class TelegramChannel(sdl.Entity):
    chat_type: str = ""       # "channel" | "supergroup" | "group"
    can_post: bool = False
    linked_at: str | None = None


class TelegramPost(sdl.Entity):
    message_id: int = 0
    date: str | None = None
    text: str = ""


class ConnectLinkResult(sdl.Entity):
    link_url: str = ""
    expires_in_seconds: int = 0


class PostResult(sdl.Entity):
    channel_id: str = ""
    message_id: int = 0
    link: str | None = None
    needs_confirmation: bool = False


class DisconnectResult(sdl.Entity):
    channel_id: str = ""
    disconnected: bool = False
