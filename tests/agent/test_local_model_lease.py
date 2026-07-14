from __future__ import annotations

import json
import os
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent import chat_completion_helpers as helpers
from agent.local_model_lease import (
    LocalModelLeaseQuarantined,
    LocalModelLeaseReleaseError,
    LocalModelLeaseStateError,
    LocalModelLeaseTimeout,
    acquire_local_model_capacity,
    local_model_lease_key,
)


def _state_files(root, provider="omlx", model="qwen"):
    key = local_model_lease_key(provider, model, "http://127.0.0.1:8080/v1")
    lease_dir = root / "shared" / "local-model-leases" / key
    return lease_dir / "state.json", lease_dir / "state.lock"


def _active_record(
    token,
    *,
    pid,
    host,
    process_started_at,
    expires_at,
):
    acquired_at = min(time.time() - 20, expires_at - 2)
    heartbeat_at = min(time.time() - 10, expires_at - 1)
    return {
        "token": token,
        "pid": pid,
        "host": host,
        "process_started_at": process_started_at,
        "priority": 0,
        "acquired_at": acquired_at,
        "heartbeat_at": heartbeat_at,
        "expires_at": expires_at,
        "max_expires_at": max(expires_at, expires_at + 10),
        "task_id": None,
        "run_id": None,
    }


def _waiter_record(
    token,
    *,
    pid,
    host,
    process_started_at,
    expires_at,
):
    return {
        "token": token,
        "pid": pid,
        "host": host,
        "process_started_at": process_started_at,
        "priority": 0,
        "created_at": time.time_ns() - 1,
        "expires_at": expires_at,
        "task_id": None,
        "run_id": None,
    }


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


def test_priority_aging_eventually_promotes_an_old_waiter():
    from agent import local_model_lease as leases

    now_ns = time.time_ns()
    old_low = {
        "token": "old-low",
        "priority": 1,
        "created_at": now_ns - 120_000_000_000,
    }
    new_high = {"token": "new-high", "priority": 50, "created_at": now_ns}

    winner = min(
        (old_low, new_high),
        key=lambda item: leases._waiter_order_key(item, now_ns=now_ns),
    )
    assert winner["token"] == "old-low"


def test_dead_active_pid_is_reclaimed(tmp_path):
    state_path, _ = _state_files(tmp_path)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({
            "version": 2,
            "active": {
                "token": "dead",
                "pid": 999999,
                "host": socket.gethostname(),
                "process_started_at": 1.0,
                "priority": 0,
                "acquired_at": time.time(),
                "heartbeat_at": time.time(),
                "expires_at": time.time() + 10,
                "max_expires_at": time.time() + 20,
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
    with pytest.raises(LocalModelLeaseStateError) as raised:
        acquire_local_model_capacity(
            provider="omlx",
            model="qwen",
            base_url="http://127.0.0.1:8080/v1",
            deadline_monotonic=time.monotonic() + 1,
            cancelled=lambda: False,
            root=tmp_path,
        )
    assert isinstance(raised.value, TimeoutError)


def test_malformed_remote_active_record_fails_closed(tmp_path):
    state_path, _ = _state_files(tmp_path)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({
            "version": 2,
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


def test_release_write_failure_is_explicit_and_retryable(monkeypatch, tmp_path):
    from agent import local_model_lease as leases

    lease = acquire_local_model_capacity(
        provider="omlx",
        model="qwen",
        base_url="http://127.0.0.1:8080/v1",
        deadline_monotonic=time.monotonic() + 2,
        cancelled=lambda: False,
        root=tmp_path,
    )
    state_path, _ = _state_files(tmp_path)
    original_write = leases._atomic_write_state

    def fail_write(*_args, **_kwargs):
        raise OSError("injected release write failure")

    monkeypatch.setattr(leases, "_atomic_write_state", fail_write)
    with pytest.raises(LocalModelLeaseReleaseError, match="durably release"):
        lease.release()
    assert lease._released is False
    assert json.loads(state_path.read_text())["active"]["token"] == lease._token

    monkeypatch.setattr(leases, "_atomic_write_state", original_write)
    lease.release()
    assert lease._released is True
    assert json.loads(state_path.read_text())["active"] is None


def test_release_lock_failure_is_explicit_and_retryable(monkeypatch, tmp_path):
    from agent import local_model_lease as leases

    lease = acquire_local_model_capacity(
        provider="omlx",
        model="qwen",
        base_url="http://127.0.0.1:8080/v1",
        deadline_monotonic=time.monotonic() + 2,
        cancelled=lambda: False,
        root=tmp_path,
    )
    original_enter = leases._StateLock.__enter__

    def fail_lock(_self):
        raise LocalModelLeaseTimeout("injected release lock failure")

    monkeypatch.setattr(leases._StateLock, "__enter__", fail_lock)
    with pytest.raises(LocalModelLeaseTimeout, match="release lock failure"):
        lease.release()
    assert lease._released is False

    monkeypatch.setattr(leases._StateLock, "__enter__", original_enter)
    lease.release()
    assert lease._released is True


def test_context_exit_preserves_body_error_when_release_fails(monkeypatch, tmp_path):
    from agent import local_model_lease as leases

    lease = acquire_local_model_capacity(
        provider="omlx",
        model="qwen",
        base_url="http://127.0.0.1:8080/v1",
        deadline_monotonic=time.monotonic() + 2,
        cancelled=lambda: False,
        root=tmp_path,
    )
    original_write = leases._atomic_write_state
    monkeypatch.setattr(
        leases,
        "_atomic_write_state",
        MagicMock(side_effect=OSError("injected context release failure")),
    )

    with pytest.raises(ValueError, match="provider failed"):
        with lease:
            raise ValueError("provider failed")
    assert lease._released is False

    monkeypatch.setattr(leases, "_atomic_write_state", original_write)
    lease.release()


def test_expired_live_owner_quarantines_route_without_overlap(tmp_path):
    from agent import local_model_lease as leases

    state_path, _ = _state_files(tmp_path)
    state_path.parent.mkdir(parents=True)
    old = _active_record(
        "expired-live",
        pid=os.getpid(),
        host=socket.gethostname(),
        process_started_at=leases._process_started_at(os.getpid()),
        expires_at=time.time() - 1,
    )
    state_path.write_text(json.dumps({"version": 2, "active": old, "waiters": []}))

    with pytest.raises(LocalModelLeaseQuarantined, match="unsettled prior attempt"):
        acquire_local_model_capacity(
            provider="omlx",
            model="qwen",
            base_url="http://127.0.0.1:8080/v1",
            deadline_monotonic=time.monotonic() + 1,
            cancelled=lambda: False,
            root=tmp_path,
        )
    assert json.loads(state_path.read_text())["active"]["token"] == "expired-live"


def test_pid_reuse_birth_mismatch_is_reclaimed(tmp_path):
    from agent import local_model_lease as leases

    state_path, _ = _state_files(tmp_path)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({
            "version": 2,
            "active": _active_record(
                "reused-pid",
                pid=os.getpid(),
                host=socket.gethostname(),
                process_started_at=(leases._process_started_at(os.getpid()) - 100),
                expires_at=time.time() + 30,
            ),
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
    )
    assert json.loads(state_path.read_text())["active"]["token"] != "reused-pid"
    lease.release()


def test_expired_remote_active_quarantines_and_prunes_waiter(tmp_path):
    state_path, _ = _state_files(tmp_path)
    state_path.parent.mkdir(parents=True)
    expired = time.time() - 1
    state_path.write_text(
        json.dumps({
            "version": 2,
            "active": _active_record(
                "remote-active",
                pid=123,
                host="remote-host",
                process_started_at=10.0,
                expires_at=expired,
            ),
            "waiters": [
                _waiter_record(
                    "remote-waiter",
                    pid=456,
                    host="remote-host",
                    process_started_at=20.0,
                    expires_at=expired,
                )
            ],
        })
    )

    with pytest.raises(LocalModelLeaseQuarantined, match="unsettled prior attempt"):
        acquire_local_model_capacity(
            provider="omlx",
            model="qwen",
            base_url="http://127.0.0.1:8080/v1",
            deadline_monotonic=time.monotonic() + 1,
            cancelled=lambda: False,
            root=tmp_path,
        )
    state = json.loads(state_path.read_text())
    assert state["active"]["token"] == "remote-active"
    assert state["waiters"] == []


def test_expired_owner_release_cannot_delete_successor(tmp_path):
    lease = acquire_local_model_capacity(
        provider="omlx",
        model="qwen",
        base_url="http://127.0.0.1:8080/v1",
        deadline_monotonic=time.monotonic() + 2,
        cancelled=lambda: False,
        root=tmp_path,
    )
    state_path, _ = _state_files(tmp_path)
    state = json.loads(state_path.read_text())
    successor = _active_record(
        "successor-token",
        pid=state["active"]["pid"],
        host=state["active"]["host"],
        process_started_at=state["active"]["process_started_at"],
        expires_at=time.time() + 30,
    )
    state["active"] = successor
    state_path.write_text(json.dumps(state))

    lease.release()
    assert lease._released is True
    assert json.loads(state_path.read_text())["active"]["token"] == "successor-token"


def test_release_stays_on_pinned_directory_after_parent_replacement(tmp_path):
    from agent import local_model_lease as leases

    lease = acquire_local_model_capacity(
        provider="omlx",
        model="qwen",
        base_url="http://127.0.0.1:8080/v1",
        deadline_monotonic=time.monotonic() + 2,
        cancelled=lambda: False,
        root=tmp_path,
    )
    state_path, _ = _state_files(tmp_path)
    original_dir = state_path.parent
    detached_dir = original_dir.with_name(f"{original_dir.name}.detached")
    original_dir.rename(detached_dir)
    original_dir.mkdir(mode=0o700)
    replacement = {
        "version": 2,
        "active": _active_record(
            "replacement-owner",
            pid=os.getpid(),
            host=socket.gethostname(),
            process_started_at=leases._process_started_at(os.getpid()),
            expires_at=time.time() + 30,
        ),
        "waiters": [],
    }
    state_path.write_text(json.dumps(replacement))

    lease.release()

    assert json.loads(state_path.read_text())["active"]["token"] == "replacement-owner"
    assert json.loads((detached_dir / "state.json").read_text())["active"] is None


def test_active_expiry_is_bounded_by_attempt_deadline(tmp_path):
    started = time.monotonic()
    lease = acquire_local_model_capacity(
        provider="omlx",
        model="qwen",
        base_url="http://127.0.0.1:8080/v1",
        deadline_monotonic=started + 0.5,
        cancelled=lambda: False,
        root=tmp_path,
    )
    state_path, _ = _state_files(tmp_path)
    active = json.loads(state_path.read_text())["active"]
    assert active["heartbeat_at"] <= active["expires_at"]
    assert active["expires_at"] <= active["max_expires_at"]
    assert active["max_expires_at"] <= time.time() + 0.6
    lease.release()


def test_lease_uses_total_budget_after_first_event_admission(tmp_path):
    started = time.monotonic()
    lease = acquire_local_model_capacity(
        provider="omlx",
        model="qwen",
        base_url="http://127.0.0.1:8080/v1",
        deadline_monotonic=started + 0.4,
        lease_deadline_monotonic=started + 2.0,
        cancelled=lambda: False,
        root=tmp_path,
    )
    state_path, _ = _state_files(tmp_path)
    active = json.loads(state_path.read_text())["active"]
    remaining = active["max_expires_at"] - time.time()
    assert 1.5 < remaining <= 2.1
    lease.release()


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
