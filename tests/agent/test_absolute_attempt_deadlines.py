from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent import chat_completion_helpers as helpers
from agent.request_budgets import (
    AttemptDeadlineExceeded,
    DEFAULT_BEDROCK_TOTAL_ATTEMPT_TIMEOUT_SECONDS,
    DEFAULT_LOCAL_TOTAL_ATTEMPT_TIMEOUT_SECONDS,
    ProviderRouteQuarantined,
    _reset_provider_route_quarantine_for_tests,
    ensure_provider_route_available,
    provider_route_key,
    resolve_attempt_budgets,
)


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


def test_local_route_without_policy_gets_finite_configurable_default():
    agent = SimpleNamespace(
        provider="custom-local",
        model="qwen-local",
        base_url="http://127.0.0.1:11434/v1",
        api_mode="chat_completions",
        _route_request_timeout_seconds=None,
        _route_total_attempt_timeout_seconds=None,
        _route_first_event_timeout_seconds=None,
    )

    with (
        patch("agent.request_budgets.get_provider_total_attempt_timeout", return_value=None),
        patch("agent.request_budgets.get_provider_first_event_timeout", return_value=None),
    ):
        budgets = resolve_attempt_budgets(agent)

    assert budgets.total_seconds == DEFAULT_LOCAL_TOTAL_ATTEMPT_TIMEOUT_SECONDS
    assert budgets.first_event_seconds is not None
    assert budgets.first_event_seconds <= budgets.total_seconds

    agent._route_total_attempt_timeout_seconds = 37.0
    agent._route_first_event_timeout_seconds = 11.0
    assert resolve_attempt_budgets(agent).total_seconds == 37.0
    assert resolve_attempt_budgets(agent).first_event_seconds == 11.0


def test_bedrock_without_policy_gets_finite_attempt_default():
    agent = SimpleNamespace(
        provider="bedrock",
        model="anthropic.claude",
        base_url="https://bedrock-runtime.us-east-1.amazonaws.com",
        api_mode="bedrock_converse",
        _route_request_timeout_seconds=None,
        _route_total_attempt_timeout_seconds=None,
        _route_first_event_timeout_seconds=None,
    )

    with (
        patch("agent.request_budgets.get_provider_total_attempt_timeout", return_value=None),
        patch("agent.request_budgets.get_provider_first_event_timeout", return_value=None),
    ):
        budgets = resolve_attempt_budgets(agent)

    assert budgets.total_seconds == DEFAULT_BEDROCK_TOTAL_ATTEMPT_TIMEOUT_SECONDS
    assert budgets.first_event_seconds is not None


def test_quarantine_route_identity_excludes_url_credentials():
    first = SimpleNamespace(
        provider="custom",
        api_mode="chat_completions",
        model="model-a",
        base_url="https://alice:first-secret@example.test/v1?token=one#fragment",
    )
    second = SimpleNamespace(
        provider="custom",
        api_mode="chat_completions",
        model="model-a",
        base_url="https://bob:second-secret@example.test/v1?token=two",
    )

    first_key = provider_route_key(first, {"model": "model-a"})
    second_key = provider_route_key(second, {"model": "model-a"})

    assert first_key == second_key
    assert "secret" not in repr(first_key)


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


def test_non_streaming_bedrock_uses_remaining_attempt_for_socket_timeouts():
    captured = []
    client = MagicMock()
    client.converse.return_value = {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": "bounded"}],
            }
        },
        "stopReason": "end_turn",
        "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
    }

    def get_client(region, *, attempt_timeout_seconds):
        captured.append((region, attempt_timeout_seconds))
        return client

    agent = MagicMock()
    agent.api_mode = "bedrock_converse"
    agent.provider = "bedrock"
    agent.model = "anthropic.claude"
    agent.base_url = "https://bedrock-runtime.us-east-1.amazonaws.com"
    agent._interrupt_requested = False
    agent._route_request_timeout_seconds = None
    agent._route_total_attempt_timeout_seconds = 2.0
    agent._route_first_event_timeout_seconds = 1.0
    agent._compute_non_stream_stale_timeout.return_value = 10.0

    with patch(
        "agent.bedrock_adapter._get_bedrock_runtime_client",
        side_effect=get_client,
    ):
        response = helpers.interruptible_api_call(
            agent,
            {"modelId": agent.model, "messages": []},
        )

    assert response.choices[0].message.content == "bounded"
    assert len(captured) == 1
    region, socket_timeout = captured[0]
    assert region == "us-east-1"
    assert 0 < socket_timeout <= 1.0
    client.close.assert_called_once()


def test_uncooperative_local_transport_is_quarantined_until_actual_unwind(
    monkeypatch,
):
    _reset_provider_route_quarantine_for_tests()
    stop = threading.Event()
    released = threading.Event()
    client = MagicMock()

    def block_forever_for_test(**_kwargs):
        stop.wait(timeout=5.0)
        return SimpleNamespace(choices=[], usage=None)

    client.chat.completions.create.side_effect = block_forever_for_test
    lease = MagicMock()
    lease.release.side_effect = released.set

    agent = MagicMock()
    agent.api_mode = "chat_completions"
    agent.provider = "omlx-local"
    agent.model = "qwen-local"
    agent.base_url = "http://127.0.0.1:8080/v1"
    agent._interrupt_requested = False
    agent._route_request_timeout_seconds = None
    agent._route_total_attempt_timeout_seconds = 0.02
    agent._route_first_event_timeout_seconds = 0.02
    agent._create_request_openai_client.return_value = client
    agent._compute_non_stream_stale_timeout.return_value = 10.0

    monkeypatch.setattr(helpers, "_PROVIDER_ABORT_JOIN_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(
        helpers,
        "_acquire_local_model_lease_for_attempt",
        MagicMock(return_value=lease),
    )

    payload = {"model": "qwen-local", "messages": []}
    try:
        with pytest.raises(AttemptDeadlineExceeded, match="deadline"):
            helpers.interruptible_api_call(agent, dict(payload))

        assert not released.is_set(), "lease must remain held by the live worker"
        with pytest.raises(ProviderRouteQuarantined, match="still unwinding"):
            helpers.interruptible_api_call(agent, dict(payload))
        assert client.chat.completions.create.call_count == 1
    finally:
        stop.set()

    assert released.wait(timeout=1.0), "worker must release only after transport unwinds"
    ensure_provider_route_available(agent, payload)
    _reset_provider_route_quarantine_for_tests()
