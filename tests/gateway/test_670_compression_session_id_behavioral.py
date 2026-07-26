"""Post-compression session_id propagation, through the real GatewayRunner.

BUILD-670: the existing coverage for #29335 asserts the invariant by reading
``gateway/run.py`` with ``inspect.getsource`` + ``ast.parse``, and its
"behavioral" companion mirrors the objects and re-implements the propagation
inline. Neither one calls the production entry point, so both stay green if
``_handle_message_with_agent`` stops propagating entirely — the exact class of
regression #29335 was.

These tests await the real ``GatewayRunner._handle_message_with_agent`` and
assert on what the handler did to the session store. Harness shape is borrowed
from ``tests/gateway/test_42039_duplicate_user_message.py``, which already
drives the same entry point.
"""

import sys
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource

SESSION_KEY = "agent:main:telegram:group:-1001:12345"
START_SESSION_ID = "sess-before-compression"
ROTATED_SESSION_ID = "sess-after-compression"


def _bootstrap(monkeypatch, tmp_path, *, session_id=START_SESSION_ID):
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    runner = gateway_run.GatewayRunner(GatewayConfig())
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
    runner._is_session_run_current = lambda _key, _gen: True
    runner._begin_session_run_generation = lambda _key: 1
    runner._reply_anchor_for_event = lambda _event: None
    runner._get_guild_id = lambda _event: None
    runner._should_send_voice_reply = lambda *_a, **_kw: False
    runner._sync_telegram_topic_binding = MagicMock()
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()

    entry = SessionEntry(
        session_key=SESSION_KEY,
        session_id=session_id,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="group",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.has_platform_message_id.return_value = False
    runner.session_store.update_session = MagicMock()

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"}
    )
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100_000,
    )
    return runner, entry


def _event():
    return MessageEvent(
        text="hello world",
        source=SessionSource(
            platform=Platform.TELEGRAM, chat_id="-1001",
            chat_type="group", user_id="12345",
        ),
        message_id="msg-670",
    )


def _source():
    return SessionSource(
        platform=Platform.TELEGRAM, chat_id="-1001",
        chat_type="group", user_id="12345",
    )


def _agent_result(session_id):
    return {
        "failed": False,
        "final_response": "done",
        "messages": [],
        "history_offset": 0,
        "last_prompt_tokens": 0,
        "session_id": session_id,
    }


async def _run(runner, agent_result):
    runner._run_agent = AsyncMock(return_value=agent_result)
    await runner._handle_message_with_agent(_event(), _source(), SESSION_KEY, 1)


@pytest.mark.asyncio
async def test_rotated_session_id_is_propagated_and_persisted(monkeypatch, tmp_path):
    """The defect: the new id reached memory but never reached disk.

    Without the ``_save()``, the next gateway restart loads the PRE-compression
    transcript and compresses forever.
    """
    runner, entry = _bootstrap(monkeypatch, tmp_path)
    await _run(runner, _agent_result(ROTATED_SESSION_ID))

    assert entry.session_id == ROTATED_SESSION_ID
    assert runner.session_store._save.called, (
        "the handler updated session_entry.session_id in memory without "
        "persisting it — this is #29335"
    )


@pytest.mark.asyncio
async def test_unchanged_session_id_does_not_touch_the_store(monkeypatch, tmp_path):
    """No rotation, no write — the guard is on the id actually changing."""
    runner, entry = _bootstrap(monkeypatch, tmp_path)
    await _run(runner, _agent_result(START_SESSION_ID))

    assert entry.session_id == START_SESSION_ID
    assert not runner.session_store._save.called


@pytest.mark.asyncio
async def test_binding_moved_mid_run_is_not_overwritten(monkeypatch, tmp_path):
    """A concurrent rebind wins over a late compression result.

    The handler snapshots the session id at run start and refuses to apply the
    agent's rotation when the binding moved underneath it. A source-shape
    assertion cannot see this branch at all: the assignment it looks for is
    the one that must NOT run here.
    """
    runner, entry = _bootstrap(monkeypatch, tmp_path)
    runner._run_agent = AsyncMock(
        side_effect=lambda *a, **k: (
            setattr(entry, "session_id", "sess-rebound-elsewhere"),
            _agent_result(ROTATED_SESSION_ID),
        )[1]
    )
    await runner._handle_message_with_agent(_event(), _source(), SESSION_KEY, 1)

    assert entry.session_id == "sess-rebound-elsewhere"
    assert not runner.session_store._save.called
