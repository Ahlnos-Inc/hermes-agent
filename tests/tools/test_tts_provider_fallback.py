"""Behavior tests for the one-shot TTS provider fallback."""

import json
from pathlib import Path

from tools import tts_tool


async def _write_edge_output(_text: str, output_path: str, config: dict) -> str:
    assert config["provider"] == "edge"
    assert config["edge"]["voice"] == "en-US-BrianNeural"
    assert "fallback" not in config
    Path(output_path).write_bytes(b"edge-audio")
    return output_path


def _gemini_with_edge_fallback() -> dict:
    return {
        "provider": "gemini",
        "gemini": {"voice": "Kore"},
        "fallback": {
            "provider": "edge",
            "edge": {"voice": "en-US-BrianNeural"},
        },
    }


def test_runtime_failure_retries_once_with_fallback_provider(tmp_path, monkeypatch):
    calls = {"gemini": 0, "edge": 0}

    def fail_gemini(*_args):
        calls["gemini"] += 1
        raise RuntimeError("429 quota exhausted")

    async def write_edge(*args):
        calls["edge"] += 1
        return await _write_edge_output(*args)

    monkeypatch.setattr(tts_tool, "_load_tts_config", _gemini_with_edge_fallback)
    monkeypatch.setattr(tts_tool, "_generate_gemini_tts", fail_gemini)
    monkeypatch.setattr(tts_tool, "_import_edge_tts", lambda: object())
    monkeypatch.setattr(tts_tool, "_generate_edge_tts", write_edge)

    output = tmp_path / "reply.mp3"
    result = json.loads(tts_tool.text_to_speech_tool("hello", str(output)))

    assert result["success"] is True
    assert result["provider"] == "edge"
    assert result["file_path"] == str(output)
    assert calls == {"gemini": 1, "edge": 1}


def test_fallback_failure_does_not_recurse(tmp_path, monkeypatch, caplog):
    calls = {"gemini": 0, "edge": 0}

    def fail_gemini(*_args):
        calls["gemini"] += 1
        raise RuntimeError("primary failed")

    async def fail_edge(*_args):
        calls["edge"] += 1
        raise ConnectionError("fallback failed")

    monkeypatch.setattr(tts_tool, "_load_tts_config", _gemini_with_edge_fallback)
    monkeypatch.setattr(tts_tool, "_generate_gemini_tts", fail_gemini)
    monkeypatch.setattr(tts_tool, "_import_edge_tts", lambda: object())
    monkeypatch.setattr(tts_tool, "_generate_edge_tts", fail_edge)

    result = json.loads(
        tts_tool.text_to_speech_tool("hello", str(tmp_path / "reply.mp3"))
    )

    assert result["success"] is False
    assert "TTS generation failed (edge): fallback failed" in result["error"]
    assert calls == {"gemini": 1, "edge": 1}
    assert sum("falling back to 'edge'" in record.message for record in caplog.records) == 1


def test_no_fallback_preserves_primary_error(tmp_path, monkeypatch):
    calls = {"gemini": 0}

    def fail_gemini(*_args):
        calls["gemini"] += 1
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(
        tts_tool,
        "_load_tts_config",
        lambda: {"provider": "gemini", "gemini": {"voice": "Kore"}},
    )
    monkeypatch.setattr(tts_tool, "_generate_gemini_tts", fail_gemini)

    result = json.loads(
        tts_tool.text_to_speech_tool("hello", str(tmp_path / "reply.mp3"))
    )

    assert result["success"] is False
    assert "TTS generation failed (gemini): provider unavailable" in result["error"]
    assert calls == {"gemini": 1}


def test_same_provider_fallback_is_ignored(tmp_path, monkeypatch):
    calls = {"gemini": 0}

    def fail_gemini(*_args):
        calls["gemini"] += 1
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(
        tts_tool,
        "_load_tts_config",
        lambda: {
            "provider": "gemini",
            "fallback": {"provider": "GEMINI"},
        },
    )
    monkeypatch.setattr(tts_tool, "_generate_gemini_tts", fail_gemini)

    result = json.loads(
        tts_tool.text_to_speech_tool("hello", str(tmp_path / "reply.mp3"))
    )

    assert result["success"] is False
    assert calls == {"gemini": 1}
