"""Regression guard for the cascading-interrupt hang (PR #6600).

Original diagnosis and fix by Kristian Vastveit (@kristianvast) in PR #6600,
against the then-inline ``_interruptible_api_call`` /
``_interruptible_streaming_api_call`` methods in run_agent.py. Those methods
have since been extracted into ``agent/chat_completion_helpers.py``, so the
fix is reapplied there and these tests target the extracted functions.

The bug: when ``agent.interrupt()`` fires during an active LLM call, the main
poll loop force-closes the worker-local httpx client to stop token generation.
That raises a transport error (RemoteProtocolError) on the worker — the
EXPECTED consequence of our own close, not a network bug. The streaming retry
loop misclassified it as a transient connection error and retried, each doomed
retry stalling for the full stream-stale timeout (up to 300s). Because the
gateway caches AIAgent instances per session, the stale worker outlived the
turn and raced the next turn's request — the root of the multi-minute
cascading-interrupt hang.

The fix: a request-local ``_request_cancelled`` token set by the poll loop
right before the force-close. The worker's exception handler checks it and
exits cleanly (no retry, no fallback, no "reconnecting" status) instead of
treating the forced error as transient.
"""
import threading
import time
import types
from unittest.mock import MagicMock

import httpx
import pytest

from agent import chat_completion_helpers as cch
from agent.request_budgets import ProviderRouteQuarantined


class _FakeInterruptError(Exception):
    """Stand-in for the transport error a force-close raises on the worker."""


def _make_agent():
    """A MagicMock agent wired with just enough surface for the helpers."""
    agent = MagicMock()
    agent.api_mode = "chat_completions"
    agent._interrupt_requested = False
    agent.verbose_logging = False
    agent._route_request_timeout_seconds = None
    agent._route_stale_timeout_seconds = None
    agent._route_total_attempt_timeout_seconds = None
    agent._route_first_event_timeout_seconds = None
    # _compute_non_stream_stale_timeout / streaming setup helpers return
    # benign values; the real call path is mocked per-test.
    agent._compute_non_stream_stale_timeout.return_value = 5.0
    return agent


def test_non_streaming_cancel_does_not_surface_network_error():
    """A force-close during a non-streaming call must raise InterruptedError,
    not the swallowed transport error."""
    agent = _make_agent()

    create_calls = {"n": 0}
    fake_client = MagicMock()

    def _create(**kwargs):
        create_calls["n"] += 1
        # Simulate the main thread firing an interrupt mid-call, then the
        # force-close raising a transport error on this worker.
        agent._interrupt_requested = True
        time.sleep(0.3)  # let the poll loop observe the interrupt + force-close
        raise httpx.RemoteProtocolError("peer closed connection")

    fake_client.chat.completions.create.side_effect = _create
    agent._create_request_openai_client.return_value = fake_client
    agent._close_request_openai_client = MagicMock()
    agent._abort_request_openai_client = MagicMock()

    t0 = time.time()
    with pytest.raises(InterruptedError):
        cch.interruptible_api_call(agent, {"model": "x", "messages": []})
    elapsed = time.time() - t0

    # The forced RemoteProtocolError must NOT surface as the raised error.
    assert create_calls["n"] == 1
    assert elapsed < 10.0, f"interrupt took {elapsed:.1f}s — should be near-instant (guarding the 30s+ hang)"


def test_interrupt_returns_before_blocking_transport_abort_finishes():
    """Quarantine is synchronous, but SDK teardown must not add a 2s wait."""
    agent = _make_agent()
    agent.provider = "interrupt-latency-provider"
    agent.model = "interrupt-latency-model"
    agent.base_url = "https://interrupt-latency.invalid/v1"
    worker_release = threading.Event()
    worker_started = threading.Event()
    fake_client = MagicMock()

    def blocked_create(**_kwargs):
        worker_started.set()
        worker_release.wait(5)
        raise httpx.RemoteProtocolError("aborted")

    def blocking_abort(*_args, **_kwargs):
        time.sleep(2)
        worker_release.set()

    fake_client.chat.completions.create.side_effect = blocked_create
    agent._create_request_openai_client.return_value = fake_client
    agent._abort_request_openai_client.side_effect = blocking_abort

    def interrupt_soon():
        assert worker_started.wait(1)
        agent._interrupt_requested = True

    threading.Thread(target=interrupt_soon, daemon=True).start()
    started = time.monotonic()
    with pytest.raises(InterruptedError):
        cch.interruptible_api_call(
            agent,
            {"model": agent.model, "messages": []},
        )
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, f"interrupt waited {elapsed:.2f}s for transport teardown"
    worker_release.set()


def test_normal_transient_error_still_raises_when_not_cancelled():
    """Regression guard: a real transport error with NO interrupt must still
    surface to the caller (so the outer retry loop can recover)."""
    agent = _make_agent()
    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = httpx.RemoteProtocolError(
        "genuine network drop"
    )
    agent._create_request_openai_client.return_value = fake_client
    agent._close_request_openai_client = MagicMock()
    agent._abort_request_openai_client = MagicMock()
    agent._interrupt_requested = False

    with pytest.raises(httpx.RemoteProtocolError):
        cch.interruptible_api_call(agent, {"model": "x", "messages": []})


def test_prompt_interrupt_quarantines_only_the_exact_route():
    """A timed-out worker fences its route until its transport has unwound."""
    agent = _make_agent()

    # First call: interrupted.
    fake_client_1 = MagicMock()
    worker_started = threading.Event()
    worker_release = threading.Event()

    def _create_1(**kwargs):
        agent._interrupt_requested = True
        worker_started.set()
        worker_release.wait(5)
        raise httpx.RemoteProtocolError("forced close turn A")

    fake_client_1.chat.completions.create.side_effect = _create_1
    agent._create_request_openai_client.return_value = fake_client_1
    agent._close_request_openai_client = MagicMock()
    agent._abort_request_openai_client = MagicMock()

    with pytest.raises(InterruptedError):
        cch.interruptible_api_call(agent, {"model": "x", "messages": []})
    assert worker_started.is_set()

    # The prompt can return before the worker exits. The matching route must
    # fail closed during that quarantine window rather than overlap the stale
    # transport; this is intentionally stronger than merely clearing the
    # agent-wide interrupt flag.
    agent._interrupt_requested = False
    with pytest.raises(ProviderRouteQuarantined):
        cch.interruptible_api_call(agent, {"model": "x", "messages": []})
    worker_release.set()

    # Once the old worker has exited, a genuine error on the next turn still
    # surfaces normally: quarantine is request/route-local, not sticky state.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            cch.ensure_provider_route_available(agent, {"model": "x", "messages": []})
            break
        except ProviderRouteQuarantined:
            time.sleep(0.02)
    else:
        pytest.fail("prompt-interrupted worker did not leave route quarantine")

    fake_client_2 = MagicMock()
    fake_client_2.chat.completions.create.side_effect = httpx.RemoteProtocolError(
        "genuine drop turn B"
    )
    agent._create_request_openai_client.return_value = fake_client_2

    with pytest.raises(httpx.RemoteProtocolError):
        cch.interruptible_api_call(agent, {"model": "x", "messages": []})
