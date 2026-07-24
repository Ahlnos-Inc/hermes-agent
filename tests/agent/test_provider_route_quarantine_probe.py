"""provider_route_is_quarantined: retry backoff waits on the real release
condition (orphaned worker thread exit) instead of blind sleeps."""

import threading
import time
from types import SimpleNamespace

# Import the MODULE, not its names: the quarantine registry is module-global,
# and an earlier test in this package can re-import agent.request_budgets. With
# `from ... import f` at module level and another `from ... import g` inside a
# test, f and g can end up bound to two different module objects with two
# different registries — the test then quarantines one and probes the other.
from agent import request_budgets as rb


def _agent():
    return SimpleNamespace(
        provider="openai-codex", api_mode="responses",
        model="gpt-5.6-sol", base_url="https://chatgpt.com/backend-api/codex",
    )


def test_probe_tracks_worker_lifetime():
    rb._reset_provider_route_quarantine_for_tests()
    agent = _agent()
    release = threading.Event()
    worker = threading.Thread(target=release.wait, daemon=True)
    worker.start()
    try:
        assert rb.quarantine_provider_route(agent, {}, worker) is True
        assert rb.provider_route_is_quarantined(agent) is True
        other = SimpleNamespace(
            provider="omlx-local", api_mode="chat",
            model="qwen3.6-35b:oq4-mtp", base_url="http://127.0.0.1:8000/v1/",
        )
        assert rb.provider_route_is_quarantined(other) is False
    finally:
        release.set()
        worker.join(timeout=5)
    assert rb.provider_route_is_quarantined(agent) is False


def test_dead_worker_never_quarantines():
    rb._reset_provider_route_quarantine_for_tests()
    agent = _agent()
    worker = threading.Thread(target=lambda: None)
    worker.start()
    worker.join(timeout=5)
    assert rb.quarantine_provider_route(agent, {}, worker) is False
    assert rb.provider_route_is_quarantined(agent) is False


def test_close_when_routes_quiet_defers_until_worker_exits():
    rb._reset_provider_route_quarantine_for_tests()
    agent = _agent()
    release = threading.Event()
    closed = threading.Event()
    worker = threading.Thread(target=release.wait, daemon=True)
    worker.start()
    try:
        assert rb.quarantine_provider_route(agent, {}, worker) is True
        rb.close_when_routes_quiet("test", closed.set, poll_seconds=0.05)
        # Orphan alive: the close must NOT have run yet.
        assert not closed.wait(0.3)
    finally:
        release.set()
        worker.join(timeout=5)
    # Orphan gone: deferred close runs promptly.
    assert closed.wait(5)


def test_close_when_routes_quiet_immediate_when_no_orphans():
    rb._reset_provider_route_quarantine_for_tests()
    closed = threading.Event()
    rb.close_when_routes_quiet("test-immediate", closed.set)
    assert closed.is_set()


def test_quarantined_retry_names_the_route_and_its_age():
    """BUILD-696: a retry into a quarantined route must be actionable.

    The 2026-07-21 failure gave the operator "API call failed after 3 retries"
    with no way to tell which route was quarantined or whether the prior
    request was seconds or minutes from unwinding.
    """
    import re

    import pytest

    rb._reset_provider_route_quarantine_for_tests()
    agent = SimpleNamespace(
        provider="omlx-local", api_mode="chat",
        model="qwen3.6-35b:oq4-mtp", base_url="http://127.0.0.1:8000/v1",
    )
    payload = {"model": "qwen3.6-35b:oq4-mtp"}
    release = threading.Event()
    worker = threading.Thread(target=release.wait, daemon=True)
    worker.start()
    try:
        # First request times out; its transport thread is still running.
        assert rb.quarantine_provider_route(agent, payload, worker) is True

        # The retry fails fast — deterministically, and saying what and why.
        with pytest.raises(rb.ProviderRouteQuarantined) as excinfo:
            rb.ensure_provider_route_available(agent, payload)
        message = str(excinfo.value)
        assert "provider=omlx-local" in message
        assert "model=qwen3.6-35b:oq4-mtp" in message
        assert "127.0.0.1:8000" in message
        assert "orphaned request thread" in message
        assert re.search(r"unwinding for \d+s", message)

        status = rb.provider_route_quarantine_status(agent, payload)
        assert status is not None
        assert status["orphan_threads"] == 1
        assert status["unwinding_seconds"] >= 0.0
    finally:
        release.set()
        worker.join(timeout=5)

    # Completion condition: the thread exits, the route admits requests again.
    rb.ensure_provider_route_available(agent, payload)
    assert rb.provider_route_quarantine_status(agent, payload) is None


def test_quarantine_status_never_leaks_credentials():
    """The endpoint in the message comes from the credential-free identity."""
    rb._reset_provider_route_quarantine_for_tests()
    agent = SimpleNamespace(
        provider="omlx-local", api_mode="chat", model="qwen3.6-35b:oq4-mtp",
        base_url="http://user:sekret@127.0.0.1:8000/v1?token=abc",
    )
    release = threading.Event()
    worker = threading.Thread(target=release.wait, daemon=True)
    worker.start()
    try:
        rb.quarantine_provider_route(agent, {}, worker)
        route = rb.provider_route_quarantine_status(agent, {})["route"]
        assert "sekret" not in route and "token=abc" not in route
        assert "127.0.0.1:8000" in route
    finally:
        release.set()
        worker.join(timeout=5)
