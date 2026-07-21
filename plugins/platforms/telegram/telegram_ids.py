"""Helpers for Telegram Bot API chat identifiers.

Telegram's Bot API accepts a ``chat_id`` in two forms: a numeric ID (an int,
e.g. ``123456789`` for a DM or ``-1001234567890`` for a channel/supergroup) or
an ``@username`` string for public channels and groups. Hermes historically
coerced every ``chat_id`` with ``int()``, which crashes on the username form
(``ValueError: invalid literal for int()``). Normalizing here lets numeric IDs
pass through as ints while usernames pass through unchanged — both are valid
values for the Bot API.
"""

from __future__ import annotations

import re
from typing import Any, Optional, Union

# Telegram usernames are 5-32 chars: letters, digits, underscores, with a
# leading "@". (Telegram also permits 4-char usernames for some legacy/official
# accounts, but the 5-32 public rule is the safe lower bound for routing.)
_TELEGRAM_USERNAME_RE = re.compile(r"@[A-Za-z0-9_]{4,32}")
_TELEGRAM_TOPIC_ID_RE = re.compile(r"[0-9]+")


class TelegramTopicIdError(ValueError):
    """Raised when a Telegram topic value cannot safely select a route."""


def _invalid_telegram_topic_id(raw: Any, source: str) -> TelegramTopicIdError:
    """Build a bounded diagnostic without allowing control characters into logs."""
    try:
        value = str(raw)
    except Exception:
        value = f"<{type(raw).__name__}>"
    value = value.replace("\r", " ").replace("\n", " ")[:80]
    return TelegramTopicIdError(
        f"Invalid Telegram topic ID for {source}: expected a positive ASCII integer (got {value!r})"
    )


def parse_telegram_topic_id(raw: Any, *, source: str) -> Optional[int]:
    """Return a positive Telegram topic id, ``None`` only for genuine absence.

    Human-readable ``#`` comments are accepted after a positive ASCII decimal
    identifier. A non-empty comment-only or malformed value is never treated as
    an omitted topic, because that would silently authorize a General/root send.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise _invalid_telegram_topic_id(raw, source)
    if isinstance(raw, int):
        # Normalize int subclasses (e.g. IntEnum) to a plain int.
        if raw > 0:
            return int(raw)
        raise _invalid_telegram_topic_id(raw, source)
    if not isinstance(raw, str):
        raise _invalid_telegram_topic_id(raw, source)

    original = raw.strip()
    if not original:
        return None
    token = original.split("#", 1)[0].strip()
    if not _TELEGRAM_TOPIC_ID_RE.fullmatch(token):
        raise _invalid_telegram_topic_id(raw, source)
    try:
        topic_id = int(token)
    except ValueError:
        # token is all ASCII digits, so the only failure is CPython's
        # integer-string-conversion length limit (>4300 digits). Keep the
        # fail-closed contract: raise our typed error, not a bare ValueError
        # that a `except TelegramTopicIdError` send-site would let escape.
        raise _invalid_telegram_topic_id(raw, source)
    if topic_id <= 0:
        raise _invalid_telegram_topic_id(raw, source)
    return topic_id


def normalize_telegram_chat_id(chat_id: Any) -> Union[int, str]:
    """Return a Bot API-compatible chat_id.

    Numeric values (incl. negative channel IDs) are returned as ``int``; any
    non-numeric value (e.g. an ``@username``) is returned as a stripped string.
    Telegram's Bot API accepts both, so this never raises on a username the way
    a bare ``int(chat_id)`` would.
    """
    chat_id_str = str(chat_id).strip()
    try:
        return int(chat_id_str)
    except (TypeError, ValueError):
        return chat_id_str


def telegram_chat_id_key(chat_id: Any) -> str:
    """Stable string key for a chat_id (for dict keys / persisted state)."""
    return str(normalize_telegram_chat_id(chat_id))


def looks_like_telegram_username(chat_id: Any) -> bool:
    """True when the value is an ``@username``-format Telegram chat identifier."""
    return bool(_TELEGRAM_USERNAME_RE.fullmatch(str(chat_id).strip()))


def parse_telegram_username_target(target_ref: Any) -> Union[str, None]:
    """Return the value when it is an ``@username`` target, else ``None``."""
    value = str(target_ref).strip()
    return value if looks_like_telegram_username(value) else None
