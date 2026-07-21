"""Regression coverage for strict Telegram topic routing."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.platforms.telegram.telegram_ids import (
    TelegramTopicIdError,
    parse_telegram_topic_id,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (2, 2),
        ("2", 2),
        (" 002 ", 2),
        ("2 # Alerts topic", 2),
        ("2#Alerts", 2),
    ],
)
def test_parser_normalizes_supported_positive_topic_ids(raw, expected):
    assert parse_telegram_topic_id(raw, source="test") == expected


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_parser_treats_only_missing_or_blank_topics_as_optional(raw):
    assert parse_telegram_topic_id(raw, source="test") is None


@pytest.mark.parametrize("raw", ["# Alerts", " # Alerts", "#", False, 0, -1, 2.0, "+2", "２"])
def test_parser_rejects_invalid_or_comment_only_topics(raw):
    with pytest.raises(TelegramTopicIdError, match="test"):
        parse_telegram_topic_id(raw, source="test")


def test_parser_fails_closed_on_overlong_digit_string():
    # All-ASCII digits past CPython's int-string conversion limit must raise
    # our typed error, not a bare ValueError a send-site could let escape.
    with pytest.raises(TelegramTopicIdError, match="test"):
        parse_telegram_topic_id("1" * 5000, source="test")


def test_parser_normalizes_int_subclass():
    from enum import IntEnum

    class Topic(IntEnum):
        ALERTS = 7

    result = parse_telegram_topic_id(Topic.ALERTS, source="test")
    assert result == 7 and type(result) is int


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2 # Alerts", "2"),
        ("  ", None),
        ("# Alerts", "# Alerts"),
        (False, "False"),
    ],
)
def test_telegram_home_channel_normalizes_or_quarantines_topics(raw, expected):
    from gateway.config import HomeChannel, Platform

    home = HomeChannel.from_dict(
        {
            "platform": "telegram",
            "chat_id": "-100123",
            "name": "Home",
            "thread_id": raw,
        }
    )

    assert home.platform is Platform.TELEGRAM
    assert home.thread_id == expected


def test_telegram_cron_topic_normalizes_annotation_without_using_lower_precedence(monkeypatch):
    from cron.scheduler import _get_home_target_thread_id

    monkeypatch.setenv("TELEGRAM_CRON_THREAD_ID", "2 # Alerts")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL_THREAD_ID", "99")

    assert _get_home_target_thread_id("telegram") == "2"


def test_adapter_rejects_comment_only_topic_without_falling_back_to_general():
    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))

    result = asyncio.run(
        adapter.send("-100123", "hello", metadata={"thread_id": "# Alerts"})
    )

    assert result.success is False
    assert result.retryable is False
    adapter._bot.send_message.assert_not_awaited()


def test_adapter_invalid_primary_alias_does_not_fall_through_to_secondary():
    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))

    result = asyncio.run(
        adapter.send(
            "-100123",
            "hello",
            metadata={"thread_id": False, "message_thread_id": 2},
        )
    )

    assert result.success is False
    assert result.retryable is False
    adapter._bot.send_message.assert_not_awaited()


def test_adapter_draft_rejects_comment_only_topic_without_bot_call():
    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._bot = MagicMock()
    adapter._bot.send_message_draft = AsyncMock(return_value=True)

    result = asyncio.run(
        adapter.send_draft("123", 1, "draft", metadata={"thread_id": "# Alerts"})
    )

    assert result.success is False
    assert result.retryable is False
    adapter._bot.send_message_draft.assert_not_awaited()
