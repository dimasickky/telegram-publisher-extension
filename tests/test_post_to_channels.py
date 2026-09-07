"""Tests for post_to_channels batch crossposting."""
import pytest
from tests.conftest import make_ctx, seed_channel
import handlers_publish
from models import PostToChannelsParams


@pytest.mark.asyncio
async def test_crosspost_preview_flow():
    ctx = make_ctx()
    await seed_channel(ctx, chat_id="-1001", can_post=True, chat_title="Channel A")
    await seed_channel(ctx, chat_id="-1002", can_post=True, chat_title="Channel B")

    params = PostToChannelsParams(
        channel_ids=["-1001", "-1002"],
        text="<b>Crosspost Test</b>",
        confirm=False,
    )
    result = await handlers_publish.post_to_channels(ctx, params)
    assert result.status == "success"
    assert result.data.needs_confirmation is True
    assert result.data.total == 2
    assert len(result.data.results) == 2
    assert all(r.status == "preview" for r in result.data.results)


@pytest.mark.asyncio
async def test_crosspost_confirm_true_publishes_all():
    ctx = make_ctx()
    await seed_channel(ctx, chat_id="-1001", can_post=True, chat_title="Channel A")
    await seed_channel(ctx, chat_id="-1002", can_post=True, chat_title="Channel B")

    ctx.http.mock_post("api.telegram.org", {
        "ok": True,
        "result": {"message_id": 101, "chat": {"username": "channel_a"}},
    })

    params = PostToChannelsParams(
        channel_ids=["-1001", "-1002"],
        text="Real broadcast message",
        confirm=True,
    )
    result = await handlers_publish.post_to_channels(ctx, params)
    assert result.status == "success"
    assert result.data.needs_confirmation is False
    assert result.data.total == 2
    assert result.data.succeeded_count == 2
    assert result.data.failed_count == 0


@pytest.mark.asyncio
async def test_crosspost_blocks_on_unlinked_channel():
    ctx = make_ctx()
    await seed_channel(ctx, chat_id="-1001", can_post=True)
    params = PostToChannelsParams(
        channel_ids=["-1001", "-9999"],
        text="Will fail upfront",
        confirm=False,
    )
    result = await handlers_publish.post_to_channels(ctx, params)
    assert result.status == "error"
    assert "not linked" in result.error.lower()
