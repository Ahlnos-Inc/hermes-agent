"""Behavioral contracts for the atomic review→fix loop-back."""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    db = home / "kanban.db"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db(db)
    return db


def _create_review(conn, *, assignee: str = "reviewer") -> str:
    return kb.create_task(conn, title="review card", assignee=assignee)


def _claim(conn, task_id: str, *, claimer: str = "worker") -> int:
    claimed = kb.claim_task(conn, task_id, claimer=claimer)
    assert claimed is not None and claimed.current_run_id is not None
    return int(claimed.current_run_id)


def test_new_fix_request_is_atomic_and_parks_review(board: Path) -> None:
    with kb.connect_closing(board) as conn:
        review = _create_review(conn)
        run_id = _claim(conn, review, claimer="reviewer")
        result = kb.request_rework(
            conn,
            review,
            finding="P1 assertion is inverted",
            fix=kb.NewFixTask(
                title="Correct the assertion",
                body="Make the expected value match the contract.",
                assignee="coder",
            ),
            request_key="review-run-1",
            actor="reviewer",
            expected_run_id=run_id,
        )

        assert result.fix_action == "created"
        assert result.review_status == "todo"
        review_row = kb.get_task(conn, review)
        assert review_row is not None
        assert review_row.status == "todo"
        assert review_row.block_kind is None
        link = conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ?", (review,)
        ).fetchone()
        assert link is not None and link["parent_id"] == result.fix_task_id
        run = conn.execute(
            "SELECT outcome, ended_at FROM task_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert run is not None and run["outcome"] == "rework_requested"
        assert run["ended_at"] is not None
        review_events = kb.list_events(conn, review)
        fix_events = kb.list_events(conn, result.fix_task_id)
        request_event = next(event for event in review_events if event.kind == "rework_requested")
        rework_for = next(event for event in fix_events if event.kind == "rework_for")
        assert request_event.id == result.request_event_id
        assert request_event.payload == rework_for.payload
        assert request_event.payload["request_key"] == "review-run-1"
        assert request_event.payload["fix_action"] == "created"


def test_adopt_request_and_done_fix_rearms_review_immediately(board: Path) -> None:
    with kb.connect_closing(board) as conn:
        review = _create_review(conn)
        fix = kb.create_task(conn, title="already landed", assignee="coder")
        assert kb.complete_task(conn, fix, result="fixed")

        result = kb.request_rework(
            conn,
            review,
            finding="verify the landed correction",
            fix=kb.ExistingFixTask(fix),
            request_key="adopt-done-1",
            actor="verifier",
            require_no_active_run=True,
        )

        assert result.fix_action == "adopted"
        assert result.review_status == "ready"
        task = kb.get_task(conn, review)
        assert task is not None and task.status == "ready" and task.block_kind is None
        assert conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?",
            (fix, review),
        ).fetchone() is not None


def test_request_key_replay_has_no_duplicate_writes(board: Path) -> None:
    with kb.connect_closing(board) as conn:
        review = _create_review(conn)
        fix = kb.create_task(conn, title="fix", assignee="coder")
        first = kb.request_rework(
            conn, review, finding="same", fix=kb.ExistingFixTask(fix),
            request_key="same-key", actor="reviewer", require_no_active_run=True,
        )
        second = kb.request_rework(
            conn, review, finding="same", fix=kb.ExistingFixTask(fix),
            request_key="same-key", actor="reviewer", require_no_active_run=True,
        )
        assert second.fix_action == "replayed"
        assert second.request_event_id == first.request_event_id
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE id = ?", (fix,)
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM task_links WHERE parent_id = ? AND child_id = ?",
            (fix, review),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? "
            "AND kind = 'rework_requested'", (review,),
        ).fetchone()[0] == 1


def test_same_run_request_key_retry_replays_before_run_cas(board: Path) -> None:
    """A retry from the run that committed the request is idempotent."""
    with kb.connect_closing(board) as conn:
        review = _create_review(conn)
        run_id = _claim(conn, review, claimer="reviewer")
        fix = kb.create_task(conn, title="fix", assignee="coder")

        first = kb.request_rework(
            conn, review, finding="same finding", fix=kb.ExistingFixTask(fix),
            request_key="same-run-key", actor="reviewer",
            expected_run_id=run_id,
        )
        second = kb.request_rework(
            conn, review, finding="same finding", fix=kb.ExistingFixTask(fix),
            request_key="same-run-key", actor="reviewer",
            expected_run_id=run_id,
        )

        assert first.fix_action == "adopted"
        assert second.fix_action == "replayed"
        assert second.replayed_same_run is True
        assert second.request_event_id == first.request_event_id


def test_failed_transition_rolls_back_fix_edge_run_and_events(board: Path, monkeypatch) -> None:
    with kb.connect_closing(board) as conn:
        review = _create_review(conn)
        run_id = _claim(conn, review)
        before_tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        before_events = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]

        def fail_link(*args, **kwargs):
            raise RuntimeError("injected link failure")

        monkeypatch.setattr(kb, "_link_tasks_in_txn", fail_link)
        with pytest.raises(RuntimeError, match="injected link failure"):
            kb.request_rework(
                conn,
                review,
                finding="must roll back",
                fix=kb.NewFixTask(title="rolled back", body=None, assignee="coder"),
                request_key="rollback-1",
                actor="reviewer",
                expected_run_id=run_id,
            )

        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before_tasks
        assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == before_events
        assert conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0] == 0
        review_row = kb.get_task(conn, review)
        assert review_row is not None and review_row.status == "running"
        assert review_row.current_run_id == run_id
        run = conn.execute(
            "SELECT ended_at, outcome FROM task_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert run is not None and run["ended_at"] is None and run["outcome"] is None


def test_stale_run_and_foreign_active_worker_fail_closed(board: Path) -> None:
    with kb.connect_closing(board) as conn:
        review = _create_review(conn)
        run_id = _claim(conn, review)
        fix = kb.create_task(conn, title="fix", assignee="coder")
        with pytest.raises(ValueError, match="stale expected_run_id"):
            kb.request_rework(
                conn, review, finding="stale", fix=kb.ExistingFixTask(fix),
                request_key="stale", actor="reviewer", expected_run_id=run_id + 1,
            )
        with pytest.raises(ValueError, match="active worker"):
            kb.request_rework(
                conn, review, finding="foreign", fix=kb.ExistingFixTask(fix),
                request_key="foreign", actor="operator", require_no_active_run=True,
            )
        assert conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? "
            "AND kind = 'rework_requested'", (review,),
        ).fetchone() is None


def test_cycle_quarantine_invalidation_and_architecture_gate_are_enforced(board: Path) -> None:
    architect_context = kb.MutationContext(
        board_key="default", principal="orchestrator-session",
        actor_type="orchestrator_agent", session_id="session-1",
        request_scope_id="turn-1", mode="enforce", phase="architecture",
    )
    with kb.connect_closing(board) as conn:
        review = _create_review(conn)
        fix = kb.create_task(conn, title="fix", assignee="coder")
        kb.link_tasks(conn, review, fix)
        with pytest.raises(ValueError, match="cycle"):
            kb.request_rework(
                conn, review, finding="cycle", fix=kb.ExistingFixTask(fix),
                request_key="cycle", actor="reviewer", require_no_active_run=True,
            )

        quarantined = kb.create_task(conn, title="quarantined", assignee="coder")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET policy_quarantined = 1 WHERE id = ?", (quarantined,))
        with pytest.raises(ValueError, match="quarantined"):
            kb.request_rework(
                conn, review, finding="quarantine", fix=kb.ExistingFixTask(quarantined),
                request_key="quarantine", actor="reviewer", require_no_active_run=True,
            )

        invalidated = kb.create_task(conn, title="invalidated", assignee="coder")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET policy_invalidated = 1 WHERE id = ?", (invalidated,))
        with pytest.raises(ValueError, match="quarantined or invalidated"):
            kb.request_rework(
                conn, review, finding="invalidated", fix=kb.ExistingFixTask(invalidated),
                request_key="invalidated", actor="reviewer", require_no_active_run=True,
            )

        gated_review = kb.create_task(
            conn, title="gated review", assignee="reviewer",
            mutation_context=architect_context,
        )
        gated_fix = kb.create_task(conn, title="gated fix", assignee="coder")
        with pytest.raises(kb.ArchitectureGateError, match="architecture_gate_open"):
            kb.request_rework(
                conn, gated_review, finding="gate", fix=kb.ExistingFixTask(gated_fix),
                request_key="gate", actor="reviewer", require_no_active_run=True,
            )


def test_concurrent_same_key_requests_commit_exactly_one_transition(board: Path) -> None:
    with kb.connect_closing(board) as conn:
        review = _create_review(conn)
        fix = kb.create_task(conn, title="fix", assignee="coder")

    def submit():
        with kb.connect_closing(board) as worker_conn:
            return kb.request_rework(
                worker_conn,
                review,
                finding="concurrent finding",
                fix=kb.ExistingFixTask(fix),
                request_key="concurrent-key",
                actor="reviewer",
                require_no_active_run=True,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: submit(), range(2)))

    assert sorted(result.fix_action for result in results) == ["adopted", "replayed"]
    with kb.connect_closing(board) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? "
            "AND kind = 'rework_requested'", (review,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM task_links WHERE parent_id = ? AND child_id = ?",
            (fix, review),
        ).fetchone()[0] == 1


def test_cli_rework_adopt_supports_dry_run_and_json(board: Path, capsys) -> None:
    from hermes_cli import kanban as kanban_cli

    with kb.connect_closing(board) as conn:
        review = _create_review(conn)
        fix = kb.create_task(conn, title="fix", assignee="coder")

    root = argparse.ArgumentParser()
    subparsers = root.add_subparsers(dest="command")
    kanban_cli.build_parser(subparsers)
    args = root.parse_args([
        "kanban", "rework", review,
        "--reason", "adopt this fix",
        "--fix-task", fix,
        "--request-key", "cli-key",
        "--dry-run", "--json",
    ])
    assert kanban_cli._cmd_rework(args) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["dry_run"] is True
    assert dry["fix_task_id"] == fix
    with kb.connect_closing(board) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind = 'rework_requested'"
        ).fetchone()[0] == 0

    args = root.parse_args([
        "kanban", "rework", review,
        "--reason", "adopt this fix",
        "--fix-task", fix,
        "--request-key", "cli-key",
        "--json",
    ])
    assert kanban_cli._cmd_rework(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["fix_action"] == "adopted"
    assert result["fix_task_id"] == fix
    with kb.connect_closing(board) as conn:
        task = kb.get_task(conn, review)
        assert task is not None and task.status == "todo"
        assert conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?",
            (fix, review),
        ).fetchone() is not None
