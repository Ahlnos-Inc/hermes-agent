"""Tests for typed block reasons + the unblock-loop breaker.

Covers the built-in fix for the kanban "blocked loop" — a worker blocks a
task, a cron unblocks it, the worker re-blocks for the same reason, repeat
forever. The fix gives ``block_task`` a typed ``kind`` and a persistent
``block_recurrences`` counter:

* ``dependency`` blocks route to ``todo`` (parent-gated, auto-resumed) and
  never enter the human ``blocked`` bucket a cron would keep unblocking.
* ``needs_input`` / ``capability`` / un-typed blocks land in ``blocked``;
  each same-cause re-block after an unblock increments ``block_recurrences``,
  and at ``BLOCK_RECURRENCE_LIMIT`` the task routes to ``triage`` for a human.
* ``unblock_task`` deliberately does NOT reset ``block_recurrences`` (the
  amnesia that let the loop run unbounded).
* A successful ``complete_task`` resets the loop memory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn, title="t"):
    """Create a task and drive it to ``running`` so block_task can act."""
    tid = kb.create_task(conn, title=title, assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    claimed = kb.claim_task(conn, tid, claimer="worker")
    assert claimed is not None
    return tid


def _make_running_again(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None


# ---------------------------------------------------------------------------
# Loop breaker
# ---------------------------------------------------------------------------


def test_first_typed_block_lands_in_blocked(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(conn, tid, reason="which key?", kind="needs_input")
        t = kb.get_task(conn, tid)
        assert t.status == "blocked"
        assert t.block_kind == "needs_input"
        assert t.block_recurrences == 1


def test_unblock_does_not_reset_recurrence_counter(kanban_home: Path) -> None:
    """The crux of the fix: unblock must preserve the loop counter."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="x", kind="needs_input")
        assert kb.get_task(conn, tid).block_recurrences == 1
        assert kb.unblock_task(conn, tid)
        t = kb.get_task(conn, tid)
        assert t.status == "ready"
        assert t.block_recurrences == 1  # NOT reset to 0
        assert t.block_kind == "needs_input"  # kind preserved for comparison


def test_same_cause_reblock_routes_to_triage(kanban_home: Path) -> None:
    """Dale's loop: block → unblock → re-block same kind → triage."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="need creds", kind="needs_input")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="still need creds", kind="needs_input")
        t = kb.get_task(conn, tid)
        assert t.status == "triage"
        assert t.block_recurrences == 2


def test_untyped_block_loop_also_protected(kanban_home: Path) -> None:
    """Legacy un-typed blocks (kind=None) still trip the breaker."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="a")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="a again")
        assert kb.get_task(conn, tid).status == "triage"


def test_different_kinds_do_not_compound(kanban_home: Path) -> None:
    """A re-block for a DIFFERENT reason resets the counter to 1."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="a", kind="needs_input")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="b", kind="capability")
        t = kb.get_task(conn, tid)
        assert t.status == "blocked"
        assert t.block_recurrences == 1


def test_block_loop_detected_event_emitted(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="x", kind="capability")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="x", kind="capability")
        events = [e for e in kb.list_events(conn, tid)
                  if e.kind == "block_loop_detected"]
        assert events, "expected a block_loop_detected event"
        payload = events[-1].payload or {}
        assert payload.get("recurrences") == 2
        assert payload.get("kind") == "capability"


# ---------------------------------------------------------------------------
# Dependency routing
# ---------------------------------------------------------------------------


def test_dependency_block_routes_to_todo(kanban_home: Path) -> None:
    """A dependency wait with an unfinished linked parent parks in todo."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        tid = _running_task(conn)
        kb.link_tasks(conn, parent, tid)
        assert kb.block_task(conn, tid, reason="need X first", kind="dependency")
        t = kb.get_task(conn, tid)
        assert t.status == "todo"
        assert t.block_kind == "dependency"
        signature_events = [
            event for event in kb.list_events(conn, tid)
            if event.kind == "failure_signature"
        ]
        assert len(signature_events) == 1
        assert parent in signature_events[0].payload["signature"]


def test_dependency_then_parent_done_promotes(kanban_home: Path) -> None:
    """A dependency-parked child becomes ready once its parent completes."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = _running_task(conn, title="child")
        kb.link_tasks(conn, parent_id=parent, child_id=child)
        kb.block_task(conn, child, reason="wait", kind="dependency")
        assert kb.get_task(conn, child).status == "todo"
        # Finish the parent, then let recompute_ready run.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
        kb.claim_task(conn, parent, claimer="worker")
        kb.complete_task(conn, parent, result="done")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"


def test_dependency_without_unfinished_parent_enters_pending_wait(
    kanban_home: Path,
) -> None:
    """A missing fix card waits without entering the human queue."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="already done", assignee="worker")
        assert kb.complete_task(conn, parent, result="done")
        child = _running_task(conn, title="child")
        kb.link_tasks(conn, parent, child)

        assert kb.block_task(
            conn, child, reason="artifact commit is unreachable", kind="dependency",
        )
        task = kb.get_task(conn, child)
        assert task.status == "todo"
        assert task.block_kind == "dependency_pending"
        assert kb.recompute_ready(conn) == 0
        assert kb.recompute_ready(conn) == 0
        promoted, refusal = kb.promote_task(conn, child, actor="operator", force=True)
        assert promoted is False
        assert refusal is not None and "materialization is pending" in refusal
        assert not [
            event for event in kb.list_events(conn, child)
            if event.kind == "failure_signature"
        ]
        events = [
            event for event in kb.list_events(conn, child)
            if event.kind == "dependency_pending"
        ]
        assert len(events) == 1
        assert events[0].payload["baseline_parent_ids"] == [parent]
        assert events[0].payload["source_event_kind"] == "kanban_block"


def test_dispatch_tick_reconciles_before_promotion_without_respawning_pending(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        child = _running_task(conn, title="pending review")
        assert kb.block_task(conn, child, reason="await fix", kind="dependency")
        result = kb.dispatch_once(conn, dry_run=True)
        assert result.promoted == 0
        assert result.spawned == []
        assert result.dependency_waits_timed_out == 0
        assert kb.get_task(conn, child).status == "todo"
        assert kb.get_task(conn, child).block_kind == "dependency_pending"


def test_pending_wait_materializes_unfinished_then_rearms_terminal_parent(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        child = _running_task(conn, title="review")
        assert kb.block_task(conn, child, reason="return to coder", kind="dependency")
        fix = kb.create_task(conn, title="fix", assignee="worker")
        kb.link_tasks(conn, fix, child)

        result = kb.reconcile_dependency_waits(conn, now=100)
        assert result.waits_materialized == 1
        assert kb.get_task(conn, child).block_kind == "dependency"

        assert kb.complete_task(conn, fix, result="fixed")
        assert kb.get_task(conn, child).status == "ready"
        assert kb.get_task(conn, child).block_kind is None


def test_pending_wait_rearms_when_new_parent_is_already_terminal(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        child = _running_task(conn, title="review")
        assert kb.block_task(conn, child, reason="return to coder", kind="dependency")
        fix = kb.create_task(conn, title="already fixed", assignee="worker")
        assert kb.complete_task(conn, fix, result="fixed")
        kb.link_tasks(conn, fix, child)

        result = kb.reconcile_dependency_waits(conn, now=100)
        assert result.waits_rearmed == 1
        assert kb.get_task(conn, child).status == "ready"
        assert kb.get_task(conn, child).block_kind is None


def test_pending_wait_sla_escalates_once_and_only_then_records_failure(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        child = _running_task(conn, title="review")
        assert kb.block_task(
            conn, child, reason="return to coder", kind="dependency",
            materialization_sla_seconds=10,
        )
        pending = [
            event for event in kb.list_events(conn, child)
            if event.kind == "dependency_pending"
        ][-1]
        deadline = pending.payload["materialize_by"]
        assert kb.reconcile_dependency_waits(conn, now=deadline - 1).timed_out == 0
        assert kb.reconcile_dependency_waits(conn, now=deadline).timed_out == 1
        task = kb.get_task(conn, child)
        assert task.status == "blocked"
        assert task.block_kind == "needs_input"
        assert len([
            event for event in kb.list_events(conn, child)
            if event.kind == "dependency_materialization_timeout"
        ]) == 1
        assert len([
            event for event in kb.list_events(conn, child)
            if event.kind == "failure_signature"
        ]) == 1
        assert kb.reconcile_dependency_waits(conn, now=deadline + 1).timed_out == 0
        assert len([
            event for event in kb.list_events(conn, child)
            if event.kind == "failure_signature"
        ]) == 1


def test_legacy_dependency_hard_block_recovers_only_with_dependency_provenance(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="fix", assignee="worker")
        child = kb.create_task(conn, title="review", assignee="worker")
        kb.link_tasks(conn, parent, child)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='blocked', block_kind='needs_input' WHERE id=?",
                (child,),
            )
            kb._append_event(
                conn, child, "blocked",
                {"reason": "dependency_unavailable: no fix card", "kind": "needs_input"},
            )
            kb._append_event(
                conn, child, "dependency_loop_detected", {"reason": "no parent"},
            )
        result = kb.reconcile_dependency_waits(conn, now=100)
        assert result.legacy_recovered == 1
        assert kb.get_task(conn, child).status == "todo"
        assert kb.get_task(conn, child).block_kind == "dependency"


def test_legacy_dependency_hard_block_stays_blocked_when_parents_are_satisfied(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="original coder card", assignee="worker")
        assert kb.complete_task(conn, parent, result="done")
        child = kb.create_task(conn, title="review", assignee="worker")
        kb.link_tasks(conn, parent, child)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='blocked', block_kind='needs_input' WHERE id=?",
                (child,),
            )
            kb._append_event(
                conn, child, "blocked",
                {"reason": "dependency_unavailable: no unfinished linked parent", "kind": "needs_input"},
            )
            kb._append_event(
                conn, child, "dependency_loop_detected", {"reason": "no unfinished parent"},
            )

        result = kb.reconcile_dependency_waits(conn, now=100)

        assert result.legacy_recovered == 0
        task = kb.get_task(conn, child)
        assert task.status == "blocked"
        assert task.block_kind == "needs_input"
        assert not [
            event for event in kb.list_events(conn, child)
            if event.kind == "dependency_recovered"
        ]


def test_human_approval_block_ignores_unrelated_parent(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="unrelated", assignee="worker")
        child = kb.create_task(conn, title="approval", assignee="worker")
        kb.link_tasks(conn, parent, child)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='blocked', block_kind='needs_input' WHERE id=?",
                (child,),
            )
            kb._append_event(
                conn, child, "blocked",
                {"reason": "legal approval required", "kind": "needs_input"},
            )
        result = kb.reconcile_dependency_waits(conn, now=100)
        assert result.legacy_recovered == 0
        assert kb.get_task(conn, child).status == "blocked"
        assert kb.get_task(conn, child).block_kind == "needs_input"


def test_reconcile_dependency_sweeps_filter_before_limit_and_honor_task_ids(
    kanban_home: Path,
) -> None:
    """Pure human gates cannot starve targeted or legacy dependency recovery."""
    with kb.connect_closing() as conn:
        human_ids = []
        for index in range(200):
            human = kb.create_task(
                conn, title=f"human gate {index}", assignee="worker",
            )
            human_ids.append(human)
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status='blocked', block_kind='needs_input' "
                    "WHERE id = ?",
                    (human,),
                )
                kb._append_event(
                    conn, human, "blocked",
                    {"reason": "waiting for approval", "kind": "needs_input"},
                )

        parent = kb.create_task(conn, title="unfinished fix", assignee="worker")
        targeted = kb.create_task(conn, title="legacy targeted review", assignee="worker")
        kb.link_tasks(conn, parent, targeted)
        with kb.write_txn(conn):
            placeholders = ",".join("?" for _ in human_ids)
            conn.execute(
                f"UPDATE tasks SET created_at=1 WHERE id IN ({placeholders})",
                human_ids,
            )
            conn.execute(
                "UPDATE tasks SET created_at=2 WHERE id IN (?, ?)",
                (parent, targeted),
            )
            conn.execute(
                "UPDATE tasks SET status='blocked', block_kind='needs_input' WHERE id=?",
                (targeted,),
            )
            kb._append_event(
                conn, targeted, "blocked",
                {"reason": "dependency_unavailable: no fix card", "kind": "needs_input"},
            )
            kb._append_event(
                conn, targeted, "dependency_loop_detected", {"reason": "no fix card"},
            )

        targeted_result = kb.reconcile_dependency_waits(
            conn, now=100, limit=200, task_ids=[targeted],
        )
        assert targeted_result.legacy_recovered == 1
        assert kb.get_task(conn, targeted).status == "todo"

        later = kb.create_task(conn, title="legacy board-wide review", assignee="worker")
        kb.link_tasks(conn, parent, later)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET created_at=3 WHERE id = ?", (later,))
            conn.execute(
                "UPDATE tasks SET status='blocked', block_kind='needs_input' WHERE id=?",
                (later,),
            )
            kb._append_event(
                conn, later, "blocked",
                {"reason": "dependency_unavailable: no fix card", "kind": "needs_input"},
            )
            kb._append_event(
                conn, later, "dependency_loop_detected", {"reason": "no fix card"},
            )

        board_result = kb.reconcile_dependency_waits(conn, now=100, limit=200)
        assert board_result.legacy_recovered == 1
        assert kb.get_task(conn, later).status == "todo"


def test_repeated_dependency_signature_blocks_before_third_spawn(
    kanban_home: Path, all_assignees_spawnable, monkeypatch,
) -> None:
    """Equivalent waits are capped by the existing dispatch breaker."""
    hooks: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        kb,
        "_fire_kanban_lifecycle_hook",
        lambda event, *_args, **kwargs: hooks.append((event, kwargs.get("reason"))),
    )
    with kb.connect_closing() as conn:
        pending_parent = kb.create_task(
            conn, title="still running", assignee="worker",
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='running' WHERE id=?",
                (pending_parent,),
            )
        done_parent = kb.create_task(conn, title="done parent", assignee="worker")
        assert kb.complete_task(conn, done_parent, result="done")
        hooks.clear()
        child = kb.create_task(
            conn, title="child", assignee="alice",
        )
        kb.link_tasks(conn, pending_parent, child)

        for _ in range(2):
            with kb.write_txn(conn):
                # The parent gate normally prevents a claim while this
                # parent is running; this direct status transition models a
                # second worker report from a raced/legacy ready row and
                # exercises the shared accounting path itself.
                conn.execute("UPDATE tasks SET status='running' WHERE id=?", (child,))
            assert kb.block_task(
                conn,
                child,
                reason="2026-07-20T12:00:00Z parent artifact is not reachable",
                kind="dependency",
            )

        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (child,))
        spawned: list[str] = []

        def fake_spawn(task, _workspace):
            spawned.append(task.id)
            return kb.SpawnReceipt(
                pid=12345,
                release=lambda: None,
                abort=lambda: None,
                process_started_at=1234.5,
                process_group_id=12345,
                session_id=12345,
            )

        result = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        task = kb.get_task(conn, child)
        loop_events = [
            event for event in kb.list_events(conn, child)
            if event.kind == "dependency_loop_detected"
        ]

    assert spawned == []
    assert task is not None
    assert (child, loop_events[-1].payload["signature"]) in result.circuit_breaker_tripped
    assert task.status == "blocked"
    assert len(loop_events) == 1
    assert loop_events[0].payload["recurrences"] == 2
    assert [event for event, _reason in hooks] == ["kanban_task_blocked"], hooks


def test_dependency_wait_signatures_do_not_cross_trip(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        parent_a = kb.create_task(conn, title="parent a", assignee="worker")
        parent_b = kb.create_task(conn, title="parent b", assignee="worker")
        child = _running_task(conn, title="child")
        kb.link_tasks(conn, parent_a, child)
        kb.link_tasks(conn, parent_b, child)
        assert kb.block_task(conn, child, reason="wait for A", kind="dependency")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (child,))
        assert kb.block_task(conn, child, reason="wait for B", kind="dependency")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (child,))
        assert kb.check_failure_signature_breaker(conn, child) is None


# ---------------------------------------------------------------------------
# Completion resets loop memory
# ---------------------------------------------------------------------------


def test_completion_clears_block_memory(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="x", kind="capability")
        kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).block_recurrences == 1
        kb.complete_task(conn, tid, result="done")
        t = kb.get_task(conn, tid)
        assert t.status == "done"
        assert t.block_recurrences == 0
        assert t.block_kind is None


# ---------------------------------------------------------------------------
# Validation + back-compat
# ---------------------------------------------------------------------------


def test_invalid_kind_rejected(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        with pytest.raises(ValueError):
            kb.block_task(conn, tid, reason="x", kind="bogus")


def test_block_without_kind_is_backward_compatible(kanban_home: Path) -> None:
    """Existing callers that pass no kind keep the old single-block behaviour."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(conn, tid, reason="legacy")
        t = kb.get_task(conn, tid)
        assert t.status == "blocked"
        assert t.block_kind is None
