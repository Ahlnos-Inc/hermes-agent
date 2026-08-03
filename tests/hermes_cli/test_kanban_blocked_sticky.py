"""Regression tests for #28712 and the follow-up circuit-breaker loop fix.

Kanban dispatcher must not auto-promote worker-initiated ``kanban_block``
sticky blocks or circuit-breaker ``gave_up`` blocks.

The bug: when a worker called ``kanban_block(reason="review-required:
...")`` to hand off to a human, the dispatcher's ``recompute_ready``
would promote the task back to ``ready`` on the next tick.  The fresh
worker found nothing to do (work already applied), exited cleanly, and
got recorded as a ``protocol_violation`` → ``gave_up`` → promote → loop
until manual intervention.

These tests pin down:

* Worker / operator-initiated blocks are sticky and survive
  ``recompute_ready``.
* Circuit-breaker blocks (``gave_up`` event, status flipped via
  ``_record_task_failure``) are sticky too. Retrying without an explicit
  unblock just re-enters the same failed worker path.
* An explicit ``kanban_unblock`` clears the sticky state.
* The full block → promote → crash → ``gave_up`` loop is broken after
  this fix: subsequent ticks leave the task blocked.

The tangentially related schema-init ordering bug originally reported
in #28712 (``init_db`` crashing on legacy DBs that pre-dated the
``session_id`` migration) is covered separately by
``test_kanban_db.py::test_connect_migrates_legacy_db_before_optional_column_indexes``,
landed via #28754 / #28781 ahead of this fix.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Worker-initiated kanban_block must be sticky
# ---------------------------------------------------------------------------


def test_worker_block_is_not_auto_promoted_by_recompute_ready(kanban_home: Path) -> None:
    """A standalone task that a worker explicitly blocks for review
    must stay blocked across an arbitrary number of dispatcher ticks.
    Before #28712's fix, ``recompute_ready`` would silently flip it
    back to ``ready`` on the very next tick."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="needs human review")
        kb.claim_task(conn, tid)
        assert kb.block_task(
            conn, tid,
            reason="review-required: please verify ACL change",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        assert kb.get_task(conn, tid).status == "blocked"

        # Hammer the promotion code — exactly the dispatcher loop's
        # behaviour, just compressed in time.
        for _ in range(5):
            promoted = kb.recompute_ready(conn)
            assert promoted == 0, "worker-blocked task must not auto-promote"
            assert kb.get_task(conn, tid).status == "blocked"


def test_worker_block_on_child_with_done_parents_is_still_sticky(kanban_home: Path) -> None:
    """The parent-completion path is the one ``recompute_ready`` was
    designed for, so it's the most dangerous false-positive: even when
    every parent is done, a worker-initiated block on the child must
    stay blocked."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="child", parents=[parent])
        kb.complete_task(conn, parent, result="parent ok")

        kb.claim_task(conn, child)
        kb.block_task(
            conn, child,
            reason="review-required: child needs sign-off",
            expected_run_id=kb.get_task(conn, child).current_run_id,
        )
        assert kb.get_task(conn, child).status == "blocked"

        promoted = kb.recompute_ready(conn)
        assert promoted == 0
        assert kb.get_task(conn, child).status == "blocked"


# ---------------------------------------------------------------------------
# Circuit-breaker blocks still auto-recover (preserve #40c1decb3 intent)
# ---------------------------------------------------------------------------


def test_circuit_breaker_block_still_auto_promotes(kanban_home: Path) -> None:
    """A child that was put into ``blocked`` *without* a worker-issued
    ``kanban_block`` (e.g. a transient crash, manual DB triage) and whose
    ``consecutive_failures`` is still *below* the circuit-breaker limit
    must get auto-promoted when its parents complete — preserves the
    pre-#28712 recovery semantics for genuinely transient failures.

    The complementary case — a block whose failure count has *reached*
    the limit must stay blocked — is covered by
    ``test_kanban_db.py::test_recompute_ready_skips_tasks_at_failure_limit``
    (#35072).  Together they pin the contract: ``recompute_ready`` defers
    the give-up decision to the same effective limit the breaker uses, so
    the two never disagree.
    """
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="child", parents=[parent])
        kb.complete_task(conn, parent, result="ok")

        # Simulate a transient circuit-breaker / direct triage that flips
        # status without emitting a ``blocked`` event — exactly what
        # ``_record_task_failure`` does below the limit.  One failure is
        # under the default limit (2), so recovery is still correct.
        conn.execute(
            "UPDATE tasks SET status='blocked', consecutive_failures=1, "
            "last_failure_error='transient error' WHERE id=?",
            (child,),
        )
        conn.commit()

        promoted = kb.recompute_ready(conn)
        assert promoted == 1
        task = kb.get_task(conn, child)
        assert task.status == "ready"
        # Counter is preserved across recovery (not reset) so the breaker
        # can still accumulate if the task keeps failing (#35072).
        assert task.consecutive_failures == 1


def test_gave_up_event_makes_block_sticky(kanban_home: Path) -> None:
    """The circuit-breaker emits ``gave_up`` (not ``blocked``).

    Treat it as sticky so parentless tasks and already-satisfied child
    tasks do not immediately respawn into the same failure.
    """
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="child", parents=[parent])
        kb.complete_task(conn, parent, result="ok")

        # Status + event match what _record_task_failure writes when
        # the breaker trips.
        conn.execute(
            "UPDATE tasks SET status='blocked' WHERE id=?", (child,),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'gave_up', NULL, ?)",
            (child, int(time.time())),
        )
        conn.commit()

        promoted = kb.recompute_ready(conn)
        assert promoted == 0
        assert kb.get_task(conn, child).status == "blocked"


def test_parentless_gave_up_block_does_not_respawn(kanban_home: Path) -> None:
    """Root tasks have no parents, so ``all(parents done)`` is vacuously
    true. A ``gave_up`` root must still stay blocked."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="root")
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'gave_up', NULL, ?)",
            (tid, int(time.time())),
        )
        conn.commit()

        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, tid).status == "blocked"


# ---------------------------------------------------------------------------
# unblock_task clears the sticky state
# ---------------------------------------------------------------------------


def test_unblock_clears_sticky_state_and_lets_block_recover(kanban_home: Path) -> None:
    """``hermes kanban unblock`` (or the ``kanban_unblock`` tool) is
    the only legitimate way out of a worker-initiated block.  After
    unblock, a *subsequent* circuit-breaker block on the same task
    must again be eligible for auto-recovery."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t")
        kb.claim_task(conn, tid)
        kb.block_task(
            conn, tid,
            reason="review-required: ...",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        assert kb.unblock_task(conn, tid)
        # After unblock the task is no longer blocked at all.
        assert kb.get_task(conn, tid).status == "ready"

        # Now simulate a *later* circuit-breaker block (no new
        # ``blocked`` event, just status flip).  The most recent
        # block/unblock event is ``unblocked`` → guard does not fire
        # → recompute can recover.
        conn.execute(
            "UPDATE tasks SET status='blocked' WHERE id=?", (tid,),
        )
        conn.commit()

        promoted = kb.recompute_ready(conn)
        assert promoted == 1
        assert kb.get_task(conn, tid).status == "ready"


# ---------------------------------------------------------------------------
# Full bug-shaped loop: block → promote → crash → gave_up → next tick
# ---------------------------------------------------------------------------


def test_protocol_violation_loop_is_broken(kanban_home: Path) -> None:
    """Reproduces the exact #28712 loop and asserts the dispatcher
    leaves the task blocked instead of cycling.

    Loop shape from the issue:

    1. Worker calls ``kanban_block`` → status='blocked',
       ``task_runs.outcome='blocked'``, ``blocked`` event.
    2. (Bug) Dispatcher promotes back to ``ready``.
    3. Fresh worker exits cleanly without terminal tool call →
       ``protocol_violation`` event.
    4. ``_record_task_failure(failure_limit=1)`` → ``gave_up`` event,
       status='blocked' again.
    5. (Bug) Dispatcher promotes again → infinite loop.

    With the fix in place, step 2 never happens — the test simulates
    one would-be loop cycle by faking the crash-then-gave_up entries
    that *would* have been written and asserts the *next* tick still
    leaves the task blocked.
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="loop reproducer")
        kb.claim_task(conn, tid)
        kb.block_task(
            conn, tid,
            reason="review-required: human eyes please",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        assert kb.get_task(conn, tid).status == "blocked"

        # First dispatcher tick — must NOT promote.
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, tid).status == "blocked"

        # Simulate the (hypothetical) protocol_violation + gave_up
        # entries that the dispatcher would have written if the bug
        # were still present.  Even with those event rows in place,
        # the worker-initiated ``blocked`` event is the most recent
        # of the ``{blocked, unblocked}`` pair, so the sticky guard
        # still fires.
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'protocol_violation', NULL, ?)",
            (tid, now),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'gave_up', NULL, ?)",
            (tid, now + 1),
        )
        conn.commit()

        # Subsequent ticks must still leave it blocked.
        for _ in range(3):
            promoted = kb.recompute_ready(conn)
            assert promoted == 0
            assert kb.get_task(conn, tid).status == "blocked"


# ---------------------------------------------------------------------------
# Schema-init recovery on legacy DBs is covered by
# tests/hermes_cli/test_kanban_db.py::test_connect_migrates_legacy_db_before_optional_column_indexes
# (landed via #28754 / #28781).  The original PR shipped a duplicate test
# here; dropped during salvage to avoid two assertions of the same contract.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# BUILD-858 Slice 1: durable initial holds and ordered sticky state
# ---------------------------------------------------------------------------


def test_initial_status_blocked_is_sticky(kanban_home: Path) -> None:
    """A task created with initial_status='blocked' must have a durable hold:
    - status='blocked', block_kind='needs_input', block_recurrences=1
    - one standard 'blocked' event with code='initial_status_blocked'
    - _has_sticky_block() recognizes it immediately
    - recompute_ready() never promotes it (even with no parents)
    """
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="human gate", initial_status="blocked",
        )
        task = kb.get_task(conn, tid)
        assert task.status == "blocked", "initial_status=blocked must persist"
        assert task.block_kind == "needs_input", "block_kind must be needs_input"
        assert task.block_recurrences == 1, "block_recurrences must be 1"

        # Must have a 'blocked' event so _has_sticky_block recognizes it.
        events = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
            (tid,),
        ).fetchall()
        kinds = [e["kind"] for e in events]
        assert "created" in kinds
        assert "blocked" in kinds, f"expected blocked event; got {kinds}"

        import json
        blocked_events = [e for e in events if e["kind"] == "blocked"]
        payload = json.loads(blocked_events[0]["payload"])
        assert payload.get("code") == "initial_status_blocked"
        assert payload.get("kind") == "needs_input"
        assert payload.get("recurrences") == 1

        # _has_sticky_block must immediately return True.
        assert kb._has_sticky_block(conn, tid), "initial blocked task must be sticky"

        # recompute_ready must not promote it, even hammered.
        for _ in range(5):
            promoted = kb.recompute_ready(conn)
            assert promoted == 0, "initial blocked task must not auto-promote"
            assert kb.get_task(conn, tid).status == "blocked"


def test_initial_status_blocked_with_done_parent_survives(kanban_home: Path) -> None:
    """An initial_status='blocked' task must stay blocked even when every parent
    is done — parent completion does not authorize promotion of an explicit hold."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        gate = kb.create_task(
            conn, title="human gate", initial_status="blocked", parents=[parent],
        )
        kb.complete_task(conn, parent, result="ok")

        # Repeated recompute must leave the gate blocked.
        for _ in range(3):
            promoted = kb.recompute_ready(conn)
            assert promoted == 0
            assert kb.get_task(conn, gate).status == "blocked"


def test_initial_status_blocked_can_be_completed(kanban_home: Path) -> None:
    """complete_task accepts blocked status — trusted terminal resolution
    (BUILD-858 Invariant I1 terminal path)."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="human gate", initial_status="blocked",
        )
        assert kb.get_task(conn, tid).status == "blocked"
        # complete_task must accept a blocked source status.
        ok = kb.complete_task(conn, tid, result="approved")
        assert ok, "complete_task must succeed on a blocked task"
        assert kb.get_task(conn, tid).status == "done"


def test_initial_status_blocked_unblock_then_spawns(kanban_home: Path) -> None:
    """Explicit unblock is the only authorized promotion path (Invariant I1)."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        gate = kb.create_task(
            conn, title="gate", initial_status="blocked", parents=[parent],
        )
        kb.complete_task(conn, parent, result="ok")

        # Unblocking must return True and put the task back to ready.
        unblocked = kb.unblock_task(conn, gate)
        assert unblocked, "unblock_task must succeed on initial-blocked task"
        assert kb.get_task(conn, gate).status == "ready"

        # After unblock, recompute_ready treats it as a normal ready task.
        assert kb.recompute_ready(conn) == 0  # already ready, no promotion needed


def test_initial_status_blocked_creates_no_run(kanban_home: Path) -> None:
    """Creating a task with initial_status='blocked' must not create a run."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="gate", initial_status="blocked",
        )
        task = kb.get_task(conn, tid)
        assert task.current_run_id is None, "initial blocked task must have no run"


def test_pre_upgrade_initial_blocked_fallback(kanban_home: Path) -> None:
    """Pre-upgrade rows: if a blocked task has a 'created' event with
    status='blocked' but no 'blocked' event, _has_sticky_block must still
    return True (ordered created-event fallback for legacy/pre-BUILD-858 rows).

    This covers the t_9deb9d53 incident shape: a task was created with
    initial_status='blocked' before the blocked event was written in the same
    transaction.  The created event's status field is the fallback authority.
    """
    import json as _json
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="child", parents=[parent])
        kb.complete_task(conn, parent, result="ok")

        # Simulate a pre-upgrade blocked row: status='blocked', created event
        # with status='blocked', but no 'blocked' event row.
        conn.execute(
            "UPDATE tasks SET status='blocked', block_kind='needs_input' WHERE id=?",
            (child,),
        )
        # Ensure the created event has status='blocked' (pre-upgrade shape).
        conn.execute(
            "UPDATE task_events SET payload = ? "
            "WHERE task_id = ? AND kind = 'created'",
            (_json.dumps({"status": "blocked", "assignee": None, "parents": [parent]}), child),
        )
        conn.commit()

        # No 'blocked' event exists — only created + promoted may be present.
        blocked_events = conn.execute(
            "SELECT id FROM task_events WHERE task_id = ? AND kind = 'blocked'",
            (child,),
        ).fetchall()
        assert blocked_events == [], "pre-upgrade row must have no blocked event"

        # The fallback must recognize the hold.
        assert kb._has_sticky_block(conn, child), \
            "created event fallback must recognize initial-blocked pre-upgrade row"

        # recompute_ready must not promote this pre-upgrade row.
        promoted = kb.recompute_ready(conn)
        assert promoted == 0
        assert kb.get_task(conn, child).status == "blocked"
