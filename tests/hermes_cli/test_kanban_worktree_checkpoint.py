"""Recoverable workspace snapshots for dirty and unborn repositories."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _init_repo(repo: Path, *, commit: bool) -> None:
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Kanban Test")
    _git(repo, "config", "user.email", "kanban@example.com")
    (repo / ".gitignore").write_text("ignored.tmp\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("committed\n", encoding="utf-8")
    if commit:
        _git(repo, "add", ".gitignore", "tracked.txt")
        _git(repo, "commit", "-m", "baseline")


def _isolated_board(tmp_path, monkeypatch, repo: Path):
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.create_board("checkpoint-test", default_workdir=str(repo))
    return kb


def test_dirty_repo_worktree_starts_from_snapshot_without_mutating_source(
    tmp_path, monkeypatch,
):
    repo = tmp_path / "repo"
    _init_repo(repo, commit=True)
    (repo / "tracked.txt").write_text("staged tracked\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    (repo / "tracked.txt").write_text("dirty tracked\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("recover me\n", encoding="utf-8")
    (repo / "ignored.tmp").write_text("do not copy\n", encoding="utf-8")
    before_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    before_status = _git(repo, "status", "--short").stdout
    before_cached = _git(repo, "diff", "--cached", "--binary").stdout

    kb = _isolated_board(tmp_path, monkeypatch, repo)
    with kb.connect(board="checkpoint-test") as conn:
        task_id = kb.create_task(
            conn,
            title="snapshot dirty WIP",
            workspace_kind="worktree",
            board="checkpoint-test",
        )
        task = kb.get_task(conn, task_id)
        workspace = kb.resolve_workspace(task, board="checkpoint-test")

    assert (workspace / "tracked.txt").read_text() == "dirty tracked\n"
    assert (workspace / "untracked.txt").read_text() == "recover me\n"
    assert not (workspace / "ignored.tmp").exists()
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before_head
    assert _git(repo, "status", "--short").stdout == before_status
    assert _git(repo, "diff", "--cached", "--binary").stdout == before_cached
    checkpoint = _git(
        repo, "rev-parse", f"refs/hermes/checkpoints/{task_id}",
    ).stdout.strip()
    assert _git(workspace, "rev-parse", "HEAD").stdout.strip() == checkpoint


def test_unborn_repo_worktree_contains_nonignored_source_and_source_stays_unborn(
    tmp_path, monkeypatch,
):
    repo = tmp_path / "repo"
    _init_repo(repo, commit=False)
    (repo / "untracked.txt").write_text("first source\n", encoding="utf-8")
    (repo / "ignored.tmp").write_text("do not copy\n", encoding="utf-8")
    before_status = _git(repo, "status", "--short").stdout
    assert _git(repo, "rev-parse", "--verify", "HEAD", check=False).returncode != 0

    kb = _isolated_board(tmp_path, monkeypatch, repo)
    with kb.connect(board="checkpoint-test") as conn:
        task_id = kb.create_task(
            conn,
            title="snapshot unborn source",
            workspace_kind="worktree",
            board="checkpoint-test",
        )
        task = kb.get_task(conn, task_id)
        workspace = kb.resolve_workspace(task, board="checkpoint-test")

    assert (workspace / "tracked.txt").read_text() == "committed\n"
    assert (workspace / "untracked.txt").read_text() == "first source\n"
    assert not (workspace / "ignored.tmp").exists()
    assert _git(repo, "rev-parse", "--verify", "HEAD", check=False).returncode != 0
    assert _git(repo, "status", "--short").stdout == before_status
    assert _git(
        repo, "show-ref", "--verify", f"refs/hermes/checkpoints/{task_id}",
    ).returncode == 0


def test_dispatch_records_checkpoint_identity(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo, commit=True)
    (repo / "tracked.txt").write_text("dispatch snapshot\n", encoding="utf-8")
    kb = _isolated_board(tmp_path, monkeypatch, repo)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)

    with kb.connect(board="checkpoint-test") as conn:
        task_id = kb.create_task(
            conn,
            title="dispatch recoverably",
            assignee="coder",
            workspace_kind="worktree",
            board="checkpoint-test",
        )
        result = kb.dispatch_once(
            conn,
            board="checkpoint-test",
            spawn_fn=lambda _task, _workspace: None,
        )
        events = kb.list_events(conn, task_id)

    assert result.spawned and result.spawned[0][0] == task_id
    checkpoint_event = next(event for event in events if event.kind == "workspace_checkpointed")
    assert checkpoint_event.payload["ref"] == f"refs/hermes/checkpoints/{task_id}"
    assert checkpoint_event.payload["sha"] == _git(
        repo, "rev-parse", checkpoint_event.payload["ref"],
    ).stdout.strip()
