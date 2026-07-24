"""Tests for the two channel-discovery paths: on_install's webhook registration
and link_channel's manual recovery.

Both exist because of one Telegram constraint: `my_chat_member` is excluded
from setWebhook's DEFAULT allowed_updates and is never replayed. So:
  - on_install must request it explicitly, or auto-discovery never fires at all;
  - link_channel must be able to recover a channel the bot joined before that
    webhook existed, since no update for it will ever arrive.

MockHTTP matches on `pattern in url` and the Bot API puts the method name in
the URL path (/bot<token>/getChat), so each Telegram method is mocked
separately. Registration order matters — first match wins — so "/getChat" is
registered AFTER "/getChatAdministrators" would otherwise swallow it; here we
sidestep that by using the trailing-precise patterns below.
"""
import pytest

from tests.conftest import make_ctx

import app as app_mod
import handlers_connect
import storage
from models import LinkChannelParams


# ── on_install: the fix that makes auto-discovery possible at all ──────────── #

@pytest.mark.asyncio
async def test_on_install_requests_my_chat_member_explicitly():
    """The whole point: without my_chat_member in allowed_updates Telegram
    silently never delivers channel-promotion events."""
    ctx = make_ctx()
    sent = _capture_post_bodies(ctx)
    ctx.http.mock_post("/setWebhook", {"ok": True, "result": True})

    result = await app_mod.on_install(ctx)

    assert result["webhook_registered"] is True
    body = sent[-1]
    assert "my_chat_member" in body["allowed_updates"]
    assert "message" in body["allowed_updates"]          # /start deep-link bind
    assert "channel_post" in body["allowed_updates"]     # post ingestion
    assert body["url"].endswith("/webhook/telegram_updates")


@pytest.mark.asyncio
async def test_on_install_passes_secret_token():
    ctx = make_ctx()
    sent = _capture_post_bodies(ctx)
    ctx.http.mock_post("/setWebhook", {"ok": True, "result": True})

    await app_mod.on_install(ctx)

    from tests.conftest import TEST_WEBHOOK_SECRET
    assert sent[-1]["secret_token"] == TEST_WEBHOOK_SECRET


@pytest.mark.asyncio
async def test_on_install_survives_telegram_failure():
    """Install must not break if Telegram is unreachable — the consequence is
    dormant auto-discovery, not a failed install."""
    ctx = make_ctx()
    # No /setWebhook mock -> MockHTTP returns 404, tg_ok() False.
    result = await app_mod.on_install(ctx)
    assert result["webhook_registered"] is False


# ── link_channel: recovering a channel added before the webhook existed ───── #

@pytest.mark.asyncio
async def test_link_channel_requires_linked_account():
    ctx = make_ctx()
    result = await handlers_connect.link_channel(
        ctx, LinkChannelParams(channel="@mychannel"))
    assert result.status == "error"
    assert result.error_code == "TG_NOT_LINKED"


@pytest.mark.asyncio
async def test_link_channel_stores_channel_when_bot_is_admin_with_post_right():
    ctx = await _linked_ctx()
    _mock_chat_admin(ctx, chat_type="channel", can_post_messages=True)

    result = await handlers_connect.link_channel(
        ctx, LinkChannelParams(channel="@mychannel"))

    assert result.status == "success"
    assert result.data.can_post is True
    rows = await storage.list_channel_records(ctx)
    assert [r["chat_id"] for r in rows] == [-1001234567890]
    # public @username must be persisted so get_channel_recent_posts works
    assert rows[0]["chat_username"] == "mychannel"


@pytest.mark.asyncio
async def test_link_channel_rejects_when_bot_lacks_post_right():
    ctx = await _linked_ctx()
    _mock_chat_admin(ctx, chat_type="channel", can_post_messages=False)

    result = await handlers_connect.link_channel(
        ctx, LinkChannelParams(channel="@mychannel"))

    assert result.status == "error"
    assert result.error_code == "TG_BOT_CANNOT_POST"
    assert await storage.list_channel_records(ctx) == []


@pytest.mark.asyncio
async def test_link_channel_rejects_when_bot_not_admin():
    ctx = await _linked_ctx()
    ctx.http.mock_post("/getChatAdministrators", {
        "ok": True,
        "result": [{"user": {"id": 555}, "status": "administrator"}],  # not our bot
    })
    ctx.http.mock_post("/getMe", {"ok": True, "result": {"id": 999, "username": "bot"}})
    ctx.http.mock_post("/getChat", {
        "ok": True,
        "result": {"id": -1001234567890, "title": "My Channel",
                   "type": "channel", "username": "mychannel"},
    })

    result = await handlers_connect.link_channel(
        ctx, LinkChannelParams(channel="@mychannel"))

    assert result.status == "error"
    assert result.error_code == "TG_BOT_NOT_ADMIN"


@pytest.mark.asyncio
async def test_link_channel_reports_unreachable_chat():
    ctx = await _linked_ctx()
    ctx.http.mock_post("/getChat", {"ok": False, "description": "chat not found"})

    result = await handlers_connect.link_channel(
        ctx, LinkChannelParams(channel="@nope"))

    assert result.status == "error"
    assert result.error_code == "TG_CHAT_NOT_REACHABLE"


@pytest.mark.asyncio
async def test_link_channel_accepts_supergroup_without_can_post_messages():
    """can_post_messages is a CHANNEL-only right; an admin bot in a supergroup
    can post even though the flag is absent. Reading it blindly used to
    mislabel such a chat as unpostable."""
    ctx = await _linked_ctx()
    _mock_chat_admin(ctx, chat_type="supergroup", can_post_messages=None)

    result = await handlers_connect.link_channel(
        ctx, LinkChannelParams(channel="-1001234567890"))

    assert result.status == "success"
    assert result.data.can_post is True


@pytest.mark.asyncio
async def test_link_channel_normalises_tme_link():
    ctx = await _linked_ctx()
    _mock_chat_admin(ctx, chat_type="channel", can_post_messages=True)

    result = await handlers_connect.link_channel(
        ctx, LinkChannelParams(channel="https://t.me/mychannel"))

    assert result.status == "success"


# ── helpers ───────────────────────────────────────────────────────────────── #

def _capture_post_bodies(ctx) -> list:
    """Record the json bodies passed to ctx.http.post — MockHTTP drops kwargs."""
    sent: list = []
    original = ctx.http.post

    async def spy(url, **kwargs):
        sent.append(kwargs.get("json") or {})
        return await original(url, **kwargs)

    ctx.http.post = spy
    return sent


def _mock_chat_admin(ctx, chat_type: str, can_post_messages):
    """Mock the getChat + getMe + getChatAdministrators trio link_channel makes.

    getChatAdministrators is registered FIRST because MockHTTP matches on
    substring and returns the first hit — "/getChat" would otherwise also match
    the getChatAdministrators URL and hand back the wrong payload.
    """
    bot_member = {"user": {"id": 999}, "status": "administrator"}
    if can_post_messages is not None:
        bot_member["can_post_messages"] = can_post_messages

    ctx.http.mock_post("/getChatAdministrators", {"ok": True, "result": [bot_member]})
    ctx.http.mock_post("/getMe", {"ok": True, "result": {"id": 999, "username": "bot"}})
    ctx.http.mock_post("/getChat", {
        "ok": True,
        "result": {"id": -1001234567890, "title": "My Channel",
                   "type": chat_type, "username": "mychannel"},
    })


async def _linked_ctx():
    """A ctx whose Telegram identity is already bound (link_channel's precondition)."""
    ctx = make_ctx()
    await ctx.store.create(storage.USER_LINK_COLLECTION, {
        "telegram_user_id": 428365104, "linked_at": "2026-07-24T00:00:00Z",
    })
    return ctx
