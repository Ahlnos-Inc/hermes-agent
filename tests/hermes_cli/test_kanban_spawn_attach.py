from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_start_gate import (
    KanbanStartGateError,
    enforce_kanban_start_gate,
)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _set_gate_env(monkeypatch, *, path, token: str, task) -> None:
    monkeypatch.setenv("HERMES_KANBAN_START_GATE_PATH", str(path))
    monkeypatch.setenv("HERMES_KANBAN_START_GATE_TOKEN", token)
    monkeypatch.setenv("HERMES_KANBAN_START_GATE_TIMEOUT_SECONDS", "0.2")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path()))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task.id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", str(task.claim_lock))


def test_start_gate_attests_exact_attached_run(kanban_home, monkeypatch, tmp_path):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="gated", assignee="worker")
        task = kb.claim_task(conn, task_id)
        assert task is not None
        kb._set_worker_pid(
            conn,
            task.id,
            os.getpid(),
            run_id=task.current_run_id,
            claim_lock=task.claim_lock,
        )

    gate_path = tmp_path / "gate"
    gate_path.write_text("release-token", encoding="utf-8")
    _set_gate_env(monkeypatch, path=gate_path, token="release-token", task=task)

    enforce_kanban_start_gate()

    assert not gate_path.exists()
    assert "HERMES_KANBAN_START_GATE_TOKEN" not in os.environ


def test_start_gate_rejects_pid_mismatch(kanban_home, monkeypatch, tmp_path):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="wrong pid", assignee="worker")
        task = kb.claim_task(conn, task_id)
        assert task is not None
        kb._set_worker_pid(
            conn,
            task.id,
            os.getpid() + 10_000,
            run_id=task.current_run_id,
            claim_lock=task.claim_lock,
        )

    gate_path = tmp_path / "gate"
    gate_path.write_text("release-token", encoding="utf-8")
    _set_gate_env(monkeypatch, path=gate_path, token="release-token", task=task)

    with pytest.raises(KanbanStartGateError, match="ownership attestation mismatch"):
        enforce_kanban_start_gate()

    assert not gate_path.exists()


def test_start_gate_times_out_without_parent_release(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_START_GATE_PATH", str(tmp_path / "missing"))
    monkeypatch.setenv("HERMES_KANBAN_START_GATE_TOKEN", "never-written")
    monkeypatch.setenv("HERMES_KANBAN_START_GATE_TIMEOUT_SECONDS", "0.01")

    with pytest.raises(KanbanStartGateError, match="timed out"):
        enforce_kanban_start_gate()


def test_dispatch_releases_gate_only_after_pid_readback(
    kanban_home,
    all_assignees_spawnable,
):
    release_observed = []
    aborted = []

    def spawn(task, workspace):
        def release():
            with kb.connect() as observer:
                task_row = kb.get_task(observer, task.id)
                run_row = kb.get_run(observer, int(task.current_run_id))
            release_observed.append((
                task_row.worker_pid,
                run_row.worker_pid,
                task_row.claim_lock,
            ))

        return kb.SpawnReceipt(
            pid=31_001,
            release=release,
            abort=lambda: aborted.append(True),
        )

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="attach", assignee="worker")
        result = kb.dispatch_once(conn, spawn_fn=spawn)

    assert [item[0] for item in result.spawned] == [task_id]
    assert release_observed[0][:2] == (31_001, 31_001)
    assert release_observed[0][2]
    assert aborted == []


def test_dispatch_aborts_and_releases_claim_when_gate_release_fails(
    kanban_home,
    all_assignees_spawnable,
):
    aborted = []

    def spawn(task, workspace):
        def release():
            raise OSError("gate write failed")

        return kb.SpawnReceipt(
            pid=31_002,
            release=release,
            abort=lambda: aborted.append(True),
        )

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="gate failure", assignee="worker")
        result = kb.dispatch_once(conn, spawn_fn=spawn)
        task = kb.get_task(conn, task_id)

    assert result.spawned == []
    assert result.spawn_errors == [(task_id, "gate write failed")]
    assert aborted == [True]
    assert task.status == "ready"
    assert task.worker_pid is None
    assert task.claim_lock is None


def test_dispatch_rejects_missing_spawn_receipt(
    kanban_home,
    all_assignees_spawnable,
):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="missing receipt", assignee="worker")
        result = kb.dispatch_once(conn, spawn_fn=lambda task, workspace: None)
        task = kb.get_task(conn, task_id)

    assert result.spawned == []
    assert result.spawn_errors == [
        (task_id, "spawn function returned no valid worker receipt")
    ]
    assert task.status == "ready"


def test_pid_attach_rejects_stale_run_without_partial_write(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="stale attach", assignee="worker")
        task = kb.claim_task(conn, task_id)
        assert task is not None
        with pytest.raises(RuntimeError, match="active run changed"):
            kb._set_worker_pid(
                conn,
                task.id,
                31_003,
                run_id=int(task.current_run_id) + 1,
                claim_lock=task.claim_lock,
            )
        task_after = kb.get_task(conn, task_id)
        run_after = kb.get_run(conn, int(task.current_run_id))
        spawned_events = [
            event for event in kb.list_events(conn, task_id) if event.kind == "spawned"
        ]

    assert task_after.worker_pid is None
    assert run_after.worker_pid is None
    assert spawned_events == []


def test_orphan_canary_scans_only_exact_run_identity(monkeypatch):
    class FakeProcess:
        def __init__(self, pid, environ=None, error=None):
            self.info = {"pid": pid}
            self._environ = environ or {}
            self._error = error

        def environ(self):
            if self._error:
                raise self._error
            return self._environ

    exact = {
        "HERMES_KANBAN_TASK": "t_exact",
        "HERMES_KANBAN_RUN_ID": "17",
        "HERMES_KANBAN_CLAIM_LOCK": "host:claim",
    }
    processes = [
        FakeProcess(303, exact),
        FakeProcess(101, {**exact, "HERMES_KANBAN_RUN_ID": "18"}),
        FakeProcess(202, exact),
        FakeProcess(404, error=RuntimeError("process vanished")),
    ]
    fake_psutil = types.SimpleNamespace(process_iter=lambda _attrs: iter(processes))
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    assert kb._scan_exact_kanban_workers("t_exact", 17, "host:claim") == [202, 303]


def test_manual_reclaim_observes_missing_pid_without_killing_scan_match(
    kanban_home,
    monkeypatch,
    caplog,
):
    host = kb._claimer_id().split(":", 1)[0]
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="orphan canary", assignee="worker")
        task = kb.claim_task(conn, task_id, claimer=f"{host}:claim")
        assert task is not None

        monkeypatch.setattr(
            kb,
            "_scan_exact_kanban_workers",
            lambda task_id, run_id, claim_lock: [42_424],
        )
        signalled = []
        with caplog.at_level("WARNING", logger=kb.__name__):
            assert kb.reclaim_task(
                conn,
                task_id,
                reason="canary test",
                signal_fn=lambda pid, sig: signalled.append((pid, sig)),
            )

    assert signalled == []
    assert "orphan_worker_canary action=observe_only" in caplog.text
    assert "worker_pids=[42424]" in caplog.text
    assert "host:claim" not in caplog.text
