"""Tests for generate_draft — AI draft writing within Telegram's post limits,
optionally tone-matched to a channel's own recent public posts.
"""
import pytest

from tests.conftest import make_ctx, seed_channel

import handlers_generate
from models import GenerateDraftParams


async def _seeded_ctx(**channel_kwargs):
    ctx = make_ctx()
    await seed_channel(ctx, **channel_kwargs)
    return ctx


@pytest.mark.asyncio
async def test_generate_draft_without_tone_sample():
    ctx = await _seeded_ctx()
    ctx.ai.set_response("Write a new post about: launch day", "<b>We're live!</b> Launch day is here.")
    result = await handlers_generate.generate_draft(
        ctx, GenerateDraftParams(channel_id="-100123", brief="launch day", sample_size=0))
    assert result.status == "success"
    assert result.data.text == "<b>We're live!</b> Launch day is here."
    assert result.data.based_on_sample is False
    assert result.data.sample_count == 0


@pytest.mark.asyncio
async def test_generate_draft_samples_public_posts_for_tone():
    ctx = await _seeded_ctx(chat_username="testchannel")
    # MockHTTP.mock_get only stores a dict body by design; the scraper needs
    # real HTML text, so register the mock directly with a string body.
    ctx.http.mock_get("t.me/s/testchannel", {})
    ctx.http._mocks[-1] = (
        "GET", "t.me/s/testchannel",
        '<div class="tgme_widget_message_text">Hello from the past post!</div>',
        200, {},
    )
    ctx.ai.set_response("Write a new post about: launch day", "New post matching tone.")
    result = await handlers_generate.generate_draft(
        ctx, GenerateDraftParams(channel_id="-100123", brief="launch day", sample_size=5))
    assert result.status == "success"
    assert result.data.based_on_sample is True
    assert result.data.sample_count == 1


@pytest.mark.asyncio
async def test_generate_draft_channel_not_found():
    ctx = make_ctx()
    result = await handlers_generate.generate_draft(
        ctx, GenerateDraftParams(channel_id="-100999", brief="hi"))
    assert result.status == "error"


@pytest.mark.asyncio
async def test_generate_draft_truncates_to_char_limit():
    ctx = await _seeded_ctx()
    long_text = "x" * 5000
    ctx.ai.set_response("Write a new post about: long", long_text)
    result = await handlers_generate.generate_draft(
        ctx, GenerateDraftParams(channel_id="-100123", brief="long", sample_size=0))
    assert result.status == "success"
    assert len(result.data.text) == 4096


@pytest.mark.asyncio
async def test_generate_draft_photo_uses_caption_limit():
    ctx = await _seeded_ctx()
    long_text = "y" * 2000
    ctx.ai.set_response("Write a new post about: photo", long_text)
    result = await handlers_generate.generate_draft(
        ctx, GenerateDraftParams(channel_id="-100123", brief="photo", has_photo=True, sample_size=0))
    assert result.status == "success"
    assert len(result.data.text) == 1024
