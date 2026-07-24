"""Shared pytest fixtures for telegram-publisher tests.

Seeds ctx.secrets with a throwaway bot token/webhook secret, mirroring the
pattern in github-connector/tests/conftest.py (seed the real config values
the handlers read, rather than mocking ctx.secrets.get itself).
"""
import pytest

from imperal_sdk.testing import MockContext, MockSecretStore

TEST_BOT_TOKEN = "123456:TEST-bot-token-not-real"
TEST_WEBHOOK_SECRET = "test-webhook-secret"


def make_ctx(user_id="user-1"):
    ctx = MockContext(user_id=user_id)
    ctx.secrets = MockSecretStore({
        "telegram_bot_token": TEST_BOT_TOKEN,
        "telegram_webhook_secret": TEST_WEBHOOK_SECRET,
    })
    return ctx


async def seed_channel(ctx, chat_id="-100123", chat_title="Test Channel", can_post=True):
    """Write a tg_channels record directly into the user's own store partition,
    the same shape save_channel_record_for_user would produce."""
    await ctx.store.create("tg_channels", {
        "chat_id": chat_id, "chat_title": chat_title, "chat_type": "channel",
        "can_post": can_post, "linked_at": "2026-07-24T00:00:00Z",
    })
