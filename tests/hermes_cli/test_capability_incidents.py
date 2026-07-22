from __future__ import annotations

import contextlib
from argparse import Namespace
import hashlib
import json
import sqlite3
import threading

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb

CAPABILITY = "google_ads_campaign_status_read"
GRANT = "a" * 64


def _claimed(conn):
    task_id = kb.create_task(
        conn,
        title="protected read",
        assignee="marketing-operator",
        created_by="orchestrator",
    )
    task = kb.claim_task(conn, task_id)
    assert task is not None and task.current_run_id is not None
    return task


def _open(conn, task, **kwargs):
    return kb.open_capability_incident(
        conn,
        task.id,
        run_id=task.current_run_id,
        capability_name=CAPABILITY,
        incident_class="missing_secret",
        grant_digest=GRANT,
        **kwargs,
    )


def test_incident_open_is_atomic_deduped_and_closes_run(tmp_path):
    db_path = tmp_path / "kanban.db"
    with contextlib.closing(kb.connect(db_path)) as conn:
        task = _claimed(conn)
        run_id = task.current_run_id

        incident = _open(conn, task)

        blocked = kb.get_task(conn, task.id)
        assert blocked is not None
        assert blocked.status == "blocked"
        assert blocked.current_run_id is None
        run = kb.latest_run(conn, task.id)
        assert run is not None and run.id == run_id
        assert run.status == "blocked" and run.ended_at is not None
        assert incident.state == "open" and incident.episode == 1
        expected_fingerprint = hashlib.sha256(
            json.dumps(
                ["capability-incident/v1", task.id, "missing_secret", CAPABILITY, GRANT],
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
        ).hexdigest()
        assert incident.fingerprint == expected_fingerprint
        assert incident.first_run_id == run_id
        assert incident.last_run_id == run_id
        assert incident.miss_count == 1
        assert incident.first_miss_at == incident.first_seen_at
        assert incident.last_miss_at == incident.last_seen_at
        assert incident.block_event_id is not None
        assert incident.evidence_comment_id is not None
        comments = kb.list_comments(conn, task.id)
        events = [e for e in kb.list_events(conn, task.id) if e.kind == "blocked"]
        assert len(comments) == 1
        assert len(events) == 1
        assert events[0].payload["capability_incident_id"] == incident.id
        row = conn.execute(
            "SELECT consecutive_failures FROM tasks WHERE id = ?",
            (task.id,),
        ).fetchone()
        assert row is not None
        assert row["consecutive_failures"] == 0

        repeated = kb.open_capability_incident(
            conn,
            task.id,
            run_id=None,
            capability_name=CAPABILITY,
            incident_class="missing_secret",
            grant_digest=GRANT,
        )
        assert repeated.id == incident.id
        assert repeated.observer_count == 2
        assert repeated.miss_count == 2
        assert len(kb.list_comments(conn, task.id)) == 1
        assert len([e for e in kb.list_events(conn, task.id) if e.kind == "blocked"]) == 1
        assert kb.unblock_task(conn, task.id) is False
        assert kb.recompute_ready(conn) == 0
        promoted, reason = kb.promote_task(conn, task.id, actor="operator", force=True)
        assert promoted is False
        assert "capability incident" in str(reason)


def test_incident_resolution_episode_supersede_and_cancel(tmp_path):
    with contextlib.closing(kb.connect(tmp_path / "kanban.db")) as conn:
        task = _claimed(conn)
        incident = _open(conn, task)

        with pytest.raises(ValueError, match="does not match"):
            kb.resolve_capability_incident(
                conn,
                incident.id,
                actor="operator",
                evidence="fresh controller validation",
                validated_capability_name=CAPABILITY,
                validated_grant_digest="b" * 64,
            )
        resolved = kb.resolve_capability_incident(
            conn,
            incident.id,
            actor="operator",
            evidence="fresh controller validation",
            validated_capability_name=CAPABILITY,
            validated_grant_digest=GRANT,
        )
        assert resolved.state == "resolved"
        assert resolved.resolved_at is not None
        assert kb.get_task(conn, task.id).status == "ready"

        second_task = kb.claim_task(conn, task.id)
        assert second_task is not None
        second = _open(conn, second_task)
        assert second.episode == 2
        superseded = kb.supersede_capability_incident(
            conn,
            second.id,
            actor="controller",
            evidence="approved activation apply",
            new_grant_digest="b" * 64,
        )
        assert superseded.state == "superseded"
        assert kb.get_task(conn, task.id).status == "ready"

        third_task = kb.claim_task(conn, task.id)
        assert third_task is not None
        third = kb.open_capability_incident(
            conn,
            task.id,
            run_id=third_task.current_run_id,
            capability_name=CAPABILITY,
            incident_class="not_authorized",
            grant_digest="b" * 64,
        )
        cancelled = kb.cancel_capability_incident(
            conn,
            third.id,
            actor="operator",
            evidence="action abandoned",
        )
        assert cancelled.state == "cancelled"
        assert kb.get_task(conn, task.id).status == "blocked"


@pytest.mark.parametrize(
    "failure_point",
    ["after_incident", "after_comment", "after_event", "after_run", "after_task"],
)
def test_incident_fault_injection_rolls_back_every_statement(tmp_path, failure_point):
    with contextlib.closing(kb.connect(tmp_path / f"{failure_point}.db")) as conn:
        task = _claimed(conn)
        run_id = task.current_run_id

        def fail(point):
            if point == failure_point:
                raise RuntimeError("injected")

        with pytest.raises(RuntimeError, match="injected"):
            _open(conn, task, fault_injector=fail)

        current = kb.get_task(conn, task.id)
        assert current is not None
        assert current.status == "running"
        assert current.current_run_id == run_id
        run = kb.latest_run(conn, task.id)
        assert run is not None and run.ended_at is None
        assert kb.list_capability_incidents(conn, task.id) == []
        assert kb.list_comments(conn, task.id) == []
        assert [e for e in kb.list_events(conn, task.id) if e.kind == "blocked"] == []


def test_repeat_observer_fault_rolls_back_counter_and_timestamps(tmp_path):
    with contextlib.closing(kb.connect(tmp_path / "repeat-observer.db")) as conn:
        task = _claimed(conn)
        first = _open(conn, task)

        def fail(point):
            if point == "after_observer":
                raise RuntimeError("injected repeat observer")

        with pytest.raises(RuntimeError, match="injected repeat observer"):
            kb.open_capability_incident(
                conn,
                task.id,
                run_id=None,
                capability_name=CAPABILITY,
                incident_class="missing_secret",
                grant_digest=GRANT,
                fault_injector=fail,
            )

        unchanged = kb.get_capability_incident(conn, first.id)
        assert unchanged is not None
        assert unchanged.observer_count == first.observer_count == 1
        assert unchanged.miss_count == first.miss_count == 1
        assert unchanged.last_seen_at == first.last_seen_at
        assert unchanged.last_miss_at == first.last_miss_at
        assert len(kb.list_comments(conn, task.id)) == 1


def test_busy_begin_immediate_writes_nothing_and_preserves_claimed_run(tmp_path):
    db_path = tmp_path / "busy.db"
    with contextlib.closing(kb.connect(db_path)) as setup:
        task = _claimed(setup)
        run_id = task.current_run_id

    with contextlib.closing(kb.connect(db_path)) as lock_conn, contextlib.closing(
        kb.connect(db_path)
    ) as observer:
        lock_conn.execute("BEGIN IMMEDIATE")
        observer.execute("PRAGMA busy_timeout=1")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            _open(observer, task)
        lock_conn.execute("ROLLBACK")

        current = kb.get_task(observer, task.id)
        assert current is not None
        assert current.status == "running"
        assert current.current_run_id == run_id
        assert kb.list_capability_incidents(observer, task.id) == []
        assert kb.list_comments(observer, task.id) == []


def test_faulted_connection_rolls_back_before_second_connection_opens_incident(
    tmp_path,
):
    db_path = tmp_path / "two-connection-fault.db"
    with contextlib.closing(kb.connect(db_path)) as setup:
        task = _claimed(setup)

    def fail(point):
        if point == "after_event":
            raise RuntimeError("injected first connection")

    with contextlib.closing(kb.connect(db_path)) as first:
        with pytest.raises(RuntimeError, match="injected first connection"):
            _open(first, task, fault_injector=fail)
    with contextlib.closing(kb.connect(db_path)) as second:
        incident = _open(second, task)
        assert incident.observer_count == 1
        assert incident.miss_count == 1
        assert len(kb.list_capability_incidents(second, task.id)) == 1
        assert len(kb.list_comments(second, task.id)) == 1
        assert len(
            [e for e in kb.list_events(second, task.id) if e.kind == "blocked"]
        ) == 1


def test_two_connections_create_one_open_incident_and_one_block_evidence(tmp_path):
    db_path = tmp_path / "kanban.db"
    with contextlib.closing(kb.connect(db_path)) as conn:
        task = _claimed(conn)
        task_id = task.id
        run_id = task.current_run_id

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def observe():
        try:
            with contextlib.closing(kb.connect(db_path)) as conn:
                barrier.wait(timeout=5)
                kb.open_capability_incident(
                    conn,
                    task_id,
                    run_id=run_id,
                    capability_name=CAPABILITY,
                    incident_class="missing_secret",
                    grant_digest=GRANT,
                )
        except BaseException as exc:  # test thread handoff
            errors.append(exc)

    threads = [threading.Thread(target=observe) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not errors

    with contextlib.closing(kb.connect(db_path)) as conn:
        incidents = kb.list_capability_incidents(conn, task_id, state="open")
        assert len(incidents) == 1
        assert incidents[0].observer_count == 2
        assert len(kb.list_comments(conn, task_id)) == 1
        assert len([e for e in kb.list_events(conn, task_id) if e.kind == "blocked"]) == 1
        runs = kb.list_runs(conn, task_id)
        assert len(runs) == 1 and runs[0].ended_at is not None


def test_different_open_fingerprint_requires_controller_supersession(tmp_path):
    with contextlib.closing(kb.connect(tmp_path / "kanban.db")) as conn:
        task = _claimed(conn)
        incident = _open(conn, task)

        with pytest.raises(ValueError, match="supersession"):
            kb.open_capability_incident(
                conn,
                task.id,
                run_id=None,
                capability_name=CAPABILITY,
                incident_class="not_authorized",
                grant_digest="b" * 64,
            )

        assert kb.list_capability_incidents(conn, task.id) == [incident]
        assert len(kb.list_comments(conn, task.id)) == 1
        assert len([e for e in kb.list_events(conn, task.id) if e.kind == "blocked"]) == 1


def test_capability_resolve_cli_exposes_explicit_cancel_without_release(
    tmp_path, monkeypatch, capsys
):
    db_path = tmp_path / "kanban.db"
    with contextlib.closing(kb.connect(db_path)) as conn:
        task = _claimed(conn)
        incident = _open(conn, task)

    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_PROFILE", "operator")
    result = kanban_cli._cmd_capability_resolve(
        Namespace(
            incident_id=incident.id,
            cancel=True,
            supersede_grant_digest=None,
            reason="approved abandonment; no restart",
            evidence=None,
            json=True,
        )
    )

    assert result == 0
    assert '"state": "cancelled"' in capsys.readouterr().out
    with contextlib.closing(kb.connect(db_path)) as conn:
        closed = kb.get_capability_incident(conn, incident.id)
        blocked_task = kb.get_task(conn, task.id)
        assert closed is not None and closed.state == "cancelled"
        assert blocked_task is not None and blocked_task.status == "blocked"
