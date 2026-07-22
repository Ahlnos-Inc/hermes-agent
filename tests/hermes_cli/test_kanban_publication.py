"""Behavioral contracts for the coder-to-releaser publication handoff."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


EXPECTED_SHA = "a" * 40


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    db = home / "kanban.db"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kb.time, "time", lambda: 1_700_000_000)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db(db)
    return db


def _create_requester(conn) -> str:
    return kb.create_task(conn, title="coder work", assignee="coder")


def _claim(conn, task_id: str) -> int:
    claimed = kb.claim_task(conn, task_id, claimer="coder-worker")
    assert claimed is not None and claimed.current_run_id is not None
    return int(claimed.current_run_id)


def _git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_publication_handoff_creates_releaser_parent_and_parks_requester(
    board: Path, tmp_path: Path,
) -> None:
    workspace = tmp_path / "coder-workspace"
    workspace.mkdir()
    with kb.connect_closing(board) as conn:
        requester = _create_requester(conn)
        run_id = _claim(conn, requester)

        result = kb.request_publication_handoff(
            conn,
            requester,
            publication=kb.NewPublicationTask(
                expected_sha=EXPECTED_SHA,
                workspace_path=str(workspace),
                remote_ref="refs/heads/main",
            ),
            request_key="publish-request-1",
            actor="coder",
            expected_run_id=run_id,
        )

        assert result.publication_action == "created"
        assert result.requester_status == "todo"
        publisher = kb.get_task(conn, result.publication_task_id)
        assert publisher is not None
        assert publisher.assignee == "releaser"
        assert publisher.workspace_path == str(workspace)
        assert conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?",
            (result.publication_task_id, requester),
        ).fetchone() is not None
        requester_row = kb.get_task(conn, requester)
        assert requester_row is not None and requester_row.status == "todo"
        closed = conn.execute(
            "SELECT outcome, ended_at FROM task_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert closed is not None
        assert closed["outcome"] == "publication_handoff_requested"
        assert closed["ended_at"] is not None
        requester_event = next(
            event for event in kb.list_events(conn, requester)
            if event.kind == "publication_handoff_requested"
        )
        publisher_event = next(
            event for event in kb.list_events(conn, result.publication_task_id)
            if event.kind == "publication_handoff_for"
        )
        assert requester_event.payload == publisher_event.payload
        assert requester_event.payload["request_key"] == "publish-request-1"
        assert requester_event.payload["idempotency_key"] == "publish-request-1"


def test_publication_completion_fails_closed_without_remote_readback(
    board: Path, tmp_path: Path,
) -> None:
    workspace = tmp_path / "coder-workspace"
    remote = tmp_path / "remote.git"
    _git("init", "--bare", str(remote))
    workspace.mkdir()
    _git("init", "-b", "main", cwd=workspace)
    _git("config", "user.email", "test@example.invalid", cwd=workspace)
    _git("config", "user.name", "Hermes Test", cwd=workspace)
    (workspace / "change.txt").write_text("committed\n", encoding="utf-8")
    _git("add", "change.txt", cwd=workspace)
    _git("commit", "-m", "commit for publication", cwd=workspace)
    expected_sha = _git("rev-parse", "HEAD", cwd=workspace)
    _git("remote", "add", "origin", str(remote), cwd=workspace)

    with kb.connect_closing(board) as conn:
        requester = _create_requester(conn)
        run_id = _claim(conn, requester)
        handoff = kb.request_publication_handoff(
            conn,
            requester,
            publication=kb.NewPublicationTask(
                expected_sha=expected_sha,
                workspace_path=str(workspace),
                remote_ref="refs/heads/main",
            ),
            request_key="publish-readback-missing",
            actor="coder",
            expected_run_id=run_id,
        )
        publisher = kb.get_task(conn, handoff.publication_task_id)
        assert publisher is not None
        publisher = kb.claim_task(conn, publisher.id, claimer="releaser-worker")
        assert publisher is not None and publisher.current_run_id is not None

        assert not kb.complete_task(
            conn,
            handoff.publication_task_id,
            result="push command reported success",
            metadata={"publication_verified": True},
            expected_run_id=publisher.current_run_id,
        )
        still_open = kb.get_task(conn, handoff.publication_task_id)
        assert still_open is not None
        assert still_open.status == "running"
        assert still_open.current_run_id == publisher.current_run_id
        requester_waiting = kb.get_task(conn, requester)
        assert requester_waiting is not None and requester_waiting.status == "todo"
        blocked = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'completion_blocked' ORDER BY id DESC LIMIT 1",
            (handoff.publication_task_id,),
        ).fetchone()
        assert blocked is not None
        assert "publication" in (blocked["payload"] or "")

        (workspace / "wrong.txt").write_text("wrong target\n", encoding="utf-8")
        _git("add", "wrong.txt", cwd=workspace)
        _git("commit", "-m", "wrong publication target", cwd=workspace)
        _git("push", "origin", "HEAD:refs/heads/main", cwd=workspace)
        assert not kb.complete_task(
            conn,
            handoff.publication_task_id,
            result="the ref moved, but not to the recorded SHA",
            expected_run_id=publisher.current_run_id,
        )
        mismatch = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'completion_blocked' ORDER BY id DESC LIMIT 1",
            (handoff.publication_task_id,),
        ).fetchone()
        assert mismatch is not None
        mismatch_payload = json.loads(mismatch["payload"])
        assert mismatch_payload["reason"] == "publication_ref_not_verified"
        assert mismatch_payload["observed_sha"] != expected_sha


def test_publication_completion_succeeds_after_remote_ref_readback(
    board: Path, tmp_path: Path,
) -> None:
    workspace = tmp_path / "coder-workspace"
    remote = tmp_path / "remote.git"
    _git("init", "--bare", str(remote))
    workspace.mkdir()
    _git("init", "-b", "main", cwd=workspace)
    _git("config", "user.email", "test@example.invalid", cwd=workspace)
    _git("config", "user.name", "Hermes Test", cwd=workspace)
    (workspace / "change.txt").write_text("committed\n", encoding="utf-8")
    _git("add", "change.txt", cwd=workspace)
    _git("commit", "-m", "commit for publication", cwd=workspace)
    expected_sha = _git("rev-parse", "HEAD", cwd=workspace)
    _git("remote", "add", "origin", str(remote), cwd=workspace)

    with kb.connect_closing(board) as conn:
        requester = _create_requester(conn)
        handoff = kb.request_publication_handoff(
            conn,
            requester,
            publication=kb.NewPublicationTask(
                expected_sha=expected_sha,
                workspace_path=str(workspace),
                remote_ref="refs/heads/main",
            ),
            request_key="publish-readback-success",
            actor="coder",
            expected_run_id=_claim(conn, requester),
        )
        publisher = kb.claim_task(
            conn, handoff.publication_task_id, claimer="releaser-worker",
        )
        assert publisher is not None and publisher.current_run_id is not None
        waiting = kb.get_task(conn, requester)
        assert waiting is not None and waiting.status == "todo"
        _git("push", "origin", "HEAD:refs/heads/main", cwd=workspace)

        assert kb.complete_task(
            conn,
            handoff.publication_task_id,
            summary="remote ref read back at the intended commit",
            expected_run_id=publisher.current_run_id,
        )
        publication = kb.get_task(conn, handoff.publication_task_id)
        requester_after = kb.get_task(conn, requester)
        assert publication is not None and publication.status == "done"
        assert requester_after is not None and requester_after.status == "ready"
        completed = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'completed' ORDER BY id DESC LIMIT 1",
            (handoff.publication_task_id,),
        ).fetchone()
        assert completed is not None
        payload = json.loads(completed["payload"])
        assert payload["publication_readback"]["verified"] is True
        assert payload["publication_readback"]["observed_sha"] == expected_sha


def test_publication_handoff_adopts_existing_publication_card(board: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "coder-workspace"
    workspace.mkdir()
    with kb.connect_closing(board) as conn:
        requester = _create_requester(conn)
        publication = kb.create_task(
            conn,
            title="existing publication card",
            assignee="releaser",
            workspace_kind="dir",
            workspace_path=str(workspace),
            publication_expected_sha=EXPECTED_SHA,
            publication_remote="origin",
            publication_ref="refs/heads/main",
        )

        result = kb.request_publication_handoff(
            conn,
            requester,
            publication=kb.ExistingPublicationTask(publication),
            request_key="publish-adopt-1",
            actor="orchestrator",
            require_no_active_run=True,
        )

        assert result.publication_action == "adopted"
        assert result.publication_task_id == publication
        assert result.requester_status == "todo"
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE publication_expected_sha IS NOT NULL"
        ).fetchone()[0] == 1


def test_publication_handoff_replay_has_no_duplicate_card_edge_or_event(
    board: Path, tmp_path: Path,
) -> None:
    workspace = tmp_path / "coder-workspace"
    workspace.mkdir()
    with kb.connect_closing(board) as conn:
        requester = _create_requester(conn)
        first = kb.request_publication_handoff(
            conn,
            requester,
            publication=kb.NewPublicationTask(
                expected_sha=EXPECTED_SHA,
                workspace_path=str(workspace),
                remote_ref="refs/heads/main",
            ),
            request_key="publish-replay-1",
            actor="orchestrator",
            require_no_active_run=True,
        )
        second = kb.request_publication_handoff(
            conn,
            requester,
            publication=kb.NewPublicationTask(
                expected_sha=EXPECTED_SHA,
                workspace_path=str(workspace),
                remote_ref="refs/heads/main",
            ),
            request_key="publish-replay-1",
            actor="orchestrator",
            require_no_active_run=True,
        )

        assert first.publication_action == "created"
        assert second.publication_action == "replayed"
        assert second.request_event_id == first.request_event_id
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE publication_expected_sha IS NOT NULL"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM task_links WHERE parent_id = ? AND child_id = ?",
            (first.publication_task_id, requester),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? "
            "AND kind = 'publication_handoff_requested'",
            (requester,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? "
            "AND kind = 'publication_handoff_for'",
            (first.publication_task_id,),
        ).fetchone()[0] == 1


def test_publication_handoff_rolls_back_card_edge_run_and_events(
    board: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "coder-workspace"
    workspace.mkdir()
    with kb.connect_closing(board) as conn:
        requester = _create_requester(conn)
        run_id = _claim(conn, requester)
        before_tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        before_events = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]

        def fail_link(*args, **kwargs):
            raise RuntimeError("injected publication link failure")

        monkeypatch.setattr(kb, "_link_tasks_in_txn", fail_link)
        with pytest.raises(RuntimeError, match="injected publication link failure"):
            kb.request_publication_handoff(
                conn,
                requester,
                publication=kb.NewPublicationTask(
                    expected_sha=EXPECTED_SHA,
                    workspace_path=str(workspace),
                    remote_ref="refs/heads/main",
                ),
                request_key="publish-rollback-1",
                actor="coder",
                expected_run_id=run_id,
            )

        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before_tasks
        assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == before_events
        assert conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0] == 0
        requester_row = kb.get_task(conn, requester)
        assert requester_row is not None
        assert requester_row.status == "running"
        assert requester_row.current_run_id == run_id
        run = conn.execute(
            "SELECT ended_at, outcome FROM task_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert run is not None and run["ended_at"] is None and run["outcome"] is None


def test_capability_block_remains_a_real_block_not_a_publication_handoff(
    board: Path,
) -> None:
    with kb.connect_closing(board) as conn:
        requester = _create_requester(conn)
        run_id = _claim(conn, requester)

        assert kb.block_task(
            conn,
            requester,
            reason="no lane can provide the required signing capability",
            kind="capability",
            expected_run_id=run_id,
        )
        task = kb.get_task(conn, requester)
        assert task is not None
        assert task.status == "blocked"
        assert task.block_kind == "capability"
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE publication_expected_sha IS NOT NULL"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? "
            "AND kind LIKE 'publication_handoff_%'",
            (requester,),
        ).fetchone()[0] == 0


def test_cli_publication_handoff_supports_dry_run_and_json(
    board: Path, tmp_path: Path, capsys,
) -> None:
    from hermes_cli import kanban as kanban_cli

    workspace = tmp_path / "coder-workspace"
    workspace.mkdir()
    with kb.connect_closing(board) as conn:
        requester = _create_requester(conn)

    root = argparse.ArgumentParser()
    subparsers = root.add_subparsers(dest="command")
    kanban_cli.build_parser(subparsers)
    argv = [
        "kanban", "publish", requester,
        "--sha", EXPECTED_SHA,
        "--workspace", f"dir:{workspace}",
        "--remote-ref", "refs/heads/main",
        "--request-key", "cli-publication-1",
        "--json",
    ]
    args = root.parse_args([*argv, "--dry-run"])
    assert kanban_cli._cmd_publish(args) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["dry_run"] is True
    assert dry["publication_action"] == "created"
    with kb.connect_closing(board) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind = 'publication_handoff_requested'"
        ).fetchone()[0] == 0

    args = root.parse_args(argv)
    assert kanban_cli._cmd_publish(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["publication_action"] == "created"
    with kb.connect_closing(board) as conn:
        publisher = kb.get_task(conn, result["publication_task_id"])
        assert publisher is not None
        assert publisher.assignee == "releaser"
        assert publisher.publication_ref == "refs/heads/main"
