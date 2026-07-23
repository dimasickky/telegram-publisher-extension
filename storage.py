"""telegram-publisher · per-user persistence (own storage, no shared backend).

Collections (all auto-partitioned by ctx.user.imperal_id, except the two
explicitly built under the shared "__webhook__" pseudo-user via `_store_for`
— same trick github-connector's storage.py uses for its install-flow state):

- `tg_link_codes` (shared "__webhook__" partition) — one-shot deep-link code
  minted by `connect_telegram` (authenticated), consumed by the webhook once
  the user hits Start in Telegram. TTL enforced by the caller checking
  `created_at`, not natively by storage.
- `tg_user_index` (shared "__webhook__" partition) — reverse index
  telegram_user_id -> imperal_id, written once the link code above is
  consumed. Lets the UNAUTHENTICATED webhook resolve a real user from just
  the telegram_user_id Telegram puts in every update (my_chat_member,
  message, channel_post authorship), the same role github-connector's
  gh_installation_index plays for installation_id.
- `tg_user_link` (the real user's OWN partition) — {telegram_user_id,
  linked_at}, so an authenticated chat.function (e.g. get_connection status)
  can read it back without needing the reverse index.
- `tg_channels` (the real user's OWN partition) — one document per bound
  channel: {chat_id, chat_title, chat_type, can_post, linked_at}. Same shape
  as wp-site-connector's `sites` collection (N per user).
"""

LINK_CODES_COLLECTION = "tg_link_codes"          # shared "__webhook__" partition
USER_INDEX_COLLECTION = "tg_user_index"          # shared "__webhook__" partition
USER_LINK_COLLECTION = "tg_user_link"            # per-user own partition
CHANNELS_COLLECTION = "tg_channels"              # per-user own partition


def _store_for(ctx, user_id: str):
    """Build a StoreClient scoped to an arbitrary user_id, reusing ctx.store's
    own gateway/auth/tenant wiring (only user_id differs) — identical
    rationale/implementation to github-connector's storage._store_for: a
    webhook ctx's identity is the pseudo-user "__webhook__" (Extension.webhook's
    docstring), not "__system__", so ctx.as_user() doesn't apply here; this
    sidesteps that by building the StoreClient directly.

    Falls back to ctx.store itself when the gateway attributes aren't present
    (imperal_sdk.testing's MockStore — a plain in-memory dict used in tests,
    which never crosses a real user boundary).
    """
    if not hasattr(ctx.store, "_gateway_url"):
        return ctx.store
    from imperal_sdk.store.client import StoreClient
    return StoreClient(
        gateway_url=ctx.store._gateway_url,
        service_token=ctx.store._auth_token,
        extension_id=ctx.store._extension_id,
        user_id=user_id,
        tenant_id=ctx.store._tenant_id,
    )


# ── Link-code flow (connect_telegram -> webhook consumes on /start) ───────── #

async def save_link_code(ctx, code: str, imperal_id: str, created_at: str) -> None:
    """Called from the authenticated `connect_telegram` chat.function, before
    handing the user the t.me deep-link. Written into the shared "__webhook__"
    partition since the webhook that consumes it has no identity of its own.
    Stores both a human-readable `created_at` and an epoch `created_ts` — the
    latter is what find_and_consume_link_code actually checks for TTL."""
    import time as _time
    store = _store_for(ctx, "__webhook__")
    await store.create(LINK_CODES_COLLECTION, {
        "code": code, "imperal_id": imperal_id, "created_at": created_at, "created_ts": _time.time(),
    })


async def find_and_consume_link_code(webhook_ctx, code: str, ttl_seconds: int = 900) -> str | None:
    """Called from the unauthenticated webhook when a /start <code> message
    arrives. One-shot: deletes on match (valid OR expired — an expired code
    must not be usable a second time either). Returns the imperal_id that
    owns this code, or None if unknown/already consumed/expired.

    TTL is enforced HERE (not left to the caller) so there's exactly one
    place this can get wrong — matches github-connector's
    find_and_consume_oauth_state doing the same for its own state tokens."""
    import time as _time
    store = _store_for(webhook_ctx, "__webhook__")
    page = await store.query(LINK_CODES_COLLECTION, limit=200)
    for doc in page.data:
        if doc.data.get("code") == code:
            owner = doc.data.get("imperal_id")
            created_ts = doc.data.get("created_ts", 0)
            await store.delete(LINK_CODES_COLLECTION, doc.id)
            if created_ts and (_time.time() - created_ts) > ttl_seconds:
                return None
            return owner
    return None


async def bind_telegram_user(webhook_ctx, imperal_id: str, telegram_user_id: int, linked_at: str) -> None:
    """Called from the webhook once a link code resolves to a real imperal_id.
    Writes BOTH: the real user's own record (tg_user_link, so authenticated
    reads don't need the reverse index) AND the shared reverse index
    (tg_user_index) the webhook itself needs to resolve future updates from
    this telegram_user_id back to this imperal_id."""
    user_store = _store_for(webhook_ctx, imperal_id)
    existing = await user_store.query(USER_LINK_COLLECTION, limit=1)
    record = {"telegram_user_id": telegram_user_id, "linked_at": linked_at}
    if existing.data:
        await user_store.update(USER_LINK_COLLECTION, existing.data[0].id, record)
    else:
        await user_store.create(USER_LINK_COLLECTION, record)

    index_store = _store_for(webhook_ctx, "__webhook__")
    idx_existing = await index_store.query(USER_INDEX_COLLECTION, where={"telegram_user_id": telegram_user_id}, limit=1)
    idx_record = {"telegram_user_id": telegram_user_id, "imperal_id": imperal_id}
    if idx_existing.data:
        await index_store.update(USER_INDEX_COLLECTION, idx_existing.data[0].id, idx_record)
    else:
        await index_store.create(USER_INDEX_COLLECTION, idx_record)


async def resolve_imperal_id_for_telegram_user(webhook_ctx, telegram_user_id: int) -> str | None:
    """Look up which real Imperal user owns a given telegram_user_id — used
    by the unauthenticated webhook to attribute my_chat_member/channel events."""
    index_store = _store_for(webhook_ctx, "__webhook__")
    page = await index_store.query(USER_INDEX_COLLECTION, where={"telegram_user_id": telegram_user_id}, limit=1)
    if page.data:
        return page.data[0].data.get("imperal_id")
    return None


async def get_telegram_user_link(ctx) -> dict | None:
    """Return this authenticated user's own {telegram_user_id, linked_at} record, or None."""
    page = await ctx.store.query(USER_LINK_COLLECTION, limit=1)
    return page.data[0].data if page.data else None


# ── Channel records (N per user, same shape as wp-site-connector's `sites`) ── #

async def list_channel_records(ctx) -> list[dict]:
    page = await ctx.store.query(CHANNELS_COLLECTION, limit=100)
    return [doc.data for doc in page.data]


async def _find_channel_doc(ctx, chat_id):
    page = await ctx.store.query(CHANNELS_COLLECTION, limit=100)
    for doc in page.data:
        if str(doc.data.get("chat_id")) == str(chat_id):
            return doc
    return None


async def get_channel_record(ctx, chat_id):
    doc = await _find_channel_doc(ctx, chat_id)
    return doc.data if doc else None


async def save_channel_record_for_user(webhook_ctx, imperal_id: str, record: dict) -> None:
    """Called from the unauthenticated webhook (my_chat_member handler) once
    admin + can_post_messages have both been verified — writes into the real
    user's own partition (via _store_for), not "__webhook__"."""
    store = _store_for(webhook_ctx, imperal_id)
    existing = None
    page = await store.query(CHANNELS_COLLECTION, limit=100)
    for doc in page.data:
        if str(doc.data.get("chat_id")) == str(record.get("chat_id")):
            existing = doc
            break
    if existing:
        await store.update(CHANNELS_COLLECTION, existing.id, record)
    else:
        await store.create(CHANNELS_COLLECTION, record)


async def delete_channel_record(ctx, chat_id) -> bool:
    doc = await _find_channel_doc(ctx, chat_id)
    if doc:
        await ctx.store.delete(CHANNELS_COLLECTION, doc.id)
        return True
    return False


async def mark_channel_disconnected_for_user(webhook_ctx, imperal_id: str, chat_id) -> None:
    """Called from the webhook when my_chat_member reports the bot was
    removed/demoted (status left/kicked/member-without-post-rights) — flips
    the stored record's can_post to False rather than deleting it outright,
    so the user still sees it (as disconnected) and can re-invite the bot."""
    store = _store_for(webhook_ctx, imperal_id)
    page = await store.query(CHANNELS_COLLECTION, limit=100)
    for doc in page.data:
        if str(doc.data.get("chat_id")) == str(chat_id):
            updated = dict(doc.data)
            updated["can_post"] = False
            await store.update(CHANNELS_COLLECTION, doc.id, updated)
            return
