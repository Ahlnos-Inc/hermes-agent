"""The ``gave_up`` alert names the real failure class and the next action.

``gave_up`` is the only terminal state whose sole exit is a person: the
dispatcher has stopped retrying and nothing else will move the task. The
render used to hardcode "gave up after repeated spawn failures" for every
trigger, so a crashing worker — the dominant case, and the one BUILD-674 was
filed for — was reported to the operator as a spawn failure with no attempt
count, no pid, and no next step, even though ``_record_task_failure`` already
puts all three in the event payload.

The payload is built by ``kanban_db._record_task_failure``; these tests drive
it through that real function so the render can't drift off the field names.
"""

from gateway.kanban_notifications import render_kanban_event
from hermes_cli import kanban_db as kb


def _board(tmp_path):
    db = tmp_path / "kanban.db"
    kb.init_db(db)
    return kb.connect(db)


def _gave_up(conn, *, outcome, extra, error="boom"):
    """Trip the breaker through the real recorder and return its event."""
    task = kb.create_task(conn, title="publish the release")
    task_id = task if isinstance(task, str) else task.id
    tripped = kb._record_task_failure(
        conn, task_id, error=error,
        outcome=outcome,
        failure_limit=1,
        release_claim=False,
        end_run=False,
        event_payload_extra=extra,
    )
    assert tripped is True
    event = [e for e in kb.list_events(conn, task_id) if e.kind == "gave_up"][-1]
    task_obj = kb.get_task(conn, task_id)
    return task_id, render_kanban_event(
        task_id=task_id, task=task_obj, event=event, board_slug="hermes-infra",
    )


def test_crash_give_up_names_crashes_not_spawn_failures(tmp_path):
    conn = _board(tmp_path)
    task_id, msg = _gave_up(
        conn,
        outcome="crashed",
        extra={
            "pid": 54717,
            "claimer": "N-MBP:50721",
            "exit_kind": "unknown",
            "branch": "build/BUILD-655-attachment-source-handoff",
        },
    )
    assert "spawn failures" not in msg
    assert "worker crashes" in msg
    # Attempts, pid and the branch holding unmerged work are the three facts a
    # post-mortem starts from.
    assert "(1/1 attempts)" in msg
    assert "pid 54717" in msg
    assert "branch build/BUILD-655-attachment-source-handoff" in msg
    # A human is the only exit, so the alert has to say what to do next, on the
    # board the task actually lives on.
    assert f"hermes kanban --board hermes-infra log {task_id}" in msg
    assert f"hermes kanban --board hermes-infra unblock {task_id}" in msg


def test_timeout_give_up_recommends_the_runtime_budget_not_the_log(tmp_path):
    conn = _board(tmp_path)
    task_id, msg = _gave_up(
        conn, outcome="timed_out", extra={"pid": 999, "sigkill": True},
    )
    assert "repeated timeouts" in msg
    assert "max_runtime" in msg
    assert f"unblock {task_id}" in msg


def test_spawn_failure_give_up_keeps_its_original_wording(tmp_path):
    """The fix must not over-correct: spawn failures really do exist."""
    conn = _board(tmp_path)
    _, msg = _gave_up(conn, outcome="spawn_failed", extra=None)
    assert "repeated spawn failures" in msg
    assert "gateway log" in msg
    # No evidence bracket when the payload carries no identifying facts.
    assert "pid" not in msg


def test_unknown_trigger_still_gets_a_next_action(tmp_path):
    conn = _board(tmp_path)
    task_id, msg = _gave_up(conn, outcome="reclaimed", extra={"pid": 7})
    assert "repeated failures" in msg
    assert f"unblock {task_id}" in msg
