"""provider_route_is_quarantined: retry backoff waits on the real release
condition (orphaned worker thread exit) instead of blind sleeps."""

import threading
import time
from types import SimpleNamespace

from agent.request_budgets import (
    _reset_provider_route_quarantine_for_tests,
    provider_route_is_quarantined,
    quarantine_provider_route,
)


def _agent():
    return SimpleNamespace(
        provider="openai-codex", api_mode="responses",
        model="gpt-5.6-sol", base_url="https://chatgpt.com/backend-api/codex",
    )


def test_probe_tracks_worker_lifetime():
    _reset_provider_route_quarantine_for_tests()
    agent = _agent()
    release = threading.Event()
    worker = threading.Thread(target=release.wait, daemon=True)
    worker.start()
    try:
        assert quarantine_provider_route(agent, {}, worker) is True
        assert provider_route_is_quarantined(agent) is True
        other = SimpleNamespace(
            provider="omlx-local", api_mode="chat",
            model="qwen3.6-35b:oq4-mtp", base_url="http://127.0.0.1:8000/v1/",
        )
        assert provider_route_is_quarantined(other) is False
    finally:
        release.set()
        worker.join(timeout=5)
    assert provider_route_is_quarantined(agent) is False


def test_dead_worker_never_quarantines():
    _reset_provider_route_quarantine_for_tests()
    agent = _agent()
    worker = threading.Thread(target=lambda: None)
    worker.start()
    worker.join(timeout=5)
    assert quarantine_provider_route(agent, {}, worker) is False
    assert provider_route_is_quarantined(agent) is False


def test_close_when_routes_quiet_defers_until_worker_exits():
    from agent.request_budgets import close_when_routes_quiet

    _reset_provider_route_quarantine_for_tests()
    agent = _agent()
    release = threading.Event()
    closed = threading.Event()
    worker = threading.Thread(target=release.wait, daemon=True)
    worker.start()
    try:
        assert quarantine_provider_route(agent, {}, worker) is True
        close_when_routes_quiet("test", closed.set, poll_seconds=0.05)
        # Orphan alive: the close must NOT have run yet.
        assert not closed.wait(0.3)
    finally:
        release.set()
        worker.join(timeout=5)
    # Orphan gone: deferred close runs promptly.
    assert closed.wait(5)


def test_close_when_routes_quiet_immediate_when_no_orphans():
    from agent.request_budgets import close_when_routes_quiet

    _reset_provider_route_quarantine_for_tests()
    closed = threading.Event()
    close_when_routes_quiet("test-immediate", closed.set)
    assert closed.is_set()
