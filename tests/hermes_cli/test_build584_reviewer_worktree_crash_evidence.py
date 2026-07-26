"""BUILD-584: reviewer crash / retry-exhaustion evidence on worktree tasks.

Reviewer worker for marketing task ``t_12455f53`` exited nonzero, crashed a
second time as a session-detached process the reaper could not classify, and
the dispatcher gave up — leaving a terminal record that said only
``pid 46479 not alive``: no exit classification, and no pointer to the branch
still holding the unmerged work in a dirty worktree.

These tests pin the terminal evidence for exactly that shape (reviewer role,
``workspace_kind='worktree'``, dirty tree, crash → crash → ``gave_up``) and
that the dirty worktree survives the give-up.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture()
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _exited_status(code: int) -> int:
    return code << 8


def _dirty_worktree(root: Path, branch: str) -> Path:
    """A real git worktree with an uncommitted change, like a live reviewer's."""
    repo = root / "repo"
    repo.mkdir(parents=True)
    run = lambda *a: subprocess.run(a, cwd=repo, check=True, capture_output=True)
    run("git", "init", "-q", "-b", "main", ".")
    (repo / "a.txt").write_text("a\n")
    run("git", "add", "-A")
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    wt = root / "wt"
    run("git", "worktree", "add", "-q", "-b", branch, str(wt))
    (wt / "a.txt").write_text("reviewer edit not yet committed\n")
    return wt


def _events(conn, tid, kind):
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id",
        (tid, kind),
    ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


def _crash_reviewer(conn, tid, pid, host, *, exit_status):
    """Run one crash cycle; ``exit_status=None`` = detached, unclassifiable."""
    conn.execute(
        "UPDATE tasks SET status='running', worker_pid=?, claim_lock=? WHERE id=?",
        (pid, f"{host}:w{pid}", tid),
    )
    conn.commit()
    if exit_status is not None:
        kb._record_worker_exit(pid, exit_status)
    kb.detect_crashed_workers(conn)


@pytest.fixture()
def reviewer_task(kanban_home, tmp_path, monkeypatch):
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    branch = "wt/t_review"
    wt = _dirty_worktree(tmp_path / "ws", branch)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="review the copy", assignee="reviewer")
        conn.execute(
            "UPDATE tasks SET workspace_path=?, branch_name=? WHERE id=?",
            (str(wt), branch, tid),
        )
        conn.commit()
        yield conn, tid, branch, wt, kb._claimer_id().split(":", 1)[0]


def test_unclassifiable_reviewer_exit_is_recorded_not_silent(reviewer_task):
    """A detached exit the reaper cannot read must still stamp its kind."""
    conn, tid, _branch, _wt, host = reviewer_task

    _crash_reviewer(conn, tid, 46479, host, exit_status=None)

    crashed = _events(conn, tid, "crashed")
    assert crashed, "no crashed event recorded"
    assert crashed[-1]["exit_kind"] == "unknown", (
        "an unreadable worker disposition must be recorded as 'unknown', not "
        f"omitted: {crashed[-1]}"
    )


def test_reviewer_gave_up_carries_exit_kind_branch_and_retry_count(reviewer_task):
    """Retry exhaustion names the classification, the branch and the count."""
    conn, tid, branch, _wt, host = reviewer_task

    _crash_reviewer(conn, tid, 46232, host, exit_status=_exited_status(1))
    _crash_reviewer(conn, tid, 46479, host, exit_status=None)

    task = kb.get_task(conn, tid)
    assert task.status == "blocked", f"breaker should have tripped, got {task.status}"

    gave_up = _events(conn, tid, "gave_up")
    assert gave_up, "no gave_up event recorded"
    payload = gave_up[-1]
    assert payload["exit_kind"] == "unknown"
    assert payload["branch"] == branch, (
        f"gave_up must name the worktree branch holding the work: {payload}"
    )
    assert payload["failures"] == 2
    assert payload["effective_limit"] == 2
    assert payload["pid"] == 46479

    # First crash was classifiable — its code survives on that event.
    first = _events(conn, tid, "crashed")[0]
    assert first["exit_kind"] == "nonzero_exit"
    assert first["exit_code"] == 1


def test_gave_up_preserves_the_dirty_worktree_and_its_diagnostic(reviewer_task):
    """Give-up must not reclaim the worktree — the diff is the evidence."""
    conn, tid, _branch, wt, host = reviewer_task

    _crash_reviewer(conn, tid, 46232, host, exit_status=_exited_status(1))
    _crash_reviewer(conn, tid, 46479, host, exit_status=None)

    assert wt.is_dir(), "worktree removed by the give-up path"
    status = subprocess.run(
        ["git", "-C", str(wt), "status", "--short"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert status.strip(), "uncommitted reviewer work was discarded"

    diag = _events(conn, tid, "crashed")[-1].get("workspace_diag")
    assert diag and diag.get("dirty") is True, (
        f"crash evidence lost the dirty-workspace diagnostic: {diag}"
    )
