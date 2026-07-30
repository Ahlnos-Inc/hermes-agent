"""Tests for dirty-workspace handling — BUILD-858 Invariant I2.

BUILD-858 changes the dirty-workspace dispatch behavior from a diagnostic-only
event loop (``dirty_workspace`` event, task stays ``ready``) to a durable
atomic block (``blocked`` event with ``code='dirty_workspace'``, task becomes
``blocked/needs_input``).

Covers:
  1. Dirty workspace -> durable blocked/needs_input transition; task is no
     longer ready; exactly one blocked event with the right payload.
  2. Clean workspace -> normal spawn proceeds.
  3. workspace_diag captured in the blocked event payload.
  4. Subsequent dispatch ticks after a dirty block are idempotent (no new
     events, no spawnable-ready row, no claim_race).
  5. Scratch workspace (no git repo) passes through cleanly.
  6. Dry-run purity: dirty check reports the task but writes nothing.
  7. No worker run is created during the dirty block transition.
  8. Collision recheck inside the CAS prevents a spurious block event.
  9. Clean-after-explicit-unblock spawns normally.
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


# -- BUILD-858 behavior tests ------------------------------------------------


def test_dispatch_once_skips_dirty_workspace(kanban_home, all_assignees_spawnable):
    """Dirty workspace -> durable blocked/needs_input; no spawn; exactly one
    blocked event with code='dirty_workspace'; task is NOT left ready."""
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append((task.id, task.assignee, workspace))
        return _owned_receipt(20_000 + len(spawns))

    dirty_ws = kanban_home / "dirty_workspace"
    _init_git_repo(dirty_ws)
    (dirty_ws / "uncommitted.txt").write_text("dirty content\n", encoding="utf-8")

    with kb.connect() as conn:
        t = kb.create_task(
            conn, title="dirty-task", assignee="alice",
            workspace_kind="dir", workspace_path=str(dirty_ws),
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (t,))
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    assert res.dirty_workspace == [t]
    assert res.auto_blocked == [t]
    assert res.spawned == []
    assert spawns == []

    # BUILD-858: task is now blocked (not left ready).
    with kb.connect() as conn:
        task = kb.get_task(conn, t)
        assert task.status == "blocked", (
            "dirty workspace must become blocked, not stay ready"
        )
        assert task.block_kind == "needs_input"
        assert task.block_recurrences == 1

        # Exactly one 'blocked' event with the right code.
        events = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
            (t,),
        ).fetchall()
        blocked_events = [e for e in events if e["kind"] == "blocked"]
        assert len(blocked_events) == 1, (
            f"expected exactly one blocked event; got {[e['kind'] for e in events]}"
        )
        payload = json.loads(blocked_events[0]["payload"])
        assert payload.get("code") == "dirty_workspace"
        assert payload.get("kind") == "needs_input"

        # No run was created.
        assert task.current_run_id is None, "dirty block must not create a run"


def test_dispatch_once_allows_clean_workspace(kanban_home, all_assignees_spawnable):
    """Clean workspace -> normal spawn proceeds, no dirty_workspace result,
    no blocked event with code='dirty_workspace'."""
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append((task.id, task.assignee, workspace))
        return _owned_receipt(20_000 + len(spawns))

    clean_ws = kanban_home / "clean_workspace"
    _init_git_repo(clean_ws)
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
            "SELECT kind, payload FROM task_events "
            "WHERE task_id = ? AND kind = 'blocked'",
            (t,),
        ).fetchall()
        for ev in events:
            p = json.loads(ev["payload"] or "{}")
            assert p.get("code") != "dirty_workspace", (
                "clean workspace must not produce a dirty_workspace blocked event"
            )


def test_workspace_diag_captured_in_event(kanban_home, all_assignees_spawnable):
    """workspace_diag payload must be present in the blocked event and must not
    contain raw secret values (only filenames/status, never file contents)."""
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
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'blocked'",
            (t,),
        ).fetchall()
        assert len(events) == 1
        payload = json.loads(events[0]["payload"])
        assert payload.get("code") == "dirty_workspace"
        assert "workspace_diag" in payload
        diag = payload["workspace_diag"]
        assert diag.get("git_repo") is True
        assert diag.get("dirty") is True
        assert "git_status_raw" in diag
        # Key invariant: workspace_diag must never leak file contents.
        raw = diag.get("git_status_raw", "")
        assert "token=abc123secret" not in raw


def test_dirty_workspace_idempotent(kanban_home, all_assignees_spawnable):
    """After the first dirty dispatch blocks the task, subsequent dispatch ticks
    produce no additional events, no spawnable-ready row, and no claim_race."""
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

        # First dispatch -- transitions to blocked.
        res1 = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        assert res1.dirty_workspace == [t]
        assert res1.spawned == []
        assert kb.get_task(conn, t).status == "blocked"

        # Second dispatch -- task is blocked, not in ready queue.
        res2 = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        assert res2.dirty_workspace == [], (
            "second tick must not see blocked task as dirty_workspace candidate"
        )
        assert res2.spawned == []
        assert res2.claim_race == [], "no claim race expected on second tick"

        # Third dispatch -- still no effect.
        res3 = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        assert res3.dirty_workspace == []
        assert res3.spawned == []

    # Only one blocked event with dirty_workspace code.
    with kb.connect() as conn:
        events = conn.execute(
            "SELECT kind, payload FROM task_events "
            "WHERE task_id = ? AND kind = 'blocked' ORDER BY id",
            (t,),
        ).fetchall()
        dirty_blocked = [
            e for e in events
            if json.loads(e["payload"] or "{}").get("code") == "dirty_workspace"
        ]
        assert len(dirty_blocked) == 1, (
            f"expected exactly one dirty_workspace blocked event; "
            f"got {len(dirty_blocked)}"
        )


def test_dirty_workspace_hook_fires_once_after_commit(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """The successful dirty CAS invokes one best-effort blocked hook after
    commit; later dispatcher ticks must not invoke it again."""
    dirty_ws = kanban_home / "hook_ws"
    _init_git_repo(dirty_ws)
    (dirty_ws / "dirty.txt").write_text("x\n", encoding="utf-8")
    hook_calls = []
    monkeypatch.setattr(
        kb,
        "_fire_kanban_lifecycle_hook",
        lambda event, task_id, **fields: hook_calls.append((event, task_id, fields)),
    )

    with kb.connect() as conn:
        t = kb.create_task(
            conn, title="hook-task", assignee="alice",
            workspace_kind="dir", workspace_path=str(dirty_ws),
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (t,))

        kb.dispatch_once(conn, spawn_fn=lambda *args: None)
        assert kb.get_task(conn, t).status == "blocked"
        assert [call[:2] for call in hook_calls] == [("kanban_task_blocked", t)]

        # The blocked task is no longer selected on subsequent ticks.
        kb.dispatch_once(conn, spawn_fn=lambda *args: None)
        assert [call[:2] for call in hook_calls] == [("kanban_task_blocked", t)]


def test_scratch_workspace_not_affected(kanban_home, all_assignees_spawnable):
    """Scratch workspaces (no git repo) are not flagged as dirty."""
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append((task.id, task.assignee, workspace))
        return _owned_receipt(20_000 + len(spawns))

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
    """_is_workspace_dirty must be pure -- no side effects on any input."""
    assert kb._is_workspace_dirty("/nonexistent/path/that/does/not/exist") is False
    assert kb._is_workspace_dirty("") is False
    assert kb._is_workspace_dirty(None) is False  # type: ignore[arg-type]


def test_dirty_block_no_run_created(kanban_home, all_assignees_spawnable):
    """The dirty-workspace transition must never create a task run
    (BUILD-858 Invariant I2: no-run contract)."""
    dirty_ws = kanban_home / "norun_ws"
    _init_git_repo(dirty_ws)
    (dirty_ws / "dirty.txt").write_text("x\n", encoding="utf-8")

    with kb.connect() as conn:
        t = kb.create_task(
            conn, title="norun-task", assignee="alice",
            workspace_kind="dir", workspace_path=str(dirty_ws),
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (t,))

        def boom_spawn(task, workspace):
            raise AssertionError("spawn_fn must not be called for dirty task")

        kb.dispatch_once(conn, spawn_fn=boom_spawn)
        task = kb.get_task(conn, t)
        assert task.status == "blocked"
        assert task.current_run_id is None, "dirty block must not create a task run"
        runs = conn.execute(
            "SELECT id FROM task_runs WHERE task_id = ?", (t,),
        ).fetchall()
        assert runs == [], "no task_runs row must be created for a dirty block"


def test_dirty_block_collision_recheck_wins(kanban_home, all_assignees_spawnable):
    """If a running task already holds the same workspace inside the write lock,
    the collision wins and no blocked event is written (BUILD-858 section 5.2)."""
    dirty_ws = kanban_home / "collision_ws"
    _init_git_repo(dirty_ws)
    (dirty_ws / "dirty.txt").write_text("x\n", encoding="utf-8")

    with kb.connect() as conn:
        task_a = kb.create_task(
            conn, title="dirty-candidate", assignee="alice",
            workspace_kind="dir", workspace_path=str(dirty_ws),
        )
        task_b = kb.create_task(
            conn, title="running-holder", assignee="alice",
            workspace_kind="dir", workspace_path=str(dirty_ws),
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_a,))
        conn.execute(
            "UPDATE tasks SET status = 'running', claim_lock = 'test' "
            "WHERE id = ?",
            (task_b,),
        )
        conn.commit()

        ws_diag = kb._capture_workspace_diag(str(dirty_ws))
        result = kb._block_dirty_ready_task(conn, task_a, ws_diag)

        # Must return the collision task ID (not None or __claim_race__).
        assert result == task_b, (
            f"expected collision task {task_b!r}; got {result!r}"
        )
        # task_a must still be ready (not blocked).
        assert kb.get_task(conn, task_a).status == "ready", (
            "collision recheck must not block the candidate task"
        )
        # No blocked event must have been written.
        events = conn.execute(
            "SELECT kind FROM task_events "
            "WHERE task_id = ? AND kind = 'blocked'",
            (task_a,),
        ).fetchall()
        assert events == [], "collision recheck must write no blocked event"


def test_dirty_block_dry_run_writes_nothing(kanban_home, all_assignees_spawnable):
    """In dry-run mode, dispatch_once must report the dirty task but write
    no events and leave the task ready (BUILD-858 acceptance: dry-run purity)."""
    dirty_ws = kanban_home / "dryrun_ws"
    _init_git_repo(dirty_ws)
    (dirty_ws / "dirty.txt").write_text("x\n", encoding="utf-8")

    with kb.connect() as conn:
        t = kb.create_task(
            conn, title="dryrun-task", assignee="alice",
            workspace_kind="dir", workspace_path=str(dirty_ws),
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (t,))
        pre_events = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (t,),
        ).fetchone()[0]

        res = kb.dispatch_once(conn, spawn_fn=lambda *a: None, dry_run=True)

    assert res.dirty_workspace == [t]
    assert res.spawned == []

    with kb.connect() as conn:
        assert kb.get_task(conn, t).status == "ready", (
            "dry-run must not mutate task status"
        )
        post_events = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (t,),
        ).fetchone()[0]
        assert post_events == pre_events, (
            f"dry-run must write no events; got {post_events - pre_events} new"
        )


def test_clean_after_unblock_spawns(kanban_home, all_assignees_spawnable):
    """After an operator unblocks a dirty-blocked task AND the workspace is
    cleaned, the next dispatch spawns normally (BUILD-858 acceptance criterion:
    clean-after-explicit-unblock spawns)."""
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append(task.id)
        return _owned_receipt(20_000 + len(spawns))

    dirty_ws = kanban_home / "cleanafter_ws"
    _init_git_repo(dirty_ws)
    dirty_file = dirty_ws / "dirty.txt"
    dirty_file.write_text("x\n", encoding="utf-8")

    with kb.connect() as conn:
        t = kb.create_task(
            conn, title="cleanafter-task", assignee="alice",
            workspace_kind="dir", workspace_path=str(dirty_ws),
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (t,))

        # First dispatch blocks the task.
        res1 = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        assert res1.dirty_workspace == [t]
        assert kb.get_task(conn, t).status == "blocked"

        # Operator cleans the workspace and makes an initial commit.
        dirty_file.unlink()
        subprocess.run(
            ["git", "-C", str(dirty_ws), "add", "-A"],
            check=True, capture_output=True, text=True,
        )
        # Use --allow-empty to handle the case where there's nothing to stage.
        subprocess.run(
            ["git", "-C", str(dirty_ws), "commit", "-m", "clean", "--allow-empty"],
            check=True, capture_output=True, text=True,
        )

        # Operator explicitly unblocks.
        assert kb.unblock_task(conn, t)
        assert kb.get_task(conn, t).status == "ready"

        # Next dispatch spawns normally.
        res2 = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        assert res2.dirty_workspace == []
        assert res2.spawned != [], "clean-after-unblock must spawn"
        assert kb.get_task(conn, t).status == "running"


def test_claim_vs_dirty_block_race_serializes(kanban_home, all_assignees_spawnable):
    """claim_task and _block_dirty_ready_task racing on the same task must
    serialize without duplicating state transitions (BUILD-858 §5.2).

    One writer wins the SQLite CAS and applies exactly one terminal transition
    (running or blocked); the other writer loses atomically without writing
    anything.  Invariants verified for each possible winner:

    * dirty-block winner  — zero task_runs, zero claimed events, exactly one
      blocked event with code='dirty_workspace', final status='blocked'.
    * claim winner        — one task_runs row, one claimed event, no
      dirty_workspace blocked event, final status='running'.

    Run ROUNDS times to expose ordering races without sleep-based assertions.
    """
    ROUNDS = 20

    dirty_ws = kanban_home / "race_test_ws"
    _init_git_repo(dirty_ws)
    (dirty_ws / "dirty.txt").write_text("x\n", encoding="utf-8")

    for round_num in range(ROUNDS):
        with kb.connect() as conn:
            t = kb.create_task(
                conn, title=f"race-task-{round_num}", assignee="coder",
                workspace_kind="dir", workspace_path=str(dirty_ws),
            )
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (t,))

        claim_result: list = []
        block_result: list = []
        errors: list = []

        def _run_claim() -> None:
            try:
                with kb.connect() as c:
                    result = kb.claim_task(c, t)
                    claim_result.append(result)
            except Exception as exc:
                errors.append(("claim", exc))

        def _run_dirty_block() -> None:
            try:
                ws_diag = kb._capture_workspace_diag(str(dirty_ws))
                with kb.connect() as c:
                    result = kb._block_dirty_ready_task(c, t, ws_diag)
                    block_result.append(result)
            except Exception as exc:
                errors.append(("block", exc))

        t1 = threading.Thread(target=_run_claim)
        t2 = threading.Thread(target=_run_dirty_block)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors, f"Round {round_num}: unexpected errors: {errors}"

        with kb.connect() as conn:
            task = kb.get_task(conn, t)
            final_status = task.status

            assert final_status in {"running", "blocked"}, (
                f"Round {round_num}: unexpected final status {final_status!r}"
            )

            if final_status == "blocked":
                # Dirty-block won: no run must exist, no claimed event.
                assert task.current_run_id is None, (
                    f"Round {round_num}: dirty-block must not create a run"
                )
                runs = conn.execute(
                    "SELECT id FROM task_runs WHERE task_id = ?", (t,),
                ).fetchall()
                assert runs == [], (
                    f"Round {round_num}: dirty-block must not write task_runs; "
                    f"got {runs}"
                )
                claimed_evts = conn.execute(
                    "SELECT id FROM task_events "
                    "WHERE task_id = ? AND kind = 'claimed'",
                    (t,),
                ).fetchall()
                assert claimed_evts == [], (
                    f"Round {round_num}: dirty-block must not emit claimed events; "
                    f"got {len(claimed_evts)}"
                )
                dirty_evts = conn.execute(
                    "SELECT payload FROM task_events "
                    "WHERE task_id = ? AND kind = 'blocked'",
                    (t,),
                ).fetchall()
                assert len(dirty_evts) == 1, (
                    f"Round {round_num}: expected exactly one blocked event; "
                    f"got {len(dirty_evts)}"
                )
                import json as _json
                code = _json.loads(dirty_evts[0]["payload"] or "{}").get("code")
                assert code == "dirty_workspace", (
                    f"Round {round_num}: blocked event code must be 'dirty_workspace'; "
                    f"got {code!r}"
                )
            else:
                # Claim won: one task_run and one claimed event must exist.
                assert task.current_run_id is not None, (
                    f"Round {round_num}: claimed task must have a run"
                )
                runs = conn.execute(
                    "SELECT id FROM task_runs WHERE task_id = ?", (t,),
                ).fetchall()
                assert len(runs) == 1, (
                    f"Round {round_num}: exactly one task_run expected; "
                    f"got {len(runs)}"
                )
                claimed_evts = conn.execute(
                    "SELECT id FROM task_events "
                    "WHERE task_id = ? AND kind = 'claimed'",
                    (t,),
                ).fetchall()
                assert len(claimed_evts) == 1, (
                    f"Round {round_num}: exactly one claimed event expected; "
                    f"got {len(claimed_evts)}"
                )
                # No dirty_workspace blocked event should exist for a claim winner.
                dirty_evts = conn.execute(
                    "SELECT payload FROM task_events "
                    "WHERE task_id = ? AND kind = 'blocked'",
                    (t,),
                ).fetchall()
                import json as _json
                dirty_codes = [
                    _json.loads(e["payload"] or "{}").get("code")
                    for e in dirty_evts
                ]
                assert "dirty_workspace" not in dirty_codes, (
                    f"Round {round_num}: claim winner must not have dirty_workspace "
                    f"blocked event; got codes {dirty_codes}"
                )
