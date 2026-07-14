from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent import chat_completion_helpers as helpers
from agent.local_model_lease import (
    LocalModelLeaseStateError,
    LocalModelLeaseTimeout,
    acquire_local_model_capacity,
    local_model_lease_key,
)


def _state_files(root, provider="omlx", model="qwen"):
    key = local_model_lease_key(provider, model, "http://127.0.0.1:8080/v1")
    lease_dir = root / "shared" / "local-model-leases" / key
    return lease_dir / "state.json", lease_dir / "state.lock"


def test_capacity_key_strips_url_credentials_query_and_fragment():
    with_secret = local_model_lease_key(
        "OMLX",
        "qwen",
        "http://user:secret@127.0.0.1:8080/v1/?token=secret#fragment",
    )
    clean = local_model_lease_key("omlx", "qwen", "http://127.0.0.1:8080/v1")
    assert with_secret == clean
    assert "secret" not in with_secret


def test_acquire_uses_shared_default_root_and_releases(monkeypatch, tmp_path):
    from agent import local_model_lease as leases

    profile_home = tmp_path / "profiles" / "worker"
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setattr(leases, "get_default_hermes_root", lambda: tmp_path)

    lease = acquire_local_model_capacity(
        provider="omlx",
        model="qwen",
        base_url="http://127.0.0.1:8080/v1",
        deadline_monotonic=time.monotonic() + 1,
        cancelled=lambda: False,
    )
    state_path, _ = _state_files(tmp_path)
    state = json.loads(state_path.read_text())
    assert state["active"]["pid"] > 0
    assert str(profile_home) not in str(state_path)

    lease.release()
    assert json.loads(state_path.read_text())["active"] is None


def test_queued_higher_priority_acquires_before_lower_priority(tmp_path):
    active = acquire_local_model_capacity(
        provider="omlx",
        model="qwen",
        base_url="http://127.0.0.1:8080/v1",
        deadline_monotonic=time.monotonic() + 3,
        cancelled=lambda: False,
        root=tmp_path,
        priority=0,
    )
    order = []
    errors = []

    def waiter(name, priority):
        try:
            lease = acquire_local_model_capacity(
                provider="omlx",
                model="qwen",
                base_url="http://127.0.0.1:8080/v1",
                deadline_monotonic=time.monotonic() + 3,
                cancelled=lambda: False,
                root=tmp_path,
                priority=priority,
            )
            order.append(name)
            time.sleep(0.03)
            lease.release()
        except BaseException as exc:  # pragma: no cover - assertion reports it
            errors.append(exc)

    low = threading.Thread(target=waiter, args=("low", 1))
    high = threading.Thread(target=waiter, args=("high", 50))
    low.start()
    high.start()

    state_path, _ = _state_files(tmp_path)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            if len(json.loads(state_path.read_text())["waiters"]) == 2:
                break
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        time.sleep(0.01)
    else:
        pytest.fail("both capacity waiters did not register")

    active.release()
    low.join(timeout=3)
    high.join(timeout=3)
    assert not errors
    assert order == ["high", "low"]


def test_dead_active_pid_is_reclaimed(tmp_path):
    state_path, _ = _state_files(tmp_path)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({
            "version": 1,
            "active": {
                "token": "dead",
                "pid": 999999,
                "host": __import__("socket").gethostname(),
                "priority": 0,
                "acquired_at": time.time(),
                "task_id": None,
                "run_id": None,
            },
            "waiters": [],
        })
    )

    lease = acquire_local_model_capacity(
        provider="omlx",
        model="qwen",
        base_url="http://127.0.0.1:8080/v1",
        deadline_monotonic=time.monotonic() + 1,
        cancelled=lambda: False,
        root=tmp_path,
        pid_alive=lambda pid: pid != 999999,
    )
    assert json.loads(state_path.read_text())["active"]["token"] != "dead"
    lease.release()


def test_live_capacity_wait_is_deadline_bounded(tmp_path):
    active = acquire_local_model_capacity(
        provider="omlx",
        model="qwen",
        base_url="http://127.0.0.1:8080/v1",
        deadline_monotonic=time.monotonic() + 1,
        cancelled=lambda: False,
        root=tmp_path,
    )
    started = time.monotonic()
    with pytest.raises(LocalModelLeaseTimeout):
        acquire_local_model_capacity(
            provider="omlx",
            model="qwen",
            base_url="http://127.0.0.1:8080/v1",
            deadline_monotonic=started + 0.08,
            cancelled=lambda: False,
            root=tmp_path,
        )
    assert time.monotonic() - started < 0.5
    active.release()


def test_corrupt_state_fails_closed(tmp_path):
    state_path, _ = _state_files(tmp_path)
    state_path.parent.mkdir(parents=True)
    state_path.write_text("not-json")
    with pytest.raises(LocalModelLeaseStateError):
        acquire_local_model_capacity(
            provider="omlx",
            model="qwen",
            base_url="http://127.0.0.1:8080/v1",
            deadline_monotonic=time.monotonic() + 1,
            cancelled=lambda: False,
            root=tmp_path,
        )


def test_malformed_remote_active_record_fails_closed(tmp_path):
    state_path, _ = _state_files(tmp_path)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({
            "version": 1,
            "active": {"token": "remote", "pid": "not-an-int", "host": "other"},
            "waiters": [],
        })
    )

    with pytest.raises(LocalModelLeaseStateError, match="unsupported shape"):
        acquire_local_model_capacity(
            provider="omlx",
            model="qwen",
            base_url="http://127.0.0.1:8080/v1",
            deadline_monotonic=time.monotonic() + 1,
            cancelled=lambda: False,
            root=tmp_path,
        )


def test_symlinked_lease_directory_fails_closed(tmp_path):
    key = local_model_lease_key("omlx", "qwen", "http://127.0.0.1:8080/v1")
    lease_root = tmp_path / "shared" / "local-model-leases"
    lease_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (lease_root / key).symlink_to(outside, target_is_directory=True)

    with pytest.raises(LocalModelLeaseStateError, match="symbolic link"):
        acquire_local_model_capacity(
            provider="omlx",
            model="qwen",
            base_url="http://127.0.0.1:8080/v1",
            deadline_monotonic=time.monotonic() + 1,
            cancelled=lambda: False,
            root=tmp_path,
        )


def test_call_boundary_skips_cloud_and_acquires_local(monkeypatch):
    acquired = []
    fake_lease = MagicMock()
    monkeypatch.setattr(
        helpers,
        "acquire_local_model_capacity",
        lambda **kwargs: acquired.append(kwargs) or fake_lease,
    )
    budgets = SimpleNamespace(total_seconds=10.0, first_event_seconds=2.0)
    local_agent = SimpleNamespace(
        provider="omlx",
        model="qwen",
        base_url="http://127.0.0.1:8080/v1",
    )
    lease = helpers._acquire_local_model_lease_for_attempt(
        local_agent,
        {"model": "qwen"},
        attempt_started_monotonic=time.monotonic(),
        attempt_budgets=budgets,
        cancelled=lambda: False,
    )
    assert lease is fake_lease
    assert len(acquired) == 1

    cloud_agent = SimpleNamespace(
        provider="openai",
        model="gpt",
        base_url="https://api.openai.com/v1",
    )
    assert (
        helpers._acquire_local_model_lease_for_attempt(
            cloud_agent,
            {"model": "gpt"},
            attempt_started_monotonic=time.monotonic(),
            attempt_budgets=budgets,
            cancelled=lambda: False,
        )
        is None
    )
    assert len(acquired) == 1


def test_streaming_capacity_failure_is_surfaced_from_worker(monkeypatch):
    monkeypatch.setenv("HERMES_STREAM_RETRIES", "0")
    monkeypatch.setattr(
        helpers,
        "acquire_local_model_capacity",
        MagicMock(side_effect=LocalModelLeaseTimeout("capacity busy")),
    )
    agent = MagicMock()
    agent.api_mode = "chat_completions"
    agent.provider = "omlx"
    agent.model = "qwen"
    agent.base_url = "http://127.0.0.1:8080/v1"
    agent._interrupt_requested = False
    agent._disable_streaming = False
    agent._route_request_timeout_seconds = None
    agent._route_total_attempt_timeout_seconds = 1.0
    agent._route_first_event_timeout_seconds = 0.5
    agent._route_stale_timeout_seconds = 1.0
    agent._stream_diag_init.return_value = {}
    agent._has_stream_consumers.return_value = False

    with pytest.raises(LocalModelLeaseTimeout, match="capacity busy"):
        helpers.interruptible_streaming_api_call(
            agent,
            {"model": "qwen", "messages": []},
        )
