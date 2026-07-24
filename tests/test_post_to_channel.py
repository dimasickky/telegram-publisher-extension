"""Tests for post_to_channel's two-step confirm flow (draft preview -> publish).

Same shape as github-connector/tests/test_handlers_p5.py: the preview call
(confirm=False, the default) must NOT call the Telegram API and must return
needs_confirmation=True with a rendered ui preview; the confirmed call
(confirm=True) must actually call sendMessage/sendPhoto.
"""
import pytest

from tests.conftest import make_ctx, seed_channel

import handlers_publish
from models import PostToChannelParams


@pytest.mark.asyncio
async def test_preview_does_not_call_telegram():
    ctx = await _seeded_ctx()
    # No sendMessage mock registered — if the tool called it anyway, MockHTTP's
    # _find would return 404 "No mock registered" and tg_ok() would be False,
    # flipping the result to error and catching an accidental live call.
    result = await handlers_publish.post_to_channel(
        ctx, PostToChannelParams(channel_id="-100123", text="hello world"))
    assert result.status == "success"
    assert result.data.needs_confirmation is True
    assert result.ui is not None


@pytest.mark.asyncio
async def test_preview_shows_channel_title_and_reminder():
    ctx = await _seeded_ctx()
    result = await handlers_publish.post_to_channel(
        ctx, PostToChannelParams(channel_id="-100123", text="<b>hello</b>"))
    assert "Test Channel" in result.summary
    assert "nothing sent yet" in result.summary.lower()
    assert result.ui.to_dict() is not None


@pytest.mark.asyncio
async def test_confirm_true_actually_posts():
    ctx = await _seeded_ctx()
    ctx.http.mock_post("api.telegram.org", {
        "ok": True,
        "result": {"message_id": 42, "chat": {"username": "test_channel"}},
    })
    result = await handlers_publish.post_to_channel(
        ctx, PostToChannelParams(channel_id="-100123", text="hello world", confirm=True))
    assert result.status == "success"
    assert result.data.message_id == 42
    assert result.data.needs_confirmation is False


@pytest.mark.asyncio
async def test_preview_still_blocks_on_cannot_post():
    ctx = await _seeded_ctx(can_post=False)
    result = await handlers_publish.post_to_channel(
        ctx, PostToChannelParams(channel_id="-100123", text="hello"))
    assert result.status == "error"


@pytest.mark.asyncio
async def test_preview_blocks_on_unknown_channel():
    ctx = make_ctx()
    result = await handlers_publish.post_to_channel(
        ctx, PostToChannelParams(channel_id="-999999", text="hello"))
    assert result.status == "error"


async def _seeded_ctx(can_post=True):
    ctx = make_ctx()
    await seed_channel(ctx, can_post=can_post)
    return ctx
