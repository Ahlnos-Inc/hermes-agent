"""
Merge-survival guard for the fork's ``platform`` kwarg on
``text_to_speech_tool`` (tools/tts_tool.py).

The adapter-level auto-TTS path (gateway/platforms/base.py) calls:
    _tts_platform = getattr(self.platform, "value", None) or str(self.platform)
    text_to_speech_tool(..., platform=_tts_platform)

That call happens OUTSIDE the agent-run task, where the
``HERMES_SESSION_PLATFORM`` contextvar is unset — so the kwarg is the only
way adapter-level auto-TTS gets Opus/voice-note delivery on Telegram. If a
future merge drops the ``platform`` kwarg (falling back to the env var
only), this test must fail.

Mirrors tests/tools/test_tts_opus_routing.py but drives the kwarg directly
instead of the env var, with the env var explicitly cleared to prove the
kwarg — not the env fallback — is what's driving ``want_opus``.
"""
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from gateway.session_context import _UNSET, _VAR_MAP
from tools import tts_tool


def _reset_session_context() -> None:
    for var in _VAR_MAP.values():
        var.set(_UNSET)


@pytest.fixture(autouse=True)
def _clean_session_platform(monkeypatch):
    _reset_session_context()
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    yield
    _reset_session_context()


async def _write_edge_output(_text: str, output_path: str, _tts_config: dict) -> str:
    Path(output_path).write_bytes(b"mp3")
    return output_path


def test_platform_kwarg_routes_opus_voice_without_env_var(tmp_path, monkeypatch):
    """platform="telegram" kwarg alone (no env var) must trigger opus conversion."""
    out = tmp_path / "speech.mp3"
    opus = tmp_path / "speech.ogg"

    def fake_convert(path: str) -> str:
        assert path == str(out)
        opus.write_bytes(b"ogg")
        return str(opus)

    convert = Mock(side_effect=fake_convert)

    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {"provider": "edge"})
    monkeypatch.setattr(tts_tool, "_import_edge_tts", lambda: object())
    monkeypatch.setattr(tts_tool, "_generate_edge_tts", _write_edge_output)
    monkeypatch.setattr(tts_tool, "_convert_to_opus", convert)

    # HERMES_SESSION_PLATFORM is unset (autouse fixture clears it) — only the
    # explicit kwarg can be driving want_opus here.
    result = json.loads(
        tts_tool.text_to_speech_tool(
            "hi", output_path=str(out), platform="telegram"
        )
    )

    assert result["success"] is True
    assert result["file_path"] == str(opus)
    assert result["voice_compatible"] is True
    assert result["media_tag"] == f"[[audio_as_voice]]\nMEDIA:{opus}"
    convert.assert_called_once_with(str(out))


def test_no_platform_kwarg_and_no_env_var_stays_mp3(tmp_path, monkeypatch):
    """Without the kwarg (and no env var), output stays native mp3 — no opus."""
    out = tmp_path / "speech.mp3"
    convert = Mock()

    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {"provider": "edge"})
    monkeypatch.setattr(tts_tool, "_import_edge_tts", lambda: object())
    monkeypatch.setattr(tts_tool, "_generate_edge_tts", _write_edge_output)
    monkeypatch.setattr(tts_tool, "_convert_to_opus", convert)

    result = json.loads(tts_tool.text_to_speech_tool("hi", output_path=str(out)))

    assert result["success"] is True
    assert result["voice_compatible"] is False
    assert result["file_path"] == str(out)
    convert.assert_not_called()
