from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent import chat_completion_helpers as helpers
from agent.request_budgets import AttemptDeadlineExceeded


class _ControlledStream:
    response = None

    def __init__(self, stop: threading.Event, *, emit_events: bool) -> None:
        self._stop = stop
        self._emit_events = emit_events

    def __iter__(self):
        while not self._stop.is_set():
            time.sleep(0.01)
            if self._emit_events:
                yield SimpleNamespace(model="local", choices=[], usage=None)


def _agent(*, total: float, first_event: float, emit_events: bool):
    stop = threading.Event()
    stream = _ControlledStream(stop, emit_events=emit_events)
    client = MagicMock()
    client.chat.completions.create.return_value = stream

    agent = MagicMock()
    agent.api_mode = "chat_completions"
    agent.provider = "ollama-local"
    agent.model = "qwen-local"
    agent.base_url = "http://127.0.0.1:11434/v1"
    agent._interrupt_requested = False
    agent._disable_streaming = False
    agent._route_request_timeout_seconds = None
    agent._route_total_attempt_timeout_seconds = total
    agent._route_first_event_timeout_seconds = first_event
    agent._route_stale_timeout_seconds = 10.0
    agent._create_request_openai_client.return_value = client
    agent._abort_request_openai_client.side_effect = lambda *_args, **_kwargs: (
        stop.set()
    )
    agent._stream_diag_init.return_value = {}
    agent._has_stream_consumers.return_value = False
    agent._is_provider_stream_parse_error.return_value = False
    return agent, stop


def test_local_stream_cannot_wait_forever_for_first_event(monkeypatch):
    monkeypatch.setenv("HERMES_STREAM_RETRIES", "0")
    agent, stop = _agent(total=1.0, first_event=0.05, emit_events=False)

    started = time.monotonic()
    with pytest.raises(AttemptDeadlineExceeded, match="no event"):
        helpers.interruptible_streaming_api_call(
            agent, {"model": "qwen-local", "messages": []}
        )

    assert time.monotonic() - started < 1.0
    assert stop.is_set()


def test_continuous_chunks_cannot_extend_total_attempt_deadline(monkeypatch):
    monkeypatch.setenv("HERMES_STREAM_RETRIES", "0")
    agent, stop = _agent(total=0.08, first_event=0.05, emit_events=True)

    started = time.monotonic()
    with pytest.raises(AttemptDeadlineExceeded, match="total deadline"):
        helpers.interruptible_streaming_api_call(
            agent, {"model": "qwen-local", "messages": []}
        )

    assert time.monotonic() - started < 1.0
    assert stop.is_set()


@pytest.mark.parametrize(
    ("emit_events", "total", "first_event", "message"),
    [
        (False, 1.0, 0.05, "no event"),
        (True, 0.08, 0.05, "total deadline"),
    ],
)
def test_bedrock_stream_obeys_absolute_attempt_deadlines(
    emit_events,
    total,
    first_event,
    message,
):
    stop = threading.Event()

    def events():
        while not stop.is_set():
            time.sleep(0.01)
            if emit_events:
                yield {"messageStart": {"role": "assistant"}}

    client = MagicMock()
    client.converse_stream.return_value = {"stream": events()}
    client.close.side_effect = stop.set

    agent = MagicMock()
    agent.api_mode = "bedrock_converse"
    agent.provider = "bedrock"
    agent.model = "anthropic.claude"
    agent.base_url = "https://bedrock-runtime.us-east-1.amazonaws.com"
    agent._interrupt_requested = False
    agent._route_request_timeout_seconds = None
    agent._route_total_attempt_timeout_seconds = total
    agent._route_first_event_timeout_seconds = first_event
    agent._has_stream_consumers.return_value = False

    started = time.monotonic()
    with (
        patch(
            "agent.bedrock_adapter._get_bedrock_runtime_client",
            return_value=client,
        ),
        pytest.raises(AttemptDeadlineExceeded, match=message),
    ):
        helpers.interruptible_streaming_api_call(
            agent,
            {"modelId": agent.model, "messages": []},
        )

    assert time.monotonic() - started < 1.0
    assert stop.is_set()
