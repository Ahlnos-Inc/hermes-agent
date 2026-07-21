"""Operator-initiated Kanban blocking fences and stops the active run."""

from __future__ import annotations

import contextlib
import json
import signal
import time

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture(autouse=True)
def _attested_worker_identity(monkeypatch):
    monkeypatch.setattr(
        kb,
        "_attest_reclaim_process_identity",
        lambda *_args, **_kwargs: True,
    )


def _attach_worker(conn, task_id: str, pid: int) -> None:
    kb._set_worker_pid(
        conn,
        task_id,
        pid,
        worker_started_at=1234.5,
        worker_pgid=pid,
        worker_sid=pid,
    )


def test_operator_block_fences_running_task_before_terminating(tmp_path) -> None:
    db_path = tmp_path / "kanban.db"
    with contextlib.closing(kb.connect(db_path)) as conn:
        task_id = kb.create_task(conn, title="stop this worker", assignee="worker")
        host = kb._claimer_id().split(":", 1)[0]
        claimed = kb.claim_task(conn, task_id, claimer=f"{host}:worker")
        assert claimed is not None and claimed.current_run_id is not None
        run_id = claimed.current_run_id
        _attach_worker(conn, task_id, 43210)
        original = kb.get_task(conn, task_id)
        assert original is not None

        signals: list[int] = []

        def signal_worker(pid: int, sig: int) -> None:
            signals.append(sig)
            assert pid == 43210
            # Termination runs only after the fencing transaction committed.
            with contextlib.closing(kb.connect(db_path)) as observer:
                fenced = kb.get_task(observer, task_id)
                assert fenced is not None
                assert fenced.status == "blocked"
                assert fenced.current_run_id == run_id
                assert fenced.worker_pid == 43210
                assert fenced.claim_lock == original.claim_lock
            raise ProcessLookupError

        result = kb.operator_block_task(
            conn,
            task_id,
            reason="operator pause",
            signal_fn=signal_worker,
        )

        assert result.accepted is True
        assert result.finalized is True
        assert signals == [signal.SIGTERM]
        blocked = kb.get_task(conn, task_id)
        assert blocked is not None
        assert blocked.status == "blocked"
        assert blocked.current_run_id is None
        assert blocked.worker_pid is None
        assert blocked.claim_lock is None
        run = kb.latest_run(conn, task_id)
        assert run is not None
        assert run.id == run_id
        assert run.status == "blocked"
        assert run.outcome == "blocked"
        assert run.ended_at is not None


def test_operator_block_keeps_surviving_worker_fenced(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "kanban.db"
    with contextlib.closing(kb.connect(db_path)) as conn:
        task_id = kb.create_task(conn, title="wedged worker", assignee="worker")
        host = kb._claimer_id().split(":", 1)[0]
        claimed = kb.claim_task(conn, task_id, claimer=f"{host}:worker")
        assert claimed is not None and claimed.current_run_id is not None
        run_id = claimed.current_run_id
        _attach_worker(conn, task_id, 54321)
        original = kb.get_task(conn, task_id)
        assert original is not None

        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
        monkeypatch.setattr(kb.time, "sleep", lambda _seconds: None)
        signals: list[int] = []

        result = kb.operator_block_task(
            conn,
            task_id,
            reason="worker did not stop",
            signal_fn=lambda _pid, sig: signals.append(sig),
        )

        assert result.accepted is True
        assert result.finalized is False
        assert signals == [signal.SIGTERM, signal.SIGKILL]
        fenced = kb.get_task(conn, task_id)
        assert fenced is not None
        assert fenced.status == "blocked"
        assert fenced.current_run_id == run_id
        assert fenced.worker_pid == 54321
        assert fenced.claim_lock == original.claim_lock
        run = kb.latest_run(conn, task_id)
        assert run is not None
        assert run.status == "running"
        assert run.ended_at is None
        assert kb.unblock_task(conn, task_id) is False
        still_fenced = kb.get_task(conn, task_id)
        assert still_fenced is not None
        assert still_fenced.status == "blocked"
        assert still_fenced.current_run_id == run_id
        assert still_fenced.worker_pid == 54321
        assert still_fenced.claim_lock == original.claim_lock
        promoted, refusal = kb.promote_task(
            conn, task_id, actor="operator", reason="unsafe retry",
        )
        assert promoted is False
        assert refusal is not None and "worker termination" in refusal
        assert kb.recompute_ready(conn) == 0
        assert kb.claim_task(conn, task_id) is None


def test_operator_block_retries_a_pending_fence(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "kanban.db"
    with contextlib.closing(kb.connect(db_path)) as conn:
        task_id = kb.create_task(conn, title="retry stop", assignee="worker")
        host = kb._claimer_id().split(":", 1)[0]
        claimed = kb.claim_task(conn, task_id, claimer=f"{host}:worker")
        assert claimed is not None and claimed.current_run_id is not None
        run_id = claimed.current_run_id
        _attach_worker(conn, task_id, 58001)

        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
        monkeypatch.setattr(kb.time, "sleep", lambda _seconds: None)
        first = kb.operator_block_task(
            conn,
            task_id,
            reason="operator pause",
            signal_fn=lambda _pid, _sig: None,
        )
        assert first.accepted is True
        assert first.finalized is False

        second = kb.operator_block_task(
            conn,
            task_id,
            reason="operator pause",
            signal_fn=lambda _pid, _sig: (_ for _ in ()).throw(ProcessLookupError()),
        )

        assert second.accepted is True
        assert second.finalized is True
        blocked = kb.get_task(conn, task_id)
        assert blocked is not None
        assert blocked.status == "blocked"
        assert blocked.current_run_id is None
        run = conn.execute(
            "SELECT status, outcome, ended_at FROM task_runs WHERE id = ?", (run_id,),
        ).fetchone()
        assert run is not None
        assert run["status"] == "blocked"
        assert run["outcome"] == "blocked"
        assert run["ended_at"] is not None


def test_operator_block_stale_run_cas_does_not_touch_replacement(tmp_path) -> None:
    db_path = tmp_path / "kanban.db"
    with contextlib.closing(kb.connect(db_path)) as conn:
        task_id = kb.create_task(conn, title="replace during stop", assignee="worker")
        host = kb._claimer_id().split(":", 1)[0]
        old = kb.claim_task(conn, task_id, claimer=f"{host}:old-worker")
        assert old is not None and old.current_run_id is not None
        old_run_id = old.current_run_id
        _attach_worker(conn, task_id, 61001)
        replacement_run_id: list[int] = []

        def terminate_old(_pid: int, sig: int) -> None:
            assert sig == signal.SIGTERM
            # Simulate a concurrent recovery writer installing a new run after
            # the operator captured the old identity.
            with contextlib.closing(kb.connect(db_path)) as racer:
                now = int(time.time())
                new_lock = f"{host}:replacement"
                with kb.write_txn(racer):
                    racer.execute(
                        "UPDATE task_runs SET status='reclaimed', "
                        "outcome='reclaimed', ended_at=? WHERE id=?",
                        (now, old_run_id),
                    )
                    cur = racer.execute(
                        "INSERT INTO task_runs "
                        "(task_id, status, claim_lock, claim_expires, "
                        "worker_pid, started_at) "
                        "VALUES (?, 'running', ?, ?, ?, ?)",
                        (task_id, new_lock, now + 900, 61002, now),
                    )
                    replacement_run_id.append(int(cur.lastrowid))
                    racer.execute(
                        "UPDATE tasks SET status='running', claim_lock=?, "
                        "claim_expires=?, worker_pid=?, current_run_id=? "
                        "WHERE id=?",
                        (
                            new_lock,
                            now + 900,
                            61002,
                            replacement_run_id[0],
                            task_id,
                        ),
                    )
            raise ProcessLookupError

        result = kb.operator_block_task(
            conn,
            task_id,
            reason="stop old run",
            signal_fn=terminate_old,
        )

        assert result.accepted is False
        assert result.finalized is False
        replacement = kb.get_task(conn, task_id)
        assert replacement is not None
        assert replacement.status == "running"
        assert replacement.current_run_id == replacement_run_id[0]
        assert replacement.worker_pid == 61002
        new_run = kb.latest_run(conn, task_id)
        assert new_run is not None
        assert new_run.id == replacement_run_id[0]
        assert new_run.status == "running"
        old_run = conn.execute(
            "SELECT status, outcome FROM task_runs WHERE id = ?", (old_run_id,),
        ).fetchone()
        assert old_run is not None
        assert old_run["status"] == "reclaimed"
        assert old_run["outcome"] == "reclaimed"


def test_operator_dependency_block_preserves_dependency_routing(tmp_path) -> None:
    db_path = tmp_path / "kanban.db"
    with contextlib.closing(kb.connect(db_path)) as conn:
        parent_id = kb.create_task(conn, title="unfinished parent", assignee="worker")
        task_id = kb.create_task(conn, title="wait for dependency", assignee="worker")
        host = kb._claimer_id().split(":", 1)[0]
        claimed = kb.claim_task(conn, task_id, claimer=f"{host}:worker")
        assert claimed is not None
        kb.link_tasks(conn, parent_id=parent_id, child_id=task_id)
        _attach_worker(conn, task_id, 62001)

        result = kb.operator_block_task(
            conn,
            task_id,
            reason="waiting for prerequisite",
            kind="dependency",
            signal_fn=lambda _pid, _sig: (_ for _ in ()).throw(ProcessLookupError()),
        )

        assert result.accepted is True
        assert result.finalized is True
        waiting = kb.get_task(conn, task_id)
        assert waiting is not None
        assert waiting.status == "todo"
        assert waiting.block_kind == "dependency"
        run = kb.latest_run(conn, task_id)
        assert run is not None
        assert run.status == "blocked"
        assert run.outcome == "blocked"


def test_operator_dependency_block_without_parent_enters_pending(tmp_path) -> None:
    db_path = tmp_path / "kanban.db"
    with contextlib.closing(kb.connect(db_path)) as conn:
        task_id = kb.create_task(conn, title="wait for materialization", assignee="worker")
        host = kb._claimer_id().split(":", 1)[0]
        claimed = kb.claim_task(conn, task_id, claimer=f"{host}:worker")
        assert claimed is not None
        _attach_worker(conn, task_id, 62002)

        result = kb.operator_block_task(
            conn,
            task_id,
            reason="review requested a fix card",
            kind="dependency",
            materialization_sla_seconds=60,
            signal_fn=lambda _pid, _sig: (_ for _ in ()).throw(ProcessLookupError()),
        )

        assert result.accepted is True
        assert result.finalized is True
        pending = kb.get_task(conn, task_id)
        assert pending is not None
        assert pending.status == "todo"
        assert pending.block_kind == "dependency_pending"
        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'dependency_pending' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert event is not None
        assert json.loads(event["payload"])["materialize_by"] >= int(time.time())
