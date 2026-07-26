"""BUILD-533 AC1: compress before dispatching to a smaller-context fallback.

2026-07-18 cascade: the primary rate-limited, the chain failed over to
omlx-local (131k window), and the loop re-dispatched the SAME 176k-token
request — a guaranteed HTTP 400 — because every fallback-activation site
``continue``s the inner retry loop, which sits BELOW the outer pre-API
compression preflight and therefore never re-reaches it.  Recovery then
crawled through the context-overflow error path (~2 min).

The fix restarts the call block on a mid-block route swap so the outer
iteration rebuilds ``api_messages`` and re-runs the preflight against the
new (smaller) window.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import run_agent
from agent.context_compressor import SUMMARY_PREFIX
from run_agent import AIAgent


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(run_agent, "jittered_backoff", lambda *a, **k: 0.0)


def _tool_defs():
    return [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "search",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]


def _mock_response(content="ok"):
    msg = SimpleNamespace(
        content=content, tool_calls=None, reasoning_content=None, reasoning=None,
    )
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="fallback/model", usage=None)


class _RateLimitError(Exception):
    status_code = 429

    def __init__(self):
        super().__init__("Error code: 429 - rate limit exceeded")
        self.response = SimpleNamespace(headers={})
        self.body = {"error": {"message": "rate limit exceeded"}}


@pytest.fixture()
def agent():
    """Primary with a roomy window and a fallback with a much smaller one."""
    with (
        patch("run_agent.get_tool_definitions", return_value=_tool_defs()),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            provider="openrouter",
            model="primary-model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=[
                {
                    "provider": "omlx-local",
                    "model": "small-window-model",
                    "base_url": "http://127.0.0.1:8080/v1",
                }
            ],
        )
        a.client = MagicMock()
        a._cached_system_prompt = "You are helpful."
        a._use_prompt_caching = False
        a.tool_delay = 0
        a.compression_enabled = True
        a.save_trajectories = False
        # Roomy primary window: the history below fits, so NO preflight
        # compression happens on the first (primary) dispatch.
        a.context_compressor.context_length = 20_000
        a.context_compressor.threshold_tokens = 18_000
        return a


def _oversized_history():
    """~1.5k rough tokens: fits the 20k primary, not the 400 fallback."""
    history = []
    for i in range(20):
        history.append(
            {"role": "user", "content": f"Message number {i} with some extra text padding"}
        )
        history.append(
            {"role": "assistant", "content": f"Response number {i} with extra padding here"}
        )
    return history


def _run_with_fallback(agent, mock_compress_return):
    """Primary 429s -> fallback (400-token window) -> assert what happened.

    Returns (mock_compress, create_calls).
    """
    fb_client = MagicMock()
    fb_client.base_url = "http://127.0.0.1:8080/v1"
    fb_client.api_key = "sk-fallback"
    fb_client._custom_headers = None
    fb_client.default_headers = None

    create_calls = []

    def _create(**kwargs):
        create_calls.append(kwargs)
        if len(create_calls) == 1:
            raise _RateLimitError()
        return _mock_response("after fallback")

    agent.client.chat.completions.create.side_effect = _create
    # try_activate_fallback swaps agent.client to fb_client, so the fallback
    # dispatch must land on the same recorder.
    fb_client.chat.completions.create.side_effect = _create

    with (
        patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(fb_client, None),
        ),
        # The fallback's real window — much smaller than the primary's.
        patch("agent.model_metadata.get_model_context_length", return_value=400),
        patch.object(agent, "_compress_context") as mock_compress,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        mock_compress.return_value = mock_compress_return
        result = agent.run_conversation("hello", conversation_history=_oversized_history())

    return mock_compress, create_calls, result


def test_fallback_to_smaller_window_compresses_before_first_request(agent):
    """The first request sent to the smaller fallback must be compressed."""
    compressed = (
        [
            {"role": "user", "content": f"{SUMMARY_PREFIX}\nPrevious conversation"},
            {"role": "user", "content": "hello"},
        ],
        "You are helpful.",
    )
    mock_compress, create_calls, result = _run_with_fallback(agent, compressed)

    assert agent._fallback_activated is True, "fallback never activated"
    assert mock_compress.call_count >= 1, (
        "no compression ran between the primary failure and the fallback "
        "request — the fallback got the uncompressed payload (BUILD-533)"
    )
    assert len(create_calls) >= 2, "fallback request never dispatched"
    # The post-fallback dispatch carries the compacted history, not the 40
    # original turns.
    fallback_messages = create_calls[-1]["messages"]
    assert len(fallback_messages) < 20, (
        f"fallback request shipped {len(fallback_messages)} messages — "
        "compression did not reach the dispatch"
    )
    assert any(
        SUMMARY_PREFIX in str(m.get("content", "")) for m in fallback_messages
    ), "fallback request does not carry the compaction summary"
    assert result["completed"] is True


def test_compressor_window_is_the_fallback_window_at_dispatch(agent):
    """Guards the ordering: the compressor is retargeted before the check."""
    compressed = (
        [
            {"role": "user", "content": f"{SUMMARY_PREFIX}\nPrevious conversation"},
            {"role": "user", "content": "hello"},
        ],
        "You are helpful.",
    )
    mock_compress, _create_calls, _result = _run_with_fallback(agent, compressed)

    c = agent.context_compressor
    assert c.provider == "omlx-local"
    assert c.context_length == 400
    # Compression was decided against the fallback's window, so the estimate
    # handed to _compress_context must exceed the fallback threshold.
    approx = mock_compress.call_args_list[0].kwargs.get("approx_tokens")
    assert approx is not None and approx > c.threshold_tokens, (
        f"compression ran against a stale window: approx={approx} "
        f"threshold={c.threshold_tokens}"
    )
