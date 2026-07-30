"""BUILD-858 integration tests — Slices 1+2+3.

Covers:
  - Invariant I1: initial_status=blocked remains sticky across parent completion
    and dispatch ticks (incident shape from t_9deb9d53).
  - Invariant I2: dirty non-worktree dir workspace transitions atomically on
    the first normal dispatch, with no run; subsequent ticks are fully
    idempotent (incident shape from t_74b7dbfd).
  - Invariant I3: human/terminal-pull lane assignees (nicholas, nolan,
    orion-cc, orion-research) remain valid non-profile assignees excluded
    from spawnable-health probes.
  - Invariant I4: exact-once guarantee — two concurrent complete_task calls
    on a blocked human gate yield one completed event, one promoted event,
    no duplicate runs (concurrent trusted human approval).
  - Invariant I5: health probes post-dispatch — a dirty block removes the
    task from has_spawnable_ready(); a blocked gate with a human assignee
    is excluded from has_spawnable_ready().
  - Credential-free temp Git fixtures reproduce the t_9deb9d53 and
    t_74b7dbfd incident shapes.
"""

from __future__ import annotations

import json
import subprocess
import threading
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
    """Make every assignee look like a real Hermes profile."""
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


def _fake_spawn(task, workspace):
    return _owned_receipt(99_000)


def _init_git_repo(repo: Path) -> None:
    """Create a git repo at *repo* with no commits."""
    repo.mkdir(parents=True, exist_ok=True)
    for cmd in [
        ["git", "init", "-b", "main", str(repo)],
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        ["git", "-C", str(repo), "config", "user.name", "Test"],
    ]:
        subprocess.run(cmd, check=True, capture_output=True, text=True)


# ---------------------------------------------------------------------------
# Invariant I1 / t_9deb9d53 incident shape
# ---------------------------------------------------------------------------


def test_i1_initial_blocked_survives_parent_completion_and_dispatch(
    kanban_home, all_assignees_spawnable,
):
    """Reproduces the t_9deb9d53 incident: a task created with
    initial_status='blocked' (a human gate) must remain blocked after parent
    completion and repeated recompute_ready / dispatch_once calls."""
    with kb.connect() as conn:
        impl = kb.create_task(conn, title="impl child")
        gate = kb.create_task(
            conn, title="human gate", initial_status="blocked",
            parents=[impl],
            assignee="nicholas",
        )

        # impl completes -- this is what triggered the bug.
        kb.complete_task(conn, impl, result="done")

        # Repeated recompute + dispatch must never promote the gate.
        for _ in range(5):
            promoted = kb.recompute_ready(conn)
            assert promoted == 0, "human gate must not be auto-promoted"
            assert kb.get_task(conn, gate).status == "blocked"

            res = kb.dispatch_once(conn, spawn_fn=_fake_spawn)
            assert gate not in res.spawned, "human gate must not spawn"

        # Verify no run was created.
        assert kb.get_task(conn, gate).current_run_id is None


def test_i1_initial_blocked_only_unblock_releases(kanban_home, all_assignees_spawnable):
    """Only an explicit unblock_task call can release an initial blocked gate."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        gate = kb.create_task(
            conn, title="gate", initial_status="blocked", parents=[parent],
        )
        kb.complete_task(conn, parent, result="ok")

        # Still blocked.
        assert kb.get_task(conn, gate).status == "blocked"
        assert kb.recompute_ready(conn) == 0

        # Explicit unblock returns True and promotes.
        assert kb.unblock_task(conn, gate)
        assert kb.get_task(conn, gate).status == "ready"


def test_i1_initial_blocked_recurrence_increments_on_re_block(kanban_home):
    """A second block of a previously unblocked initial hold must increment
    block_recurrences through the shared canonical helper."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="gate", initial_status="blocked")
        assert kb.get_task(conn, tid).block_recurrences == 1

        # Unblock then re-block as a no-run ready task.
        assert kb.unblock_task(conn, tid)
        assert kb.block_task(
            conn, tid, reason="second hold", kind="needs_input",
            require_no_active_run=True,
        )
        task = kb.get_task(conn, tid)
        assert task.block_recurrences == 2, (
            "re-block with same kind must increment recurrences"
        )


# ---------------------------------------------------------------------------
# Invariant I2 / t_74b7dbfd incident shape
# ---------------------------------------------------------------------------


def test_i2_dirty_dir_workspace_blocks_on_first_dispatch(
    kanban_home, all_assignees_spawnable,
):
    """Reproduces t_74b7dbfd: a ready profile card with a dirty non-worktree
    dir workspace blocks on the first dispatch; subsequent ticks produce no
    additional events and no spawnable-ready row."""
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append(task.id)
        return _owned_receipt(99_001 + len(spawns))

    dirty_ws = kanban_home / "t74b7dbfd_sim"
    _init_git_repo(dirty_ws)
    (dirty_ws / "untracked.py").write_text("# leaked\n", encoding="utf-8")

    with kb.connect() as conn:
        t = kb.create_task(
            conn, title="architect task", assignee="coder",
            workspace_kind="dir", workspace_path=str(dirty_ws),
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (t,))

        # First tick: dirty block.
        res1 = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        assert res1.dirty_workspace == [t]
        assert res1.spawned == []
        assert spawns == []
        task = kb.get_task(conn, t)
        assert task.status == "blocked"
        assert task.block_kind == "needs_input"
        assert task.current_run_id is None

        # Tick 2 and 3: task is blocked -- not in ready queue.
        for tick in (2, 3):
            res = kb.dispatch_once(conn, spawn_fn=fake_spawn)
            assert res.dirty_workspace == [], f"tick {tick}: blocked task must not recur"
            assert res.spawned == []
            assert kb.get_task(conn, t).status == "blocked"

    # Verify exactly one blocked event with dirty_workspace code.
    with kb.connect() as conn:
        dirty_events = conn.execute(
            "SELECT kind, payload FROM task_events "
            "WHERE task_id = ? AND kind = 'blocked'",
            (t,),
        ).fetchall()
        dirty_codes = [
            json.loads(e["payload"] or "{}").get("code")
            for e in dirty_events
        ]
        assert dirty_codes.count("dirty_workspace") == 1, \
            f"expected exactly one dirty_workspace blocked event; got {dirty_codes}"


def test_i2_worktree_task_excluded_from_dirty_block(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """Pre-materialization worktree tasks are excluded from dirty preflight;
    their workspace_kind='worktree' must bypass the dirty check entirely."""
    dirty_path = kanban_home / "repo_root"
    _init_git_repo(dirty_path)
    (dirty_path / "staged.py").write_text("x\n", encoding="utf-8")

    spawns = []

    def fake_spawn(task, workspace):
        spawns.append(task.id)
        return _owned_receipt(99_100)

    monkeypatch.setattr(
        kb,
        "_resolve_worktree_workspace",
        lambda task, **_kwargs: (str(dirty_path), None, None, None),
    )

    with kb.connect() as conn:
        t = kb.create_task(
            conn, title="worktree-task", assignee="coder",
            workspace_kind="worktree", workspace_path=str(dirty_path),
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (t,))
        # dispatch_once will try to claim and then fail on worktree resolution,
        # but that's ok -- the key assertion is that dirty_workspace is empty.
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        assert t not in res.dirty_workspace, \
            "worktree task must be excluded from dirty pre-flight check"


# ---------------------------------------------------------------------------
# Invariant I3: BUILD-661 assignee validity
# ---------------------------------------------------------------------------


def test_i3_human_assignees_excluded_from_spawnable_ready(kanban_home, monkeypatch):
    """nicholas, nolan, orion-cc, orion-research must not trigger
    has_spawnable_ready() -- they are valid non-profile lanes."""
    # These assignees are NOT Hermes profiles.
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    human_lanes = ["nicholas", "nolan", "orion-cc", "orion-research"]

    with kb.connect() as conn:
        for assignee in human_lanes:
            kb.create_task(conn, title=f"gate/{assignee}", assignee=assignee)

        # has_spawnable_ready() must report False for human-only ready queues.
        assert not kb.has_spawnable_ready(conn), \
            "human/terminal-pull assignees must not register as spawnable"


def test_i3_real_profile_does_trigger_spawnable(kanban_home, all_assignees_spawnable):
    """A ready task assigned to a real profile DOES trigger has_spawnable_ready()."""
    with kb.connect() as conn:
        kb.create_task(conn, title="profile task", assignee="coder")
        assert kb.has_spawnable_ready(conn)


# ---------------------------------------------------------------------------
# Invariant I4: exact-once concurrent human-gate completion
# ---------------------------------------------------------------------------


def test_i4_concurrent_gate_completion_exact_once(kanban_home):
    """Two concurrent complete_task calls on a blocked human gate must yield:
    - exactly one returns True (CAS winner)
    - exactly one completed event
    - zero gate task_runs (no claim, no synthetic run — BUILD-858 §5.3)
    - zero claimed events (gate was never dispatched)
    - exactly one promoted event on the implementation child
    - no duplicate runs or claim events
    (BUILD-858 §5.3)
    """
    # Use two separate connections to simulate concurrent callers.
    with kb.connect() as conn1:
        gate = kb.create_task(
            conn1, title="human gate", initial_status="blocked",
        )
        child = kb.create_task(conn1, title="implementation child", parents=[gate])

    results = []
    errors = []

    def _try_complete(idx: int) -> None:
        try:
            with kb.connect() as c:
                ok = kb.complete_task(c, gate)
                results.append(ok)
        except Exception as exc:
            errors.append((idx, exc))

    t1 = threading.Thread(target=_try_complete, args=(1,))
    t2 = threading.Thread(target=_try_complete, args=(2,))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, f"unexpected errors: {errors}"
    # Exactly one completion must have succeeded.
    assert results.count(True) == 1, \
        f"exactly one complete_task must win; got {results}"
    assert results.count(False) == 1, \
        f"exactly one complete_task must lose (CAS); got {results}"

    with kb.connect() as conn:
        task = kb.get_task(conn, gate)
        assert task.status == "done"

        # Exactly one completed event.
        completed_events = conn.execute(
            "SELECT id FROM task_events WHERE task_id = ? AND kind = 'completed'",
            (gate,),
        ).fetchall()
        assert len(completed_events) == 1, \
            f"expected exactly one completed event; got {len(completed_events)}"

        # Gate completion releases the implementation child exactly once.
        assert kb.get_task(conn, child).status == "ready"
        promoted_events = conn.execute(
            "SELECT id FROM task_events WHERE task_id = ? AND kind = 'promoted'",
            (child,),
        ).fetchall()
        assert len(promoted_events) == 1, \
            f"expected exactly one child promoted event; got {len(promoted_events)}"

        # Zero task_runs: complete_task on a never-claimed blocked gate
        # must not synthesize a run (BUILD-858 §5.3 no-run contract).
        gate_runs = conn.execute(
            "SELECT id FROM task_runs WHERE task_id = ?", (gate,),
        ).fetchall()
        assert gate_runs == [], (
            "no task_runs must be created for a human-gate completion "
            f"without result/summary/metadata; got {gate_runs}"
        )

        # Zero claimed events: the gate was never dispatched.
        claimed_events = conn.execute(
            "SELECT id FROM task_events WHERE task_id = ? AND kind = 'claimed'",
            (gate,),
        ).fetchall()
        assert claimed_events == [], (
            f"no claimed events expected on an initial-blocked gate; "
            f"got {len(claimed_events)}"
        )


# ---------------------------------------------------------------------------
# Invariant I5: post-dispatch health semantics
# ---------------------------------------------------------------------------


def test_i5_dirty_block_removes_from_spawnable_ready(
    kanban_home, all_assignees_spawnable,
):
    """After a dirty block, has_spawnable_ready() must return False
    (the blocked task is no longer in the ready queue)."""
    dirty_ws = kanban_home / "health_test_ws"
    _init_git_repo(dirty_ws)
    (dirty_ws / "dirty.txt").write_text("x\n", encoding="utf-8")

    with kb.connect() as conn:
        t = kb.create_task(
            conn, title="health-task", assignee="coder",
            workspace_kind="dir", workspace_path=str(dirty_ws),
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (t,))

        # Before dispatch: spawnable.
        assert kb.has_spawnable_ready(conn), "ready task must be spawnable before dispatch"

        kb.dispatch_once(conn, spawn_fn=_fake_spawn)
        assert kb.get_task(conn, t).status == "blocked"

        # After dirty block: no longer spawnable.
        assert not kb.has_spawnable_ready(conn), \
            "dirty-blocked task must not appear in has_spawnable_ready"


def test_i5_dirty_block_plus_true_failure_still_actionable(
    kanban_home, all_assignees_spawnable,
):
    """If a dirty block occurs alongside another true spawn failure, the
    remaining failure is still actionable (not hidden by the dirty block)."""
    dirty_ws = kanban_home / "true_fail_ws"
    _init_git_repo(dirty_ws)
    (dirty_ws / "dirty.txt").write_text("x\n", encoding="utf-8")

    def boom_spawn(task, workspace):
        raise RuntimeError("workspace mount error")

    with kb.connect() as conn:
        dirty_t = kb.create_task(
            conn, title="dirty", assignee="coder",
            workspace_kind="dir", workspace_path=str(dirty_ws),
        )
        failing_t = kb.create_task(conn, title="failing", assignee="coder")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (dirty_t,))
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (failing_t,))

        res = kb.dispatch_once(conn, spawn_fn=boom_spawn)

    # Dirty task is blocked; failing task surfaces as a spawn error.
    assert dirty_t in res.dirty_workspace
    failing_ids = [tid for tid, _ in res.spawn_errors]
    assert failing_t in failing_ids, \
        "spawn error alongside dirty block must remain actionable"
