"""App-declared structured error codes for telegram-connector.

Pairs with the platform taxonomy (imperal_sdk.chat.error_codes) for problems
specific to reaching Telegram's Bot API / this extension's own connect flow —
not the Imperal backend itself. Every code matches the SDK's app-declared
pattern ^[A-Z][A-Z0-9_]{2,63}$ (imperal_sdk.types.action_result.ActionResult.error).
"""

TG_NOT_LINKED = "TG_NOT_LINKED"                     # user has no telegram_user_id bound yet
TG_LINK_EXPIRED = "TG_LINK_EXPIRED"                 # link code expired or already consumed
TG_CHANNEL_NOT_FOUND = "TG_CHANNEL_NOT_FOUND"       # channel_id not in this user's store
TG_NOT_CHANNEL_ADMIN = "TG_NOT_CHANNEL_ADMIN"       # the linked telegram_user_id isn't an admin of that chat
TG_BOT_CANNOT_POST = "TG_BOT_CANNOT_POST"           # bot is admin but lacks can_post_messages
TG_BOT_UNREACHABLE = "TG_BOT_UNREACHABLE"           # network/transport failure calling api.telegram.org
TG_SEND_FAILED = "TG_SEND_FAILED"                   # Bot API returned ok:false on sendMessage/sendPhoto
TG_MESSAGE_TOO_LONG = "TG_MESSAGE_TOO_LONG"          # text exceeds Telegram's post length limits after split
