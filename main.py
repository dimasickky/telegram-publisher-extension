"""Entrypoint for the web-kernel and CLI tools (imperal validate/build).
Sets up sys.path, purges stale module cache, then imports ext/chat and all
handler modules so their decorators register on the same Extension instance.
"""
import os
import sys

_EXT_DIR = os.path.dirname(os.path.abspath(__file__))
if _EXT_DIR not in sys.path:
    sys.path.insert(0, _EXT_DIR)

# Purge stale cached modules so a fresh load always registers decorators correctly
# (the validator may run multiple extensions in the same process).
_LOCAL = ("app", "handlers_connect", "handlers_publish", "handlers_read",
          "models", "storage", "telegram_client", "error_codes", "skeleton",
          "panels")
for _mod in _LOCAL:
    sys.modules.pop(_mod, None)

from app import ext, chat  # noqa: E402,F401
import handlers_connect  # noqa: E402,F401
import handlers_publish  # noqa: E402,F401
import handlers_read  # noqa: E402,F401
import skeleton  # noqa: E402,F401
import panels  # noqa: E402,F401
