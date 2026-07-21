"""Behavioral regression coverage for compression session-id persistence.

When the agent rotates a session while compressing, the gateway must update its
live ``SessionEntry`` and persist that routing change before the next inbound
turn. Otherwise a restarted gateway reloads the pre-compression transcript and
compresses it again.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource


SESSION_KEY = "agent:main:telegram:group:-1001:12345"
ORIGINAL_SESSION_ID = "sess-before-compression"
ROTATED_SESSION_ID = "sess-after-compression"


def _bootstrap_runner(monkeypatch, tmp_path) -> tuple[gateway_run.GatewayRunner, SessionEntry]:
    """Build a runner that drives the production post-agent persistence path."""
    runner = gateway_run.GatewayRunner(GatewayConfig())
    entry = SessionEntry(
        session_key=SESSION_KEY,
        session_id=ORIGINAL_SESSION_ID,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="group",
    )

    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._handle_active_session_busy_message = AsyncMock(return_value=False)
    runner._session_db = MagicMock()
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._cache_session_source = lambda _key, _source: None
    runner._is_session_run_current = lambda _key, _generation: True
    runner._begin_session_run_generation = lambda _key: 1
    runner._reply_anchor_for_event = lambda _event: None
    runner._get_guild_id = lambda _event: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._sync_telegram_topic_binding = MagicMock()
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()

    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.has_platform_message_id.return_value = False
    runner.session_store.update_session = MagicMock()

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "test-key"}
    )
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100_000,
    )
    return runner, entry


def _event() -> MessageEvent:
    return MessageEvent(
        text="compress this conversation",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-1001",
            chat_type="group",
            user_id="12345",
        ),
        message_id="message-1",
    )


def _agent_result(session_id: str) -> dict[str, object]:
    return {
        "final_response": "Compression completed.",
        "messages": [
            {"role": "user", "content": "compress this conversation"},
            {"role": "assistant", "content": "Compression completed."},
        ],
        "tools": [],
        "history_offset": 0,
        "last_prompt_tokens": 0,
        "session_id": session_id,
    }


@pytest.mark.asyncio
async def test_agent_compression_rotation_persists_gateway_session_mapping(monkeypatch, tmp_path):
    """A rotated agent session is saved through the real gateway handler.

    This runs ``GatewayRunner._handle_message_with_agent`` rather than copying
    its post-agent conditional into the test. A regression that removes the
    handler's ``_save()`` call therefore leaves the in-memory entry updated but
    fails this persistence assertion.
    """
    runner, entry = _bootstrap_runner(monkeypatch, tmp_path)
    runner._run_agent = AsyncMock(return_value=_agent_result(ROTATED_SESSION_ID))

    await runner._handle_message_with_agent(_event(), _event().source, SESSION_KEY, 1)

    assert entry.session_id == ROTATED_SESSION_ID
    runner.session_store._save.assert_called_once_with()
    runner.session_store._record_gateway_session_peer.assert_called_once_with(
        ROTATED_SESSION_ID, SESSION_KEY, _event().source
    )


@pytest.mark.asyncio
async def test_gateway_does_not_persist_mapping_when_agent_session_is_unchanged(monkeypatch, tmp_path):
    """Normal turns avoid an unnecessary routing-index write."""
    runner, entry = _bootstrap_runner(monkeypatch, tmp_path)
    runner._run_agent = AsyncMock(return_value=_agent_result(ORIGINAL_SESSION_ID))

    await runner._handle_message_with_agent(_event(), _event().source, SESSION_KEY, 1)

    assert entry.session_id == ORIGINAL_SESSION_ID
    runner.session_store._save.assert_not_called()
    runner.session_store._record_gateway_session_peer.assert_not_called()
