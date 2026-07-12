"""
Merge-survival guard for the fork's opt-in
``platforms.telegram.extra.voice_via_send_audio`` flag
(plugins/platforms/telegram/adapter.py::TelegramAdapter.send_voice).

Wear OS Telegram doesn't render bot-sent sendVoice bubbles at all, but
Telegram's server coerces the same .ogg/.opus payload into a playable voice
note when it arrives via sendAudio. The flag routes .ogg/.opus through
bot.send_audio (media key "audio") instead of bot.send_voice (media key
"voice") when opted in. If a future merge drops the flag or the branch,
.ogg/.opus always goes to send_voice regardless of config and this test
must fail.

Mirrors the _make_adapter / connected_adapter pattern used by
TestSendVoice in tests/gateway/test_telegram_documents.py.
"""
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig


def _ensure_telegram_mock():
    """Install mock telegram modules so TelegramAdapter can be imported."""
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return

    telegram_mod = MagicMock()
    telegram_mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    telegram_mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    telegram_mod.constants.ChatType.GROUP = "group"
    telegram_mod.constants.ChatType.SUPERGROUP = "supergroup"
    telegram_mod.constants.ChatType.CHANNEL = "channel"
    telegram_mod.constants.ChatType.PRIVATE = "private"

    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, telegram_mod)


_ensure_telegram_mock()

from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


def _make_adapter(voice_via_send_audio: bool) -> TelegramAdapter:
    config = PlatformConfig(
        enabled=True,
        token="fake-token",
        extra={"voice_via_send_audio": voice_via_send_audio},
    )
    adapter = TelegramAdapter(config)
    bot = AsyncMock()
    bot.send_voice = AsyncMock(return_value=SimpleNamespace(message_id=201))
    bot.send_audio = AsyncMock(return_value=SimpleNamespace(message_id=202))
    adapter._bot = bot
    return adapter


@pytest.mark.asyncio
async def test_flag_true_routes_ogg_through_send_audio(tmp_path):
    """voice_via_send_audio=True: .ogg goes through bot.send_audio with media key 'audio'."""
    adapter = _make_adapter(voice_via_send_audio=True)
    audio_file = tmp_path / "voice.ogg"
    audio_file.write_bytes(b"OggS" + b"\x00" * 16)

    result = await adapter.send_voice(chat_id="12345", audio_path=str(audio_file))

    assert result.success is True
    adapter._bot.send_audio.assert_awaited_once()
    adapter._bot.send_voice.assert_not_awaited()
    _, kwargs = adapter._bot.send_audio.call_args
    assert "audio" in kwargs
    assert "voice" not in kwargs


@pytest.mark.asyncio
async def test_flag_false_routes_ogg_through_send_voice(tmp_path):
    """voice_via_send_audio=False (default): .ogg goes through bot.send_voice with media key 'voice'."""
    adapter = _make_adapter(voice_via_send_audio=False)
    audio_file = tmp_path / "voice.ogg"
    audio_file.write_bytes(b"OggS" + b"\x00" * 16)

    result = await adapter.send_voice(chat_id="12345", audio_path=str(audio_file))

    assert result.success is True
    adapter._bot.send_voice.assert_awaited_once()
    adapter._bot.send_audio.assert_not_awaited()
    _, kwargs = adapter._bot.send_voice.call_args
    assert "voice" in kwargs
    assert "audio" not in kwargs
