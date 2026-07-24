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

# ── prompt shape: length is a TARGET, not just a ceiling ──────────────────── #

def test_prompt_states_a_target_length_not_only_the_cap():
    """With only "max N characters" in the prompt, the model lands far under it and
    produces three-sentence stubs that read like placeholders in a real feed. The
    cap is a hard limit; the desired length has to be stated separately."""
    prompt = handlers_generate._build_prompt("about a footballer", 4096, [])
    assert "4096" in prompt, "the hard cap is still declared"
    assert "900" in prompt and "1500" in prompt, "a target range is declared too"
    assert "TARGET, not the limit" in prompt


def test_prompt_asks_for_the_hook_context_why_link_shape():
    prompt = handlers_generate._build_prompt("about a footballer", 4096, [])
    for expected in ("hook", "context", "why it matters", "close with"):
        assert expected in prompt, f"prompt should ask for: {expected}"
    assert "Never pad" in prompt, "length must not be reached by padding"


def test_photo_caption_target_fits_the_shorter_cap():
    """A photo post is capped at 1024, so the 900-1500 target would exceed it."""
    prompt = handlers_generate._build_prompt("about a footballer", 1024, [])
    assert "600" in prompt and "950" in prompt
    assert "1500" not in prompt


def test_sample_tone_matching_does_not_override_the_length_target():
    """Style samples say "match their length" — if the channel's existing posts are
    short stubs, that instruction would perpetuate exactly the problem."""
    prompt = handlers_generate._build_prompt("x", 4096, ["tiny post"])
    assert "tone" in prompt.lower()
    assert "TARGET, not the limit" in prompt
