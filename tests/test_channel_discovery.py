"""Tests for channel discovery: the pending-backfill path and link_channel.

Two distinct reasons a channel can be missing from the list, with two different
fixes — and only one of them is Telegram's fault:

  - The bot WAS promoted while our webhook was live, so `my_chat_member` did
    arrive (it is in setWebhook's default allowed_updates — the default set is
    "all except chat_member, message_reaction, message_reaction_count"), but the
    promoting Telegram user hadn't run /start yet, so there was no imperal_id to
    attribute the chat to. That update used to be dropped on the floor; it is now
    parked in tg_pending_channels and claimed when /start binds the identity.
  - The bot was promoted when no webhook existed at all. Nothing was ever
    delivered and Telegram never replays updates, nor does the Bot API expose any
    "list my chats" method — so the only way back is to ask about a specific chat
    by name: link_channel.

MockHTTP matches on `pattern in url` and the Bot API puts the method name in the
URL path (/bot<token>/getChat), so each Telegram method is mocked separately.
First match wins, so "/getChatAdministrators" must be registered BEFORE
"/getChat" — otherwise the shorter pattern swallows it.
"""
import pytest

from tests.conftest import make_ctx

import handlers_connect
import storage
from models import LinkChannelParams


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


# ── pending backfill: bot promoted BEFORE /start ───────────────────────────── #

def _promotion(chat_type="channel", can_post=True, status="administrator",
               promoter_id=428365104, chat_id=-1001234567890):
    """A my_chat_member update shaped like Telegram's own."""
    new_member = {"user": {"id": 999}, "status": status}
    if chat_type == "channel" and can_post is not None:
        new_member["can_post_messages"] = can_post
    return {"my_chat_member": {
        "chat": {"id": chat_id, "title": "My Channel", "type": chat_type,
                 "username": "mychannel"},
        "from": {"id": promoter_id},
        "new_chat_member": new_member,
    }}


@pytest.mark.asyncio
async def test_promotion_by_unknown_user_is_parked_not_dropped():
    """The regression that made a correctly-added channel invisible forever:
    no imperal_id yet -> event used to be discarded, and Telegram never resends."""
    ctx = make_ctx()
    await handlers_connect._handle_my_chat_member(ctx, _promotion())

    page = await ctx.store.query(storage.PENDING_CHANNELS_COLLECTION, limit=10)
    assert len(page.data) == 1
    parked = page.data[0].data
    assert parked["telegram_user_id"] == 428365104
    assert parked["record"]["chat_title"] == "My Channel"
    assert parked["record"]["can_post"] is True
    # username must survive so tone-sampling works once claimed
    assert parked["record"]["chat_username"] == "mychannel"


@pytest.mark.asyncio
async def test_demotion_by_unknown_user_is_not_parked():
    """Nothing to remember about a chat the bot was removed from."""
    ctx = make_ctx()
    await handlers_connect._handle_my_chat_member(
        ctx, _promotion(status="left"))
    page = await ctx.store.query(storage.PENDING_CHANNELS_COLLECTION, limit=10)
    assert page.data == []


@pytest.mark.asyncio
async def test_parking_same_chat_twice_does_not_duplicate():
    ctx = make_ctx()
    await handlers_connect._handle_my_chat_member(ctx, _promotion(can_post=False))
    await handlers_connect._handle_my_chat_member(ctx, _promotion(can_post=True))

    page = await ctx.store.query(storage.PENDING_CHANNELS_COLLECTION, limit=10)
    assert len(page.data) == 1
    # the later state wins
    assert page.data[0].data["record"]["can_post"] is True


@pytest.mark.asyncio
async def test_start_claims_parked_channel():
    """End-to-end of the fix: promote first, /start second, channel appears."""
    ctx = make_ctx()
    await handlers_connect._handle_my_chat_member(ctx, _promotion())

    # mint a link code the way connect_telegram would, then run /start
    await storage.save_link_code(ctx, "code123", "user-1", "2026-07-24T00:00:00Z")
    ctx.http.mock_post("/sendMessage", {"ok": True, "result": {"message_id": 1}})
    await handlers_connect._handle_start(ctx, {
        "text": "/start code123", "from": {"id": 428365104},
    })

    channels = await storage.list_channel_records(ctx)
    assert len(channels) == 1
    assert channels[0]["chat_title"] == "My Channel"
    assert channels[0]["can_post"] is True
    # and the pending row is consumed, so a second /start can't re-add it
    page = await ctx.store.query(storage.PENDING_CHANNELS_COLLECTION, limit=10)
    assert page.data == []


@pytest.mark.asyncio
async def test_start_only_claims_own_telegram_user_id():
    """Another user's parked channel must never leak into this user's list."""
    ctx = make_ctx()
    await handlers_connect._handle_my_chat_member(
        ctx, _promotion(promoter_id=111111, chat_id=-100999))

    await storage.save_link_code(ctx, "code123", "user-1", "2026-07-24T00:00:00Z")
    ctx.http.mock_post("/sendMessage", {"ok": True, "result": {"message_id": 1}})
    await handlers_connect._handle_start(ctx, {
        "text": "/start code123", "from": {"id": 428365104},
    })

    assert await storage.list_channel_records(ctx) == []
    page = await ctx.store.query(storage.PENDING_CHANNELS_COLLECTION, limit=10)
    assert len(page.data) == 1  # still parked for its real owner


@pytest.mark.asyncio
async def test_supergroup_promotion_is_postable():
    """can_post_messages is channel-only; an admin bot in a supergroup can post."""
    ctx = make_ctx()
    await handlers_connect._handle_my_chat_member(
        ctx, _promotion(chat_type="supergroup", can_post=None))
    page = await ctx.store.query(storage.PENDING_CHANNELS_COLLECTION, limit=10)
    assert page.data[0].data["record"]["can_post"] is True


@pytest.mark.asyncio
async def test_claim_expires_stale_parked_rows():
    """Parked rows live in the shared __webhook__ partition that every link-code
    query pages through, so an unclaimed one must not sit there forever."""
    import time as _time
    ctx = make_ctx()
    await handlers_connect._handle_my_chat_member(ctx, _promotion(promoter_id=111))
    await handlers_connect._handle_my_chat_member(
        ctx, _promotion(promoter_id=222, chat_id=-1009999999999))

    # Age the first row past the TTL.
    page = await ctx.store.query(storage.PENDING_CHANNELS_COLLECTION, limit=10)
    stale = next(d for d in page.data if d.data.get("telegram_user_id") == 111)
    aged = dict(stale.data)
    aged["created_ts"] = _time.time() - (31 * 24 * 3600)
    await ctx.store.update(storage.PENDING_CHANNELS_COLLECTION, stale.id, aged)

    # A bind for an unrelated user still sweeps the expired row.
    claimed = await storage.claim_pending_channels(ctx, "imp_u_other", 333)
    assert claimed == []
    left = await ctx.store.query(storage.PENDING_CHANNELS_COLLECTION, limit=10)
    owners = {d.data.get("telegram_user_id") for d in left.data}
    assert owners == {222}, "expired row should be swept, live row kept"


@pytest.mark.asyncio
async def test_expired_row_is_not_claimed_by_its_owner():
    """Too old to trust: the bot may have been removed since. link_channel is the
    honest way back, not a stale record."""
    import time as _time
    ctx = make_ctx()
    await handlers_connect._handle_my_chat_member(ctx, _promotion(promoter_id=111))
    page = await ctx.store.query(storage.PENDING_CHANNELS_COLLECTION, limit=10)
    doc = page.data[0]
    aged = dict(doc.data)
    aged["created_ts"] = _time.time() - (31 * 24 * 3600)
    await ctx.store.update(storage.PENDING_CHANNELS_COLLECTION, doc.id, aged)

    claimed = await storage.claim_pending_channels(ctx, "imp_u_RogDc6K4_L", 111)
    assert claimed == []
    left = await ctx.store.query(storage.PENDING_CHANNELS_COLLECTION, limit=10)
    assert left.data == []


# ── helpers ───────────────────────────────────────────────────────────────── #

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
