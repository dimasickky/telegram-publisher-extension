"""telegram-publisher · sidebar panel — connection status + linked channels.

Same shape as github-connector's panels.py: a connect button when not
linked yet, otherwise a live list of every channel/group the bot has been
promoted to admin in, with a badge showing whether it can actually POST
there (can_post_messages is a distinct, narrower grant than plain admin —
see handlers_publish.py's docstring). This is the "active places where the
bot is admin" view — no separate settings screen, everything lands here.
"""
from imperal_sdk import ui

from app import ext
import handlers_connect
import storage


def _channel_badge(record: dict) -> ui.Badge:
    if record.get("can_post"):
        return ui.Badge(label="can post", color="green")
    return ui.Badge(label="read only", color="yellow")


def _channel_subtitle(record: dict) -> str:
    chat_type = record.get("chat_type", "")
    return chat_type.capitalize() if chat_type else ""


@ext.panel(
    "sidebar",
    slot="left",
    title="Telegram",
    default_width=280,
    min_width=200,
    max_width=400,
    refresh=(
        "on_event:telegram-publisher-extension.connect_telegram,"
        "telegram-publisher-extension.channel_disconnected"
    ),
)
async def sidebar(ctx, **kwargs):
    link = await storage.get_telegram_user_link(ctx)

    if not link:
        deep_link = await handlers_connect.create_connect_deep_link(ctx)
        children = [ui.Empty(message="Telegram account not connected yet.")]
        if deep_link:
            children.append(ui.Button(
                "Connect Telegram", icon="Send", variant="primary",
                on_click=ui.Open(deep_link),
            ))
        else:
            children.append(ui.Text(
                "Telegram bot is not configured yet. Contact the extension developer."
            ))
        return ui.Stack(gap=3, children=children)

    channels = await storage.list_channel_records(ctx)

    header = ui.Stack(direction="h", gap=2, children=[
        ui.Badge(color="green"),
        ui.Text("Telegram connected"),
    ])

    if not channels:
        body = ui.Empty(
            message="No channels yet — add the bot as admin (with 'Post messages') "
                    "to any channel or group and it'll show up here automatically."
        )
    else:
        active = sum(1 for r in channels if r.get("can_post"))
        summary = ui.Text(f"{active} of {len(channels)} channel(s) postable", variant="caption")
        items = [
            ui.ListItem(
                id=str(r.get("chat_id")),
                title=r.get("chat_title", str(r.get("chat_id"))),
                subtitle=_channel_subtitle(r),
                badge=_channel_badge(r),
                actions=[{
                    "icon": "Trash2",
                    "on_click": ui.Call("disconnect_telegram_channel", channel_id=str(r.get("chat_id"))),
                    "confirm": f"Unlink \"{r.get('chat_title', '')}\"? The bot stays admin on Telegram's side.",
                }],
            )
            for r in channels
        ]
        body = ui.Stack(gap=2, children=[summary, ui.List(items=items)])

    return ui.Stack(gap=3, children=[header, ui.Divider(), body])
