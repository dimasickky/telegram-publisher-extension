"""Store reads must not confuse a page size with a quantity.

The shared "__webhook__" partition is the dangerous one: it holds the pending
link codes and parked channels of EVERY user at once, so a scan of "the first
200 rows" is not a generous margin, it is a time bomb. Past the cap a valid
code simply stopped being found and /start silently did nothing.

These tests pin three things:

1. `find_and_consume_link_code` resolves a code that sits far outside any page
   window (it is a `where=` point lookup now, not a scan).
2. TTL expiry still applies to a code found that way — the fix must not have
   quietly dropped the expiry check that was inside the old loop.
3. `claim_pending_channels` still sees and drops OTHER users' expired rows.
   That one deliberately did NOT get a `where=` filter, because it doubles as
   the only expiry sweep this collection has; narrowing it would look tidier
   and let the junk grow forever. This test is what stops a future "cleanup"
   from making that mistake.
"""
import time

import pytest
from imperal_sdk.testing import MockContext

import storage


@pytest.mark.asyncio
async def test_link_code_found_far_beyond_the_old_scan_window():
    """A valid code must resolve with far more than 200 rows parked."""
    ctx = MockContext(user_id="__webhook__")

    for i in range(250):
        await ctx.store.create(
            storage.LINK_CODES_COLLECTION,
            {"code": f"other-code-{i}", "imperal_id": f"imp_other_{i}",
             "created_ts": time.time()},
        )
    await ctx.store.create(
        storage.LINK_CODES_COLLECTION,
        {"code": "real-code", "imperal_id": "imp_u_target",
         "created_ts": time.time()},
    )

    owner = await storage.find_and_consume_link_code(ctx, "real-code")
    assert owner == "imp_u_target", (
        "a code past the 200-row mark must still resolve — this is the /start "
        "that used to do nothing at all"
    )


@pytest.mark.asyncio
async def test_link_code_is_one_shot():
    """Consuming a code deletes it — a replay must not bind twice."""
    ctx = MockContext(user_id="__webhook__")
    await ctx.store.create(
        storage.LINK_CODES_COLLECTION,
        {"code": "c1", "imperal_id": "imp_u_1", "created_ts": time.time()},
    )

    assert await storage.find_and_consume_link_code(ctx, "c1") == "imp_u_1"
    assert await storage.find_and_consume_link_code(ctx, "c1") is None


@pytest.mark.asyncio
async def test_expired_link_code_still_rejected_after_the_rewrite():
    """TTL was enforced inside the old loop — it must survive the where= fix."""
    ctx = MockContext(user_id="__webhook__")
    await ctx.store.create(
        storage.LINK_CODES_COLLECTION,
        {"code": "stale", "imperal_id": "imp_u_1",
         "created_ts": time.time() - 9999},
    )

    owner = await storage.find_and_consume_link_code(ctx, "stale", ttl_seconds=60)
    assert owner is None, "an expired code must not bind an account"
    # …and it is still consumed, so it cannot be retried.
    page = await ctx.store.query(storage.LINK_CODES_COLLECTION, limit=10)
    assert page.data == []


@pytest.mark.asyncio
async def test_unknown_link_code_returns_none():
    ctx = MockContext(user_id="__webhook__")
    assert await storage.find_and_consume_link_code(ctx, "nope") is None


@pytest.mark.asyncio
async def test_pending_channel_upsert_survives_int_vs_str_chat_id():
    """chat_id is stored as int by the webhook but arrives as str from chat.

    This is why chat_id is compared with str() in Python instead of being put
    into the `where=` clause: an exact-match filter on mixed types would turn
    the upsert into a duplicate row on every promote/demote churn.
    """
    ctx = MockContext(user_id="__webhook__")

    await storage.save_pending_channel(ctx, 555, {"chat_id": -100123, "title": "first"})
    # Same channel, chat_id now a string — must UPDATE, not duplicate.
    await storage.save_pending_channel(ctx, 555, {"chat_id": "-100123", "title": "second"})

    page = await ctx.store.query(storage.PENDING_CHANNELS_COLLECTION, limit=50)
    assert len(page.data) == 1, "int/str chat_id must not create a second row"
    assert page.data[0].data["record"]["title"] == "second"


@pytest.mark.asyncio
async def test_claim_still_sweeps_other_users_expired_rows():
    """The sweep must keep seeing rows it does not own.

    If someone ever 'optimises' claim_pending_channels with
    where={"telegram_user_id": ...}, this fails — which is the point. That loop
    is the only expiry sweep this shared collection has.
    """
    ctx = MockContext(user_id="__webhook__")

    # An ancient row belonging to a DIFFERENT telegram user.
    await ctx.store.create(
        storage.PENDING_CHANNELS_COLLECTION,
        {"telegram_user_id": 999, "chat_id": -1,
         "record": {"chat_id": -1}, "created_ts": time.time() - (60 * 24 * 3600)},
    )
    # A live row for the user doing the claiming.
    await storage.save_pending_channel(ctx, 555, {"chat_id": -100777, "title": "mine"})

    claimed = await storage.claim_pending_channels(ctx, "imp_u_1", 555)

    assert [c["chat_id"] for c in claimed] == [-100777], "only own rows are claimed"
    left = await ctx.store.query(storage.PENDING_CHANNELS_COLLECTION, limit=50)
    assert left.data == [], (
        "the other user's expired row must have been swept — narrowing this "
        "query with where= would let junk accumulate forever"
    )


@pytest.mark.asyncio
async def test_fresh_row_of_another_user_is_left_alone():
    """Sweeping expired rows must not touch someone else's LIVE row."""
    ctx = MockContext(user_id="__webhook__")

    await storage.save_pending_channel(ctx, 999, {"chat_id": -2, "title": "not mine"})
    await storage.save_pending_channel(ctx, 555, {"chat_id": -100777, "title": "mine"})

    claimed = await storage.claim_pending_channels(ctx, "imp_u_1", 555)

    assert [c["chat_id"] for c in claimed] == [-100777]
    left = await ctx.store.query(storage.PENDING_CHANNELS_COLLECTION, limit=50)
    assert len(left.data) == 1
    assert left.data[0].data["telegram_user_id"] == 999
