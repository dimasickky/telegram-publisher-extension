"""Tests for paginated history reading, batch analysis and the digest cache.

The bug these lock down: every read path used to fetch exactly ONE page of
t.me/s/ (20 posts) and slice it, so any request for more silently returned at
most 20 — "analyse the channel" meant "glance at the most recent 20". These
tests assert real cursor paging, and that the skeleton reads the cached digest
instead of scraping (a network fetch on the ambient refresh timer would be a
slow-loop hazard for every user).
"""
import pytest

from tests.conftest import make_ctx, seed_channel

import handlers_read
import handlers_analyze
import skeleton
import storage
from models import AnalyzePostsParams, GetRecentPostsParams


def _page_html(ids, prefix="post"):
    """Build a t.me/s/ page the way the real one is shaped: each post's
    data-post attribute immediately precedes its text block."""
    parts = []
    for i in ids:
        parts.append(f'<div class="tgme_widget_message" data-post="testchannel/{i}">')
        parts.append(f'<div class="tgme_widget_message_text">{prefix} {i}</div>')
        parts.append("</div>")
    return "".join(parts)


def _mock_page(ctx, url_fragment, html):
    """Register a GET mock whose body is raw HTML text (MockHTTP.mock_get only
    stores dict bodies, but the scraper needs a string)."""
    ctx.http.mock_get(url_fragment, {})
    ctx.http._mocks[-1] = ("GET", url_fragment, html, 200, {})


@pytest.mark.asyncio
async def test_parse_page_pairs_ids_with_texts():
    pairs = handlers_read._parse_page(_page_html([10, 11, 12]))
    assert pairs == [(10, "post 10"), (11, "post 11"), (12, "post 12")]


@pytest.mark.asyncio
async def test_parse_page_keeps_text_when_ids_are_missing():
    """Ids are navigation, text is the payload: a layout change that hides
    data-post must not turn a live channel into 'no posts found'."""
    html = '<div class="tgme_widget_message_text">orphan text</div>'
    assert handlers_read._parse_page(html) == [(0, "orphan text")]


@pytest.mark.asyncio
async def test_fetch_walks_back_past_the_first_page():
    """A limit above one page must page backwards, not silently cap at 20."""
    ctx = make_ctx()
    # Page 1 = ids 21..40 (newest), page 2 = ids 1..20, reached via ?before=21.
    _mock_page(ctx, "t.me/s/testchannel?before=21", _page_html(range(1, 21)))
    _mock_page(ctx, "t.me/s/testchannel", _page_html(range(21, 41)))

    texts, reason = await handlers_read._fetch_recent_post_texts(
        ctx, {"chat_username": "testchannel"}, limit=30)

    assert reason == ""
    assert len(texts) == 30           # not clamped to a single page of 20
    assert texts[-1] == "post 40"     # newest last
    assert texts[0] == "post 11"      # walked into page 2


@pytest.mark.asyncio
async def test_fetch_stops_early_on_short_channel():
    ctx = make_ctx()
    _mock_page(ctx, "t.me/s/testchannel", _page_html([1, 2, 3]))
    texts, reason = await handlers_read._fetch_recent_post_texts(
        ctx, {"chat_username": "testchannel"}, limit=100)
    assert reason == ""
    assert len(texts) == 3


@pytest.mark.asyncio
async def test_analyze_writes_digest_and_reports_metrics():
    ctx = make_ctx()
    await seed_channel(ctx, chat_username="testchannel")
    _mock_page(ctx, "t.me/s/testchannel?before=21", _page_html(range(1, 21)))
    _mock_page(ctx, "t.me/s/testchannel", _page_html(range(21, 41)))

    result = await handlers_analyze.analyze_channel_posts(
        ctx, AnalyzePostsParams(channel_id="-100123", max_posts=40))

    assert result.status == "success"
    assert result.data.posts_scanned == 40
    assert result.data.pages_fetched >= 2      # proof it batched
    assert result.data.median_length > 0

    cached = await storage.get_post_digest(ctx, "-100123")
    assert cached is not None
    assert cached["posts_scanned"] == 40


@pytest.mark.asyncio
async def test_analyze_refuses_private_channel_without_preview():
    ctx = make_ctx()
    await seed_channel(ctx)  # no chat_username
    result = await handlers_analyze.analyze_channel_posts(
        ctx, AnalyzePostsParams(channel_id="-100123"))
    assert result.status == "error"
    assert result.error_code == "TG_NO_PUBLIC_PREVIEW"


@pytest.mark.asyncio
async def test_skeleton_serves_digest_without_any_http():
    """The ambient snapshot must come from cache — never from the network."""
    ctx = make_ctx()
    await seed_channel(ctx, chat_username="testchannel")
    await storage.save_post_digest(ctx, {
        "chat_id": "-100123", "posts_scanned": 40, "median_length": 120,
        "top_words": ["launch", "update"],
        "recent_previews": [f"preview {i}" for i in range(15)],
        "reached_start": True,
    })

    snap = (await skeleton.channels_overview(ctx))["response"]

    assert snap["channels"][0]["posts"] == 40
    assert snap["channels"][0]["full_scan"] is True
    # Previews live at SECTION level, not inside the channel entry: the
    # kernel's renderer drops nested lists from list items entirely.
    assert "recent_posts" not in snap["channels"][0]
    assert len(snap["recent_posts"]) == skeleton._SKELETON_RECENT
    # No GET was issued while building ambient context.
    assert not any(c[0] == "GET" for c in getattr(ctx.http, "calls", []))


@pytest.mark.asyncio
async def test_skeleton_shape_survives_classifier_budgets():
    """Guard the three renderer rules this snapshot is shaped around.

    The kernel projects a skeleton section into the classifier envelope through
    hard caps (imperal_kernel/hub/classifier/skeleton_summary.py): nested
    dict/list values inside a list item are SKIPPED, only the first 6 scalar
    fields of an item render, and each item is cut at ~110 chars. A shape that
    ignores those caps is not a loud failure — the data just silently never
    reaches the brain, which is exactly the bug this replaced. So the contract
    is asserted here rather than trusted.
    """
    ctx = make_ctx()
    await seed_channel(ctx, chat_username="testchannel")
    await storage.save_post_digest(ctx, {
        "chat_id": "-100123", "posts_scanned": 7, "median_length": 749,
        "top_words": ["alpha", "beta", "gamma", "delta", "epsilon"],
        "recent_previews": [
            "Headline one\n\nBody paragraph that continues well past the preview cap "
            "to prove the trailing text is dropped rather than wrapped.",
            "Second post\nwith a newline",
        ],
        "reached_start": True,
    })

    snap = (await skeleton.channels_overview(ctx))["response"]
    entry = snap["channels"][0]

    # 1. Every value on a channel entry is a scalar, or it vanishes in render.
    for key, value in entry.items():
        assert not isinstance(value, (dict, list)), f"{key} would be dropped"

    # 2. An entry stays within the render window. The cap counts only the
    #    fields rendered as k=v: the kernel consumes one label key (`title`)
    #    and one id key (`id`) into the item header, so those two are free and
    #    the 6-field budget applies to what is left. Verified directly against
    #    the kernel renderer: a 7th payload field is silently dropped.
    payload = [k for k in entry if k not in ("id", "title")]
    assert len(payload) <= 6, f"fields past the 6th never render: {payload}"

    # 3. No preview carries a newline — the envelope is one line per section,
    #    so an embedded newline would corrupt the block.
    for post in snap["recent_posts"]:
        assert "\n" not in post["title"]
        assert len(post["title"]) <= skeleton._PREVIEW_CHARS
        # `title` is one of the kernel's label keys — that is what makes the
        # item render its content instead of collapsing to an opaque count.
        assert post["title"]


@pytest.mark.asyncio
async def test_skeleton_omits_post_fields_before_any_analysis():
    ctx = make_ctx()
    await seed_channel(ctx, chat_username="testchannel")
    snap = (await skeleton.channels_overview(ctx))["response"]
    assert "posts" not in snap["channels"][0]
    assert snap["recent_posts"] == []
