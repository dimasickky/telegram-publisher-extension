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


class GenerateDraftParams(BaseModel):
    channel_id: str = Field(description="Channel id from a previous list_telegram_channels call — never invent it")
    brief: str = Field(description="What the post should say/announce — a topic, brief, or rough draft in plain language")
    has_photo: bool = Field(
        default=False,
        description="Whether this draft will be posted with a photo attached — caps the text at Telegram's shorter caption limit (1024 chars) instead of the full message limit (4096)",
    )
    sample_size: int = Field(
        default=10, ge=0, le=50,
        description="How many recent public posts to sample for tone/style matching (0 = skip tone matching, just write to the brief)",
    )


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


class DraftResult(sdl.Entity):
    channel_id: str = ""
    text: str = ""
    based_on_sample: bool = False
    sample_count: int = 0


class DisconnectResult(sdl.Entity):
    channel_id: str = ""
    disconnected: bool = False
