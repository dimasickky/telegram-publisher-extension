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
        # The CALLER is an admin (so the authorisation check passes and we reach
        # the bot check), but our bot is not in the list.
        "result": [
            {"user": {"id": 428365104}, "status": "creator"},
            {"user": {"id": 555}, "status": "administrator"},  # not our bot
        ],
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


@pytest.mark.asyncio
async def test_link_channel_rejects_caller_who_is_not_a_chat_admin():
    """The bot identity is shared by every Imperal user, so "the bot is admin
    here" must NOT be enough to link a chat: otherwise knowing a public
    @username would let anyone attach someone else's channel to their own
    account and publish into it using the bot's rights."""
    ctx = await _linked_ctx()
    _mock_chat_admin(ctx, chat_type="channel", can_post_messages=True,
                     caller_is_admin=False)

    result = await handlers_connect.link_channel(
        ctx, LinkChannelParams(channel="@mychannel"))

    assert result.status == "error"
    assert result.error_code == "TG_NOT_CHANNEL_ADMIN"
    # and nothing was stored
    page = await ctx.store.query("tg_channels", limit=10)
    assert page.data == []


@pytest.mark.asyncio
async def test_two_users_linking_same_channel_are_independent():
    """Both admins of one channel may link it; each gets their own record in
    their own partition, and neither overwrites or disconnects the other."""
    ctx_a = await _linked_ctx()
    _mock_chat_admin(ctx_a, chat_type="channel", can_post_messages=True)
    res_a = await handlers_connect.link_channel(
        ctx_a, LinkChannelParams(channel="@mychannel"))
    assert res_a.status == "success"

    ctx_b = make_ctx(user_id="user-2")
    await ctx_b.store.create(storage.USER_LINK_COLLECTION, {
        "telegram_user_id": 777, "linked_at": "2026-07-24T00:00:00Z",
    })
    bot_member = {"user": {"id": 999}, "status": "administrator",
                  "can_post_messages": True}
    ctx_b.http.mock_post("/getChatAdministrators", {"ok": True, "result": [
        bot_member, {"user": {"id": 777}, "status": "administrator"}]})
    ctx_b.http.mock_post("/getMe", {"ok": True, "result": {"id": 999, "username": "bot"}})
    ctx_b.http.mock_post("/getChat", {"ok": True, "result": {
        "id": -1001234567890, "title": "My Channel", "type": "channel",
        "username": "mychannel"}})
    res_b = await handlers_connect.link_channel(
        ctx_b, LinkChannelParams(channel="@mychannel"))
    assert res_b.status == "success"

    a_rows = await ctx_a.store.query("tg_channels", limit=10)
    b_rows = await ctx_b.store.query("tg_channels", limit=10)
    assert len(a_rows.data) == 1 and len(b_rows.data) == 1
    assert a_rows.data[0].data["chat_id"] == b_rows.data[0].data["chat_id"]


# ── multi-user: the bot is shared, ownership is not ────────────────────────── #

@pytest.mark.asyncio
async def test_two_users_can_each_hold_the_same_channel_independently():
    """Two admins of the same channel both linking it is a normal situation, not a
    conflict: the bot identity is shared, but records live per user."""
    ctx_a = make_ctx(user_id="user-a")
    ctx_b = make_ctx(user_id="user-b")

    record = {"chat_id": -1001234567890, "chat_title": "Shared Channel",
              "chat_type": "channel", "can_post": True, "linked_at": "x",
              "chat_username": "shared"}
    await storage.save_channel_record_for_user(ctx_a, "imp_u_a", dict(record))
    await storage.save_channel_record_for_user(ctx_b, "imp_u_b", dict(record))

    a = await ctx_a.store.query(storage.CHANNELS_COLLECTION, limit=10)
    b = await ctx_b.store.query(storage.CHANNELS_COLLECTION, limit=10)
    assert len(a.data) == 1 and len(b.data) == 1
    assert a.data[0].id != b.data[0].id, "separate records, separate partitions"


@pytest.mark.asyncio
async def test_one_user_losing_the_bot_does_not_disconnect_the_other():
    """A demotion reported for one user must not flip anyone else's record."""
    ctx_a = make_ctx(user_id="user-a")
    ctx_b = make_ctx(user_id="user-b")
    record = {"chat_id": -1001234567890, "chat_title": "Shared Channel",
              "chat_type": "channel", "can_post": True, "linked_at": "x"}
    await storage.save_channel_record_for_user(ctx_a, "imp_u_a", dict(record))
    await storage.save_channel_record_for_user(ctx_b, "imp_u_b", dict(record))

    await storage.mark_channel_disconnected_for_user(ctx_a, "imp_u_a", -1001234567890)

    a = await ctx_a.store.query(storage.CHANNELS_COLLECTION, limit=10)
    b = await ctx_b.store.query(storage.CHANNELS_COLLECTION, limit=10)
    assert a.data[0].data["can_post"] is False
    assert b.data[0].data["can_post"] is True, "other user's link is untouched"


@pytest.mark.asyncio
async def test_link_channel_refuses_a_channel_the_caller_does_not_administer():
    """The shared-bot authorisation hole: without this check, knowing a public
    @username would let anyone link someone else's channel and post into it."""
    ctx = await _linked_ctx()
    _mock_chat_admin(ctx, chat_type="channel", can_post_messages=True,
                     caller_is_admin=False)

    result = await handlers_connect.link_channel(
        ctx, LinkChannelParams(channel="@someoneelseschannel"))

    assert result.status == "error"
    assert result.error_code == "TG_NOT_CHANNEL_ADMIN"
    page = await ctx.store.query(storage.CHANNELS_COLLECTION, limit=10)
    assert page.data == [], "nothing may be stored for a channel that isn't yours"


# ── helpers ───────────────────────────────────────────────────────────────── #

def _mock_chat_admin(ctx, chat_type: str, can_post_messages, caller_is_admin: bool = True):
    """Mock the getChat + getMe + getChatAdministrators trio link_channel makes.

    getChatAdministrators is registered FIRST because MockHTTP matches on
    substring and returns the first hit — "/getChat" would otherwise also match
    the getChatAdministrators URL and hand back the wrong payload.

    The admin list carries BOTH the bot and (by default) the calling user: the
    bot being admin authorises the POST, the caller being admin authorises the
    LINK. caller_is_admin=False models someone trying to link a channel that
    isn't theirs.
    """
    bot_member = {"user": {"id": 999}, "status": "administrator"}
    if can_post_messages is not None:
        bot_member["can_post_messages"] = can_post_messages

    admins = [bot_member]
    if caller_is_admin:
        admins.append({"user": {"id": 428365104}, "status": "creator"})

    ctx.http.mock_post("/getChatAdministrators", {"ok": True, "result": admins})
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
