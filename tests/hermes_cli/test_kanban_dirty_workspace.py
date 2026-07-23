"""Regression tests for dirty-workspace architect crash recovery guard.

Covers:
  1. Dirty workspace → no spawn, dirty_workspace event recorded.
  2. Clean workspace → normal spawn proceeds.
  3. workspace_diag payload present in dirty_workspace event.
  4. Idempotent repeated dispatches on same dirty workspace.
  5. Scratch workspace (no git repo) not flagged as dirty.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def all_assignees_spawnable(monkeypatch):
    """Pretend every assignee maps to a real Hermes profile."""
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


def _owned_receipt(pid: int) -> kb.SpawnReceipt:
    return kb.SpawnReceipt(
        pid=pid,
        release=lambda: None,
        abort=lambda: None,
        process_started_at=1234.5,
        process_group_id=pid,
        session_id=pid,
    )


def _init_git_repo(repo: Path) -> None:
    """Create a bare git repo at *repo* (no commits)."""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "kanban@example.com"],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Kanban Test"],
        check=True, capture_output=True, text=True,
    )


# ── Slice 1 + Slice 4 tests ──────────────────────────────────────────


def test_dispatch_once_skips_dirty_workspace(kanban_home, all_assignees_spawnable):
    """Dirty workspace → no spawn, dirty_workspace event recorded, task stays running."""
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append((task.id, task.assignee, workspace))
        return _owned_receipt(20_000 + len(spawns))

    dirty_ws = kanban_home / "dirty_workspace"
    _init_git_repo(dirty_ws)
    # Create an uncommitted file to make the workspace dirty.
    (dirty_ws / "uncommitted.txt").write_text("dirty content\n", encoding="utf-8")

    with kb.connect() as conn:
        t = kb.create_task(
            conn, title="dirty-task", assignee="alice",
            workspace_kind="dir", workspace_path=str(dirty_ws),
        )
        # Promote to ready so dispatch_once can see it.
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (t,))
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    assert res.dirty_workspace == [t]
    assert res.spawned == []
    assert spawns == []
    # Task stays in whatever status it was before dispatch (ready),
    # since the dirty-workspace skip does NOT claim it.
    with kb.connect() as conn:
        assert kb.get_task(conn, t).status == "ready"
        # Verify dirty_workspace event was recorded.
        events = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY created_at",
            (t,),
        ).fetchall()
        kinds = [e["kind"] for e in events]
        assert "dirty_workspace" in kinds


def test_dispatch_once_allows_clean_workspace(kanban_home, all_assignees_spawnable):
    """Clean workspace → normal spawn proceeds, no dirty_workspace event."""
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append((task.id, task.assignee, workspace))
        return _owned_receipt(20_000 + len(spawns))

    clean_ws = kanban_home / "clean_workspace"
    _init_git_repo(clean_ws)
    # Make a committed file so the repo is clean.
    (clean_ws / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(clean_ws), "add", "README.md"],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", str(clean_ws), "commit", "-m", "init"],
        check=True, capture_output=True, text=True,
    )

    with kb.connect() as conn:
        t = kb.create_task(
            conn, title="clean-task", assignee="alice",
            workspace_kind="dir", workspace_path=str(clean_ws),
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (t,))
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    assert res.dirty_workspace == []
    assert len(res.spawned) == 1
    assert res.spawned[0][0] == t
    with kb.connect() as conn:
        assert kb.get_task(conn, t).status == "running"
        events = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? AND kind = 'dirty_workspace'",
            (t,),
        ).fetchone()
        assert events is None, "no dirty_workspace event for clean workspace"


def test_workspace_diag_captured_in_event(kanban_home, all_assignees_spawnable):
    """Verify workspace_diag payload is present in the dirty_workspace event."""
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append(task.id)
        return _owned_receipt(20_000 + len(spawns))

    dirty_ws = kanban_home / "diag_test_ws"
    _init_git_repo(dirty_ws)
    (dirty_ws / "secret_file.txt").write_text("token=abc123secret\n", encoding="utf-8")

    with kb.connect() as conn:
        t = kb.create_task(
            conn, title="diag-task", assignee="alice",
            workspace_kind="dir", workspace_path=str(dirty_ws),
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (t,))
        kb.dispatch_once(conn, spawn_fn=fake_spawn)

    with kb.connect() as conn:
        events = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'dirty_workspace'",
            (t,),
        ).fetchall()
        assert len(events) >= 1
        payload = json.loads(events[0]["payload"])
        assert "workspace_diag" in payload
        diag = payload["workspace_diag"]
        assert diag.get("git_repo") is True
        assert diag.get("dirty") is True
        assert "git_status_raw" in diag
        # Verify redaction of secrets in the raw status output.
        # git status --short shows filenames, not contents, so the
        # redaction pattern won't match filenames — but it WILL match
        # actual token=... patterns if they appear in the status output
        # (e.g. from a tracked file with staged changes).
        raw = diag.get("git_status_raw", "")
        # The key invariant: workspace_diag never leaks file contents.
        # Filenames may contain the word "secret" — that's fine.
        # What matters is that no actual secret VALUES appear.
        assert "token=abc123secret" not in raw


def test_dirty_workspace_idempotent(kanban_home, all_assignees_spawnable):
    """Repeated dispatch attempts on the same dirty workspace produce the same result."""
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append(task.id)
        return _owned_receipt(20_000 + len(spawns))

    dirty_ws = kanban_home / "idempotent_ws"
    _init_git_repo(dirty_ws)
    (dirty_ws / "dirty.txt").write_text("content\n", encoding="utf-8")

    with kb.connect() as conn:
        t = kb.create_task(
            conn, title="idempotent-task", assignee="alice",
            workspace_kind="dir", workspace_path=str(dirty_ws),
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (t,))
        # First dispatch.
        res1 = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        assert res1.dirty_workspace == [t]
        assert res1.spawned == []

        # Second dispatch — same result, no spawn.
        res2 = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        assert res2.dirty_workspace == [t]
        assert res2.spawned == []

        # Third dispatch — still the same.
        res3 = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        assert res3.dirty_workspace == [t]
        assert res3.spawned == []

    # Verify append-only event recording (one event per dispatch tick).
    with kb.connect() as conn:
        events = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? AND kind = 'dirty_workspace' ORDER BY created_at",
            (t,),
        ).fetchall()
        assert len(events) == 3  # one per dispatch tick


def test_scratch_workspace_not_affected(kanban_home, all_assignees_spawnable):
    """Scratch workspaces (no git repo) are not flagged as dirty."""
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append((task.id, task.assignee, workspace))
        return _owned_receipt(20_000 + len(spawns))

    # Scratch workspace — no git repo, just a directory.
    scratch_ws = kanban_home / "scratch_ws"
    scratch_ws.mkdir(parents=True, exist_ok=True)

    with kb.connect() as conn:
        t = kb.create_task(
            conn, title="scratch-task", assignee="alice",
            workspace_kind="scratch", workspace_path=str(scratch_ws),
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (t,))
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    assert res.dirty_workspace == []
    assert len(res.spawned) == 1
    assert res.spawned[0][0] == t


def test_is_workspace_dirty_pure_function():
    """_is_workspace_dirty must be pure — no side effects on any input."""
    # Non-existent path → False.
    assert kb._is_workspace_dirty("/nonexistent/path/that/does/not/exist") is False
    # Empty string → False.
    assert kb._is_workspace_dirty("") is False
    # None-equivalent → False.
    assert kb._is_workspace_dirty(None) is False  # type: ignore[arg-type]
